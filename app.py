"""
多策略 RAG 文件問答系統 v3 — PWA + RWD 版
Gradio + FastAPI · ChromaDB · Groq · PDF / DOCX · 8 種檢索策略

本機執行:  python app.py
預先下載模型(部署建置階段用):  python app.py --download
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import chromadb
import gradio as gr
import uvicorn
from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from groq import Groq
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer

# ══════════════════════════════════════════════════════════
#  設定(可用環境變數覆寫)
# ══════════════════════════════════════════════════════════
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

CHROMA_PATH = os.environ.get("CHROMA_PATH", os.path.join(BASE_DIR, "chroma_db"))
MODEL_CACHE = os.environ.get("MODEL_CACHE", os.path.join(BASE_DIR, "model_cache"))
MODEL_NAME = os.environ.get(
    "EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "20"))
COLLECTION = "rag_chunks"
CHILD_COLLECTION = "rag_child_chunks"
THEME_COLOR = "#2d6a4f"


# ══════════════════════════════════════════════════════════
#  共用資源(所有使用者共用:嵌入模型、ChromaDB)— 延遲載入,讓服務更快開 port
# ══════════════════════════════════════════════════════════
class Shared:
    _lock = threading.Lock()
    _model = None
    _chroma = None
    collection = None
    child_collection = None

    @classmethod
    def model(cls):
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    from sentence_transformers import SentenceTransformer

                    cls._model = SentenceTransformer(MODEL_NAME, cache_folder=MODEL_CACHE)
        return cls._model

    @classmethod
    def collections(cls):
        if cls._chroma is None:
            with cls._lock:
                if cls._chroma is None:
                    client = chromadb.PersistentClient(path=CHROMA_PATH)
                    # 文件內容只存在各使用者的記憶體 session,啟動時清掉上次殘留的向量
                    for name in (COLLECTION, CHILD_COLLECTION):
                        try:
                            client.delete_collection(name)
                        except Exception:
                            pass
                    cls.collection = client.create_collection(
                        COLLECTION, metadata={"hnsw:space": "cosine"}
                    )
                    cls.child_collection = client.create_collection(
                        CHILD_COLLECTION, metadata={"hnsw:space": "cosine"}
                    )
                    cls._chroma = client
        return cls.collection, cls.child_collection


# ══════════════════════════════════════════════════════════
#  RAG 核心(每位使用者一個實例,API Key 與文件互不干擾)
# ══════════════════════════════════════════════════════════
class MultiStrategyRAG:
    STRATEGY_MAP = {
        "semantic": "1  ChromaDB 語意搜尋",
        "tfidf": "2  TF-IDF 關鍵詞",
        "hybrid": "3  混合搜尋",
        "rerank": "4  重新排序",
        "multi_query": "5  多查詢擴展",
        "compress": "6  上下文壓縮",
        "parent_child": "7  父子文檔",
        "hyde": "8  假設性答案 HyDE",
    }
    STRATEGY_FUNCS = {
        "semantic": "s_semantic",
        "tfidf": "s_tfidf",
        "hybrid": "s_hybrid",
        "rerank": "s_rerank",
        "multi_query": "s_multi_query",
        "compress": "s_compress",
        "parent_child": "s_parent_child",
        "hyde": "s_hyde",
    }

    def __init__(self) -> None:
        self.client: Groq | None = None
        self.key_origin = ""  # "server" | "user" | ""
        server_key = os.getenv("GROQ_API_KEY", "").strip()
        if server_key:
            self.client = Groq(api_key=server_key)
            self.key_origin = "server"

        self.session_id: str | None = None
        self.source_name = ""
        self.file_type = ""
        self.chunks: list[str] = []
        self.child_chunks: list[str] = []
        self.tfidf_vectorizer: TfidfVectorizer | None = None
        self.tfidf_matrix = None

    # ── API Key ──────────────────────────────────────────
    def set_api_key(self, api_key: str) -> None:
        key = (api_key or "").strip()
        if key:
            self.client = Groq(api_key=key)
            self.key_origin = "user"
        else:
            server_key = os.getenv("GROQ_API_KEY", "").strip()
            self.client = Groq(api_key=server_key) if server_key else None
            self.key_origin = "server" if server_key else ""

    # ── 文件載入 ─────────────────────────────────────────
    def load_document(self, file_path: str) -> str:
        try:
            path = Path(file_path)
            if not path.exists():
                return "✗ 載入失敗：找不到檔案"

            suffix = path.suffix.lower()
            if suffix not in (".pdf", ".docx"):
                return "✗ 目前僅支援 PDF 與 DOCX 檔案"

            self._cleanup()  # 換文件時移除舊的向量資料
            self.chunks, self.child_chunks = [], []
            self.tfidf_vectorizer, self.tfidf_matrix = None, None

            self.source_name = path.name
            self.file_type = suffix.lstrip(".")
            safe_stem = re.sub(r"[^a-zA-Z0-9]+", "_", path.stem)[:30]
            self.session_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_stem}"

            if suffix == ".pdf":
                full_text, stats = self._extract_pdf(path)
            else:
                full_text, stats = self._extract_docx(path)

            if not full_text.strip():
                return "✗ 載入失敗：文件沒有可擷取文字，可能是掃描圖片檔，需先 OCR"

            self.chunks = self._split(full_text, chunk_size=800, overlap=150)
            if not self.chunks:
                return "✗ 載入失敗：切段後沒有有效內容"

            self._build_chroma_index()
            self._build_tfidf_index()
            self._build_child_index()

            return (
                f"✓ 成功載入 {self.source_name}\n"
                f"類型：{suffix.upper().lstrip('.')} · {stats}\n"
                f"{len(self.chunks)} 個主片段 · {len(self.child_chunks)} 個子片段"
            )
        except Exception as exc:
            return f"✗ 載入失敗：{type(exc).__name__}: {exc}"

    def _cleanup(self) -> None:
        if not self.session_id:
            return
        try:
            for col in Shared.collections():
                col.delete(where={"session_id": self.session_id})
        except Exception:
            pass

    # ── 文字擷取 ─────────────────────────────────────────
    def _extract_pdf(self, path: Path) -> tuple[str, str]:
        reader = PdfReader(str(path))
        parts = []
        for idx, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"\n[PDF 第 {idx} 頁]\n{text}")
        return "\n".join(parts), f"{len(reader.pages)} 頁"

    def _extract_docx(self, path: Path) -> tuple[str, str]:
        doc = Document(str(path))
        blocks: list[str] = []
        para_count = table_count = 0

        for child in doc.element.body.iterchildren():
            if isinstance(child, CT_P):
                text = Paragraph(child, doc).text.strip()
                if text:
                    para_count += 1
                    blocks.append(text)
            elif isinstance(child, CT_Tbl):
                table_count += 1
                tbl_text = self._table_to_text(Table(child, doc))
                if tbl_text.strip():
                    blocks.append(f"\n[DOCX 表格 {table_count}]\n{tbl_text}")

        return "\n\n".join(blocks), f"{para_count} 段落 / {table_count} 表格"

    def _table_to_text(self, table: Table) -> str:
        rows = []
        for row in table.rows:
            cells = [re.sub(r"\s+", " ", c.text).strip() for c in row.cells if c.text.strip()]
            if cells:
                rows.append(" | ".join(cells))
        return "\n".join(rows)

    def _split(self, text: str, chunk_size: int, overlap: int) -> list[str]:
        clean = re.sub(r"\s+", " ", text).strip()
        step = max(1, chunk_size - overlap)
        return [
            c
            for start in range(0, len(clean), step)
            if (c := clean[start : start + chunk_size].strip())
        ]

    # ── Index 建立 ───────────────────────────────────────
    def _encode(self, texts: list[str]) -> list[list[float]]:
        return (
            Shared.model()
            .encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            .astype("float32")
            .tolist()
        )

    def _add_batched(self, col, ids, docs, metas, batch: int = 256) -> None:
        for i in range(0, len(docs), batch):
            sl = slice(i, i + batch)
            col.add(
                ids=ids[sl],
                documents=docs[sl],
                metadatas=metas[sl],
                embeddings=self._encode(docs[sl]),
            )

    def _build_chroma_index(self) -> None:
        sid = self.session_id
        ids = [f"{sid}_chunk_{i:05d}" for i in range(len(self.chunks))]
        metas = [
            {
                "session_id": sid,
                "source": self.source_name,
                "file_type": self.file_type,
                "chunk_index": i,
            }
            for i in range(len(self.chunks))
        ]
        col, _ = Shared.collections()
        self._add_batched(col, ids, self.chunks, metas)

    def _build_tfidf_index(self) -> None:
        self.tfidf_vectorizer = TfidfVectorizer(
            analyzer="char", ngram_range=(2, 4), max_features=3000
        )
        self.tfidf_matrix = self.tfidf_vectorizer.fit_transform(self.chunks)

    def _build_child_index(self) -> None:
        sid = self.session_id
        docs, ids, metas = [], [], []
        for pidx, parent in enumerate(self.chunks):
            for cidx, child in enumerate(self._split(parent, chunk_size=300, overlap=50)):
                docs.append(child)
                ids.append(f"{sid}_parent_{pidx:05d}_child_{cidx:03d}")
                metas.append(
                    {
                        "session_id": sid,
                        "source": self.source_name,
                        "file_type": self.file_type,
                        "parent_index": pidx,
                        "child_index": cidx,
                    }
                )
        self.child_chunks = docs
        if docs:
            _, child_col = Shared.collections()
            self._add_batched(child_col, ids, docs, metas)

    # ── 工具函式 ─────────────────────────────────────────
    def _chroma_search(self, query: str, k: int, child: bool = False) -> list[dict[str, Any]]:
        if not self.session_id:
            return []
        col, child_col = Shared.collections()
        target = child_col if child else col
        results = target.query(
            query_embeddings=self._encode([query]),
            n_results=max(1, k),
            where={"session_id": self.session_id},
            include=["documents", "metadatas", "distances"],
        )
        docs = results.get("documents", [[]])[0] or []
        metas = results.get("metadatas", [[]])[0] or []
        dists = results.get("distances", [[]])[0] or []
        return [
            {"text": d, "metadata": m or {}, "distance": dist}
            for d, m, dist in zip(docs, metas, dists)
        ]

    def _dedupe(self, chunks: list[str], k: int) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for c in chunks:
            key = c[:120]
            if key not in seen:
                seen.add(key)
                out.append(c)
            if len(out) >= k:
                break
        return out

    def _llm(self, prompt: str, max_tokens: int = 300, temperature: float = 0.3) -> str | None:
        if not self.client:
            return None
        try:
            r = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return r.choices[0].message.content
        except Exception:
            return None

    # ── 8 種策略 ──────────────────────────────────────────
    def s_semantic(self, query: str, k: int = 3) -> list[str]:
        return [r["text"] for r in self._chroma_search(query, k)]

    def s_tfidf(self, query: str, k: int = 3) -> list[str]:
        if self.tfidf_vectorizer is None or self.tfidf_matrix is None:
            return []
        qv = self.tfidf_vectorizer.transform([query])
        scores = (self.tfidf_matrix * qv.T).toarray().flatten()
        return [self.chunks[i] for i in scores.argsort()[-k:][::-1]]

    def s_hybrid(self, query: str, k: int = 3) -> list[str]:
        return self._dedupe(self.s_semantic(query, k * 2) + self.s_tfidf(query, k * 2), k)

    def s_rerank(self, query: str, k: int = 3) -> list[str]:
        candidates = self.s_semantic(query, k * 2)
        if not self.client:
            return candidates[:k]
        scored: list[tuple[str, float]] = []
        for chunk in candidates:
            prompt = (
                f"問題：{query}\n\n文本：{chunk[:500]}\n\n"
                f"請只輸出 0 到 10 的相關度分數（僅數字）："
            )
            resp = self._llm(prompt, max_tokens=10, temperature=0)
            nums = re.findall(r"\d+(?:\.\d+)?", resp or "")
            scored.append((chunk, float(nums[0]) if nums else 0.0))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [c for c, _ in scored[:k]]

    def s_multi_query(self, query: str, k: int = 3) -> list[str]:
        queries = [query]
        prompt = f"將以下問題改寫成 3 個角度不同的繁體中文問題，每行一題，不加編號：\n{query}"
        resp = self._llm(prompt, max_tokens=200, temperature=0.7)
        if resp:
            extras = [ln.strip("-• 1234567890.、 ") for ln in resp.splitlines() if ln.strip()]
            queries += extras[:3]
        chunks: list[str] = []
        for q in queries:
            chunks.extend(self.s_semantic(q, 2))
        return self._dedupe(chunks, k)

    def s_compress(self, query: str, k: int = 3) -> list[str]:
        chunks = self.s_semantic(query, k)
        if not self.client:
            return chunks
        compressed = []
        for chunk in chunks:
            prompt = (
                f"從以下文本中，提取與問題「{query}」最相關的 1-2 句，"
                f"保留繁體中文，不要添加任何解釋：\n\n{chunk}"
            )
            resp = self._llm(prompt, max_tokens=180, temperature=0)
            compressed.append((resp or "").strip() or chunk[:350])
        return compressed

    def s_parent_child(self, query: str, k: int = 3) -> list[str]:
        hits = self._chroma_search(query, k * 3, child=True)
        seen_parents: list[int] = []
        for h in hits:
            pidx = h.get("metadata", {}).get("parent_index")
            if isinstance(pidx, int) and pidx not in seen_parents:
                seen_parents.append(pidx)
            if len(seen_parents) >= k:
                break
        return [self.chunks[i] for i in seen_parents if 0 <= i < len(self.chunks)]

    def s_hyde(self, query: str, k: int = 3) -> list[str]:
        prompt = f"請對以下問題給出一段假設性簡短答案（繁體中文）：\n{query}"
        hypo = self._llm(prompt, max_tokens=250, temperature=0.7) or query
        return self.s_semantic(hypo, k)

    # ── 問答 ─────────────────────────────────────────────
    def generate_answer(self, query: str, strategy_key: str, top_k: int) -> tuple[str, str]:
        if not self.chunks:
            return "請先上傳並載入 PDF 或 DOCX 文件。", ""
        if not (query or "").strip():
            return "請輸入問題。", ""

        fn = getattr(self, self.STRATEGY_FUNCS.get(strategy_key, "s_semantic"))
        chunks = fn(query, int(top_k))
        context = "\n\n—\n\n".join(chunks)

        label = self.STRATEGY_MAP.get(strategy_key, strategy_key)
        source_preview = (
            f"文件：{self.source_name}\n"
            f"策略：{label} · 片段數：{len(chunks)}\n\n"
            f"{'─' * 40}\n\n{context}"
        )

        if not self.client:
            return (
                "⚠ 尚未設定 Groq API Key。\n\n"
                "請在「Step 00」輸入您的 Groq API Key 並點擊「套用」後再提問。\n"
                "（檢索已完成，可展開下方「查看檢索到的文本片段」確認結果）",
                source_preview,
            )

        prompt = f"""請根據以下上下文回答問題。若上下文無相關資訊，請明確說明無法從文件回答，不要自行編造。

