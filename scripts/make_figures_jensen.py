"""为《黄仁勋重返英伟达》专题绘制自制信息图（手机优先版）。

为什么全部自制：外部新闻图有版权与防盗链风险，公众号正文里一旦裂图就是事故。
本脚本只画「有据可查的公开数字 + 官方公告口径」，配色沿用 app/render/wechat.py
与 app/render/cover.py 的 industry 主色（深青绿渐变 / #00A88F / #5FE0C8）。

为什么是 750px 宽 + 大字号：公众号正文图片在手机上按约 375px 全宽显示，
1080px 画布上的 18px 小字缩放后只剩 6px，等于糊成一片。这里统一 750px 画布、
最小字号 21px、说明正文 23px，缩放后仍有 11~13px，手机竖屏可直接读。
封面 fig00 例外：走系统 make_cover 的 1080×460 规范（列表缩略图本来就只放大标题）。

用法：python -m scripts.make_figures_jensen
产物：articles/figures/*.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.render.cover import make_cover

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "articles" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

W = 750
HEADER_H = 148
FOOTER_H = 62
PAD = 36

ACCENT = (0, 168, 143)
ACCENT_L = (95, 224, 200)
ACCENT_SOFT = (233, 248, 245)
DARK_TOP = (9, 34, 33)
DARK_BOT = (16, 84, 78)
BG = (242, 248, 247)
CARD = (255, 255, 255)
INK = (27, 29, 34)
SUB = (96, 112, 120)
LINE = (213, 228, 225)
WARM = (208, 106, 46)
WHITE = (255, 255, 255)

FONT_B = "C:/Windows/Fonts/msyhbd.ttc"
FONT_R = "C:/Windows/Fonts/msyh.ttc"


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT_R, size)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def canvas(height: int, no: str, title: str, sub: str) -> Image.Image:
    img = Image.new("RGB", (W, height), BG)
    d = ImageDraw.Draw(img)
    for y in range(HEADER_H):
        d.line([(0, y), (W, y)], fill=_lerp(DARK_TOP, DARK_BOT, y / HEADER_H))
    d.rounded_rectangle([PAD, 25, PAD + 4, 60], radius=2, fill=ACCENT_L)
    d.text((PAD + 15, 22), f"图 {no}", font=font(20), fill=ACCENT_L)
    d.text((PAD + 15, 48), "业界观察 · 黄仁勋重返英伟达", font=font(17, False), fill=(168, 206, 199))
    d.text((PAD, 76), title, font=font(38), fill=WHITE)
    d.text((PAD, 124), sub, font=font(18, False), fill=(183, 211, 206))
    return img


def footer(img: Image.Image, source: str) -> None:
    d = ImageDraw.Draw(img)
    h = img.height
    d.line([(PAD, h - FOOTER_H + 12), (W - PAD, h - FOOTER_H + 12)], fill=LINE, width=1)
    d.text((PAD, h - FOOTER_H + 24), f"来源：{source}", font=font(16, False), fill=SUB)
    d.text((W - PAD, h - FOOTER_H + 24), "制表：业界观察", font=font(16, False),
           fill=SUB, anchor="ra")


def wrap(d: ImageDraw.ImageDraw, text: str, fnt, maxw: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and d.textlength(cur + ch, font=fnt) > maxw:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- fig00 封面
def fig00() -> Path:
    out = OUT / "fig00_cover.png"
    make_cover(
        {"title": "黄仁勋重返英伟达", "subtitle": "十一天，一次 180 度转身 · 业界观察专题"},
        "industry", "2026.11.23", out_path=out,
    )
    return out


# ---------------------------------------------------------------- 时间轴类
TIMELINE_11 = [
    ("11.08", "全员信与独立调查", "致信员工致歉；董事会启动独立调查，当时仍表示对管理层抱有信心。"),
    ("11.10", "外部法律顾问到位", "独立董事聘请外部律师；公司强调业务与产品路线图不中断。"),
    ("11.11", "监管文件里的交接", "董事会接受辞职，即刻生效；克雷斯任临时 CEO，琼斯任首任董事会主席。"),
    ("11.12", "对外公告", "新闻稿公布辞职，股价开盘重挫约 14%，市值跌破 4 万亿美元。"),
    ("11.17", "财报会：无意恢复", "琼斯称无意恢复其职务，已聘猎头；黄仁勋同日表达回归意愿。"),
    ("11.20", "压力升温", "股价单日再跌 14%；公开呼吁在致歉后让其回归。"),
    ("11.22", "预期成形", "多方公开表态，不反对黄仁勋重返英伟达。"),
    ("11.23", "全票复职", "三项职务即刻恢复，盘前涨幅一度超过 30%。"),
]

MILESTONES = [
    ("1993", "三人创立英伟达", "4 月 5 日创立，目标是把 3D 图形带进游戏与多媒体市场。"),
    ("1997", "RIVA 128 拉回悬崖边", "NV1 与 Direct3D 不兼容，距破产仅 30 天；RIVA 128 四个月出货破百万片。"),
    ("1999", "GPU 被写进产业", "GeForce 256 发布并定义为世界上第一个 GPU，公司同年上市。"),
    ("2006", "CUDA：六年蛰伏", "力排众议把 CUDA 内置进所有 GPU，当时市值仅约 10 亿美元。"),
    ("2012", "AlexNet 叩开 AI 之门", "深度学习模型靠英伟达 GPU 夺冠，CUDA 成为难以迁移的护城河。"),
    ("2018", "RTX 与 Omniverse", "RTX 实现实时光追；2022 年推出 Omniverse，公司转向计算平台。"),
    ("2026", "AI 算力核心供应商", "超 400 万开发者、4 万余家企业使用其技术，单季营收上千亿美元。"),
]


def timeline(no: str, title: str, sub: str, nodes: list[tuple[str, str, str]],
             source: str, filename: str, row_h: int = 132) -> Path:
    top = HEADER_H + 28
    height = top + len(nodes) * row_h + 20 + FOOTER_H
    img = canvas(height, no, title, sub)
    d = ImageDraw.Draw(img)

    line_x = PAD + 22
    d.line([(line_x, top + 12), (line_x, top + (len(nodes) - 1) * row_h + 22)],
           fill=LINE, width=3)
    for i, (tag, head, desc) in enumerate(nodes):
        y = top + i * row_h
        d.ellipse([line_x - 7, y + 12, line_x + 7, y + 26], fill=ACCENT, outline=WHITE, width=2)
        box = (PAD + 52, y - 4, W - PAD, y + row_h - 14)
        d.rounded_rectangle(box, radius=12, fill=CARD)
        d.rounded_rectangle([box[0], box[1], box[0] + 4, box[3]], radius=2, fill=ACCENT)
        d.text((box[0] + 18, y + 8), tag, font=font(31), fill=ACCENT)
        d.text((box[0] + 132, y + 10), head, font=font(27), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(23, False), box[2] - box[0] - 40)[:2]):
            d.text((box[0] + 18, y + 50 + j * 30), ln, font=font(23, False), fill=SUB)

    footer(img, source)
    out = OUT / filename
    img.save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------- 财报数字卡
def fig03() -> Path:
    top = HEADER_H + 28
    height = top + 3 * 124 + 20 + 172 + 20 + FOOTER_H
    img = canvas(height, "03", "一份超预期的三季报", "FY2027 Q3 · 截至 2026 年 10 月 25 日")
    d = ImageDraw.Draw(img)

    cards = [
        ("总营收", "1,158.44", "亿美元", "同比 +103%"),
        ("数据中心业务", "1,076", "亿美元", "同比 +110%"),
        ("GAAP 净利润", "652.87", "亿美元", "同比 +105%"),
    ]
    for i, (label, num, unit, yoy) in enumerate(cards):
        y = top + i * 124
        d.rounded_rectangle([PAD, y, W - PAD, y + 108], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 4, y + 108], radius=2, fill=ACCENT)
        d.text((PAD + 22, y + 12), label, font=font(25), fill=SUB)
        nf = font(48)
        d.text((PAD + 22, y + 44), num, font=nf, fill=INK)
        nx = PAD + 22 + d.textlength(num, font=nf) + 10
        d.text((nx, y + 68), unit, font=font(22, False), fill=SUB)
        pill_w = d.textlength(yoy, font=font(25)) + 22
        px = W - PAD - pill_w - 18
        d.rounded_rectangle([px, y + 50, px + pill_w, y + 88], radius=18, fill=ACCENT_SOFT)
        d.text((px + 11, y + 55), yoy, font=font(25), fill=ACCENT)

    y1 = top + 3 * 124 + 20
    d.rounded_rectangle([PAD, y1, W - PAD, y1 + 172], radius=14, fill=CARD)
    notes = [
        "摊薄每股收益 2.70 美元（同比 +108%），营收超 1,080 亿指引中值与 1,093.3 亿市场预期。",
        "数据中心收入环比 +21%，高于市场预期的 1,018.7 亿美元。",
        "克雷斯将 FY2028 收入增速预期由约 70% 上调至约 75%。",
    ]
    yy = y1 + 18
    for n in notes:
        for j, ln in enumerate(wrap(d, n, font(21, False), W - 2 * PAD - 62)[:2]):
            if j == 0:
                d.ellipse([PAD + 22, yy + 9, PAD + 29, yy + 16], fill=ACCENT)
            d.text((PAD + 40, yy), ln, font=font(21, False), fill=INK)
            yy += 26
        yy += 12

    footer(img, "英伟达 FY2027 Q3 财报及电话会（公开报道整理）")
    out = OUT / "fig03_earnings.png"
    img.save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------- 市值账本
def fig04() -> Path:
    top = HEADER_H + 28
    height = top + 436 + 20 + 126 + 20 + FOOTER_H
    img = canvas(height, "04", "十一天的市值账本", "芯片没变，CUDA 没变，财报没变")
    d = ImageDraw.Draw(img)
    cw = (W - 2 * PAD - 20) // 2
    panels = [
        (PAD, "离任期间", (247, 249, 250), (120, 140, 145), [
            ("七个交易日累计", "-27.7%"), ("市值蒸发", "约 1.16 万亿美元"),
            ("11 月 20 日单日", "-14%"), ("较 10 月高点", "回落近 50%"),
            ("11/11 收盘市值", "4.19 万亿美元"),
        ]),
        (PAD + cw + 20, "复职当日", ACCENT_SOFT, ACCENT, [
            ("盘前一度", "+31%"), ("开盘涨幅", "+33%"), ("股价", "166.61 美元"),
            ("总市值", "4.01 万亿美元"), ("单日市值增加", "超 1 万亿美元"),
        ]),
    ]
    for x, head, bg, tone, rows in panels:
        d.rounded_rectangle([x, top, x + cw, top + 436], radius=16, fill=bg)
        d.rounded_rectangle([x, top, x + cw, top + 5], radius=3, fill=tone)
        d.text((x + 22, top + 18), head, font=font(31), fill=tone)
        for i, (k, v) in enumerate(rows):
            yy = top + 66 + i * 72
            d.text((x + 22, yy), k, font=font(21, False), fill=SUB)
            d.text((x + 22, yy + 26), v, font=font(29), fill=INK)

    y1 = top + 436 + 20
    d.rounded_rectangle([PAD, y1, W - PAD, y1 + 126], radius=14, fill=CARD)
    d.rounded_rectangle([PAD, y1, PAD + 5, y1 + 126], radius=2, fill=ACCENT)
    quote = "同一批投资者，先定价「没有黄仁勋的英伟达」，再定价「黄仁勋回来了」。这组波动，是对创始人个人权重的一次压力测试。"
    for j, ln in enumerate(wrap(d, quote, font(23), W - 2 * PAD - 56)[:3]):
        d.text((PAD + 24, y1 + 20 + j * 32), ln, font=font(23), fill=INK)

    footer(img, "界面新闻、财联社、极目新闻 11-23 报道（盘中数据随行情变动）")
    out = OUT / "fig04_marketcap.png"
    img.save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------- 产业链条形
CHAIN = [
    ("Nebius Group（算力云）", 29.17, ACCENT),
    ("CoreWeave（算力云）", 27.60, ACCENT),
    ("Coherent（光互联）", 22.21, ACCENT),
    ("Lumentum（光互联）", 21.68, ACCENT),
    ("Hut 8（算力云）", 18.35, ACCENT),
    ("台积电（晶圆代工）", 17.40, ACCENT),
    ("西部数据（存储）", 16.29, ACCENT),
    ("美光科技（存储）", 14.44, ACCENT),
    ("AMD（GPU 竞品）", 9.14, WARM),
]


def fig05() -> Path:
    top = HEADER_H + 36
    row_h = 60
    height = top + len(CHAIN) * row_h + 18 + 92 + FOOTER_H
    img = canvas(height, "05", "产业链的连锁反应", "复职后 AI 半导体板块盘中表现")
    d = ImageDraw.Draw(img)
    # 名称最长是「Nebius Group（算力云）」，22px 下约 254px；条形起点必须让到 302，
    # 否则第一行的绿条会盖住名称尾巴（手机端尤其明显）。
    x0, x1 = 302, W - PAD - 92
    span = x1 - x0
    for i, (name, val, color) in enumerate(CHAIN):
        y = top + i * row_h
        d.text((PAD, y + 12), name, font=font(22), fill=INK)
        d.rounded_rectangle([x0, y + 10, x1, y + 38], radius=14, fill=(229, 238, 236))
        d.rounded_rectangle([x0, y + 10, x0 + int(span * val / 32.0), y + 38],
                            radius=14, fill=color)
        d.text((W - PAD, y + 10), f"+{val:.2f}%", font=font(25), fill=color, anchor="ra")

    y1 = top + len(CHAIN) * row_h + 18
    d.rounded_rectangle([PAD, y1, W - PAD, y1 + 92], radius=14, fill=CARD)
    note = "纳指涨 6.95%、标普 500 涨 4.93%；AMD 涨幅明显弱于上游供应链，「去英伟达化」替代逻辑阶段性降温。"
    for j, ln in enumerate(wrap(d, note, font(21, False), W - 2 * PAD - 44)[:2]):
        d.text((PAD + 22, y1 + 16 + j * 30), ln, font=font(21, False), fill=SUB)

    footer(img, "极目新闻、界面新闻 11-23 盘中行情（非投资建议）")
    out = OUT / "fig05_supply_chain.png"
    img.save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------- 治理对比
GOVERNANCE = [
    ("总裁兼 CEO", "克雷斯（临时） → 黄仁勋"),
    ("董事会主席", "琼斯（首任，新设） → 留任"),
    ("CFO", "克雷斯兼任 → 回任 CFO"),
    ("独立董事调查", "启动 → 继续推进直至完成"),
    ("首席合规官", "未设 → 新设，向治理委员会汇报"),
    ("国安与出口管制委员会", "离任期间新设 → 延续，按季度开会"),
    ("政府调查与问询", "配合 → 继续配合"),
]


def fig06() -> Path:
    top = HEADER_H + 74
    row_h = 66
    height = top + len(GOVERNANCE) * row_h + 20 + FOOTER_H
    img = canvas(height, "06", "被改写的治理结构", "复职公告里最容易被忽略的几句话")
    d = ImageDraw.Draw(img)

    # 标签列留 280px：最长「国安与出口管制委员会」11 个中文字 × 23px ≈ 253px，
    # 第二列起点必须 ≥ PAD+280，否则长标签会压到右侧内容上。
    d.rounded_rectangle([PAD, top - 50, PAD + 268, top - 8], radius=10, fill=(227, 234, 233))
    d.text((PAD + 16, top - 44), "治理事项", font=font(21), fill=SUB)
    d.rounded_rectangle([PAD + 280, top - 50, W - PAD, top - 8], radius=10, fill=ACCENT_SOFT)
    d.text((PAD + 296, top - 44), "11 月 12 日之后 → 11 月 23 日之后", font=font(20), fill=ACCENT)

    for i, (k, v) in enumerate(GOVERNANCE):
        y = top + i * row_h
        if i % 2 == 0:
            d.rounded_rectangle([PAD, y - 4, W - PAD, y + row_h - 10], radius=12, fill=CARD)
        d.text((PAD + 16, y + 14), k, font=font(22), fill=INK)
        for j, ln in enumerate(wrap(d, v, font(21, False), W - PAD - (PAD + 280) - 16)[:2]):
            d.text((PAD + 280, y + 6 + j * 28), ln, font=font(21, False), fill=SUB)

    footer(img, "英伟达官网 11/12、11/23 公告及公开报道")
    out = OUT / "fig06_governance.png"
    img.save(out, format="PNG", optimize=True)
    return out


def main() -> None:
    made = [fig00()]
    made.append(timeline("01", "十一天：一次 180 度转身",
                         "从「即刻生效」到「一致通过」",
                         TIMELINE_11,
                         "英伟达公告、SEC 文件、腾讯新闻、财联社等",
                         "fig01_timeline.png"))
    made.append(timeline("02", "三十三年与三次押注",
                         "为什么公司命运与一个人绑定得如此紧",
                         MILESTONES,
                         "英伟达官网时间线、人民日报客户端等",
                         "fig02_milestones.png"))
    made += [fig03(), fig04(), fig05(), fig06()]
    for p in made:
        print(f"  {p.name}  {Image.open(p).size}")


if __name__ == "__main__":
    main()
