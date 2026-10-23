"""大模型 Prompt 模板（三类内容 + 统一排版规格）。

核心设计：**让模型写「结构化内容」，不让模型写 HTML。**
模型输出一份 ArticleSpec JSON，排版由 app/render 里的模板负责，
这样版式永远稳定、不会因为模型心情好坏而变形，也便于二次编辑。

ArticleSpec 支持的块类型：
  h2 / h3 / p / bullets / numbered / quote / highlight
  meta_card（论文信息卡）/ contest_card（赛事卡）/ figure / table / divider

所有事实性数字必须来自输入素材；模型不得自行编造。提示词里对这点做了强约束。
"""
from __future__ import annotations

import re

from ..config import topics

BRAND = topics.raw.get("brand", {})
BRAND_NAME = BRAND.get("name", "底层视觉论文笔记")
SLOGAN = BRAND.get("slogan", "只聊底层视觉，只挑值得读的那几篇")

SYSTEM = f"""你是公众号「{BRAND_NAME}」的主笔，长期跟踪底层视觉（low-level vision）方向的论文与工业落地。
公众号定位：{SLOGAN}。

【读者画像】
算法工程师、硕博研究生、影像/视频方向的技术产品经理。他们看得懂 PSNR/LPIPS，不需要科普什么是超分；
但他们时间很少，需要你替他们判断「这篇值不值得读、能不能用、代价是什么」。

【文风】
1. 专业、克制、有判断。像一个读过很多论文的师兄在讲，而不是论文摘要的翻译机。
2. 拒绝「本文提出了一种…实验证明…」这种摘要腔。用「问题—做法—代价」的叙事：
   先讲清楚它要解决的真实痛点，再讲它怎么做的，最后讲它付出了什么代价。
3. 句子短，段落短。一段不超过 4 行（约 90 字），长句拆开。
4. 术语第一次出现给中文 + 英文，例如「感知损失（perceptual loss）」；之后只用简称。
5. 允许有态度：「这个设计很讨巧」「代价是推理慢了 3 倍」「这个指标提升意义有限」。
   但判断必须有依据，不能为了犀利而犀利。
6. 禁止使用：震惊、炸裂、封神、王炸、碾压、吊打、遥遥领先、必读、史上最强。
7. 不写「首先/其次/最后」的三段式套话；不写空洞的总结段。

【硬性约束】
- 所有数字（PSNR、参数量、FPS、奖金、日期）必须能在给定素材中找到。找不到就不写，或写「论文未披露」。
- 素材里给了实验表格或带数字的结论句时，正文必须引用其中至少 3 个具体数字并写出比较（谁比谁高多少）；
  禁止用「素材未提供具体数值」「无法量化」这类说明搪塞——先把手上的数字用完，再谈信息边界。
- 不得编造作者单位、开源地址、成绩排名。
- 不得引用素材之外的图片 URL。需要配图时只引用给定的 F 编号。
- 输出必须是合法 JSON，不要 Markdown 代码块包裹，不要任何解释性文字。
"""

# ---------------------------------------------------------------- 输出规格
SPEC_GUIDE = """
【输出 JSON 规格】
{
  "title": "公众号标题，<= 28 字，要具体（含方法名/关键数字/明确结论），不要标题党",
  "subtitle": "副标题，<= 40 字，一句话点出这篇的核心矛盾或价值",
  "digest": "摘要，<= 100 字，用于分享卡片",
  "lead": ["导语段 1", "导语段 2"],
  "sections": [ ...块序列... ],
  "takeaway": "一句话总结，<= 60 字，要有判断，不要复述标题",
  "sources": [{"name": "论文/文章标题", "url": "链接"}]
}

可用块（按需组合，不要堆砌）：
{"type":"h2", "text":"01 | 标题"}                       // 一级小节，编号用 01/02/03
{"type":"h3", "text":"小标题"}
{"type":"p", "text":"段落，可用 **加粗** 强调关键词"}
{"type":"bullets", "items":["要点 1","要点 2"]}
{"type":"numbered", "items":["步骤 1","步骤 2"]}
{"type":"quote", "text":"原文引用 / 作者观点，注明出处"}
{"type":"highlight", "title":"核心结论", "text":"一句话高亮，整篇不超过 2 个"}
{"type":"meta_card", "rows":[["论文标题","..."],["期刊/会议","..."],["作者团队","..."],["研究机构","..."],["开源代码","..."]]}
{"type":"figure", "ref":"F1", "caption":"图 1：整体架构。左边是…"}
{"type":"table", "header":["方法","PSNR↑","LPIPS↓"], "rows":[["Ours","32.1","0.21"]]}
{"type":"contest_card", "name":"赛事名", "track":"赛题方向", "prize":"奖金池", "deadline":"报名/截止", "entry":"入口链接", "desc":"一句话点评"}
{"type":"divider"}

篇幅：论文 1800–2600 字；业界 1400–2000 字；竞赛 900–1400 字（不含表格）。
"""

