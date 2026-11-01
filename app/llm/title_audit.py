"""赛事标题的「先自审，再动笔」：黑话/文艺腔检测、可读性校验与通俗化重构。

背景（实测事故）：赛事稿出过这样一条标题——

    76 个生物任务、9 大领域：BioXArena 把 LLM 智能体拉进湿实验门口

数字 Hook 和赛事锚点都在，看起来「要素齐全」，但读者读完根本不知道这场比赛
考什么：「湿实验门口」是从官网措辞里硬搬来的生造隐喻，「拉进…门口」也不构成
任何信息。标题的职责是把赛事翻译成人话，不是把官方措辞复述一遍。

所以这里把「编辑部的判断」写成可执行的代码：
  1. 闸门（contest_title_issues）：空洞文艺腔、生造行话、缺规模 Hook、缺赛事锚点，
     命中任意一条就判定「需要重写」；
  2. 兜底（polish_title / compose_contest_title）：没有大模型可用、或重写没通过闸门时，
     用确定性规则把标题拉回「人话」区间——删空洞修饰，再不行按
     「规模数字 + 赛事名 + 这场在测什么」的爆款公式重新拼一条。
"""
from __future__ import annotations

import re

# 赛事稿标题的字数上限：公众号上限 64 字，但超过 ~56 字的标题在列表页只能看到半句，
# 规模 Hook 还没露脸读者就划走了。40 字是推荐值（Prompt 里写明），
# 56 才是判定不合格的硬线——「生物版 ImageNet 来了！76 个任务、9 大领域，
# BioXArena 开启 AI 智能体『湿实验』首秀」这类长标题属于可接受的爆款句式。
MAX_CONTEST_TITLE = 56
RECOMMENDED_CONTEST_TITLE = 40

# ---------------------------------------------------------------- 闸门一：空洞文艺腔
# 判断标准：把这个词删掉，句子意思不变 → 它就不该出现在标题里
_HOLLOW_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"门口|大门|殿堂|深水区"),
     "比喻型空话——「门口」删掉后句子意思不变，属于拿意象凑字数"),
    (re.compile(r"之巅|元年|新纪元|前夜|里程碑|盛宴|狂飙|星辰大海|史诗|改写历史|登顶"),
     "空洞文艺腔——不承载任何事实，读者读完拿不到信息"),
)

# ---------------------------------------------------------------- 闸门二：生造行话
# 只收「实测出现过 / 明显读不懂」的短语，宁窄不宽：误杀好标题的代价比放过一条高
_JARGON_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:拉|推|拽|带|引|塞)(?:进|入|到)[^，。！？：:；]{0,12}(?:门口|大门|殿堂|深水区)"),
     "生造隐喻——「把 X 拉进 Y 门口」读者只能读出画面，读不出这场比赛在测什么"),
    (re.compile(r"拓扑空间|流形|表征空间|隐空间对齐|语义对齐空间|特征子空间"),
     "论文内部术语——不是标题语言，普通读者无法秒懂"),
    (re.compile(r"端到端(?:对齐|打通|贯通)|全链路(?:对齐|打通)"),
     "直译式行话——读者看不出具体做了什么动作"),
    (re.compile(r"范式(?:跃迁|革命|重构)|认知升维|维度打击"),
     "抽象大词——没有指向具体的任务、数据或结果"),
    (re.compile(r"(?:对齐|打通|重构|沉淀|赋能)[^，。！？：:；]{0,8}(?:生态|闭环|底座|飞轮|中台)"),
     "互联网黑话——赛事稿不需要这类词"),
)

# ---------------------------------------------------------------- 闸门三：可读性
# 规模 Hook：数字 + 量词（76 个任务 / 9 大领域 / 3 万美元 / 2 条赛道 / 1727 队 / 2 万奖金）
# 量词表必须够宽：实测「1727 队抢 2 万奖金」因为缺「队」和裸「万」被误判成没有数字 Hook
_NUM_HOOK_RE = re.compile(
    r"\d[\d,\.]*\s*(?:万|亿)?\s*(?:个|项|类|大|支|场|条|张|份|种|轮|步|天|队|人|名|题|道|关|届"
    r"|期|次|台|套|枚|美元|美金|元|GB|TB)"
    r"|\d[\d,\.]*\s*(?:万|亿)(?![0-9A-Za-z])"      # 「2 万奖金」：裸万 + 汉字也认
)
# 设问句也是一种钩子（「大模型如何做生物实验？」）
_ASK_RE = re.compile(r"[？?]|如何|怎么|能不能|能否|是什么|靠什么|凭什么")
# 「考什么」的动作动词：标题里出现这些，读者才知道比赛在干什么
_DO_VERB_RE = re.compile(
    r"测(?:试|评|一测)?|考(?:什么|的是|核)?|做(?:实验|题|任务)|写(?:出|一份|代码)?|"
    r"跑(?:通|分|起来)?|识别|生成|重建|预测|定位|分割|驾驶|规划|控制|复现|调参|上分|刷榜"
)
_LATIN_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-\.]{2,}")
# 素材里的规模数字（用于判断「素材明明给了数字，标题却没接住」）。
# 名词必须落在白名单里：否则「9 大领域共 76 个任务」会被贪婪吃成「9大领域共」。
_MATERIAL_NUM_RE = re.compile(
    r"\d[\d,\.]*\s*(?:万|亿)?\s*(?:个|项|类|大|支|场|条|张|份|种|轮)?\s*"
    r"(?:[\u4e00-\u9fff]{0,2})?"
    r"(?:任务|赛题|赛道|领域|样本|数据|队伍|参赛队|团队|奖金|美元|元|图片|视频|类别)"
)


