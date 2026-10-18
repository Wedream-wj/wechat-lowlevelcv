"""封面生成：1200×675（公众号首图 2.35:1 安全区），纯本地绘制，不依赖外部图片服务。

设计上刻意克制：深色底 + 单一强调色 + 大标题，缩到手机列表里也能看清。
如果系统没有中文字体，会自动回退并提示，不会抛异常阻断出稿。
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import settings, topics

W, H = 1200, 675
FONT_CANDIDATES = {
    "bold": [
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/msyhbd.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ],
    "regular": [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ],
}

PALETTE = {
    "paper": ((16, 24, 48), (36, 58, 132), "#7FA0FF"),
    "industry": ((9, 34, 33), (16, 84, 78), "#5FE0C8"),
    "contest": ((38, 24, 16), (104, 56, 26), "#FFB67A"),
}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES["bold" if bold else "regular"]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    """按像素宽度折行，中英文混排都能处理。"""
    lines, current = [], ""
    for ch in text:
        trial = current + ch
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = ch
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and draw.textlength(lines[-1], font=font) > max_width - 40:
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def _gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(size[1]):
        ratio = y / max(size[1] - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size[0], y)], fill=color)
    return img


def _decorate(img: Image.Image, accent: str) -> None:
    """右上角同心圆 + 底部细网格，给纯色底一点层次，但不抢文字。"""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    cx, cy = W - 120, 60
    for r in range(80, 420, 46):
        d.arc([cx - r, cy - r, cx + r, cy + r], start=200, end=340,
              fill=_rgba(accent, 26), width=2)
    for x in range(0, W, 40):
        d.line([(x, H - 1), (x, H - 40)], fill=(255, 255, 255, 8), width=1)
    for y in range(H - 40, H, 20):
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 8), width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))


def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def make_cover(spec: dict, module: str, date_str: str | None = None, out_path: str | Path | None = None) -> str:
    date_str = date_str or date.today().strftime("%Y.%m.%d")
    top, bottom, accent = PALETTE.get(module, PALETTE["paper"])
    img = _gradient((W, H), top, bottom)
    _decorate(img, accent)
    draw = ImageDraw.Draw(img)

    brand = topics.raw.get("brand", {})
    label_map = {"paper": "PAPER NOTES", "industry": "INDUSTRY", "contest": "CHALLENGES"}

    # 顶部：品牌 + 模块
    draw.rectangle([72, 64, 78, 96], fill=accent)
    draw.text((94, 64), brand.get("name", "底层视觉论文笔记"), font=_font(26, True), fill=(255, 255, 255))
    draw.text((96, 100), label_map.get(module, "NOTES"), font=_font(16, False),
              fill=_rgba(accent, 210)[:3])

    # 主标题
    title = str(spec.get("title", "")).strip()
    title_font = _font(64 if len(title) <= 18 else 56, True)
    lines = _wrap(draw, title, title_font, W - 160, max_lines=3)
    y = 200
    for line in lines:
        draw.text((72, y), line, font=title_font, fill=(255, 255, 255), spacing=8)
        y += 84

    # 强调线
    draw.rectangle([72, y + 8, 152, y + 13], fill=accent)

    # 副标题
    subtitle = str(spec.get("subtitle", "")).strip()
    if subtitle:
        sub_font = _font(25, False)
        for line in _wrap(draw, subtitle, sub_font, W - 200, max_lines=2):
            draw.text((72, y + 34), line, font=sub_font, fill=(214, 220, 235))
            y += 38

    # 底部信息
    draw.text((72, H - 84), f"{date_str}　·　{brand.get('slogan', '')}",
              font=_font(19, False), fill=(168, 178, 198))

    out = Path(out_path) if out_path else settings.image_dir / f"cover_{module}_{date_str.replace('.', '')}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return str(out)


def make_square_thumb(spec: dict, module: str, out_path: str | Path | None = None) -> str:
    """封面缩略图（2.35:1 之外的备用方图，部分场景需要）。"""
    cover = Path(make_cover(spec, module))
    img = Image.open(cover)
    side = min(img.size)
    left = (img.width - side) // 2
    img = img.crop((left, 0, left + side, side))
    out = Path(out_path) if out_path else settings.image_dir / f"thumb_{module}.png"
    img.save(out, quality=92)
    return str(out)
