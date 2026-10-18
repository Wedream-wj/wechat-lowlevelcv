"""离线生成三份排版演示稿（不联网、不调模型）。

用法：
    python -m scripts.demo

产出 out/demo_{paper,industry,contest}.html，可直接用浏览器打开看效果，
也是验证「结构化稿 → 微信内联 HTML」这条链路的最小样例。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import ROOT, settings
from app.llm.mock import DEMO_SPECS
from app.render import wechat
from app.render.cover import make_cover
from app.render.markdown import spec_to_markdown
from app.render.preview import render_preview
MODULE_LABEL = {"paper": "论文精读", "industry": "业界观察", "contest": "赛事速递"}

OUT = settings.out_dir
ASSETS = OUT / "assets"
DATE = "2026.10.18"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"

INK = "#1B2A4A"
PRIMARY = "#2A4BD7"
TEAL = "#00A88F"
LINE = "#C9D4E8"


def _font(size: int, bold: bool = True):
    for path in ([FONT_BOLD, FONT_REG] if bold else [FONT_REG, FONT_BOLD]):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _box(draw, xy, title, sub="", fill="#FFFFFF", border=PRIMARY, title_color=INK):
    x1, y1, x2, y2 = xy
    draw.rounded_rectangle(xy, radius=12, fill=fill, outline=border, width=2)
    if sub:
        draw.text(((x1 + x2) / 2, y1 + 22), title, font=_font(21), fill=title_color, anchor="mm")
        draw.text(((x1 + x2) / 2, y1 + 54), sub, font=_font(14, False), fill="#6B7280", anchor="mm")
    else:
        draw.text(((x1 + x2) / 2, (y1 + y2) / 2), title, font=_font(20), fill=title_color, anchor="mm")


def _arrow(draw, x1, y1, x2, y2, color="#8A9BC8"):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=2)
    if x2 >= x1:
        draw.polygon([(x2, y2), (x2 - 9, y2 - 5), (x2 - 9, y2 + 5)], fill=color)


def draw_architecture() -> Path:
    """画一张示意用的架构图（演示排版中的 figure 块）。"""
    W, H = 1200, 560
    img = Image.new("RGB", (W, H), "#FCFDFF")
    d = ImageDraw.Draw(img)

    d.text((48, 34), "Freqformer · Overall Architecture", font=_font(22), fill=INK)
    d.text((48, 66), "figure for layout preview", font=_font(12, False), fill="#9AA3B2")

    # 输入
    _box(d, (48, 228, 196, 332), "输入", "低质图像", fill="#F7F9FF")
    _arrow(d, 196, 280, 244, 280)

    # 频率分解
    _box(d, (244, 214, 396, 346), "频率分解", "高频 / 低频", fill="#EEF2FF")
    d.line([(396, 280), (430, 280)], fill="#8A9BC8", width=2)
    d.line([(430, 160), (430, 400)], fill="#8A9BC8", width=2)
    _arrow(d, 430, 160, 468, 160)
    _arrow(d, 430, 400, 468, 400)

    # 双分支
    _box(d, (468, 104, 700, 216), "高频分支", "空间局部纹理", fill="#FFFFFF", border=PRIMARY)
    _box(d, (468, 344, 700, 456), "低频分支", "尺度鲁棒色偏", fill="#FFFFFF", border=TEAL,
         title_color="#0B5C50")
    _arrow(d, 700, 160, 748, 160)
    _arrow(d, 700, 400, 748, 400)

    # 分支内模块
    _box(d, (748, 116, 916, 204), "Transformer ×N", "局部窗口注意力", fill="#F5F7FF", border="#B9C6F2")
    _box(d, (748, 356, 916, 444), "Transformer ×N", "通道 + 全局建模", fill="#F2FBF9", border="#9BDCCF")

    # 融合
    d.line([(916, 160), (944, 160)], fill="#8A9BC8", width=2)
    d.line([(916, 400), (944, 400)], fill="#8A9BC8", width=2)
    d.line([(944, 160), (944, 400)], fill="#8A9BC8", width=2)
    _arrow(d, 944, 280, 968, 280)
    _box(d, (968, 216, 1078, 344), "FCT 融合", "可学习频率合成", fill="#FFF8EC", border="#E8B06A",
         title_color="#8A5314")
    _arrow(d, 1078, 280, 1102, 280)
    _box(d, (1102, 224, 1178, 336), "输出", "重建图像", fill="#F0FAF6", border=TEAL,
         title_color="#0B5C50")

    # 图例
    d.line([(48, 486), (1152, 486)], fill="#EDF0F7", width=1)
    d.text((48, 504), "SA-CA：空间感知通道注意力，用于抑制摩尔纹敏感区域的冗余响应",
           font=_font(13, False), fill="#8A90A0")

    path = ASSETS / "demo_arch.png"
    img.save(path, quality=95)
    return path


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    arch = draw_architecture()

    for module, spec in DEMO_SPECS.items():
        cover_path = make_cover(spec, module, DATE, ASSETS / f"demo_cover_{module}.png")
        figures = {}
        if module == "paper":
            figures["F1"] = {
                "path": "assets/demo_arch.png",
                "caption": "Freqformer 总体架构：频率分解后由双分支分别处理高频纹理与低频色偏，FCT 负责融合。",
                "title": "Freqformer",
            }
        body = wechat.render_body(spec, figures, module, DATE)
        preview = render_preview(
            spec, body, module,
            cover=f"assets/demo_cover_{module}.png",
            issue_id=f"demo_{module}",
            date_str=DATE,
            module_label=MODULE_LABEL.get(module, module),
        )
        (OUT / f"demo_{module}.html").write_text(preview, encoding="utf-8")
        (OUT / f"demo_{module}_body.html").write_text(body, encoding="utf-8")
        (OUT / f"demo_{module}.md").write_text(spec_to_markdown(spec, figures), encoding="utf-8")
        print(f"[demo] {module:9s} -> out/demo_{module}.html  cover={Path(cover_path).name}")

    print(f"[demo] arch figure -> {arch}")


if __name__ == "__main__":
    main()
