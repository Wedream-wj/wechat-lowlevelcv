"""封面渲染：1080×460（公众号大图 2.35:1），有素材图就走「图片 + 左侧压暗」版式，
没有就走品牌渐变底图。

关于尺寸：公众号首图规范是 2.35:1（推荐 900×383）。早先这里画的是 1200×675（16:9），
微信按 2.35:1 中心裁切会切掉上下 25%——标题正好在裁切线附近，于是「封面上的字
少半截」。改成本身就是 2.35:1，微信再怎么裁都不动内容。

关于图片：不直接拿原图当封面——论文插图多为白底、细线，缩到手机列表里几乎看不清；
统一加左侧深色渐变蒙版 + 右下角同色系压边，标题始终落在暗区上，白底图也压得住。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import settings, topics
from .cn_words import is_word, segment_cjk

# 2.35:1：与微信大图封面一致
W, H = 1080, 460
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


def _canvas_size() -> tuple[int, int]:
    """尺寸允许在 YAML 里改：不同账号的封面规范偶尔不一样。"""
    cfg = topics.raw.get("cover") or {}
    w = int(cfg.get("width") or W)
    h = int(cfg.get("height") or H)
    return (w if w > 400 else W, h if h > 200 else H)


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES["bold" if bold else "regular"]:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 折行
# 语义断行：ASCII 词整体原子（「Real-IISR」「PSNR」不劈开）；标点之后是天然断点；
# 中文串可断，但断点由动态规划均衡各行宽度并惩罚丑断点——
# 实测旧贪心逐字断把「真实红外超分」切成「…真实红 / 外超分」，读者一眼出戏。
_CJK_MIN, _CJK_MAX = 0x2E80, 0x9FFF        # CJK 部首扩展 ~ 基本区（含全角标点区）
# 这些字符之后断行零惩罚（空格是最自然的断点：英文词之间、数字与量词之间都靠它）
_GOOD_BREAK_AFTER = set("　 :：|｜。；，、？！）》」』”,")
# 数字串里的连接符：「1+16」「2×4」「16:9」是一个整体，断在中间等于把数字劈开
_NUM_JOINERS = set("+×÷*/%=:：~")
# 这些字符放在行首很难看（只向左吸附的虚词与标点）：断在它们前面要付大代价。
# 注意别把「把/被/在/从」这类介词放进来——它们向右管辖，做行首完全成立
# （「…步扩散 / 把超分推到 2K 以上」就是标准的中文标题断法）。
_BAD_LINE_START = set("的了吗呢吧啊嘛呀与和或及而就还都很更最、。，；：？！）》」』”…—·")
# 数字后面的量词/单位：断在它们之后，等于把「16 步扩散」的修饰关系切断
_MEASURE_WORDS = set("步倍张帧秒个种次层级维组项块条支台片路篇轮遍")
# 这些单字只向右管辖（介词/单字动词），留在行尾等于把一个短语吊在半空
_DANGLING_TAIL = set("用把在被从对给让使向于由以")
# 后置方位词只向左吸附（「2K 以上」「3 倍以内」），断在它们前面等于把量纲拆散
_POSTPOSITIONS = frozenset(("以上", "以下", "以内", "之间", "之后", "之前", "左右", "起"))
# 结构助词与体标记：只能跟在词后，绝不能出现在行首（中文排版的硬禁忌）
_SUFFIX_CHARS = set("的地得了着过")
# 常见技术词：被行尾/行首劈开时（「流模 | 型」「超 | 分」）要给重罚
_NO_BREAK_BIGRAMS = frozenset((
    "模型", "框架", "扩散", "超分", "图像", "视频", "网络", "训练", "推理", "数据",
    "算法", "特征", "重建", "生成", "损失", "精度", "效率", "参数", "结构", "模块",
    "采样", "噪声", "真实", "场景", "基准", "指标", "性能", "实验", "方法", "结果",
    "视觉", "底层", "质量", "变换", "编码", "解码", "注意", "计算", "退化", "增强",
))


def _is_cjk(ch: str) -> bool:
    return _CJK_MIN <= ord(ch) <= _CJK_MAX


def _fuse_numeric(units: list[str]) -> list[str]:
    """把「数字 + 运算符 + 数字」熔成一个原子：1 + 16 → 1+16。

    封面标题里「用 1+16 步扩散」被断成「用 1 / +16 步扩散」是最刺眼的一种断法，
    读者第一眼看到行首的「+16」会直接误会成「加 16」。这里在分词阶段就焊死。
    """
    out: list[str] = []
    i = 0
    while i < len(units):
        u = units[i]
        if (u in _NUM_JOINERS and out and out[-1][-1].isdigit()
                and i + 1 < len(units) and units[i + 1][:1].isdigit()):
            out[-1] = out[-1] + u + units[i + 1]
            i += 2
            continue
        out.append(u)
        i += 1
    return out


def _units(text: str) -> list[str]:
    """把标题切成不可劈开的原子单元。

    - 中文连续段先过词表分词（`cn_words.segment_cjk`）：「引导」「超分」这类词
      是单个原子，DP 再也切不到词中间；
    - ASCII 词（含 .-'_ 连字）整体原子，`Real-IISR` / `PSNR` 不会被劈开；
    - 标点、空格各自成原子，作为天然断点；
    - 最后把「数字 + 运算符 + 数字」熔成一个原子（1+16 / 2×4）。
    """
    out: list[str] = []
    cjk: list[str] = []

    def flush_cjk() -> None:
        if cjk:
            out.extend(segment_cjk("".join(cjk)))
            cjk.clear()

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _is_cjk(ch):
            cjk.append(ch)
            i += 1
            continue
        flush_cjk()
        if ch.isascii() and (ch.isalnum() or ch in "._-'"):
            j = i
            while j < n and text[j].isascii() and (text[j].isalnum() or text[j] in "._-'"):
                j += 1
            out.append(text[i:j])
            i = j
        else:
            out.append(ch)
            i += 1
    flush_cjk()
    return _fuse_numeric(out)


_HARD_BREAK = 1e9          # 硬禁断点：数字与其量词之间，任何折行方案都不许碰


def _prev_solid(units: list[str], i: int) -> int:
    """units[i] 往前第一个非空格原子的下标。"""
    j = i - 1
    while j >= 0 and units[j].isspace():
        j -= 1
    return j


def _break_penalty(units: list[str], j: int) -> float:
    """在 units[j-1] 与 units[j] 之间断行的代价（0 = 天然断点）。

    数值与「均衡度」同量级（均衡项已归一到千像素量级），语义惩罚才压得住
    「两行更整齐」的诱惑——否则「16 步 | 扩散」这种断法总能靠对齐赢下来。
    数字与量词的粘连用硬禁（_HARD_BREAK）：那不是审美问题，是语义错误。
    """
    if j <= 0:
        return 0.0
    last, nxt = units[j - 1], units[j]
    # units[j-1] 之前最近的实原子：判断「量词前面是不是数字」要用它
    prev_idx = _prev_solid(units, j - 1)
    prev_atom = units[prev_idx] if prev_idx >= 0 else ""
    # 硬规则一：数字与量词之间不许断——三种断法（断在空格前 / 断在空格后 /
    # 断在量词后）全部封死。「1+16 | 步扩散」读起来像「加 16」，是封面最忌的断法。
    if (nxt.isspace() and j + 1 < len(units) and units[j + 1] in _MEASURE_WORDS
            and last[-1:].isdigit()):
        return _HARD_BREAK
    if last.isspace() and nxt in _MEASURE_WORDS and prev_atom[-1:].isdigit():
        return _HARD_BREAK
    # 硬规则二：量词后面紧跟名词也不许断（「16 步 | 扩散」把修饰关系切断）
    if (len(last) == 1 and last in _MEASURE_WORDS and _is_cjk(nxt[0])
            and prev_atom[-1:].isdigit()):
        return _HARD_BREAK
    # 行首侧惩罚（与断点左侧是否标点无关，先算出来）
    head_pen = 0.0
    if nxt[0] in _BAD_LINE_START:
        head_pen += 120.0   # 行首是「的/了/与/…」这类只向左吸附的虚词
    if nxt[0] in _SUFFIX_CHARS:
        head_pen += 400.0   # 「的/地/得/了/着/过」跑到行首是排版禁忌
    if nxt[0] in _NUM_JOINERS:
        head_pen += 200.0   # 行首出现「+16」：读者会读成「加十六」
    if nxt in _POSTPOSITIONS:
        head_pen += 180.0   # 「推到 2K | 以上」：量纲被拆到下一行
    if last[-1] in _GOOD_BREAK_AFTER:
        # 空格/标点后断行天然自然，但行尾若只剩一个介词（「…用 ␣」）仍要罚——
        # 罚则该断点变成「标点后 0 分」的漏洞，DP 会专挑这种位置下刀。
        if last.isspace() and len(prev_atom) == 1 and prev_atom in _DANGLING_TAIL:
            return head_pen + 200.0
        return head_pen
    pen = head_pen
    if last[-1].isdigit() and _is_cjk(nxt[0]):
        pen += 45.0         # 数字直接接汉字（2K | 以上 之外的情况）也尽量不断
    if len(last) == 1 and last in _MEASURE_WORDS and _is_cjk(nxt[0]):
        pen += 60.0         # 「16 步 | 扩散」：量词被留在上一行，修饰关系断掉
    if len(last) == 1 and last in _DANGLING_TAIL:
        pen += 220.0        # 行尾只剩一个介词/单字动词（「…Real-IISR：用 | 自回归」）
    if (len(last) == 1 and _is_cjk(last) and not is_word(last)
            and _is_cjk(nxt[0])):
        pen += 80.0         # 未登录单字紧贴下一词 → 很可能是一个没进词表的词，别切
    if (_is_cjk(last[-1]) and _is_cjk(nxt[0])
            and (last[-1] + nxt[0]) in _NO_BREAK_BIGRAMS):
        pen += 80.0         # 「流模 | 型」「超 | 分」：常见技术词从中间劈开
    if _is_cjk(last[-1]) and _is_cjk(nxt[0]):
        pen += 12.0         # 无标点的中文-中文硬断：轻微惩罚，让 DP 优先找更好的点
    else:
        pen += 18.0         # 中英边界硬断（Real-IISR / 用 之类）
    return pen


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    """按像素宽度折行：ASCII 词不劈开、标点后优先断、中文断点走 DP 均衡。

    DP 目标 = Σ(每行剩余空白²) + 断点惩罚，行数 1..max_lines 里取最优；
    空白²天然杜绝「最后一个字孤零零两三个」的短尾行（旧贪心的典型翻车点）。
    全都装不下时允许最后一行溢出（大代价），再由调用方按省略号截断兜底。
    """
    units = _units(text or "")
    if not units:
        return []
    widths = [draw.textlength(u, font=font) for u in units]
    n = len(units)
    INF = float("inf")
    # dp[k][i] = 前 i 个单元排成 k 行的最小代价；cut[k][i] 记录该行起点
    dp = [[INF] * (n + 1) for _ in range(max_lines + 1)]
    cut = [[0] * (n + 1) for _ in range(max_lines + 1)]
    dp[0][0] = 0.0
    for k in range(1, max_lines + 1):
        for i in range(k, n + 1):
            best, best_j = INF, 0
            for j in range(k - 1, i):
                if dp[k - 1][j] == INF:
                    continue
                lw = sum(widths[j:i])
                over = lw - max_width
                if over > 0:
                    if k < max_lines:
                        continue          # 非末行绝不许溢出
                    cost = dp[k - 1][j] + over * over / 20.0
                else:
                    # 均衡项归一到「千像素」量级，与断点语义惩罚同量级——
                    # 否则「两行更齐」永远压倒「别把词劈开」
                    cost = dp[k - 1][j] + (max_width - lw) ** 2 / 1000.0
                cost += _break_penalty(units, j)
                if cost < best:
                    best, best_j = cost, j
            dp[k][i], cut[k][i] = best, best_j
    # 行数取最优（文本一行放得下就不会硬拆三行）
    best_k = min(range(1, max_lines + 1), key=lambda k: dp[k][n])
    if dp[best_k][n] == INF:
        # 极端窄画布：退回贪心逐行塞，保证总有输出
        lines, current = [], ""
        for u in units:
            if current and draw.textlength(current + u, font=font) > max_width:
                lines.append(current.rstrip())
                current = u
            else:
                current += u
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        lines = lines or [text]
    else:
        bounds = [n]
        i, k = n, best_k
        while k > 0:
            bounds.append(cut[k][i])
            i, k = cut[k][i], k - 1
        bounds.reverse()
        lines = ["".join(units[bounds[t]:bounds[t + 1]]).strip()
                 for t in range(len(bounds) - 1)]
        lines = [ln for ln in lines if ln]
    # 溢出兜底：末行超宽时用省略号收尾
    if draw.textlength(lines[-1], font=font) > max_width:
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _fits(draw: ImageDraw.ImageDraw, lines: list[str], text: str, font,
          max_width: int) -> bool:
    """折行是否「完整且不溢出」：没被省略号截断、每行都在画布内。"""
    if not lines:
        return False
    if "".join(lines).replace(" ", "") != (text or "").replace(" ", ""):
        return False
    return all(draw.textlength(ln, font=font) <= max_width for ln in lines)


# 封面主标题的字号梯度：先争取两行（醒目），行数实在压不住再放开到三行。
# 下限 24px：再小在手机列表里就只剩一团墨点，宁可多一行。
_TITLE_SIZES_2LINE = (48, 44, 40, 36)
_TITLE_SIZES_ANY = (48, 44, 40, 36, 32, 28, 24)


def _fit_title(draw: ImageDraw.ImageDraw, text: str, max_width: int,
               prefer_lines: int = 2, max_lines: int = 3,
               sizes_short: tuple[int, ...] | None = None,
               sizes_any: tuple[int, ...] | None = None) -> tuple:
    """在「不拆词、不溢出」的前提下挑最大字号（动态字号兜底）。

    顺序：先按 target 行数（默认两行）从大字号往下试；两行在 32px 上仍放不下，
    再放开到三行从 48px 重试。整段文字始终按词原子折行，任何一步都不会劈词。
    """
    text = (text or "").strip()
    if not text:
        return _font(48, True), []
    short_pool = sizes_short or _TITLE_SIZES_2LINE
    any_pool = sizes_any or _TITLE_SIZES_ANY
    plans = [(prefer_lines, short_pool)]
    if max_lines > prefer_lines:
        plans.append((max_lines, any_pool))
    for limit, pool in plans:
        for size in pool:
            font = _font(size, True)
            lines = _wrap(draw, text, font, max_width, max_lines=limit)
            if len(lines) <= limit and _fits(draw, lines, text, font, max_width):
                return font, lines
    # 极端长标题：最小字号 + 省略号，也不回头去劈词
    font = _font(any_pool[-1], True)
    return font, _wrap(draw, text, font, max_width, max_lines=max_lines)


def _gradient(size: tuple[int, int], top: tuple[int, int, int],
              bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", size, top)
    draw = ImageDraw.Draw(img)
    for y in range(size[1]):
        ratio = y / max(size[1] - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size[0], y)], fill=color)
    return img


def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha)


def _decorate(img: Image.Image, accent: str) -> None:
    """高阶科技感底图（「数智化风」保底层，业界稿没抓到实景图时的最后防线）。

    分层叠加（全部低透明度，保证左侧标题暗区不被干扰）：
    1. 右上径向光晕——accent 色由内向外三档衰减，替代旧版一根孤零零的圆弧；
    2. 点阵——右半区 24px 网格的小点，密度往右下递减，制造「数字粒子」质感；
    3. 对角光带——两条 45° 扫光，模拟仪表盘的镜面反光；
    4. 电路线——固定 seed 的折线 + 节点（可复现，不抖动）；
    5. 细噪点——压住渐变色带（banding）。
    底部细网格保留：它是三模块共用的视觉锚。
    """
    import random

    w, h = img.size
    rng = random.Random(20260501)          # 固定 seed：同一张底图永远长得一样
    accent_rgb = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)

    # 1) 右上径向光晕：实心圆 + 高斯模糊（ImageDraw 直接赋像素会内外覆盖，
    #    画同心阶梯圆是错的——模糊才是真正的径向衰减，两层叠加出「辉光核 + 外晕」）
    from PIL import ImageFilter

    cx, cy = int(w * 0.86), int(h * 0.18)
    for radius, alpha, blur in ((int(h * 0.55), 58, 26), (int(h * 0.22), 90, 16)):
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                   fill=(*accent_rgb, alpha))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=blur))
        overlay = Image.alpha_composite(overlay, glow)
    d = ImageDraw.Draw(overlay)
    # 2) 点阵：右半区，越靠右越密（alpha 随 x 增大）
    for gx in range(int(w * 0.55), w, 24):
        strength = int(6 + 18 * (gx - w * 0.55) / max(w * 0.45, 1))
        for gy in range(20, h - 24, 24):
            if rng.random() < 0.18:        # 随机稀疏，避免死板的满铺
                continue
            r = 2 if (gx // 24 + gy // 24) % 3 else 1
            d.ellipse([gx - r, gy - r, gx + r, gy + r], fill=(255, 255, 255, strength))
    # 3) 对角光带：从右上向左下的两条宽渐变带
    for offset, alpha, width_px in ((-0.10, 14, 56), (0.06, 9, 30)):
        x0 = int(w * (0.62 + offset))
        for i in range(width_px):
            t = i / max(width_px - 1, 1)
            a = int(alpha * (1 - abs(t - 0.5) * 2) ** 2)
            if a <= 0:
                continue
            d.line([(x0 + i, 0), (x0 + i - int(h * 0.45), h)],
                   fill=(*accent_rgb, a), width=1)
    # 4) 电路线：右上角出三条向左延伸的走线，端点带「焊盘」
    for line_idx in range(3):
        y0 = int(h * (0.16 + line_idx * 0.09))
        pts = [(w, y0)]
        x = w
        y = y0
        for _ in range(rng.randint(2, 3)):
            x -= rng.randint(70, 150)
            pts.append((x, y))
            y += rng.choice((-14, 14, 0))
            pts.append((x, y))
        d.line(pts, fill=(*accent_rgb, 36), width=2)
        for px, py in pts[1:-1]:
            d.ellipse([px - 3, py - 3, px + 3, py + 3], fill=(*accent_rgb, 60))
    # 5) 细噪点：随机 4000 个 1px 白点，打散渐变色带
    for _ in range(4000):
        px, py = rng.randrange(w), rng.randrange(h)
        d.point((px, py), fill=(255, 255, 255, rng.randint(3, 10)))
    # 底部细网格（保留旧版锚点）
    for x in range(0, w, 40):
        d.line([(x, h - 1), (x, h - 32)], fill=(255, 255, 255, 8), width=1)
    for y in range(h - 32, h, 18):
        d.line([(0, y), (w, y)], fill=(255, 255, 255, 8), width=1)
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))


def _cover_fill(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """等比放大到铺满，再居中裁切（cover 语义）。"""
    w, h = size
    ratio = max(w / img.width, h / img.height)
    new = (max(w, int(img.width * ratio + 0.5)), max(h, int(img.height * ratio + 0.5)))
    img = img.resize(new, Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _hero_layer(hero: str | Path, size: tuple[int, int], fit: str) -> Image.Image | None:
    """把素材图铺成封面底：cover 裁切铺满 / contain 居中留白（给 logo 用）。"""
    try:
        img = Image.open(hero)
        img.load()
    except Exception:
        return None
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = size
    if fit == "contain":
        # logo 不裁：裁掉一半 wordmark 比放大更难看。右侧放白底圆角卡片保证对比度
        canvas = Image.new("RGB", size, (12, 16, 28))
        box_w, box_h = int(w * 0.40), int(h * 0.52)
        ratio = min(box_w / img.width, box_h / img.height, 3.0)
        thumb = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                           Image.LANCZOS)
        x = int(w * 0.94) - thumb.width
        y = (h - thumb.height) // 2
        pad = 22
        card = Image.new("RGBA", (thumb.width + pad * 2, thumb.height + pad * 2), (255, 255, 255, 236))
        mask = Image.new("L", card.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, card.width - 1, card.height - 1],
                                               radius=16, fill=255)
        canvas.paste(card.convert("RGB"), (x - pad, y - pad), mask)
        canvas.paste(thumb, (x, y))
        return canvas
    return _cover_fill(img, size)


def _scrim(size: tuple[int, int], strength: int = 255) -> Image.Image:
    """左黑右透的横向蒙版：标题永远落在暗区，白底论文插图也压得住。

    曲线是实测调出来的：0–30% 全不透明（品牌字 + 标题前半段的底），
    之后按 1.6 次幂缓降、到 90% 才归零。衰减太快的话，白底 UI 截图会在
    标题后半段透出来（x=500 处亮度冲到 148，白字直接看不清）。
    """
    w, h = size
    col = Image.new("L", (256, 1))
    for x in range(256):
        t = x / 255
        if t <= 0.30:
            alpha = strength
        elif t >= 0.90:
            alpha = 0
        else:
            alpha = strength * (1 - ((t - 0.30) / 0.60) ** 1.6)
        col.putpixel((x, 0), int(max(0, min(255, alpha))))
    return col.resize((w, h), Image.BILINEAR)


def _bottom_scrim(size: tuple[int, int]) -> Image.Image:
    w, h = size
    band = int(h * 0.34)
    col = Image.new("L", (1, band))
    for y in range(band):
        col.putpixel((0, y), int(150 * (y / max(band - 1, 1)) ** 1.4))
    strip = col.resize((w, band), Image.BILINEAR)
    full = Image.new("L", size, 0)
    full.paste(strip, (0, h - band))
    return full


def _shadow_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font,
                 fill, on_photo: bool = True) -> None:
    """照片背景上直接写字会糊：先描一层暗影再写正文。纯色底不必加，加了显脏。"""
    if not on_photo:
        draw.text(xy, text, font=font, fill=fill)
        return
    x, y = xy
    draw.text((x + 1, y + 2), text, font=font, fill=(0, 0, 0), stroke_width=2,
              stroke_fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=fill)


def make_cover(spec: dict, module: str, date_str: str | None = None,
               out_path: str | Path | None = None,
               hero: str | Path | None = None, hero_fit: str = "cover") -> str:
    """出封面。hero 传素材图路径；传空或不合法则退回品牌渐变底图。"""
    date_str = date_str or date.today().strftime("%Y.%m.%d")
    size = _canvas_size()
    w, h = size
    top, bottom, accent = PALETTE.get(module, PALETTE["paper"])
    base = _gradient(size, top, bottom)
    _decorate(base, accent)

    photo = _hero_layer(hero, size, hero_fit) if hero else None
    if photo is not None:
        img = photo
        # 整体轻压暗：白底 UI 截图（亮度 200+）不压的话，标题后半段会透出来
        img = Image.blend(img, Image.new("RGB", size, (10, 13, 24)), 0.16)
        img.paste(Image.composite(Image.new("RGB", size, (6, 9, 18)), img, _scrim(size)), (0, 0))
        img.paste(Image.composite(Image.new("RGB", size, (4, 6, 12)), img, _bottom_scrim(size)), (0, 0))
        # 右侧压一道同色系极淡渐变，让图片和左暗区过渡不生硬
        tint = Image.new("RGB", size, tuple(int(c * 0.55) for c in bottom))
        img.paste(Image.blend(img, tint, 0.10), (0, 0))
    else:
        img = base

    draw = ImageDraw.Draw(img)
    brand = topics.raw.get("brand", {})
    label_map = {"paper": "PAPER NOTES", "industry": "INDUSTRY", "contest": "CHALLENGES"}

    margin = 58
    text_w = int(w * (0.60 if photo is not None else 0.86))

    # 顶部：品牌 + 模块
    draw.rectangle([margin, 42, margin + 5, 72], fill=accent)
    _shadow_text(draw, (margin + 20, 38), brand.get("name", "底层视觉论文笔记"),
                 _font(22, True), (255, 255, 255), photo is not None)
    draw.text((margin + 22, 66), label_map.get(module, "NOTES"), font=_font(13, False),
              fill=_rgba(accent, 235)[:3])

    # 主标题：自下而上排版，先给日期和副标题留位，标题再按行数往上顶
    title = str(spec.get("title", "")).strip()
    subtitle = str(spec.get("subtitle", "")).strip()
    date_y = h - 46
    sub_y = date_y - 34 if subtitle else date_y
    accent_y = sub_y - 20
    title_font, lines = _fit_title(draw, title, text_w, prefer_lines=2, max_lines=3)
    line_h = title_font.size + 10
    y = max(112, accent_y - 14 - len(lines) * line_h)
    for line in lines:
        _shadow_text(draw, (margin, y), line, title_font, (255, 255, 255), photo is not None)
        y += line_h

    # 强调线
    draw.rectangle([margin, accent_y, margin + 74, accent_y + 5], fill=accent)

    # 副标题（只给一行：两行会和底部日期挤在一起）
    if subtitle:
        sub_font = _font(19, False)
        one = subtitle
        if draw.textlength(one, font=sub_font) > text_w:
            keep = max(4, len(subtitle) - 2)
            while keep > 4 and draw.textlength(subtitle[:keep], font=sub_font) > text_w - 14:
                keep -= 1
            one = subtitle[:keep] + "…"
        draw.text((margin, sub_y), one, font=sub_font, fill=(206, 213, 230))

    # 底部信息
    draw.text((margin, date_y), f"{date_str}　·　{brand.get('slogan', '')}",
              font=_font(16, False), fill=(168, 178, 198))

    suffix = ".jpg" if photo is not None else ".png"
    out = Path(out_path) if out_path else \
        settings.image_dir / f"cover_{module}_{date_str.replace('.', '')}{suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".jpg":
        img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    else:
        img.save(out, format="PNG", optimize=True)
    return str(out)


def make_square_thumb(spec: dict, module: str, out_path: str | Path | None = None,
                      hero: str | Path | None = None, hero_fit: str = "cover") -> str:
    """1:1 方形缩略图（部分分享位需要）。有素材图时重绘成方图，而不是裁 2.35:1。"""
    size = _canvas_size()
    square = (size[1], size[1])
    top, bottom, accent = PALETTE.get(module, PALETTE["paper"])
    photo = None
    if hero:
        try:
            img = Image.open(hero)
            img.load()
            if img.mode != "RGB":
                img = img.convert("RGB")
            photo = _cover_fill(img, square)
        except Exception:
            photo = None
    if photo is not None:
        photo.paste(Image.composite(Image.new("RGB", square, (6, 9, 18)), photo,
                                    _scrim(square, 210)), (0, 0))
        img = photo
    else:
        img = _gradient(square, top, bottom)
        _decorate(img, accent)

    draw = ImageDraw.Draw(img)
    brand = topics.raw.get("brand", {})
    margin = 48
    draw.rectangle([margin, 44, margin + 5, 74], fill=accent)
    _shadow_text(draw, (margin + 18, 40), brand.get("name", "底层视觉论文笔记"),
                 _font(20, True), (255, 255, 255))
    title = str(spec.get("title", "")).strip()
    tfont, lines = _fit_title(draw, title, square[0] - margin * 2,
                              prefer_lines=3, max_lines=4,
                              sizes_short=(40, 36, 32, 28),
                              sizes_any=(40, 36, 32, 28, 24))
    y = square[1] - 80 - (len(lines) - 1) * (tfont.size + 8)
    for line in lines:
        _shadow_text(draw, (margin, y), line, tfont, (255, 255, 255))
        y += tfont.size + 8

    out = Path(out_path) if out_path else settings.image_dir / f"thumb_{module}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="JPEG", quality=90, optimize=True, progressive=True)
    return str(out)