# ---------------------------------------------------------------- 通用片段
FACT_RULES = """
【事实纪律】
- 素材里没有的数字，一个都不要写。
- 不确定的信息（如作者单位、奖金池）写「官方未公布」，不要猜。
- 素材的 tables / result_snippets 里有数字时，必须引用至少 3 个并写出比较差值
  （「25.26 vs 24.10，高 1.16 dB」这种），不得用「论文未提供具体数值」「无法量化」搪塞。
- 某个指标确实没给时，写「论文未披露 X」，同时把已经给出的数字写出来，不要因为缺一项就整节摆烂。
- 评价要落在具体设计上：说「用熵路由把平滑区和高频区分开，避免了教师伪影污染」，
  而不是「设计非常巧妙」。
- 如果素材质量不足以支撑一整篇，就诚实写出信息边界，不要注水。
"""

# ---------------------------------------------------------------- 模块 A
PAPER_PROMPT = """
【任务】写一篇论文精读，发在「底层视觉论文笔记」。

【结构建议】
1. 导语（lead，2 段）：第一段用一个具体场景/矛盾把读者拉进来；第二段交代这篇论文干了什么、为什么值得读。
2. meta_card：论文信息卡（标题、会议/期刊、作者团队、研究机构、开源代码）。信息不全的行直接省略。
3. h2 "01 | 研究背景与痛点"：讲清任务背景与已有方法的具体死结（卡在哪个环节、为什么走不通），
   不要泛泛说「效果不好」。能点名被挑战的 SOTA 方法更好。
4. h2 "02 | 核心创新与方法拆解"：核心。先用一段话讲整体思路，再按模块拆（h3 + p / bullets）。
   必须引用架构图（figure，ref 用素材提供的编号，图注写「图 X：…」）。
   每个模块要说清「为什么这么设计、替代了已有方法的哪个组件」，而不只是「是什么」。
5. h2 "03 | 关键实验结果解读"：这是全文证据段，必须有硬数字。
   素材 tables 是从论文 HTML 直接抽出的实验对比表，数字可信：**至少用 table 块原样呈现 1 张**
   （可裁列，但数字照抄、不得改写），表格前后各写一段解读：
   提升集中在哪些指标、比最强对手高/低多少（写成「PSNR 高 0.42 dB」「LPIPS 低 0.019」这样的差值，
   差值必须拿表里两个数逐位现算），
   代价是什么。正文合计至少引用 3 个来自 tables 或 result_snippets 的具体数字。
   **禁止自己另拼一张表**：表格里每个数字都必须来自素材 tables，素材没有的数字一个都不许写。
   素材里有第二张 figure（结果对比图）时引用它，图注写「图 X：…」。
   **禁止**出现「素材未提供具体数值」「无法量化」「表格数据缺失」这类内部说明。
6. h2 "04 | 与 SOTA 的对比与代价"：把表格数字翻译成结论。点名最强对手（方法名 + 它在哪项指标多少），
   写清领先/落后多少、参数量与推理开销（Params / 速度，素材有就写数字，没有写「论文未披露推理开销」）、
   什么场景下会被反超。作者结论与你的判断用「值得注意的是」区分。
7. h2 "05 | 顺带值得一看"（仅当一期有多篇候选）：其余 1–2 篇各 2–3 句（bullets）。单篇素材时省略本节。
8. takeaway：一句话给结论，告诉读者「什么情况下该用这个方法」。

【写作要求】
- 至少引用 1 张图；图注要写「图 X：…」，并解释图中哪一部分对应正文哪句话。
- 不要按论文章节顺序流水账。按「问题—方法—证据—对比—边界」重组。
""" + FACT_RULES + SPEC_GUIDE