上下文：
{context}

問題：{query}

請用繁體中文詳細回答，並以條列方式整理重點："""

        try:
            r = self.client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": "你是專業的文件分析與 RAG 問答助手。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1024,
                temperature=0.3,
            )
            return r.choices[0].message.content, source_preview
        except Exception as exc:
            return f"生成失敗：{type(exc).__name__}: {exc}", source_preview


# ══════════════════════════════════════════════════════════
#  PWA <head>(manifest、圖示、Service Worker、安裝按鈕)
# ══════════════════════════════════════════════════════════
HEAD = f"""
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="{THEME_COLOR}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="RAG 問答">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="32x32" href="/icons/favicon-32.png">
""" + """
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(console.warn);
  });
}

let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  showInstallButton();
});
window.addEventListener("appinstalled", () => {
  deferredPrompt = null;
  const b = document.getElementById("pwa-install");
  if (b) b.remove();
});

function showInstallButton() {
  if (document.getElementById("pwa-install")) return;
  const btn = document.createElement("button");
  btn.id = "pwa-install";
  btn.textContent = "📲 安裝 App";
  btn.onclick = async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    await deferredPrompt.userChoice;
    deferredPrompt = null;
    btn.remove();
  };
  document.body.appendChild(btn);
}
</script>
"""

# ══════════════════════════════════════════════════════════
#  RWD 樣式(使用 Gradio 主題變數,亮/暗色模式皆可讀)
# ══════════════════════════════════════════════════════════
CSS = """
:root { --safe-b: env(safe-area-inset-bottom, 0px); }
.gradio-container {
    max-width: 1200px !important; margin: auto;
    padding-left: max(12px, env(safe-area-inset-left)) !important;
    padding-right: max(12px, env(safe-area-inset-right)) !important;
}
footer {display: none !important;}