def _item_text(item) -> str:
    """素材里所有能出现规模数字的文本，拼成一段供正则扫描。"""
    if item is None:
        return ""
    extra = getattr(item, "extra", {}) or {}
    detail = extra.get("contest_detail") or {}
    parts = [
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "summary", "") or ""),
        str(getattr(item, "category", "") or ""),
        str(detail.get("official_brief") or "")[:2000],
        str(detail.get("page_excerpt") or "")[:1200],
        str(extra.get("prize") or ""),
    ]
    return " ".join(p for p in parts if p)


def material_hooks(item) -> list[str]:
    """从素材里摘出可做标题 Hook 的规模短语（去重、保留原顺序、最多 4 条）。"""
    text = _item_text(item)
    out: list[str] = []
    seen: set[str] = set()
    for m in _MATERIAL_NUM_RE.finditer(text):
        hook = re.sub(r"\s+", " ", m.group(0)).strip()
        if len(hook) < 3 or hook in seen:
            continue
        # 只保留「数字 + 量词 + 名词」的完整形态，光秃秃的「9 个」不成钩子
        if not re.search(r"\d", hook) or not re.search(r"[\u4e00-\u9fff]{2,}$", hook):
            continue
        seen.add(hook)
        out.append(hook)
        if len(out) >= 4:
            break
    return out


def _has_anchor(title: str, item) -> bool:
    """标题里有没有赛事锚点：拉丁赛事名 / 平台主办方名 / 与素材标题重叠的中文词。

    中文重叠放宽到「连续两字」：实测「1727 队抢 2 万奖金：天池这场表格赛考的不是 OCR」
    对素材「知乎知学堂大模型挑战赛：大模型复杂表格识别与内容理解」——标题用「表格赛」
    指代赛题，4 字重叠判定误报「看不到赛事名」。两字重叠足以识别这种简称式锚点。
    """
    if item is None:
        return True
    src = str(getattr(item, "title", "") or "")
    low = title.lower()
    for tok in set(_LATIN_NAME_RE.findall(src)):
        if tok.lower() in low:
            return True
    # 平台 / 主办方名也是锚点（「天池」「Kaggle」「阿里云」这类俗称多来自 extra）
    extra = getattr(item, "extra", {}) or {}
    for field_name in ("platform", "organizer"):
        value = str(extra.get(field_name) or "")
        for tok in re.split(r"[\s/／、，,]+", value):
            if len(tok) >= 2 and tok in title:
                return True
    title_cjk = re.sub(r"[^\u4e00-\u9fff]", "", title)
    src_cjk = re.sub(r"[^\u4e00-\u9fff]", "", src)
    for i in range(max(0, len(src_cjk) - 1)):
        if src_cjk[i:i + 2] and src_cjk[i:i + 2] in title_cjk:
            return True
    return False


def contest_title_issues(title: str, item=None) -> list[str]:
    """赛事标题自审：返回问题清单，空列表表示通过。

    每一条问题都是给重写 Prompt 用的可执行描述（说清哪里不对、为什么不对），
    不是简单的关键词命中日志。
    """
    t = (title or "").strip()
    if not t:
        return ["标题为空"]
    issues: list[str] = []

    for pat, why in _HOLLOW_RULES:
        m = pat.search(t)
        if m:
            issues.append(f"「{m.group(0)}」是{why}")
    for pat, why in _JARGON_RULES:
        m = pat.search(t)
        if m:
            issues.append(f"「{m.group(0)}」是{why}")

    if len(t) > MAX_CONTEST_TITLE:
        issues.append(f"标题 {len(t)} 字，超过 {MAX_CONTEST_TITLE} 字上限")

    has_num = bool(_NUM_HOOK_RE.search(t))
    has_ask = bool(_ASK_RE.search(t))
    has_verb = bool(_DO_VERB_RE.search(t))
    if not has_num and not has_ask and not has_verb:
        issues.append("没有可读钩子：既没有规模数字，也没说清这场比赛考什么")

    # 素材里明明有数字，标题却一个都没接住 → 缺量化 Hook（只在素材确实有数字时要求）
    if not has_num and material_hooks(item):
        hooks = "、".join(material_hooks(item)[:2])
        issues.append(f"素材里有可用的规模数字（{hooks}），标题却没有用上")

    if not _has_anchor(t, item):
        issues.append("标题里看不到赛事名，读者不知道这是哪场比赛")

    return issues