# ---------------------------------------------------------------- 模块 B
INDUSTRY_PROMPT = """
【任务】写一篇业界动态解读，发在「底层视觉论文笔记」。读者要的不是新闻复述，而是「这事对做底层视觉的人意味着什么」。

【结构建议】
1. lead（2 段）：第一段交代事件本身（谁、什么时候、发布了什么），第二段给出你的核心判断（这件事的真正看点）。
2. h2 "01 | 发生了什么"：把事实讲清楚，不评价。涉及数字（融资额、参数规模、指标）必须来自素材。
3. h2 "02 | 技术上看，真正的变化在哪"：这是主体。拆解技术路线：
   - 它解决了此前哪条路走不通的问题？
   - 相比现有方案，改动在架构、数据还是训练范式？
   - 有没有藏着成本（算力、数据标注、推理延迟）？
   可以用 bullets 分点，但每点必须有信息增量。
4. h2 "03 | 对底层视觉意味着什么"：落到读者身上。可以是研究选题的启示、工程选型的参考、或者一个需要警惕的泡沫。
5. h2 "04 | 冷静一下"：写清楚信息的边界、厂商话术里需要打折的部分。这一节是本号的立场。
6. takeaway：一句话，给判断而不是给情绪。

【写作要求】
- 不要写成产品发布会通稿。每段都要有「所以呢」。
- 如果是开源工具/模型发布，优先写清：能力边界、显存/速度门槛、和现有方案怎么选。
- 如果是融资/商业新闻，重点写技术路线与商业化之间的张力，不要罗列投资方。
- 禁止把厂商 benchmark 当成定论，要提示「这是官方数据，第三方复现待观察」。
""" + FACT_RULES + SPEC_GUIDE

# ---------------------------------------------------------------- 模块 C
CONTEST_PROMPT = """
【任务】写一期赛事速递，发在「底层视觉论文笔记」。读者想快速判断：哪个值得打、门槛多高、来不来得及。

【结构建议】
1. lead（2 段）：第一段用一句话概括这期赛事的共同特征（比如「本期 4 场比赛，3 场聚焦真实场景复原，2 场即将截止」）；
   第二段给出参赛建议的总纲（适合谁、不适合谁）。
2. h2 "01 | 本期值得关注的赛事"：每个赛事一个 contest_card，后面跟 1 段解读（这道题的难点在哪、什么样的方案有优势）。
   赛事之间用 divider 分隔。
3. h2 "02 | 时间线与怎么选"：用 table 汇总（赛事 / 方向 / 截止时间 / 奖金 / 难度），再给 2–3 条选择建议。
4. h2 "03 | 参赛避坑"：基于赛题特性给具体建议（数据泄漏、评测指标陷阱、算力预算、组队策略）。
5. takeaway：一句话，比如「离截止还有 X 天，先跑通 baseline 再谈创新」。

【写作要求】
- 素材已经过时效过滤，只包含未截止、仍在进行或即将开始的赛事：不要再推荐素材之外的赛事。
- deadline 照抄素材原文；素材里的 days_left 是距截止的剩余天数，解读时写成「剩 N 天」；
  没有日期的写「待官方公布」，绝不自行补一个日期。
- 赛事名称、奖金、时间节点必须来自素材；缺失写「待官方公布」，绝不编造。
- 每条赛事必须给出「赛题方向」，不能只写名称。
- 难度判断要说明依据（数据规模、是否有 baseline、往届参赛人数）。
- 入口链接原样保留，方便读者直接点。
""" + FACT_RULES + SPEC_GUIDE

PROMPTS = {"paper": PAPER_PROMPT, "industry": INDUSTRY_PROMPT, "contest": CONTEST_PROMPT}

MODULE_TITLE = {"paper": "论文精读", "industry": "业界观察", "contest": "赛事速递"}
# 素材里有数字、成稿却没引用时的补写要求（见 thin_evidence 判定）
QUANT_RETRY = (
    "这一稿的实验解读缺少数值证据，读起来没有说服力。请保持原有的结构、标题与已写的分析，"
    "但务必重写「关键实验结果解读」与「与 SOTA 的对比与代价」两节：\n"
    "1) 至少用 1 个 table 块把素材 tables 里的一张实验对比表原样呈现（数字照抄，可裁列）；\n"
    "2) 正文合计引用至少 3 个来自 tables 或 result_snippets 的具体数字，并写出比较差值\n"
    "   （例如「PSNR 25.26 vs 24.64，高 0.62 dB」「LPIPS 0.1253 对比 0.1382 更低」）；\n"
    "3) 点名最强对手与它的指标，写清领先/落后多少，以及参数量、推理开销的代价；\n"
    "4) 删除「素材未提供具体数值」「无法量化」这类内部说明。数字只能来自素材，不得编造。"
)