#hdr {
    background: linear-gradient(135deg, #40916c 0%, #2d6a4f 55%, #1b4332 100%);
    color: #fff; border-radius: 16px; padding: 26px 32px; margin-bottom: 16px;
    box-shadow: 0 10px 28px rgba(45,106,79,.28);
}
.hdr-eyebrow {font-size: 11px; letter-spacing: 2.5px; text-transform: uppercase; opacity: .8; margin-bottom: 6px;}
.hdr-title   {font-size: 26px; font-weight: 700; margin: 0 0 6px;}
.hdr-sub     {font-size: 14px; opacity: .9; line-height: 1.5;}
.pill {display: inline-block; margin: 10px 6px 0 0; padding: 3px 10px; border-radius: 16px;
       font-size: 11px; background: rgba(255,255,255,.16); border: 1px solid rgba(255,255,255,.3);}

.sec-label {font-size: 11px; letter-spacing: 1.5px; text-transform: uppercase; opacity: .7;
            font-weight: 700; margin: 14px 0 8px;}
.card-box {border: 1px solid var(--border-color-primary) !important; border-radius: 14px !important;
           padding: 16px !important; background: var(--background-fill-primary) !important;}

#apikey-box {border: 1.5px solid #f0c96a !important; border-radius: 10px !important; padding: 10px !important;}
#ask-btn, #load-btn {background: #2d6a4f !important; color: #fff !important; border: 0 !important; border-radius: 8px !important;}
#apply-key-btn {background: #b87a1a !important; color: #fff !important; border: 0 !important; border-radius: 8px !important;}