def has_hollow(title: str) -> bool:
    """标题里是否有空洞文艺腔/生造行话——只有这类问题才值得动兜底重写。"""
    t = title or ""
    return any(p.search(t) for p, _ in _HOLLOW_RULES) or any(
        p.search(t) for p, _ in _JARGON_RULES)


# ---------------------------------------------------------------- 兜底一：删空洞修饰
_HOLLOW_STRIP = ("门口", "大门", "殿堂", "深水区", "之巅", "元年", "新纪元", "前夜",
                 "里程碑", "盛宴", "狂飙", "星辰大海", "史诗级", "改写历史")
_TAIL_JUNK_RE = re.compile(r"[，、,;；:：\s—\-]+$")


def polish_title(title: str) -> str:
    """去掉空洞修饰与悬空标点，返回清洗后的标题（不做语义改写，宁保守不失真）。

    「把 LLM 智能体拉进湿实验门口」→「把 LLM 智能体拉进湿实验」：
    删掉比喻尾巴，句子反而通顺，读者也终于知道这场比赛让 AI 去干什么。
    """
    t = (title or "").strip()
    if not t:
        return t
    out = t
    for word in _HOLLOW_STRIP:
        out = out.replace(word, "")
    out = re.sub(r"\s{2,}", " ", out)
    out = _TAIL_JUNK_RE.sub("", out)
    out = re.sub(r"[，、,]{2,}", "，", out)
    # 削得太狠（把主句一起带走了）就不动，保留原题交给人看
    return out if len(out) >= 8 else t


# ---------------------------------------------------------------- 兜底二：公式化重构
# 任务方向 → 一句人话「这场比赛考什么」
_TASK_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("生物", "基因", "蛋白", "细胞", "医学", "药物", "临床", "分子"),
     "测大模型能不能真做生物实验"),
    (("超分", "去噪", "复原", "增强", "去模糊", "修复", "去雨", "低光"),
     "把低质图像复原到可交付质量"),
    (("表格", "文档", "OCR", "票据", "公式"), "把图片里的表格读成结构化数据"),
    (("自动驾驶", "感知", "车端", "规划", "机器人", "具身"), "让模型在真实场景里扛住长尾"),
    (("智能体", "Agent", "agent", "多智能体"), "看智能体能不能把多步任务一次跑通"),
    (("多模态", "VLM", "视觉语言", "图文"), "考多模态模型看不看得懂真实场景"),
    (("时序", "预测", "销量", "金融", "风控"), "比谁把预测误差压得更低"),
    (("检测", "分割", "跟踪", "识别"), "在真实数据上比检测与分割的精度"),
)


def _contest_name(item) -> str:
    """赛事名：优先拉丁名（BioXArena / NTIRE 2026），否则取素材标题的主干。"""
    src = str(getattr(item, "title", "") or "")
    latin = [t for t in _LATIN_NAME_RE.findall(src) if len(t) >= 3]
    if latin:
        # 拉丁名后面紧跟年份时一起带上（NTIRE 2026）
        m = re.search(rf"{re.escape(latin[0])}\s*20\d{{2}}", src)
        return m.group(0) if m else latin[0]
    head = re.split(r"[：:｜|（(—–\-]", src)[0].strip()
    return head if len(head) <= 18 else head[:16]


def _task_phrase(item) -> str:
    text = " ".join([str(getattr(item, "title", "") or ""),
                     str(getattr(item, "summary", "") or ""),
                     str(getattr(item, "category", "") or "")])
    for words, phrase in _TASK_HINTS:
        if any(w in text for w in words):
            return phrase
    cat = str(getattr(item, "category", "") or "").strip()
    return f"把「{cat}」做到工程可用" if cat else "用真实任务检验工程上限"


def compose_contest_title(item, max_len: int = MAX_CONTEST_TITLE) -> str:
    """按「规模数字 + 赛事名 + 这场在测什么」重拼标题（无 LLM 时的确定性兜底）。

    只承诺两件事：数字来自素材、句式通顺；具体措辞不追求比大模型好，
    但一定比「把 X 拉进 Y 门口」这种黑话标题可读。
    """
    if item is None:
        return ""
    hooks = material_hooks(item)
    name = _contest_name(item)
    task = _task_phrase(item)
    body = f"{name}：{task}"
    if hooks:
        title = f"{'、'.join(hooks[:2])}！{body}"
    else:
        title = f"{name}开赛：{task}"
    if len(title) > max_len:
        title = body if len(body) <= max_len else body[:max_len]
    return title


