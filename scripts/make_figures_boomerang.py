"""为《“我走得有点仓促”：为黄仁勋辞职的工程师，11 天后考虑回英伟达》绘信息图。

全部自制：外部新闻图有版权与防盗链风险，公众号正文裂图就是事故。图内只写
「公开报道可查的事实 + 明确标注的业内观点」，配色沿用 industry 主色（深青绿）。

手机优先：750px 画布、最小字号 21px，375px 宽手机上仍有 11~13px 可读。
fig00 例外：1080×460 宽幅主视觉，同时作正文头图与系统封面的 hero。

用法：python -m scripts.make_figures_boomerang
产物：articles/figures2/*.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "articles" / "figures2"
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


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def canvas(height: int, no: str, title: str, sub: str) -> Image.Image:
    img = Image.new("RGB", (W, height), BG)
    d = ImageDraw.Draw(img)
    for y in range(HEADER_H):
        d.line([(0, y), (W, y)], fill=_lerp(DARK_TOP, DARK_BOT, y / HEADER_H))
    d.rounded_rectangle([PAD, 25, PAD + 4, 60], radius=2, fill=ACCENT_L)
    d.text((PAD + 15, 22), f"图 {no}", font=font(20), fill=ACCENT_L)
    d.text((PAD + 15, 48), "业界观察 · 英伟达人才去留", font=font(17, False), fill=(168, 206, 199))
    d.text((PAD, 76), title, font=font(36), fill=WHITE)
    d.text((PAD, 124), sub, font=font(18, False), fill=(183, 211, 206))
    return img


def footer(img: Image.Image, source: str) -> None:
    d = ImageDraw.Draw(img)
    h = img.height
    d.line([(PAD, h - FOOTER_H + 12), (W - PAD, h - FOOTER_H + 12)], fill=LINE, width=1)
    lines = wrap(d, f"来源：{source}", font(16, False), W - 2 * PAD - 190)
    for j, ln in enumerate(lines[:2]):
        d.text((PAD, h - FOOTER_H + 20 + j * 20), ln, font=font(16, False), fill=SUB)
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


def save(img: Image.Image, name: str) -> Path:
    out = OUT / name
    img.save(out, format="PNG", optimize=True)
    return out


# ---------------------------------------------------------------- fig00 主视觉
def fig00() -> Path:
    w, h = 1080, 460
    img = Image.new("RGB", (w, h), DARK_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp((7, 27, 27), (13, 66, 62), y / h))
    # 电路板纹理：右侧稀疏走线 + 节点，暗示算力主题，不出现任何企业标识
    import random
    rng = random.Random(20261125)
    for i in range(7):                       # 只画上半部，避开底部流程节点区
        y = 44 + i * 44
        x = 690 + rng.randint(0, 90)
        d.line([(x, y), (w - 26, y)], fill=(30, 104, 96), width=2)
        steps = rng.randint(2, 4)
        for s in range(steps):
            xx = x + (w - 26 - x) * s // steps
            d.ellipse([xx - 4, y - 4, xx + 4, y + 4], fill=(45, 138, 125))
    d.rounded_rectangle([56, 42, 60, 84], radius=2, fill=ACCENT_L)
    d.text((72, 40), "业界观察 · 英伟达人才去留", font=font(22), fill=ACCENT_L)
    d.text((56, 108), "11", font=font(148), fill=WHITE)
    d.text((238, 176), "天", font=font(58), fill=ACCENT_L)
    d.text((56, 272), "从「用脚投票」到「愿意回归」", font=font(44), fill=WHITE)
    d.text((56, 330), "一名上海工程师的去留，与一家四万亿美元公司的权力回摆",
           font=font(23, False), fill=(183, 211, 206))
    nodes = [("11.12", "辞任"), ("11.15", "离职"), ("11.23", "复职"), ("11.24", "松口")]
    x = 56
    for i, (day, label) in enumerate(nodes):
        d.rounded_rectangle([x, 386, x + 152, 424], radius=8, fill=(18, 74, 70))
        d.text((x + 12, 392), day, font=font(23), fill=ACCENT_L)
        d.text((x + 84, 392), label, font=font(23), fill=WHITE)
        if i < len(nodes) - 1:
            d.text((x + 158, 392), "→", font=font(22), fill=(120, 176, 168))
        x += 200
    return save(img, "fig00_main.png")


# ---------------------------------------------------------------- 封面专用 hero
def hero_clean() -> Path:
    """无文字的电路纹理底图：给 make_cover 当 hero。

    fig00 底部有四个流程节点，作封面 hero 时会被压暗后从标题下方透出来，
    视觉上像「多了一行残影」。封面只需要纹理，标题文字由 make_cover 自己画。
    """
    w, h = 1080, 460
    img = Image.new("RGB", (w, h), DARK_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp((7, 27, 27), (13, 66, 62), y / h))
    import random
    rng = random.Random(20261125)
    for i in range(8):
        y = 40 + i * 52
        x = 660 + rng.randint(0, 110)
        d.line([(x, y), (w - 26, y)], fill=(30, 104, 96), width=2)
        steps = rng.randint(2, 4)
        for s in range(steps):
            xx = x + (w - 26 - x) * s // steps
            d.ellipse([xx - 4, y - 4, xx + 4, y + 4], fill=(45, 138, 125))
    return save(img, "hero_clean.png")


# ---------------------------------------------------------------- fig01 时间轴
TIMELINE = [
    ("11.08", "致歉与立案", "全员公开信；董事会启动独立调查，当时仍对管理层抱有信心。"),
    ("11.12", "辞任即刻生效", "三项职务卸下，克雷斯任临时 CEO；股价开盘跌约 13%。"),
    ("11.15", "工程师发帖离职", "戴士钦称黄仁勋离开是自己离开的重要原因，帖子超 13 万赞。"),
    ("11.17", "财报会公开拒绝", "琼斯称无意恢复 CEO 职务；黄仁勋同日表态愿意回归。"),
    ("11.20", "手续与告别帖", "办完离职、权限当日关停；次日发帖「一切都结束得如此之快」。"),
    ("11.22", "转折", "贝森特不反对回归；SEC 文件显示董事会已作出复职决定。"),
    ("11.23", "全票复职", "三项职务即刻恢复，总部数百员工鼓掌欢呼。"),
    ("11.24", "松口", "「可能做得太仓促了」，愿意探讨回归的可能性。"),
]


def fig01() -> Path:
    top = HEADER_H + 28
    row_h = 132
    height = top + len(TIMELINE) * row_h + 20 + FOOTER_H
    img = canvas(height, "01", "十一天，一条反转时间线", "从「即刻生效」到「愿意探索回归」")
    d = ImageDraw.Draw(img)
    line_x = PAD + 22
    d.line([(line_x, top + 12), (line_x, top + (len(TIMELINE) - 1) * row_h + 22)],
           fill=LINE, width=3)
    for i, (tag, head, desc) in enumerate(TIMELINE):
        y = top + i * row_h
        d.ellipse([line_x - 7, y + 12, line_x + 7, y + 26], fill=ACCENT, outline=WHITE, width=2)
        box = (PAD + 52, y - 4, W - PAD, y + row_h - 14)
        d.rounded_rectangle(box, radius=12, fill=CARD)
        d.rounded_rectangle([box[0], box[1], box[0] + 4, box[3]], radius=2, fill=ACCENT)
        d.text((box[0] + 18, y + 8), tag, font=font(27), fill=ACCENT)
        d.text((box[0] + 152, y + 10), head, font=font(26), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(22, False), box[2] - box[0] - 40)[:2]):
            d.text((box[0] + 18, y + 50 + j * 29), ln, font=font(22, False), fill=SUB)
    footer(img, "英伟达公告、SEC 文件、界面新闻、第一财经、21 世纪经济报道等 11 月公开报道")
    return save(img, "fig01_timeline.png")


# ---------------------------------------------------------------- fig02 人物档案
PROFILE = [
    ("身份", "前英伟达上海研发中心高级研究工程师"),
    ("方向", "机器人学习 / 具身智能"),
    ("入职", "2025 年 11 月，任职一年多"),
    ("此前", "逐际动力、众擎机器人 机器人学习工程师"),
]
REASONS = [
    ("长期押注", "CUDA 早于深度学习热潮六年，AI 布局早于成为焦点；具身智能是同一套逻辑。"),
    ("工程师文化", "鼓励探索、承担风险、从错误中学习；Top 5 邮件直达创始人，想法能被听到。"),
    ("沟通缺失", "未获足以理解情况的解释，「被排除在重要决策之外」的失落感。"),
]


def fig02() -> Path:
    top = HEADER_H + 28
    card_h = 246
    gap = 20
    reason_h = 158
    height = top + card_h + gap + len(REASONS) * (reason_h + 14) + 20 + FOOTER_H
    img = canvas(height, "02", "一位上海工程师与三条理由", "戴士钦（Darrell Dai）· 离职声明梳理")
    d = ImageDraw.Draw(img)
    # 档案卡
    d.rounded_rectangle([PAD, top, W - PAD, top + card_h], radius=16, fill=CARD)
    d.rounded_rectangle([PAD, top, PAD + 5, top + card_h], radius=2, fill=ACCENT)
    d.text((PAD + 24, top + 18), "戴士钦  Darrell Dai", font=font(34), fill=INK)
    d.rounded_rectangle([W - PAD - 178, top + 22, W - PAD - 18, top + 62],
                        radius=18, fill=ACCENT_SOFT)
    d.text((W - PAD - 166, top + 28), "告别帖 13 万+ 赞", font=font(21), fill=ACCENT)
    for i, (k, v) in enumerate(PROFILE):
        y = top + 76 + i * 40
        d.text((PAD + 24, y), k, font=font(22), fill=ACCENT)
        d.text((PAD + 96, y), v, font=font(22, False), fill=INK)
    # 三条理由
    y = top + card_h + gap
    for i, (head, desc) in enumerate(REASONS):
        d.rounded_rectangle([PAD, y, W - PAD, y + reason_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + reason_h], radius=2, fill=ACCENT)
        d.ellipse([PAD + 22, y + 20, PAD + 58, y + 56], fill=ACCENT_SOFT)
        d.text((PAD + 31, y + 24), str(i + 1), font=font(24), fill=ACCENT)
        d.text((PAD + 72, y + 20), head, font=font(28), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(22, False), W - 2 * PAD - 100)[:3]):
            d.text((PAD + 72, y + 60 + j * 29), ln, font=font(22, False), fill=SUB)
        y += reason_h + 14
    footer(img, "领英资料、新浪财经、界面新闻、第一财经、腾讯科技 11 月报道")
    return save(img, "fig02_profile.png")


# ---------------------------------------------------------------- fig03 为什么走
CUDA = [
    ("2006", "CUDA 内置进所有 GPU", "显卡只是游戏配件，GPU 做科学计算看不到回报"),
    ("2008", "金融危机股价重挫", "研发预算仍未被削减"),
    ("2012", "AlexNet 引爆深度学习", "六年蛰伏成为难以迁移的护城河"),
    ("2026", "具身智能 / 物理 AI", "同样的长周期、同样的爬坡阶段"),
]
WHY = [
    ("押注的人", "最怕的不是换 CEO，而是换掉愿意为十年后下注的人。"),
    ("被听见的机制", "Top 5 邮件与直接交流，把数万人的公司压缩成可感知的关系。"),
    ("解释的缺位", "信息或许暂不能公开，但「被排除在外」的感受是真实的。"),
]


def fig03() -> Path:
    top = HEADER_H + 28
    cuda_h = 4 * 74 + 46
    gap = 20
    why_h = 132
    height = top + cuda_h + gap + len(WHY) * (why_h + 14) + 20 + FOOTER_H
    img = canvas(height, "03", "他怕的不是换了个 CEO", "CUDA 的六年，与具身智能的当下")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([PAD, top, W - PAD, top + cuda_h], radius=16, fill=CARD)
    d.text((PAD + 22, top + 14), "一条被反复提起的技术押注线", font=font(26), fill=INK)
    for i, (year, head, desc) in enumerate(CUDA):
        y = top + 52 + i * 74
        d.rounded_rectangle([PAD + 20, y, PAD + 116, y + 38], radius=10, fill=ACCENT_SOFT)
        d.text((PAD + 30, y + 5), year, font=font(23), fill=ACCENT)
        d.text((PAD + 132, y + 2), head, font=font(24), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 160)[:1]):
            d.text((PAD + 132, y + 34 + j * 26), ln, font=font(21, False), fill=SUB)
    y = top + cuda_h + gap
    for head, desc in WHY:
        d.rounded_rectangle([PAD, y, W - PAD, y + why_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + why_h], radius=2, fill=WARM)
        d.text((PAD + 24, y + 16), head, font=font(28), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(22, False), W - 2 * PAD - 52)[:2]):
            d.text((PAD + 24, y + 58 + j * 29), ln, font=font(22, False), fill=SUB)
        y += why_h + 14
    footer(img, "公开报道与行业评论整理；长周期类比属业界分析，非公司官方表态")
    return save(img, "fig03_reasons.png")


# ---------------------------------------------------------------- fig04 留人动作
ACTIONS = [
    ("财报电话会", "美银证券分析师追问如何留人；克雷斯称稳定队伍是当前最重要的工作。"),
    ("卸任后首场全员大会", "宣布 12 月提前加薪并发放额外股权激励，打破春季调薪惯例。"),
    ("薪酬结构", "股票占比较高，股价波动直接传导到员工实际收益。"),
]
RATE = [("英伟达近年全球员工流失率", "2.5%—3.7%", 3.7),
        ("科技行业常态水平", "15%—20%", 20.0)]


def fig04() -> Path:
    top = HEADER_H + 28
    act_h = 118
    rate_h = 236
    quote_h = 118
    height = top + len(ACTIONS) * (act_h + 12) + 20 + rate_h + 20 + quote_h + 20 + FOOTER_H
    img = canvas(height, "04", "公司怎么留人", "提前加薪、额外股权，与一副「金手铐」")
    d = ImageDraw.Draw(img)
    y = top
    for head, desc in ACTIONS:
        d.rounded_rectangle([PAD, y, W - PAD, y + act_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + act_h], radius=2, fill=ACCENT)
        d.text((PAD + 22, y + 12), head, font=font(26), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(22, False), W - 2 * PAD - 48)[:2]):
            d.text((PAD + 22, y + 52 + j * 28), ln, font=font(22, False), fill=SUB)
        y += act_h + 12
    y += 8
    d.rounded_rectangle([PAD, y, W - PAD, y + rate_h], radius=16, fill=CARD)
    d.text((PAD + 22, y + 14), "流失率对照（多家媒体援引，口径不一）", font=font(24), fill=INK)
    x0, x1 = PAD + 22, W - PAD - 178
    span = x1 - x0
    for i, (label, val, num) in enumerate(RATE):
        yy = y + 62 + i * 82
        d.text((PAD + 22, yy), label, font=font(22, False), fill=SUB)
        d.rounded_rectangle([x0, yy + 30, x1, yy + 58], radius=14, fill=(229, 238, 236))
        d.rounded_rectangle([x0, yy + 30, x0 + int(span * num / 20.0), yy + 58],
                            radius=14, fill=ACCENT if i == 0 else WARM)
        d.text((W - PAD - 14, yy + 28), val, font=font(26), fill=ACCENT if i == 0 else WARM,
               anchor="ra")
    y += rate_h + 20
    d.rounded_rectangle([PAD, y, W - PAD, y + quote_h], radius=14, fill=ACCENT_SOFT)
    q = "有评论概括得很锋利：股权解决的是「走不走得起」，解决不了「还想不想留」。"
    for j, ln in enumerate(wrap(d, q, font(23), W - 2 * PAD - 50)[:2]):
        d.text((PAD + 24, y + 22 + j * 34), ln, font=font(23), fill=INK)
    footer(img, "第一财经、界面新闻、腾讯科技及行业分析文章（流失率数字为媒体援引）")
    return save(img, "fig04_retention.png")


# ---------------------------------------------------------------- fig05 资本账本
MARKET = [
    ("收盘涨幅", "+31.48%", "报 165.05 美元"),
    ("单日市值增加", "约 9520 亿美元", "2003 年以来最大单日涨幅"),
    ("总市值", "约 3.98 万亿美元", "此前七个交易日累计下跌 27.7%"),
    ("卖空者浮亏", "约 122 亿美元", "ORTEX 按盘中高点 168.5 美元测算"),
]
VIEWS = [
    ("伯恩斯坦", "上调至「跑赢大盘」", "此前下调的主因正是黄仁勋离职", ACCENT),
    ("摩根大通", "维持信用债低配", "调查、出口管制、融资承诺等风险未出清", WARM),
    ("穆迪", "展望正面 → 稳定", "未来 12—18 个月不太可能上调", WARM),
]


def fig05() -> Path:
    top = HEADER_H + 28
    card_h = 92
    view_h = 106
    height = top + len(MARKET) * (card_h + 12) + 20 + len(VIEWS) * (view_h + 12) + 20 + FOOTER_H
    img = canvas(height, "05", "回归那天的三张脸色", "股价、空头与券商并不一致的判断")
    d = ImageDraw.Draw(img)
    y = top
    for label, val, note in MARKET:
        d.rounded_rectangle([PAD, y, W - PAD, y + card_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + card_h], radius=2, fill=ACCENT)
        d.text((PAD + 22, y + 12), label, font=font(22), fill=SUB)
        d.text((PAD + 22, y + 42), val, font=font(32), fill=INK)
        d.text((W - PAD - 18, y + 50), note, font=font(20, False), fill=SUB, anchor="ra")
        y += card_h + 12
    y += 8
    for name, verdict, reason, tone in VIEWS:
        d.rounded_rectangle([PAD, y, W - PAD, y + view_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + view_h], radius=2, fill=tone)
        d.text((PAD + 22, y + 12), name, font=font(26), fill=INK)
        d.text((PAD + 190, y + 14), verdict, font=font(24), fill=tone)
        for j, ln in enumerate(wrap(d, reason, font(21, False), W - 2 * PAD - 48)[:1]):
            d.text((PAD + 22, y + 58 + j * 26), ln, font=font(21, False), fill=SUB)
        y += view_h + 12
    footer(img, "21 世纪经济报道、界面新闻、中新经纬、ORTEX 数据（行情数据随市场变动）")
    return save(img, "fig05_market.png")


# ---------------------------------------------------------------- fig06 事实边界与点评
BOUNDARY = [
    "未公开披露有多少员工因黄仁勋离职而主动辞职",
    "未公布针对这部分员工的返聘安排",
    "无公开信息显示戴士钦已获得返聘邀请",
]
TAKES = [
    ("薪酬与股权", "能留住工位，留不住归属感"),
    ("财报与基本面", "能证明业务没坏，证明不了愿景可信度没受损"),
    ("治理制度化", "首席合规官与独立调查继续，是这 11 天留下的监督节点"),
]


def fig06() -> Path:
    top = HEADER_H + 28
    b_h = 190
    t_h = 118
    height = top + b_h + 20 + len(TAKES) * (t_h + 14) + 20 + 112 + 20 + FOOTER_H
    img = canvas(height, "06", "还没递出的返聘邀请", "把「创始人依赖」摆上台面")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([PAD, top, W - PAD, top + b_h], radius=16, fill=(255, 249, 240))
    d.rounded_rectangle([PAD, top, PAD + 5, top + b_h], radius=2, fill=WARM)
    d.text((PAD + 22, top + 14), "截至 11 月 24 日，三条冷静的事实边界", font=font(25), fill=INK)
    for i, line in enumerate(BOUNDARY):
        y = top + 56 + i * 42
        d.ellipse([PAD + 24, y + 8, PAD + 34, y + 18], fill=WARM)
        d.text((PAD + 46, y), line, font=font(22, False), fill=INK)
    y = top + b_h + 20
    for head, desc in TAKES:
        d.rounded_rectangle([PAD, y, W - PAD, y + t_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + t_h], radius=2, fill=ACCENT)
        d.text((PAD + 22, y + 14), head, font=font(26), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(22, False), W - 2 * PAD - 48)[:2]):
            d.text((PAD + 22, y + 54 + j * 28), ln, font=font(22, False), fill=SUB)
        y += t_h + 14
    d.rounded_rectangle([PAD, y + 4, W - PAD, y + 108], radius=14, fill=ACCENT_SOFT)
    q = "真正的考验不是「他还在不在」，而是当某天他不在时，允许工程师冒险、允许暂时算不过账的机制，还能否自己运转。"
    for j, ln in enumerate(wrap(d, q, font(23), W - 2 * PAD - 50)[:3]):
        d.text((PAD + 24, y + 18 + j * 30), ln, font=font(23), fill=INK)
    footer(img, "第一财经、英伟达 11 月 23 日复职公告及公开报道；点评为业界观察整理")
    return save(img, "fig06_analysis.png")


def main() -> None:
    made = [fig00(), hero_clean(), fig01(), fig02(), fig03(), fig04(), fig05(), fig06()]
    for p in made:
        print(f"  {p.name}  {Image.open(p).size}")


if __name__ == "__main__":
    main()