/* 策略選單:Radio 變成卡片格 */
.strat-radio .wrap {display: grid !important; grid-template-columns: repeat(2, 1fr); gap: 8px;}
.strat-radio label {
    display: flex !important; align-items: center; justify-content: center; text-align: center;
    margin: 0 !important; padding: 10px 8px !important; min-height: 44px;
    border: 1.5px solid var(--border-color-primary) !important; border-radius: 10px !important;
    cursor: pointer; transition: border-color .15s, background .15s;
}
.strat-radio label:hover {border-color: #2d6a4f !important;}
.strat-radio label.selected {border-color: #2d6a4f !important; background: rgba(45,106,79,.14) !important; font-weight: 700;}
.strat-radio input[type="radio"] {display: none !important;}
.strat-desc {font-size: 13px; opacity: .85; margin: 2px 0 6px;}

.answer-box {min-height: 160px; padding: 12px 14px; border: 1px solid var(--border-color-primary);
             border-radius: 10px; line-height: 1.7; overflow-wrap: anywhere;}

#pwa-install {
    position: fixed; right: 16px; bottom: calc(16px + var(--safe-b)); z-index: 9999;
    border: 0; border-radius: 999px; padding: 12px 20px; font-size: 1rem; font-weight: 600;
    color: #fff; cursor: pointer; background: linear-gradient(135deg, #40916c, #1b4332);
    box-shadow: 0 8px 24px rgba(45,106,79,.45);
}

/* ---------- 平板 ---------- */
@media (max-width: 1024px) {
    #hdr {padding: 22px 24px;}
    .hdr-title {font-size: 22px;}
}

/* ---------- 手機 ---------- */
@media (max-width: 768px) {
    #hdr {padding: 18px; border-radius: 14px;}
    .hdr-title {font-size: 19px;}
    .hdr-sub {font-size: 13px;}
    .pill {font-size: 10px; padding: 2px 8px; margin: 8px 4px 0 0;}

    .main-row {flex-direction: column !important; gap: 12px !important;}
    .main-row > * {min-width: 100% !important; width: 100% !important;}
    .card-box {padding: 12px !important;}

    button, input, select, textarea {font-size: 16px !important;}  /* 避免 iOS 聚焦自動縮放 */
    button {min-height: 44px;}                                     /* 觸控目標 ≥ 44px */
}

/* ---------- 小手機 ---------- */
@media (max-width: 360px) {
    .strat-radio .wrap {grid-template-columns: 1fr;}
}
"""

THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.green,
    neutral_hue=gr.themes.colors.stone,
    radius_size="lg",
    font=[gr.themes.GoogleFont("Noto Sans TC"), "sans-serif"],
)

HEADER_HTML = """
<div id="hdr">
  <div class="hdr-eyebrow">Intelligent Document Analysis · v3</div>
  <div class="hdr-title">多策略 RAG 文件問答系統</div>
  <div class="hdr-sub">上傳 PDF / DOCX，使用 ChromaDB 向量資料庫與 8 種 RAG 檢索策略進行問答</div>
  <div>
    <span class="pill">Groq API</span>
    <span class="pill">ChromaDB</span>
    <span class="pill">PDF / DOCX</span>
    <span class="pill">SentenceTransformers</span>
    <span class="pill">PWA</span>
  </div>
</div>
"""

STRATEGY_INFO = [
    ("semantic", "語意搜尋", "ChromaDB 向量相似度，最通用", "🔍"),
    ("tfidf", "TF-IDF", "字元 n-gram 關鍵詞統計", "📊"),
    ("hybrid", "混合搜尋", "語意 + TF-IDF 結果合併去重", "⚡"),
    ("rerank", "重新排序", "LLM 對候選片段二次評分", "🎯"),
    ("multi_query", "多查詢擴展", "自動生成多角度問題聯合搜尋", "🔄"),
    ("compress", "上下文壓縮", "LLM 提取最相關句子精簡上下文", "✂️"),
    ("parent_child", "父子文檔", "小片段定位 → 回傳對應大片段", "📂"),
    ("hyde", "HyDE", "先生成假設答案再語意搜尋", "💡"),
]
STRATEGY_LOOKUP = {k: (n, d) for k, n, d, _ in STRATEGY_INFO}

EXAMPLE_QS = [
    ["這份文件的主要內容是什麼？"],
    ["文件中提到哪些重要概念或定義？"],
    ["有哪些關鍵數據、統計資料或案例？"],
    ["文件的結論或建議是什麼？"],
    ["文件提及哪些潛在風險或挑戰？"],
]


# ══════════════════════════════════════════════════════════
#  Gradio 介面(以 gr.State 隔離每位使用者)
# ══════════════════════════════════════════════════════════
def _get(state: MultiStrategyRAG | None) -> MultiStrategyRAG:
    return state if isinstance(state, MultiStrategyRAG) else MultiStrategyRAG()


def apply_api_key(state, api_key):
    rag = _get(state)
    rag.set_api_key(api_key)
    key = (api_key or "").strip()
    if key:
        masked = key[:6] + "****" + key[-4:] if len(key) > 12 else "****"
        msg = f"✓ API Key 已套用（{masked}）"
    elif rag.key_origin == "server":
        msg = "✓ 已改用伺服器預設 API Key"
    else:
        msg = "⚠ API Key 已清除，無法呼叫 LLM"
    return rag, msg


def upload_document(state, file):
    rag = _get(state)
    if file is None:
        return rag, "⚠ 請選擇 PDF 或 DOCX 檔案"
    path = file if isinstance(file, str) else getattr(file, "name", str(file))
    try:
        size_mb = Path(path).stat().st_size / 1024 / 1024
    except OSError:
        size_mb = 0
    if size_mb > MAX_FILE_MB:
        return rag, f"✗ 檔案過大（{size_mb:.1f} MB），上限 {MAX_FILE_MB:g} MB"
    return rag, rag.load_document(path)


def describe_strategy(key):
    name, desc = STRATEGY_LOOKUP.get(key, (key, ""))
    return f"**{name}**:{desc}"


def ask(state, query, strategy_key, top_k):
    rag = _get(state)
    answer, source = rag.generate_answer(query, strategy_key, int(top_k))
    return rag, answer, source


def create_interface():
    server_key = bool(os.getenv("GROQ_API_KEY", "").strip())

    with gr.Blocks(title="多策略 RAG 文件問答", css=CSS, theme=THEME, head=HEAD) as demo:
        gr.HTML(HEADER_HTML)
        session = gr.State(None)

        with gr.Row(equal_height=False, elem_classes="main-row"):
            # ── 左欄:設定 ─────────────────────────────
            with gr.Column(scale=1, min_width=320, elem_classes="card-box"):
                gr.HTML("<div class='sec-label'>Step 00 · Groq API Key</div>")
                with gr.Group(elem_id="apikey-box"):
                    api_key_input = gr.Textbox(
                        placeholder="gsk_xxxxxxxxxxxxxxxxxxxxxxxx",
                        type="password",
                        lines=1,
                        show_label=False,
                    )
                    apply_key_btn = gr.Button("套用 API Key", size="sm", elem_id="apply-key-btn")
                    api_key_status = gr.Textbox(
                        value=(
                            "✓ 伺服器已提供預設 API Key（可自行輸入覆蓋）"
                            if server_key
                            else "⚠ 尚未設定 API Key"
                        ),
                        interactive=False,
                        lines=1,
                        show_label=False,
                    )

                gr.HTML("<div class='sec-label'>Step 01 · 上傳文件</div>")
                file_input = gr.File(label="PDF / DOCX", file_types=[".pdf", ".docx"])
                load_btn = gr.Button("↑ 載入文件", elem_id="load-btn")
                status = gr.Textbox(label="狀態", interactive=False, lines=3)

                gr.HTML("<div class='sec-label'>Step 02 · 選擇 RAG 策略</div>")
                strategy = gr.Radio(
                    choices=[(f"{icon} {name}", key) for key, name, _, icon in STRATEGY_INFO],
                    value="semantic",
                    show_label=False,
                    elem_classes="strat-radio",
                )
                strategy_desc = gr.Markdown(
                    describe_strategy("semantic"), elem_classes="strat-desc"
                )

                gr.HTML("<div class='sec-label'>Step 03 · 搜尋參數</div>")
                topk = gr.Slider(1, 10, value=3, step=1, label="Top-K 片段數量")

            # ── 右欄:問答 ─────────────────────────────
            with gr.Column(scale=2, min_width=320, elem_classes="card-box"):
                gr.HTML("<div class='sec-label'>Step 04 · 輸入問題</div>")
                qin = gr.Textbox(
                    placeholder="例如：這份文件的核心論點是什麼？",
                    lines=4,
                    show_label=False,
                )
                ask_btn = gr.Button("提問", variant="primary", size="lg", elem_id="ask-btn")

                gr.HTML("<div class='sec-label'>AI 回答</div>")
                ans = gr.Markdown("_回答會顯示在這裡_", elem_classes="answer-box")

                with gr.Accordion("▸ 查看檢索到的文本片段", open=False):
                    src = gr.Textbox(lines=10, interactive=False, show_label=False)

                gr.Examples(examples=EXAMPLE_QS, inputs=qin, label="範例問題")

        # ── 事件綁定 ──────────────────────────────────
        key_io = dict(inputs=[session, api_key_input], outputs=[session, api_key_status])
        apply_key_btn.click(apply_api_key, **key_io)
        api_key_input.submit(apply_api_key, **key_io)
        load_btn.click(upload_document, [session, file_input], [session, status])
        strategy.change(describe_strategy, strategy, strategy_desc)

        ask_io = dict(inputs=[session, qin, strategy, topk], outputs=[session, ans, src])
        ask_btn.click(ask, **ask_io)
        qin.submit(ask, **ask_io)

    return demo


demo = create_interface()

# ══════════════════════════════════════════════════════════
#  FastAPI:PWA 必要檔案放在根目錄(Service Worker 需要 / 的範圍),Gradio 掛在最後
# ══════════════════════════════════════════════════════════
app = FastAPI(title="多策略 RAG 文件問答")


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(
        os.path.join(STATIC_DIR, "manifest.json"), media_type="application/manifest+json"
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        os.path.join(STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/offline.html", include_in_schema=False)
def offline():
    return FileResponse(os.path.join(STATIC_DIR, "offline.html"), media_type="text/html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


app.mount("/icons", StaticFiles(directory=os.path.join(STATIC_DIR, "icons")), name="icons")

# 必須最後掛載,否則會攔截上面的路由
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    if "--download" in sys.argv:  # 建置階段預先下載嵌入模型
        Shared.model()
        print("embedding model ready:", MODEL_NAME)
        sys.exit(0)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
