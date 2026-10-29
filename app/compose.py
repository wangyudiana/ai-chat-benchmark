# -*- coding: utf-8 -*-
"""把同一个问题下各产品的长截图横向拼成一张对比图，可直接贴进文档。"""
import pathlib

from PIL import Image, ImageDraw, ImageFont

from . import store

COL_W = 640
MAX_COL_H = 9000
PAD = 24
HEAD_H = 56
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]
_fonts = {}


def font(size):
    if size in _fonts:
        return _fonts[size]
    f = None
    for p in FONT_CANDIDATES:
        if pathlib.Path(p).exists():
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                pass
    _fonts[size] = f or ImageFont.load_default()
    return _fonts[size]


def wrap(draw, text, fnt, width, max_lines=3):
    lines, cur = [], ""
    for ch in text.replace("\n", " "):
        if draw.textlength(cur + ch, font=fnt) > width:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                lines[-1] = lines[-1][:-1] + "…"
                return lines
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def compose_job(jid):
    base = store.job_dir(jid)
    data = store.get_results(jid)
    sites = data["sites"]
    if len(sites) < 2:
        return 0
    out_dir = base / "compare"
    out_dir.mkdir(exist_ok=True)
    by_q = {}
    for r in data["results"]:
        by_q.setdefault(r["query_id"], {})[r["site"]] = r
    f_title, f_head, f_meta = font(26), font(22), font(17)
    made = 0
    for q in data["queries"]:
        recs = by_q.get(q["id"])
        if not recs:
            continue
        cols = []
        for s in sites:
            r = recs.get(s["key"])
            im = None
            if r and r.get("screenshot") and (base / r["screenshot"]).exists():
                im = Image.open(base / r["screenshot"]).convert("RGB")
                h = int(im.height * COL_W / im.width)
                im = im.resize((COL_W, max(1, h)), Image.LANCZOS)
                if h > MAX_COL_H:
                    im = im.crop((0, 0, COL_W, MAX_COL_H))
            cols.append((s, r, im))
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        total_w = PAD + len(cols) * (COL_W + PAD)
        title = f"{q['id']}  " + " → ".join(q.get("turns") or [q["query"]])
        title_lines = wrap(probe, title, f_title, total_w - 2 * PAD)
        title_h = PAD + len(title_lines) * 38 + 12
        body_h = max((im.height if im else 200) for _, _, im in cols)
        canvas = Image.new("RGB", (total_w, title_h + HEAD_H + body_h + PAD), (236, 240, 244))
        d = ImageDraw.Draw(canvas)
        for i, line in enumerate(title_lines):
            d.text((PAD, PAD + i * 38), line, fill=(24, 32, 44), font=f_title)
        for i, (s, r, im) in enumerate(cols):
            x, y = PAD + i * (COL_W + PAD), title_h
            d.rectangle([x, y, x + COL_W, y + HEAD_H - 8], fill=(255, 255, 255))
            d.text((x + 14, y + 10), s["name"], fill=(24, 32, 44), font=f_head)
            if r:
                meta = []
                if r.get("ttft_ms") is not None:
                    meta.append(f"首字 {r['ttft_ms'] / 1000:.1f}s")
                if r.get("total_ms") is not None:
                    meta.append(f"完成 {r['total_ms'] / 1000:.1f}s")
                meta.append(f"{len(r.get('response_text') or '')} 字")
                if r.get("status") != "ok":
                    meta.append("超时" if r.get("status") == "timeout" else "失败")
                txt = "   ".join(meta)
                d.text((x + COL_W - 14 - d.textlength(txt, font=f_meta), y + 14), txt, fill=(90, 102, 120), font=f_meta)
            if im:
                canvas.paste(im, (x, y + HEAD_H))
            else:
                d.text((x + 14, y + HEAD_H + 20), "无截图", fill=(150, 150, 150), font=f_meta)
        name = q["id"].replace("#", "_")
        canvas.save(out_dir / f"{name}.jpg", quality=86, optimize=True)
        made += 1
    return made
