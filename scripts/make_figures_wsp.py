"""为《不训练、不反向传播：MIT 让 GPT-6 Astra 直接改写 Qwen 权重》绘信息图。

全部自制：权重矩阵网格、层级 MLP 示意、前后对照条形、实验规模阶梯等
都是与「权重空间编程 / GPT-6 Astra 改 Qwen 权重」主题强一致的技术图表，
不使用任何外部新闻图或企业标识，规避版权、防盗链与张冠李戴风险。

手机优先：750px 画布、正文最小 21px 字号，375px 宽手机上仍有 10~13px 可读。
fig00 例外：1080×460 宽幅主视觉（正文头图）；hero_clean 为无文字封面底图。

用法：python -m scripts.make_figures_wsp
产物：articles/figures4/*.png
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "articles" / "figures4"
OUT.mkdir(parents=True, exist_ok=True)

W = 750
HEADER_H = 148
FOOTER_H = 62
PAD = 36

ACCENT = (72, 102, 236)        # 靛蓝：主强调
ACCENT_L = (140, 164, 255)
ACCENT_SOFT = (233, 238, 255)
CYAN = (0, 178, 176)           # 电光青：数据/正向
CYAN_L = (86, 220, 214)
CYAN_SOFT = (226, 249, 247)
DARK_TOP = (14, 18, 46)
DARK_BOT = (34, 40, 96)
BG = (244, 246, 252)
CARD = (255, 255, 255)
INK = (24, 28, 48)
SUB = (94, 102, 128)
LINE = (216, 222, 240)
WARM = (222, 128, 40)          # 橙：质疑 / 边界
WARM_SOFT = (255, 247, 235)
GREY_BAR = (222, 228, 240)
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
    d.rounded_rectangle([PAD, 25, PAD + 4, 60], radius=2, fill=CYAN_L)
    d.text((PAD + 15, 22), f"图 {no}", font=font(20), fill=CYAN_L)
    d.text((PAD + 15, 48), "业界观察 · 权重空间编程", font=font(17, False), fill=(170, 184, 240))
    d.text((PAD, 76), title, font=font(36), fill=WHITE)
    d.text((PAD, 124), sub, font=font(18, False), fill=(186, 196, 240))
    return img


def footer(img: Image.Image, source: str) -> None:
    d = ImageDraw.Draw(img)
    h = img.height
    d.line([(PAD, h - FOOTER_H + 12), (W - PAD, h - FOOTER_H + 12)], fill=LINE, width=1)
    lines = wrap(d, f"来源：{source}", font(16, False), W - 2 * PAD - 190)
    for j, ln in enumerate(lines[:2]):
        d.text((PAD, h - FOOTER_H + 20 + j * 20), ln, font=font(16, False), fill=SUB)
    d.text((W - PAD, h - FOOTER_H + 24), "制图：业界观察", font=font(16, False),
           fill=SUB, anchor="ra")


# 词元化断行：连续的 ASCII 字母/数字/型号（Python、51.0%、Qwen3.5-0.8B、
# Abliteration、131,072）视为一个不可拆词元，中文逐字可断。避免把英文单词和
# 带小数点/百分号的数字劈成两行（本项目信息图上一版的目检事故）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,%+\-_:/']*|.", re.S)
# 禁则标点：不能落在行首，超宽时吸附到上一行行尾（与 cover._hang_punctuation 同款）
_HANG_PUNCT = set("，。、；：？！）」』】〉》%.,;:!?）」』】〉》〉.,;·")


def wrap(d: ImageDraw.ImageDraw, text: str, fnt, maxw: int) -> list[str]:
    lines, cur = [], ""
    for para in text.split("\n"):
        for tk in _TOKEN_RE.findall(para):
            if cur and d.textlength(cur + tk, font=fnt) > maxw:
                if tk in _HANG_PUNCT:
                    # 禁则标点强制跟随当前行（该行允许略微超宽），绝不成孤行；
                    # 注意不能只吸附到 lines[-1]——首行还没提交时同样会发生。
                    cur += tk
                    continue
                lines.append(cur)
                cur = tk
            else:
                cur += tk
        if cur:
            lines.append(cur)
            cur = ""
    return lines


def save(img: Image.Image, name: str) -> Path:
    out = OUT / name
    img.save(out, format="PNG", optimize=True)
    return out


def weight_grid(d, x0, y0, cols, rows, cell, gap, hi_cells: set[tuple[int, int]],
                base=(46, 56, 118), hi=CYAN_L, seed=0) -> None:
    """画一片权重矩阵网格，少数格子高亮（象征被改动的 0.016% 权重）。"""
    import random
    rng = random.Random(seed or 20261128)
    for r in range(rows):
        for c in range(cols):
            x = x0 + c * (cell + gap)
            y = y0 + r * (cell + gap)
            if (r, c) in hi_cells:
                fill = hi
            else:
                v = rng.randint(-12, 16)
                fill = tuple(max(0, min(255, base[i] + v)) for i in range(3))
            d.rounded_rectangle([x, y, x + cell, y + cell], radius=2, fill=fill)


# ---------------------------------------------------------------- fig00 主视觉
def fig00() -> Path:
    w, h = 1080, 460
    img = Image.new("RGB", (w, h), DARK_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp((11, 15, 40), (24, 30, 78), y / h))
    # 右侧：权重矩阵网格 + 极少数高亮格（131,072 / 8 亿 ≈ 0.016%）
    hi = {(3, 6), (3, 7), (6, 2), (6, 3), (8, 11), (2, 13)}
    weight_grid(d, 618, 66, 15, 11, 22, 7, hi, base=(40, 50, 110), hi=CYAN_L, seed=7)
    d.line([(604, 60), (604, 400)], fill=(70, 84, 150), width=2)
    d.text((618, 386), "WEIGHT MATRIX  ·  Qwen3.5-0.8B", font=font(17, False),
           fill=(150, 164, 226))
    # 左侧：标题区
    d.rounded_rectangle([56, 42, 60, 84], radius=2, fill=CYAN_L)
    d.text((72, 40), "业界观察 · AI 自进化前沿", font=font(22), fill=CYAN_L)
    d.text((56, 104), "权重空间编程", font=font(76), fill=WHITE)
    d.text((58, 200), "不训练 · 不梯度 · 直接改写权重", font=font(28), fill=ACCENT_L)
    d.text((56, 262), "GPT-6 Astra  →  Qwen3.5-0.8B", font=font(30), fill=WHITE)
    d.text((56, 316), "0.016% 的权重手术：131,072 / 800,000,000", font=font(23, False),
           fill=(186, 198, 240))
    d.text((56, 356), "MATH-500  7.9%  →  48.1%", font=font(25), fill=CYAN_L)
    return save(img, "fig00_main.png")


# ---------------------------------------------------------------- 封面专用 hero
def hero_clean() -> Path:
    """无文字的权重矩阵+渐变底图：给 make_cover 当 hero，标题由 make_cover 自己画。

    左侧保持干净深色留白（封面标题叠在这里），右侧只有矩阵纹理，
    不放任何文字，避免像上次那样从标题下方透出「残影」。
    """
    w, h = 1080, 460
    img = Image.new("RGB", (w, h), DARK_TOP)
    d = ImageDraw.Draw(img)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=_lerp((10, 14, 38), (22, 28, 72), y / h))
    hi = {(2, 5), (5, 1), (5, 2), (7, 9), (9, 12), (1, 11)}
    weight_grid(d, 640, 52, 15, 11, 23, 8, hi, base=(38, 48, 106), hi=(70, 200, 196), seed=11)
    # 稀疏走线，暗示「权重被读取 / 翻译」
    for i in range(5):
        yy = 88 + i * 72
        x = 596 + (i % 3) * 16
        d.line([(x, yy), (632, yy)], fill=(56, 72, 140), width=2)
        d.ellipse([x - 4, yy - 4, x + 4, yy + 4], fill=(86, 116, 200))
    return save(img, "hero_clean.png")


# ---------------------------------------------------------------- fig01 时间轴
TIMELINE = [
    ("11.02", "博客发布", "MIT CSAIL 四人发布 WSP 报告，附公开日志、改后权重与 HF Space。"),
    ("11.02", "原帖引爆", "Takehi 的 X 帖 14 小时浏览超 550 万，Hacker News 多帖热议。"),
    ("11.03", "中文刷屏", "机器之心编译全文，腾讯／新浪／凤凰／iThome 等多源跟进。"),
    ("11.03—04", "质疑与复现", "Honnibal 指两类实验混排；Willison 用本地模型复现权重读取。"),
    ("11.04", "复测数字", "HF 的 Ahsen Khaliq 按日志重建权重，MATH-500 复跑得 51.0%。"),
    ("11.07", "争议综述", "InfoQ 汇总 Beyer 等质疑，「被吹成神」与「泼冷水」并存。"),
]


def fig01() -> Path:
    top = HEADER_H + 28
    row_h = 138
    height = top + len(TIMELINE) * row_h + 20 + FOOTER_H
    img = canvas(height, "01", "一张博客，48 小时刷屏与反转", "从发布到质疑、复现的时间线")
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
        d.text((box[0] + 18, y + 8), tag, font=font(25), fill=ACCENT)
        d.text((box[0] + 178, y + 10), head, font=font(26), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(21, False), box[2] - box[0] - 40)[:2]):
            d.text((box[0] + 18, y + 52 + j * 29), ln, font=font(21, False), fill=SUB)
    footer(img, "MIT CSAIL 研究博客、机器之心、腾讯新闻、凤凰网科技、InfoQ、iThome 等 11 月公开报道")
    return save(img, "fig01_timeline.png")


# ---------------------------------------------------------------- fig02 机制
ABILITIES = [
    ("读", "从权重认出网络学过什么、藏了什么缺陷",
     "实验 1—3、7：认游戏、认扩散形状、认训练语言与复制头；判断谄媚型模型。"),
    ("修", "定位并改掉一个具体的失败模式",
     "实验 4、8：修复卡在坡上的小车策略；剪掉 Qwen 的推理循环方向。"),
    ("写", "从零构造一组能用的权重",
     "实验 5、6：零样本手写 MinAtar 游戏网络与 150 万参数围棋网络。"),
]


def fig02() -> Path:
    top = HEADER_H + 28
    cmp_h = 214
    gap = 22
    cap_h = 152
    height = top + cmp_h + gap + len(ABILITIES) * (cap_h + 14) + 20 + FOOTER_H
    img = canvas(height, "02", "把权重当代码，而不是训练结果", "权重空间编程的三类能力与路径差异")
    d = ImageDraw.Draw(img)
    # 路径对比
    d.rounded_rectangle([PAD, top, W - PAD, top + cmp_h], radius=16, fill=CARD)
    d.text((PAD + 22, top + 14), "同一个目标：让开源模型表现更好", font=font(25), fill=INK)
    mid = PAD + (W - 2 * PAD) // 2
    d.line([(mid, top + 56), (mid, top + cmp_h - 18)], fill=LINE, width=2)
    left = [("常规路径", "提示词 / 微调 / 蒸馏 / LoRA"),
            ("谁在改权重", "Adam、Muon 等优化器"),
            ("依据", "梯度下降与损失函数")]
    right = [("权重空间编程", "通用 LLM 直接读写权重"),
             ("谁在改权重", "大语言模型的推理"),
             ("依据", "自然语言目标，不经优化器")]
    d.text((PAD + 22, top + 60), left[0][0], font=font(23), fill=SUB)
    d.text((PAD + 22, top + 94), left[0][1], font=font(21, False), fill=INK)
    d.text((PAD + 22, top + 132), left[1][0], font=font(21), fill=SUB)
    d.text((PAD + 22, top + 162), left[1][1], font=font(21, False), fill=INK)
    d.text((mid + 20, top + 60), right[0][0], font=font(23), fill=ACCENT)
    for j, ln in enumerate(wrap(d, right[0][1], font(21, False), W - PAD - mid - 42)[:2]):
        d.text((mid + 20, top + 92 + j * 27), ln, font=font(21, False), fill=INK)
    d.text((mid + 20, top + 146), "不经优化器 · 不用梯度", font=font(21, False), fill=ACCENT)
    # 三种能力
    y = top + cmp_h + gap
    for i, (tag, head, desc) in enumerate(ABILITIES):
        d.rounded_rectangle([PAD, y, W - PAD, y + cap_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + cap_h], radius=2, fill=CYAN)
        d.rounded_rectangle([PAD + 20, y + 18, PAD + 78, y + 76], radius=12, fill=CYAN_SOFT)
        d.text((PAD + 34, y + 26), tag, font=font(38), fill=CYAN)
        d.text((PAD + 96, y + 18), head, font=font(25), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 116)[:2]):
            d.text((PAD + 96, y + 60 + j * 29), ln, font=font(21, False), fill=SUB)
        y += cap_h + 14
    footer(img, "MIT CSAIL《Sparks of Weight-Space Programming》博客全文（机器之心编译）")
    return save(img, "fig02_wsp.png")


# ---------------------------------------------------------------- fig03 实验谱系
SCALE = [
    ("1.7 万 ~ 17.2 万", "实验 1 · 2 · 4 · 5", "微型网络 · 权重直接进上下文",
     "认游戏、认扩散图形、修小车策略、手写 MinAtar 网络；不给工具、不跑网络。", ACCENT),
    ("720 万", "实验 3", "字符级小语言模型 · 仅 Python 分析",
     "去首尾层只看中间 4 个 Transformer 块，认出三种训练语言、找全复制头。", CYAN),
    ("2.7 亿 / 150 万", "实验 6 · 7", "Gemma 谄媚检测 · 围棋沙箱",
     "谄媚检测四次失败；围棋网络超出上下文，须借 Python 沙箱与前向预算。", WARM),
    ("8 亿", "实验 8（头条）", "Qwen3.5-0.8B · Notebook 沙箱",
     "诊断推理循环并定向消融；最终只改动 131,072 个权重，占 0.016%。", ACCENT),
]


def fig03() -> Path:
    top = HEADER_H + 28
    card_h = 158
    height = top + len(SCALE) * (card_h + 14) + 20 + 118 + FOOTER_H
    img = canvas(height, "03", "从 1.7 万到 8 亿：实验规模谱系", "工具条件随规模一起变，这是争议的起点")
    d = ImageDraw.Draw(img)
    y = top
    for i, (scale, exp, cond, desc, tone) in enumerate(SCALE):
        d.rounded_rectangle([PAD, y, W - PAD, y + card_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + card_h], radius=2, fill=tone)
        d.rounded_rectangle([PAD + 20, y + 16, PAD + 240, y + 56], radius=10, fill=ACCENT_SOFT)
        d.text((PAD + 30, y + 21), scale, font=font(24), fill=tone)
        d.text((PAD + 256, y + 22), exp, font=font(24), fill=INK)
        d.text((PAD + 22, y + 68), cond, font=font(22), fill=ACCENT)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 44)[:2]):
            d.text((PAD + 22, y + 100 + j * 28), ln, font=font(21, False), fill=SUB)
        y += card_h + 14
    d.rounded_rectangle([PAD, y + 2, W - PAD, y + 110], radius=14, fill=WARM_SOFT)
    q = "失败案例同样公开：实验 7 四次都没能分辨谄媚型 Gemma；补充实验里 Astra 也未能提高 Gemma 的数学成绩。"
    for j, ln in enumerate(wrap(d, q, font(21, False), W - 2 * PAD - 48)[:3]):
        d.text((PAD + 22, y + 16 + j * 29), ln, font=font(21, False), fill=INK)
    footer(img, "MIT CSAIL 博客实验 1—8 与公开实验日志；参数量按原文与媒体整理，口径见正文事实边界")
    return save(img, "fig03_experiments.png")


# ---------------------------------------------------------------- fig04 手术四步
STEPS = [
    ("01", "诊断", "96 次测试里 85 次撞上 32,768 Token 上限——模型陷在推理循环里，答案写不完。"),
    ("02", "定位", "把循环行为追溯到第 15 层与第 19 层 MLP 写入的一个特定「重复方向」。"),
    ("03", "消融", "从这两层各自的 64 个输出列中减去该方向（特征消融 / Abliteration）。"),
    ("04", "落盘", "共改动 131,072 个权重，占 8 亿参数的 0.016%；其余部分原样保留。"),
]


def fig04() -> Path:
    top = HEADER_H + 28
    step_h = 146
    height = top + len(STEPS) * (step_h + 12) + 20 + 118 + FOOTER_H
    img = canvas(height, "04", "0.016% 的手术，分四步完成", "实验 8：Qwen3.5-0.8B 权重定向消融流程")
    d = ImageDraw.Draw(img)
    y = top
    for i, (no, head, desc) in enumerate(STEPS):
        d.rounded_rectangle([PAD, y, W - PAD, y + step_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + step_h], radius=2, fill=ACCENT)
        d.rounded_rectangle([PAD + 20, y + 20, PAD + 88, y + 78], radius=12, fill=ACCENT_SOFT)
        d.text((PAD + 34, y + 27), no, font=font(30), fill=ACCENT)
        d.text((PAD + 108, y + 20), head, font=font(29), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 128)[:2]):
            d.text((PAD + 108, y + 64 + j * 29), ln, font=font(21, False), fill=SUB)
        if i < len(STEPS) - 1:
            d.text((W // 2 - 12, y + step_h - 2), "↓", font=font(22), fill=ACCENT_L)
        y += step_h + 12
    d.rounded_rectangle([PAD, y + 2, W - PAD, y + 110], radius=14, fill=CYAN_SOFT)
    q = "沙箱：Notebook 式 Python 环境，权重已加载；GSM8K / MATH 练习题，10 / 25 / 50 次前向预算；无梯度，评测集不开放。"
    for j, ln in enumerate(wrap(d, q, font(21, False), W - 2 * PAD - 48)[:3]):
        d.text((PAD + 22, y + 14 + j * 29), ln, font=font(21, False), fill=INK)
    footer(img, "MIT CSAIL 博客实验 8、公开实验日志；InfoQ / AI 前线对特征消融的复盘")
    return save(img, "fig04_surgery.png")


# ---------------------------------------------------------------- fig05 数据对照
NUMBERS = [
    ("MATH-500", 7.9, 48.1, "7.9%", "48.1%", ACCENT),
    ("GSM8K", 13.8, 67.1, "13.8%", "67.1%", ACCENT),
    ("IFEval 指令遵循", 21.0, 62.3, "21.0%", "62.3%", ACCENT),
    ("触及 Token 上限（越低越好）", 93.0, 33.0, "93%", "33%", WARM),
]


def fig05() -> Path:
    top = HEADER_H + 28
    row_h = 132
    gemma_h = 150
    height = top + len(NUMBERS) * (row_h + 12) + 18 + gemma_h + 20 + FOOTER_H
    img = canvas(height, "05", "四项指标前后对照", "50 次前向预算下的结果，与一组关键反证")
    d = ImageDraw.Draw(img)
    y = top
    x0, x1 = PAD + 22, W - PAD - 152
    span = x1 - x0
    for label, before, after, bs, as_, tone in NUMBERS:
        d.rounded_rectangle([PAD, y, W - PAD, y + row_h], radius=14, fill=CARD)
        d.text((PAD + 22, y + 12), label, font=font(23), fill=INK)
        for k, (val, txt, col, fillc) in enumerate([
                (before, bs, SUB, GREY_BAR), (after, as_, tone, tone)]):
            yy = y + 48 + k * 34
            d.text((PAD + 22, yy - 4), "原" if k == 0 else "改", font=font(20), fill=col)
            d.rounded_rectangle([x0 + 34, yy, x1, yy + 24], radius=12, fill=(236, 240, 250))
            bw = max(12, int(span * val / 100.0) - 34)
            d.rounded_rectangle([x0 + 34, yy, x0 + 34 + bw, yy + 24], radius=12, fill=fillc)
            d.text((W - PAD - 16, yy - 2), txt, font=font(25), fill=col, anchor="ra")
        y += row_h + 12
    y += 6
    d.rounded_rectangle([PAD, y, W - PAD, y + gemma_h], radius=16, fill=WARM_SOFT)
    d.rounded_rectangle([PAD, y, PAD + 5, y + gemma_h], radius=2, fill=WARM)
    d.text((PAD + 22, y + 14), "反证：Gemma 3 270M 没能被「教会」数学", font=font(25), fill=INK)
    txt = "它仅 8.5% 的答案触顶，错题多是普通算术与代数失误；Astra 反复修改，MATH-500 提升不超过约 1 个百分点。多答对的 579 题里有 576 题原本是超时截断得零分。"
    for j, ln in enumerate(wrap(d, txt, font(21, False), W - 2 * PAD - 46)[:3]):
        d.text((PAD + 22, y + 54 + j * 29), ln, font=font(21, False), fill=INK)
    footer(img, "MIT CSAIL 博客实验 8 与补充实验；InfoQ / AI 前线、52nlp、iThome 复测数字")
    return save(img, "fig05_numbers.png")


# ---------------------------------------------------------------- fig06 争议
CRITIQUE = [
    ("自主性被夸大", "Beyer：非常酷，但吹得太过了", "系统提示词已要求排查「死循环截断」，并禁止用提前终止思考投机。", WARM),
    ("两类实验混排", "Honnibal：证明的是不同能力", "无工具裸读只在实验 1/2/4（1.7 万—17.2 万权重）；Qwen 用了 Python 与最多 50 批练习题。", WARM),
    ("规模与规则边界", "2 万 / 148 万 vs 8 亿", "Qwen 约为围棋网络的 540 倍；规则确定的小任务可行，语义不确定任务易崩塌。", WARM),
]
REPRO = [
    "Willison：本地 Qwen3.8 27B 无工具，四款游戏认对三款",
    "Kimi K3.1 Pro 认出 Breakout 仅用 9 分 41 秒；HF 重建权重复跑 MATH-500 得 51.0%",
]


def fig06() -> Path:
    top = HEADER_H + 28
    card_h = 150
    repro_h = 168
    height = top + len(CRITIQUE) * (card_h + 14) + 18 + repro_h + 20 + 92 + FOOTER_H
    img = canvas(height, "06", "争议三层，与一次独立复现", "没人质疑数字，倒下的是叙事")
    d = ImageDraw.Draw(img)
    y = top
    for head, verdict, desc, tone in CRITIQUE:
        d.rounded_rectangle([PAD, y, W - PAD, y + card_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + card_h], radius=2, fill=tone)
        d.text((PAD + 22, y + 14), head, font=font(26), fill=INK)
        d.text((PAD + 22, y + 52), verdict, font=font(22), fill=tone)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 46)[:2]):
            d.text((PAD + 22, y + 88 + j * 28), ln, font=font(21, False), fill=SUB)
        y += card_h + 14
    y += 4
    d.rounded_rectangle([PAD, y, W - PAD, y + repro_h], radius=16, fill=CYAN_SOFT)
    d.rounded_rectangle([PAD, y, PAD + 5, y + repro_h], radius=2, fill=CYAN)
    d.text((PAD + 22, y + 12), "另一半证据：核心声明站住了", font=font(25), fill=INK)
    for i, line in enumerate(REPRO):
        yy = y + 52 + i * 52
        d.ellipse([PAD + 24, yy + 8, PAD + 34, yy + 18], fill=CYAN)
        for j, ln in enumerate(wrap(d, line, font(21, False), W - 2 * PAD - 66)[:2]):
            d.text((PAD + 46, yy + j * 27), ln, font=font(21, False), fill=INK)
    y += repro_h + 20
    d.rounded_rectangle([PAD, y, W - PAD, y + 84], radius=14, fill=ACCENT_SOFT)
    q = "Willison 给复现划了边界：单人、单次、非正式——「我还不会宣布源代码时代结束了」。"
    for j, ln in enumerate(wrap(d, q, font(22), W - 2 * PAD - 48)[:2]):
        d.text((PAD + 22, y + 16 + j * 30), ln, font=font(22), fill=INK)
    footer(img, "InfoQ / AI 前线、Hacker News 讨论、Simon Willison 博客、智源社区、iThome、DataCamp")
    return save(img, "fig06_debate.png")


# ---------------------------------------------------------------- fig07 产业影响
IMPACT = [
    ("模型后市场降本", "若可复制，大量「够用但不完美」的小模型将有一条绕过训练基础设施的修复通道。"),
    ("模型资产可诊断化", "行为可定位到「第 15 / 19 层的某个方向」，机制可解释性从论文走进工程现场。"),
    ("RSI 的实证信号", "迭代瓶颈若部分转向「模型自身的诊断能力」，scaling law 之外出现第二条曲线。"),
]
NEXT = ["不运行模型即可审查（含后门检测）", "降低优化成本 / 一次性生成初始权重",
        "不依赖数据增删能力，避免灾难性遗忘", "让递归自我改进走更直接的路线",
        "跨架构模型合并（移植功能电路）"]


def fig07() -> Path:
    top = HEADER_H + 28
    card_h = 128
    power_h = 182
    next_h = 226
    height = top + len(IMPACT) * (card_h + 14) + 18 + power_h + 18 + next_h + 20 + FOOTER_H
    img = canvas(height, "07", "谁有权改谁的模型", "三层产业影响、权力图景与团队下一步")
    d = ImageDraw.Draw(img)
    y = top
    for i, (head, desc) in enumerate(IMPACT):
        d.rounded_rectangle([PAD, y, W - PAD, y + card_h], radius=14, fill=CARD)
        d.rounded_rectangle([PAD, y, PAD + 5, y + card_h], radius=2, fill=ACCENT)
        d.text((PAD + 22, y + 12), f"0{i+1}  {head}", font=font(26), fill=INK)
        for j, ln in enumerate(wrap(d, desc, font(21, False), W - 2 * PAD - 46)[:2]):
            d.text((PAD + 22, y + 54 + j * 28), ln, font=font(21, False), fill=SUB)
        y += card_h + 14
    y += 4
    d.rounded_rectangle([PAD, y, W - PAD, y + power_h], radius=16, fill=WARM_SOFT)
    d.rounded_rectangle([PAD, y, PAD + 5, y + power_h], radius=2, fill=WARM)
    d.text((PAD + 22, y + 12), "权力图景：闭源旗舰改写开源模型", font=font(25), fill=INK)
    txt = "被改的是 Qwen3.5-0.8B，动手的是 OpenAI 闭源旗舰。也要说全：目前只能除障、不能造能；开源本就以让渡部分控制权换扩散，最大受益者可能恰是中小团队。审查对象应从「单个模型」扩展到「模型之间的关系」。"
    for j, ln in enumerate(wrap(d, txt, font(21, False), W - 2 * PAD - 46)[:4]):
        d.text((PAD + 22, y + 50 + j * 28), ln, font=font(21, False), fill=INK)
    y += power_h + 18
    d.rounded_rectangle([PAD, y, W - PAD, y + next_h], radius=16, fill=CARD)
    d.text((PAD + 22, y + 12), "团队列出的下一步（尚未实验验证）", font=font(25), fill=INK)
    for i, line in enumerate(NEXT):
        yy = y + 52 + i * 33
        d.ellipse([PAD + 24, yy + 8, PAD + 34, yy + 18], fill=CYAN)
        d.text((PAD + 46, yy), line, font=font(21, False), fill=INK)
    footer(img, "凤凰网科技、腾讯新闻、InfoQ 产业分析；MIT CSAIL 博客「下一步计划」与局限性说明")
    return save(img, "fig07_impact.png")


def main() -> None:
    made = [fig00(), hero_clean(), fig01(), fig02(), fig03(), fig04(), fig05(), fig06(), fig07()]
    for p in made:
        print(f"  {p.name}  {Image.open(p).size}")


if __name__ == "__main__":
    main()