# ---------------------------------------------------------------- 业界标题自审
# 业界稿的标题问题不是「黑话」，是「绕」：实测出过
# 「DeepSeek 开源 Agent 底座：把人类注意力从 3 到 5 个会话里解出来」——
# 主体、动作、数字都在，但「把 X 从 A 到 B 里解出来」这种句式读者要读两遍。
# 产业标题的职责是一句话交代「谁、干了什么、为什么反常」，不承担修辞。
MAX_INDUSTRY_TITLE = 34

_INDUSTRY_TWIST_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"把[^，。：:？]{1,16}从[^，。：:？]{1,16}(?:里|中)(?:解|抽|捞|摘|拔|拽)出"),
     "句式绕弯——「把 X 从 A 到 B 里解出来」需要读两遍，改成「主体 + 动作 + 结果」"),
    (re.compile(r"的(?:背后|另一面|真相)$|背后(?:的逻辑|是什么)"),
     "句式绕弯——「X 的背后」不承载信息，直接把结论写出来"),
    (re.compile(r"^(?:关于|聊聊|说说|谈谈)"), "空转开头——不要以「关于/聊聊」起手，直接说事"),
)
_INDUSTRY_EMPTY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"重磅|官宣|来袭|引爆|刷屏|沸腾|炸场"), "空话营销词——不承载任何事实"),
)
# 业界标题里出现论文/仓库统计，说明内容已经串题（Prompt 与清洗层都会拦，标题这里先拦一道）
_INDUSTRY_NOISE_TITLE = re.compile(
    r"(?:eccv|cvpr|iccv|neurips|arxiv)\s*(?:20\d{2})?\s*(?:论文|paper)|\d[\d,]*\s*个?\s*stars?",
    re.I,
)


def title_width(text: str) -> int:
    """公众号标题的「视觉字数」：汉字 1 格，连续的英文/数字算 1 格。

    直接 len() 会把「DeepSeek 开源 Agent 底座」算成 22 字，实际排出来只有 12 格，
    按 len 卡上限会把正常标题误判成超长。
    """
    cjk = len(re.findall(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]", text or ""))
    latin = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", text or ""))
    return cjk + latin


def industry_title_issues(title: str, item=None) -> list[str]:
    """业界标题的三闸自审：主体在不在、句式直不直、有没有空话。

    与赛事标题的区别：这里不查「规模 Hook」（产业新闻不一定有数字），
    只查「读者能不能一句话说清这条消息是什么」。
    """
    text = (title or "").strip()
    if not text:
        return ["标题为空"]
    issues: list[str] = []
    width = title_width(text)
    if width > MAX_INDUSTRY_TITLE:
        issues.append(f"标题约 {width} 字，超过 {MAX_INDUSTRY_TITLE} 字上限")
    for pattern, why in _INDUSTRY_TWIST_RULES:
        if pattern.search(text):
            issues.append(why)
    for pattern, why in _INDUSTRY_EMPTY_RULES:
        if pattern.search(text):
            issues.append(why)
    if _INDUSTRY_NOISE_TITLE.search(text):
        issues.append("标题里夹带论文/仓库统计，与产业事件无关")
    # 主体缺失：素材标题里点得出的品牌，成稿标题一个都没有 → 读者不知道是谁
    if item is not None:
        try:
            from ..filters.events import event_signature

            brands = event_signature(str(getattr(item, "title", "") or "")).brands
        except Exception:
            brands = frozenset()
        low = text.lower()
        if brands and not any(str(b).lower() in low for b in brands):
            issues.append("标题没有点出素材里的主体名（读者看不出是谁）")
    return issues


def industry_title_fallback(item) -> str:
    """没有大模型可用时的确定性兜底：用素材原题，剥掉媒体自己的营销前缀再截断。

    媒体标题常带「刚刚，」「重磅」「官宣」这类词——那是它们的流量写法，
    本号的口径是克制，所以兜底稿也要先洗一遍再上。
    """
    src = re.sub(r"\s+", " ", str(getattr(item, "title", "") or "")).strip()
    src = re.sub(r"^(?:刚刚|今天|昨日|昨夜|近日|突发)[，,、:：]\s*", "", src)
    src = re.sub(r"(?:重磅|官宣|突发|独家)", "", src)
    src = src.strip(" ！!。，,、")
    if not src:
        return ""
    if title_width(src) <= MAX_INDUSTRY_TITLE:
        return src
    cut = src[:MAX_INDUSTRY_TITLE]
    for mark in ("：", ":", "，", ",", "、", " "):
        pos = cut.rfind(mark)
        if pos >= 12:
            return cut[:pos]
    return cut
