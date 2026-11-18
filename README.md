# 📄 多策略 RAG 文件問答系統(PWA + RWD 版)

上傳 PDF / DOCX,以 **ChromaDB** 向量資料庫與 **8 種 RAG 檢索策略** 搭配 **Groq** LLM 進行文件問答。
可安裝成手機/電腦 App(PWA),版面支援手機、平板、桌機(RWD)。

## 功能
- 上傳 PDF / DOCX(DOCX 的表格也會擷取),自動切段並建立向量索引
- 8 種檢索策略:語意搜尋、TF-IDF、混合搜尋、重新排序、多查詢擴展、上下文壓縮、父子文檔、HyDE
- 使用者在介面輸入自己的 Groq API Key
- **PWA**:Manifest、Service Worker、離線提示頁、可安裝、自訂圖示
- **RWD**:手機上左右欄改為上下堆疊、策略選單為兩欄卡片、觸控按鈕 ≥ 44px、支援 iPhone 安全區、亮/暗色模式皆可讀

## 與原版(v2)的差異
| 項目 | v2 | 本版 |
|---|---|---|
| 使用者狀態 | 全域單一物件,所有訪客共用文件與 API Key | 每位使用者各自一份(`gr.State`),互不影響 |
| 策略選單 | HTML 按鈕 + 隱藏 Textbox(Gradio 不會渲染 `visible=False` 的元件,JS 無法寫入) | `gr.Radio` 以 CSS 排成卡片 |
| 環境變數 Key | 預填到網頁輸入框(公開部署會洩漏) | 只在伺服器端當備援,不會送到瀏覽器 |
| 嵌入模型 | 啟動時載入 | 首次使用才載入,服務更快開 port;建置階段可預先下載 |
| ChromaDB | 重複上傳不斷累積 | 換文件時刪除舊資料,啟動時清空殘留 |
| 大型文件 | 一次寫入 | 分批寫入,並有檔案大小上限 |

## 專案結構
```
.
├── app.py             # FastAPI + Gradio 主程式
├── requirements.txt
├── render.yaml        # (可選)Render Blueprint 設定
├── .python-version    # Python 3.11.9
├── make_icons.py      # (可選)重新產生 PWA 圖示
├── README.md
└── static/
    ├── manifest.json  # PWA 設定
    ├── sw.js          # Service Worker
    ├── offline.html   # 離線頁
    └── icons/         # 192 / 512 / maskable / apple-touch / favicon
```

## 本機執行
```bash
pip install -r requirements.txt
python app.py
```
開啟 http://localhost:7860。第一次載入文件時會下載嵌入模型(約數百 MB)。

## 部署到 Render

### 1. 推上 GitHub(`static/` 要一併提交)
```bash
git init
git add .
git commit -m "init rag pwa"
git branch -M main
git remote add origin https://github.com/<你的帳號>/<repo名稱>.git
git push -u origin main
```

### 2. 在 Render 建立服務
**方式 A:Blueprint**:New + → Blueprint → 選 repo,會讀取 `render.yaml`。
**方式 B:手動**:New + → Web Service → 選 repo,設定:

| 欄位 | 值 |
|---|---|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt && python app.py --download` |
| Start Command | `python app.py` |
| Health Check Path | `/healthz` |
| Environment | `PYTHON_VERSION` = `3.11.9` |

Build Command 結尾的 `python app.py --download` 會在建置階段先下載嵌入模型,避免第一次提問才下載而逾時。

### 3. 記憶體
嵌入模型加 PyTorch 執行時約需 1 GB 左右記憶體。Render 免費與 Starter 方案為 512 MB,**很可能記憶體不足(OOM)**,建議使用 2 GB 以上的方案(`render.yaml` 預設 `standard`)。實際規格與價格請以 Render 當前方案為準。

### 可選環境變數
| 變數 | 說明 | 預設 |
|---|---|---|
| `GROQ_API_KEY` | 伺服器預設 Key。**設定後所有訪客都會用你的額度**,公開部署建議留空,讓使用者自行輸入 | 無 |
| `GROQ_MODEL` | Groq 模型名稱 | `llama-3.1-8b-instant` |
| `EMBED_MODEL` | 嵌入模型 | `paraphrase-multilingual-MiniLM-L12-v2` |
| `MAX_FILE_MB` | 上傳檔案大小上限 | `20` |
| `CHROMA_PATH` / `MODEL_CACHE` | ChromaDB 與模型快取路徑 | 專案內 `chroma_db/`、`model_cache/` |

## 安裝成 App
- **Android / 電腦 Chrome、Edge**:點右下角「📲 安裝 App」,或網址列的安裝圖示。
- **iPhone / iPad(Safari)**:分享 → 加入主畫面(iOS 不會顯示安裝按鈕)。

PWA 需要 HTTPS,Render 已內建;本機 `localhost` 也可以。

## 驗證 PWA
Chrome 開發者工具 → **Application**:Manifest 無錯誤、Service Workers 為 activated;或用 **Lighthouse** 檢查。

## 注意事項
- 文件內容與向量只存在伺服器記憶體與暫存的 ChromaDB,Render 重啟或休眠後需要重新上傳。
- 使用者的 API Key 只存在該使用者的 session,不會寫入硬碟,也不會顯示給其他訪客。
- 掃描影像型 PDF 沒有文字層,需先 OCR。
- Service Worker 只快取圖示與離線頁,不快取問答資料;離線時無法使用,只會看到提示頁。
- 「重新排序」「多查詢擴展」「上下文壓縮」「HyDE」會多次呼叫 Groq API,較耗額度與時間。
- 更新 PWA 快取時,修改 `static/sw.js` 內的 `VERSION`。
