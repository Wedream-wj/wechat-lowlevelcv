"""两篇 NTIRE 2026 历史稿的契合主题封面主视觉（hero，1080×460）。

2026-11-26 修复：《离岸流分割》《摄影修图迁移》两稿原本共用
`cover_contest_20261123.jpg`（文件里写的是第三篇《恶劣条件 3D 重建》的标题）。
这两张 hero 用 PIL 自制、零外部图片：
  · 规避新闻图版权、微信 CDN 防盗链与素材库裂图风险；
  · 视觉主题与赛题严格对应 —— 海岸/海浪/分割遮罩 vs 修图前后色彩迁移对比。

构图遵循 `make_cover` 的左暗蒙版：主体视觉一律置于右半，左侧留给标题白字。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from app.render.cover import _font

W, H = 1080, 460
OUT = Path(__file__).resolve().parents[1] / "articles" / "figures3"


def _text(draw: ImageDraw.ImageDraw, xy, text: str, size: int,
          fill, bold: bool = True) -> None:
    draw.text(xy, text, font=_font(size, bold), fill=fill)


def _label(draw: ImageDraw.ImageDraw, x: int, y: int, text: str,
           color=(255, 255, 255), size: int = 14) -> None:
    """右上角小标签：细下划线 + 等距大写英文，科技信息图质感。"""
    _text(draw, (x, y), text, size, color, bold=False)
    w = draw.textlength(text, font=_font(size, False))
    draw.line([(x, y + size + 8), (x + min(w, 300), y + size + 8)], fill=color, width=1)


def rip_current() -> Path:
    """离岸流分割：海岸俯视 + 碎波带 + 高亮离岸流通道的分割掩膜。"""
    img = Image.new("RGB", (W, H), (8, 20, 34))
    d = ImageDraw.Draw(img)

    # 右半海面：自上而下由青转深蓝
    for y in range(H):
        t = y / H
        d.line([(360, y), (W, y)],
               fill=(int(12 + 18 * t), int(52 + 34 * t), int(82 + 40 * t)))

    # 近岸沙带（底部）
    for y in range(366, H):
        t = (y - 366) / (H - 366)
        d.line([(360, y), (W, y)],
               fill=(int(118 + 58 * t), int(96 + 44 * t), int(62 + 30 * t)))
    d.line([(360, 366), (W, 366)], fill=(214, 186, 140), width=2)

    # 碎波带：多条正弦浪线（从 y=126 起，避开右上角的文字标签区）
    for k in range(7):
        y0 = 126 + k * 38
        pts = [(x, int(y0 + 9 * math.sin((x - 360) / 58 + k * 0.72)
                       + 5 * math.sin((x - 360) / 23)))
               for x in range(360, W, 5)]
        d.line(pts, fill=(176, 218, 234), width=2)

    # 离岸流通道 + 分割掩膜（半透明层，避免遮挡浪线）
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    poly = [(716, 366), (792, 366), (872, 128), (806, 128)]
    od.polygon(poly, fill=(95, 224, 200, 66))
    od.line(poly + [poly[0]], fill=(95, 224, 200, 235), width=3)
    # 通道内掩膜网格（分割任务的视觉隐喻）
    for j in range(6):
        y = 348 - j * 38
        half = 38 - j * 3
        cx = 754 + j * 10
        od.line([(cx - half, y), (cx + half, y)], fill=(95, 224, 200, 90), width=1)
    # 离岸方向箭头
    od.line([(838, 302), (838, 158)], fill=(255, 182, 122, 245), width=5)
    od.polygon([(838, 142), (827, 166), (849, 166)], fill=(255, 182, 122, 245))
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")

    d = ImageDraw.Draw(img)
    _text(d, (600, 34), "RIPDETSEG  /  NTIRE 2026", 22, (255, 255, 255))
    _text(d, (602, 66), "RIP CURRENT SEGMENTATION", 15, (168, 214, 232), bold=False)
    _label(d, 908, 300, "MASK", color=(95, 224, 200), size=14)
    _label(d, 600, 410, "F1 / F2 @ IoU 0.5 & 0.4:0.95", color=(196, 214, 226), size=13)
    _text(d, (600, 92), "RipVIS  ·  detection + instance segmentation",
          13, (150, 182, 202), bold=False)
    return _save(img, "hero_rip_current.png")


def retouching() -> Path:
    """摄影修图迁移：同一场景编辑前（冷暗）→ 编辑后（暖亮）的色彩迁移对比。"""
    img = Image.new("RGB", (W, H), (10, 14, 22))
    d = ImageDraw.Draw(img)

    def scene(x0: int, x1: int, sky_top, sky_bot, mtn, sun) -> None:
        w = x1 - x0
        # 天空渐变
        for y in range(96, 322):
            t = (y - 96) / 226
            d.line([(x0, y), (x1, y)],
                   fill=tuple(int(sky_top[i] + (sky_bot[i] - sky_top[i]) * t)
                              for i in range(3)))
        # 日轮
        cx, cy = x0 + int(w * 0.66), 196
        d.ellipse([cx - 32, cy - 32, cx + 32, cy + 32], fill=sun)
        # 远山 / 地平线
        d.polygon([(x0, 322), (x0 + int(w * 0.28), 242),
                   (x0 + int(w * 0.46), 292), (x0 + int(w * 0.70), 218),
                   (x1, 322)], fill=mtn)
        d.rectangle([x0, 322, x1, 352],
                    fill=tuple(max(0, c - 34) for c in mtn))
        # 前景反光带
        d.rectangle([x0, 352, x1, 372],
                    fill=tuple(min(255, c + 26) for c in mtn))

    # 左：编辑前（低饱和冷调）；右：编辑后（暖调高亮）
    scene(470, 770, (56, 68, 84), (98, 112, 126), (46, 58, 70), (168, 178, 188))
    scene(770, W, (250, 148, 88), (255, 216, 150), (128, 84, 62), (255, 244, 198))

    # 分界 + 迁移箭头
    d.line([(770, 96), (770, 372)], fill=(255, 255, 255), width=2)
    d.line([(722, 232), (818, 232)], fill=(255, 255, 255), width=4)
    d.polygon([(826, 232), (806, 222), (806, 242)], fill=(255, 255, 255))

    # 色彩映射色卡（冷 → 暖）
    cold = [(56, 68, 84), (78, 96, 112), (108, 126, 138), (146, 160, 168)]
    warm = [(196, 96, 58), (232, 138, 74), (248, 178, 96), (255, 216, 150)]
    for i, (c, c2) in enumerate(zip(cold, warm)):
        x = 470 + i * 76
        d.rectangle([x, 392, x + 62, 410], fill=c)
        d.rectangle([x, 414, x + 62, 420], fill=c2)

    _text(d, (470, 34), "PHOTO RETOUCHING TRANSFER", 22, (255, 255, 255))
    _text(d, (472, 66), "NTIRE 2026  ·  reference-guided editing", 15,
          (206, 196, 184), bold=False)
    _text(d, (486, 108), "BEFORE", 14, (226, 236, 246), bold=False)
    _text(d, (786, 108), "AFTER", 14, (72, 44, 28), bold=False)
    _label(d, 470, 430, "INR  ·  TEST-TIME OPTIMIZATION", color=(206, 196, 184), size=13)
    return _save(img, "hero_retouching.png")


def _save(img: Image.Image, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    img.save(p, format="PNG", optimize=True)
    print(f"  {p.name}  {img.size}  {p.stat().st_size // 1024} KB")
    return p


def main() -> None:
    print("生成两篇 NTIRE 2026 稿的契合主题封面 hero：")
    rip_current()
    retouching()


if __name__ == "__main__":
    main()
