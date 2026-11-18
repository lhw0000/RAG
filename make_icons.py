"""產生 PWA 圖示(需 Pillow)。執行:python make_icons.py"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons")
os.makedirs(OUT, exist_ok=True)

GREEN_DARK = (27, 67, 50)
GREEN = (64, 145, 108)
ACCENT = (45, 106, 79)
AMBER = (245, 176, 65)


def gradient(size):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = tuple(int(GREEN[i] + (GREEN_DARK[i] - GREEN[i]) * t) for i in range(3))
    return img


def draw_icon(size, maskable=False):
    s = size * 4
    img = gradient(s).convert("RGBA")
    d = ImageDraw.Draw(img)
    k = s * (0.56 if maskable else 0.70) / 100  # 100 單位 = 圖示 56% / 70%
    cx, cy = s / 2, s / 2

    def P(u, v):  # 單位座標 → 像素(以圖形包圍盒中心 (57,52) 置中)
        return (cx + (u - 57) * k, cy + (v - 52) * k)

    # 文件頁面(右上角摺角)
    d.polygon([P(18, 8), P(52, 8), P(70, 26), P(70, 90), P(18, 90)], fill=(255, 255, 255, 255))
    d.polygon([P(52, 8), P(52, 26), P(70, 26)], fill=(214, 228, 221, 255))
    # 文字行
    for y in (40, 52, 64):
        d.rounded_rectangle([*P(28, y), *P(58, y + 5)], radius=2.5 * k, fill=ACCENT)
    # 放大鏡
    r = 17
    cxm, cym = 68, 68
    d.ellipse([*P(cxm - r, cym - r), *P(cxm + r, cym + r)], fill=(255, 255, 255, 255))
    d.ellipse([*P(cxm - r, cym - r), *P(cxm + r, cym + r)], outline=AMBER, width=int(7 * k))
    d.line([P(80, 80), P(95, 95)], fill=AMBER, width=int(9 * k))
    for pt in (P(80, 80), P(95, 95)):  # 圓頭
        rr = 4.5 * k
        d.ellipse([pt[0] - rr, pt[1] - rr, pt[0] + rr, pt[1] + rr], fill=AMBER)

    img = img.resize((size, size), Image.LANCZOS)
    if not maskable:
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, s, s], radius=s * 0.22, fill=255)
        img.putalpha(mask.resize((size, size), Image.LANCZOS))
    return img


draw_icon(192).save(os.path.join(OUT, "icon-192.png"))
draw_icon(512).save(os.path.join(OUT, "icon-512.png"))
draw_icon(512, maskable=True).save(os.path.join(OUT, "icon-maskable-512.png"))
draw_icon(180, maskable=True).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"))
draw_icon(32).save(os.path.join(OUT, "favicon-32.png"))
print("icons generated in", OUT)