# 素材里一个实验数字都没有时的补写要求：此时任何指标表都是编的
NO_NUMERIC_RETRY = (
    "这一稿的实验解读用了素材里并不存在的数字或表格，属于编造，必须改掉。"
    "这批素材里没有任何实验表格或带数字的结论句，所以「关键实验结果解读」"
    "与「与 SOTA 的对比与代价」两节：\n"
    "1) 删除所有 table 块，以及所有具体指标数值（PSNR/SSIM/LPIPS/参数量/耗时等一个都不要写）；\n"
    "2) 只引用素材原文里确实出现的相对说法（例如摘要里的「提速 100 倍」「码率节省超过 50%」），"
    "并说明它相对哪个基线；\n"
    "3) 用定性语言讲清强弱与代价：谁在保真或感知上更强、代价落在参数量还是推理步数、什么场景会被反超；\n"
    "4) 不要写「素材未提供」「无法量化」这类内部说明，也不要解释为什么没有数字。"
)

_DIGIT_RE = re.compile(r"\d")
# 数字 token（含小数与千分位），用于核对表格 cells 是否真的来自素材
_NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 证据段（03/04）小标题；正文里的百分比、倍数是派生说法，不参与差值核对
_EVIDENCE_HEAD_RE = re.compile(r"^0?[34]\b|^0?[34]\s*[\|｜.、:：]")
_PROSE_NUM_RE = re.compile(r"(?<![\d.])\d+\.\d+(?![\d.])(?!\s*[%％×倍x])")
# 指标名：正文里「差值」跟着哪个指标，就只在该指标的数值行/列里核对
_METRIC_RE = re.compile(r"HDR-VDP2|DISTS|MANIQA|MUSIQ|CLIPIQA|LPIPS|PSNR|SSIM|BPP", re.I)


def has_numeric_material(items: list) -> bool:
    """素材里是否带了可引用的实验数据（表格或带指标的结论句）。"""
    for item in items:
        extra = getattr(item, "extra", {}) or {}
        if extra.get("tables") or extra.get("result_snippets"):
            return True
    return False


def thin_evidence(spec: dict) -> bool:
    """03/04 两节是否完全没有数字证据（无表格且正文不含任何数字）。"""
    in_evidence = False
    for block in spec.get("sections") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        text = str(block.get("text") or "")
        if kind in ("h2", "h3"):
            # 小节标题以 03/04 开头即进入证据区间，遇到别的小节退出
            in_evidence = bool(_EVIDENCE_HEAD_RE.match(text.strip()))
            continue
        if not in_evidence:
            continue
        if kind == "table":
            return False
        blob = text + " " + " ".join(str(x) for x in (block.get("items") or []))
        if _DIGIT_RE.search(blob):
            return False
    return True


def material_number_pool(items: list) -> set[str]:
    """素材里出现过的全部数字 token（表格 cells + 数值结论句 + 标题摘要）。"""
    pool: set[str] = set()
    for item in items:
        extra = getattr(item, "extra", {}) or {}
        texts: list[str] = [getattr(item, "title", "") or "", getattr(item, "summary", "") or ""]
        for table in extra.get("tables") or []:
            texts.extend(str(c) for c in table.get("header") or [])
            for row in table.get("rows") or []:
                texts.extend(str(c) for c in row or [])
        texts.extend(str(s) for s in extra.get("result_snippets") or [])
        for text in texts:
            pool.update(tok.replace(",", "") for tok in _NUM_TOKEN_RE.findall(text))
    return pool


def _table_bad_numbers(block: dict, pool: set[str]) -> list[str]:
    cells: list = list(block.get("header") or [])
    for row in block.get("rows") or []:
        cells.extend(row or [])
    bad: list[str] = []
    for cell in cells:
        for tok in _NUM_TOKEN_RE.findall(str(cell)):
            tok = tok.replace(",", "")
            if tok not in pool and tok not in bad:
                bad.append(tok)
    return bad


def unsupported_table_numbers(spec: dict, items: list) -> list[str]:
    """成稿表格里出现、但素材中找不到的数字（模型自造对比表的典型症状）。"""
    pool = material_number_pool(items)
    bad: list[str] = []
    for block in spec.get("sections") or []:
        if isinstance(block, dict) and block.get("type") == "table":
            bad.extend(n for n in _table_bad_numbers(block, pool) if n not in bad)
    return bad


def drop_unsupported_tables(spec: dict, items: list) -> int:
    """重写后仍编数字时的兜底：删掉含未核实数字的 table 块，返回删除张数。"""
    pool = material_number_pool(items)
    kept, dropped = [], 0
    for block in spec.get("sections") or []:
        if isinstance(block, dict) and block.get("type") == "table" and _table_bad_numbers(block, pool):
            dropped += 1
            continue
        kept.append(block)
    if dropped:
        spec["sections"] = kept
    return dropped


def _cell_numbers(cell: str) -> list[float]:
    nums: list[float] = []
    for tok in _NUM_TOKEN_RE.findall(str(cell)):
        try:
            nums.append(float(tok.replace(",", "")))
        except ValueError:
            continue
    return nums


def _material_number_groups(items: list) -> list[tuple[set[float], list[list[float]]]]:
    """每张素材表/每条数值结论句：数值集合 + 同行同列（同句内）的数值分组。

    差值只允许在同一行、同一列或同一句里成立。一张 9 列表里任意两数相减，
    两位小数几乎总能凑出目标值（实测 2.81 被 22.87-25.68 蒙中），那样闸门等于没有。
    """
    groups: list[tuple[set[float], list[list[float]]]] = []
    for item in items:
        extra = getattr(item, "extra", {}) or {}
        for table in extra.get("tables") or []:
            header = [str(c) for c in (table.get("header") or [])]
            rows = [[str(c) for c in (row or [])] for row in (table.get("rows") or [])]
            values: set[float] = set()
            lines: list[list[float]] = []
            columns: dict[int, list[float]] = {}
            for ci, cell in enumerate(header):
                nums = _cell_numbers(cell)
                values.update(nums)
                columns.setdefault(ci, []).extend(nums)
            for row in rows:
                line: list[float] = []
                for ci, cell in enumerate(row):
                    nums = _cell_numbers(cell)
                    values.update(nums)
                    line.extend(nums)
                    columns.setdefault(ci, []).extend(nums)
                lines.append(line)
            lines.extend(columns.values())
            groups.append((values, lines))
        for snippet in extra.get("result_snippets") or []:
            nums = _cell_numbers(snippet)
            if len(nums) >= 2:
                groups.append((set(nums), [nums]))
    return groups


def _metric_name(text: str) -> str:
    m = _METRIC_RE.search(str(text))
    return m.group(0).upper() if m else ""


def _metric_lines(items: list) -> dict[str, list[list[float]]]:
    """按指标名归拢素材表里的数值行与数值列，供「差值必须落在同名指标内」核对。

    只看「同一行/同一列」仍然太宽：实测 2.82 被一张无关表里 39.35-36.53 蒙中，
    而正文那句话讲的是 MUSIQ。行标签/表头带指标名时，差值只在该指标的数值里找。
    """
    out: dict[str, list[list[float]]] = {}

    def add(name: str, nums: list[float]) -> None:
        if len(nums) >= 2:
            out.setdefault(name, []).append(nums)

    for item in items:
        extra = getattr(item, "extra", {}) or {}
        for table in extra.get("tables") or []:
            header = [str(c) for c in (table.get("header") or [])]
            rows = [[str(c) for c in (row or [])] for row in (table.get("rows") or [])]
            for row in rows:
                name = _metric_name(" ".join(row[:2]))
                if name:
                    add(name, [n for cell in row[1:] for n in _cell_numbers(cell)])
            for ci, cell in enumerate(header):
                name = _metric_name(cell)
                if not name:
                    continue
                add(name, [n for row in rows if ci < len(row)
                           for n in _cell_numbers(row[ci])])
    return out


def _explained_by_material(tok: str, pool: set[str],
                           groups: list[tuple[set[float], list[list[float]]]],
                           scoped: list[list[float]] | None = None) -> bool:
    """素材原值、原值的舍入、同一张表同行同列（或同一句）两数之差，都算可核对。"""
    if tok in pool:
        return True
    try:
        target = float(tok)
    except ValueError:
        return False
    decimals = len(tok.split(".")[1])
    for values, lines in groups:
        if any(round(n, decimals) == target for n in values):
            return True
    if scoped:
        return any(round(abs(a - b), decimals) == target
                   for line in scoped for i, a in enumerate(line) for b in line[i + 1:])
    for _values, lines in groups:
        for line in lines:
            if any(round(abs(a - b), decimals) == target
                   for i, a in enumerate(line) for b in line[i + 1:]):
                return True
    return False


def unsupported_prose_numbers(spec: dict, items: list) -> list[str]:
    """03/04 节正文里对不上素材的小数（实测多为模型算错的差值，如 2.78 写成 2.81）。"""
    pool = material_number_pool(items)
    groups = _material_number_groups(items)
    metric_lines = _metric_lines(items)
    bad: list[str] = []
    in_evidence = False
    for block in spec.get("sections") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        text = str(block.get("text") or "")
        if kind in ("h2", "h3"):
            in_evidence = bool(_EVIDENCE_HEAD_RE.match(text.strip()))
            continue
        if not in_evidence or kind == "table":
            continue
        blob = text + " " + " ".join(str(x) for x in (block.get("items") or []))
        metrics = [(m.start(), m.group(0).upper()) for m in _METRIC_RE.finditer(blob)]
        for m in _PROSE_NUM_RE.finditer(blob):
            tok = m.group(0)
            if tok in bad:
                continue
            name = ""
            for pos, label in metrics:
                if pos >= m.start():
                    break
                name = label
            if not _explained_by_material(tok, pool, groups, metric_lines.get(name)):
                bad.append(tok)
    return bad


def quant_retry_instruction(bad_numbers: list[str], has_material: bool = True) -> str:
    """二次重写的附加指令：缺数值证据 / 表格里编了数字 / 素材本就没有数字。"""
    if not has_material:
        return NO_NUMERIC_RETRY
    if not bad_numbers:
        return QUANT_RETRY
    return QUANT_RETRY + (
        "\n5) 上一稿的实验解读里出现了素材中不存在的数字（"
        + "、".join(bad_numbers[:8])
        + "），这些是编造的。表格只能照抄素材 tables 里的单元格；正文只能用素材原值，"
        "差值要拿素材同一张表里的两个数逐位相减并核对，凑不出就并列两个原始数字、不要硬写差值。"
    )



def _item_payload(index: int, item, figure_ids: list[str]) -> dict:
    payload = {
        "id": f"P{index}",
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "venue": item.venue,
        "year": item.year,
        "authors": item.authors[:8],
        "category": item.category,
        "abstract": (item.summary or "")[:1600],
        "code_url": item.code_url,
        "score": item.score,
        "keywords": item.keywords[:8],
        "figures": figure_ids,
    }
    if item.extra.get("deadline"):
        payload["deadline"] = item.extra["deadline"]
    if item.extra.get("prize"):
        payload["prize"] = item.extra["prize"]
    if item.extra.get("platform"):
        payload["platform"] = item.extra["platform"]
    if item.extra.get("figure_caption"):
        payload["figure_caption"] = item.extra["figure_caption"]
    if item.extra.get("figure_caption_2"):
        payload["figure_caption_2"] = item.extra["figure_caption_2"]
    tables = item.extra.get("tables") or []
    if tables:
        payload["tables"] = tables[:3]
    snippets = item.extra.get("result_snippets") or []
    if snippets:
        payload["result_snippets"] = snippets
    if item.extra.get("days_left") is not None:
        payload["days_left"] = item.extra["days_left"]
    return payload


def build_user_prompt(module: str, items: list, figures: dict[str, dict], date_str: str) -> str:
    """把素材整理成模型友好的 JSON，附带图片编号表。"""
    import json

    material = []
    for idx, item in enumerate(items, 1):
        ids = [fid for fid, meta in figures.items() if meta.get("item_id") == f"{item.module}:{item.fingerprint}"]
        material.append(_item_payload(idx, item, ids))

    figure_table = [
        {"ref": fid, "caption": meta.get("caption", "")[:200], "from": meta.get("title", "")[:80]}
        for fid, meta in figures.items()
    ]

    payload = {
        "date": date_str,
        "module": module,
        "brand": {"name": BRAND_NAME, "slogan": SLOGAN},
        "materials": material,
        "figures": figure_table,
    }
    return (
        PROMPTS[module]
        + "\n\n【本期素材】\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请严格按上述 JSON 规格输出，只输出 JSON。"
    )
