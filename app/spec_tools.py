"""ArticleSpec 的后处理工具：章节标题/编号规范化 + 移动端表格列压缩。

为什么单独一个模块：这两件事都是「成稿之后的规格修正」，
而且要在三个环节共用——
  1. 组稿前：素材里的 9 列实验表先压成 5 列再喂给模型，模型才不会照抄超宽表；
  2. 成稿后：模型自己也可能拼出超宽表、或因省略某节留下编号断层，代码层强制修正；
  3. 排版前：渲染层再兜一次底，保证发出去的 HTML 里 SECTION 永远是 01,02,03…

设计原则：**只动结构，不动数字**。压缩表格只删列，不合并、不改写任何单元格，
避免把论文原文的数字改错。
"""
from __future__ import annotations

import re

# 手机端公众号正文可视宽度约 340–360pt：一列方法名 + 4 列指标是舒适上限，
# 再多就要横向滚动，阅读体验会明显变差。
MAX_TABLE_COLS = 5

# ------------------------------------------------------------------ 章节编号
# 只认「01 | 标题」「1｜标题」这类带编号的 h2；序号位数不限制，统一重排为两位。
_H2_NUM_RE = re.compile(r"^(\d{1,2})\s*[|｜]\s*(.+)$")

# 章节标题的正式叫法 ← 历史叫法/口语叫法。只收敛「同一节的不同写法」，
# 不做语义改写：栏目对收尾节的正式命名是「写在最后」，早期的「冷静一下」
# 偏口语，且容易让模型把这一节写成零散质疑而不是终局判断，因此统一归到正式名。
_SECTION_TITLE_ALIASES: dict[str, str] = {
    "冷静一下": "写在最后",
    "冷静想想": "写在最后",
    "冷静一点": "写在最后",
    "冷静看待": "写在最后",
    "写在结尾": "写在最后",
    "结语": "写在最后",
    "尾声": "写在最后",
    # 赛事 / Baseline 稿的 05 节：早期叫「社区复盘」，标题本身就在暗示搬运。
    # 现在统一为工程视角的正式名——文章讲的是主笔对方案的复现判断，不是社区帖子目录。
    "社区复盘": "复现要点：冠军方案的工程细节",
    "社区复盘：冠军选手怎么调": "复现要点：冠军方案的工程细节",
    "社区经验": "复现要点：冠军方案的工程细节",
    "社区方案复盘": "复现要点：冠军方案的工程细节",
    "社区方案": "复现要点：冠军方案的工程细节",
}

# 标题可能带副标题（「冷静一下：信息边界」），比对时只取主干
_TITLE_HEAD_RE = re.compile(r"[：:（(【\[—–\-/|｜]")


def _numbered_h2(block: dict) -> re.Match | None:
    if not isinstance(block, dict) or block.get("type") != "h2":
        return None
    return _H2_NUM_RE.match(str(block.get("text") or "").strip())


def normalize_section_titles(spec: dict) -> int:
    """把 h2 标题里的历史叫法收敛到正式名，返回被改写的节数。

    编号原样保留（编号连续性由 renumber_sections 负责），只改标题文字。
    若全篇已经有一节叫正式名，则不再把别的节改成同名，避免出现两个「写在最后」。
    """
    blocks = [b for b in (spec.get("sections") or []) if _numbered_h2(b)]
    taken = {m.group(2).strip() for m in (_numbered_h2(b) for b in blocks) if m}
    changed = 0
    for block in blocks:
        m = _numbered_h2(block)
        title = m.group(2).strip()
        head = _TITLE_HEAD_RE.split(title)[0].strip()
        canon = _SECTION_TITLE_ALIASES.get(title) or _SECTION_TITLE_ALIASES.get(head)
        if not canon or title == canon or canon in taken:
            continue
        block["text"] = f"{m.group(1)} | {canon}"
        taken.add(canon)
        changed += 1
    return changed


def renumber_sections(spec: dict) -> bool:
    """把 h2 小节的编号按出现顺序重排为 01,02,03…，消除跳号与重号。

    实测触发场景：单篇论文素材时「05 | 总结」按规则省略，模型却仍把最后一节写成
    「06 | 写在最后」，正文里就出现 04 → 06 的断层。这里不做任何语义判断，
    只认「第几个带编号的小节」——顺序对了，编号必然连续。
    返回是否发生了修改。
    """
    sections = spec.get("sections") or []
    if not any(_numbered_h2(b) for b in sections):
        return False
    changed = False
    seq = 0
    for block in sections:
        m = _numbered_h2(block)
        if not m:
            continue
        seq += 1
        new_text = f"{seq:02d} | {m.group(2).strip()}"
        if str(block.get("text") or "").strip() != new_text:
            block["text"] = new_text
            changed = True
    return changed


# ------------------------------------------------------------------ 元时间清洗
# 模型很爱写「当前日期 2026-10-28，距报名关闭只剩 3 天」这类句子：读者看的是推文，
# 不是编辑工作日历；而且这类日期一旦跨天发出就自相矛盾。Prompt 里已明令禁止，
# 这里再兜一层——命中即整句删除，不动其他数字。
_META_TIME_RE = re.compile(
    r"当前日期|截至今天|今天是\s*\d{4}|距(?:发稿|今天|推送)|发稿(?:时|日|前)|"
    r"本文(?:成稿|写于|发表于)|写作(?:时|于)|截稿(?:时|日)"
)
_SENT_SPLIT_RE = re.compile(r"[^。！？]*[。！？]|[^。！？]+$")


def strip_meta_time(spec: dict) -> int:
    """删掉正文里「当前日期/距发稿」这类元信息句，返回被删句数。

    只处理句子级的整句删除：一段里其他句子原样保留；删空了的块直接移除，
    避免留下空段落或空 bullet。
    """
    removed = 0
    sections = spec.get("sections") or []
    kept_blocks = []
    for block in sections:
        if not isinstance(block, dict):
            kept_blocks.append(block)
            continue
        btype = block.get("type")
        if btype in ("p", "quote", "highlight"):
            text = str(block.get("text") or "")
            if not _META_TIME_RE.search(text):
                kept_blocks.append(block)
                continue
            sentences = [s for s in _SENT_SPLIT_RE.findall(text) if s.strip()]
            kept = [s for s in sentences if not _META_TIME_RE.search(s)]
            removed += len(sentences) - len(kept)
            if not kept:
                continue  # 整块都是元信息，直接丢
            block["text"] = "".join(kept).strip()
            kept_blocks.append(block)
        elif btype in ("bullets", "numbered"):
            items = block.get("items") or []
            kept = [it for it in items
                    if not (isinstance(it, str) and _META_TIME_RE.search(it))]
            if len(kept) != len(items):
                removed += len(items) - len(kept)
                if not kept:
                    continue
                block["items"] = kept
            kept_blocks.append(block)
        else:
            kept_blocks.append(block)
    if removed:
        spec["sections"] = kept_blocks
    return removed


# 「3 天报名倒计时」「距报名截止只剩 2 天」这类标题尾巴：只在报名口已关时才清，
# 因为此时它一定与正文的「报名已结束」矛盾（赛程整体仍在 ≠ 还能报名）。
_TITLE_COUNTDOWN_RE = re.compile(
    r"[，、,;；:：\s—-]*\d+\s*天(?:报名)?倒计时"
    r"|[，、,;；:：]\s*报名(?:仅|只)?(?:还剩|剩|还有)\s*\d+\s*天"
    r"|[，、,;；:：]\s*距(?:报名|提交)?截止(?:仅|只)?(?:还剩|剩|还有)\s*\d+\s*天"
)
_TITLE_TAIL_RE = re.compile(r"[，、,;；:：\s—\-]+$")


def strip_countdown_title(spec: dict, registration_closed: bool) -> bool:
    """报名已关时摘掉标题里的倒计时短语，返回是否改动。

    只在「删完还剩一个像个标题的句子」时才动手（≥8 字），避免把标题削成半个词。
    """
    if not registration_closed:
        return False
    title = str(spec.get("title") or "")
    new = _TITLE_TAIL_RE.sub("", _TITLE_COUNTDOWN_RE.sub("", title)).strip()
    if new == title or len(new) < 8:
        return False
    spec["title"] = new
    return True


# ------------------------------------------------------------------ 措辞替换
# 「抄走 / 抄作业 / 照抄」这类词在公众号里既粗糙又容易引战，规范用词是「复用 / 借鉴 / 迁移」。
# Prompt 里已明令禁止，但模型会换个说法回来（「最值得抄作业的一处」），代码层兜底替换。
# 学术化文风（仅 paper 模块）：口语动词/比喻 → 顶会审稿人口径的标准表述。
# 赛事/业界稿里「最强对手」这类词是正常说法（榜单对比的不是论文方法），不参与替换。
_ACADEMIC_WORDING_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"代价写在效率上"), "方法的局限性主要体现在计算效率与参数量上"),
    (re.compile(r"([^。\n；]{0,24})是让出去的"), r"\1属于主动的定量取舍（Trade-off）"),
    (re.compile(r"什么场景(下)?会被反超"), "方法的适用场景与潜在局限"),
    (re.compile(r"什么场景(下)?会失手"), "哪类输入上会失效"),
    (re.compile(r"最强对手"), "最先进的方法"),
    (re.compile(r"一起给"), "一同提出"),
    (re.compile(r"讨巧"), "设计得颇为经济"),
    (re.compile(r"预处理后处理"), "预处理与后处理"),
    (re.compile(r"白捡的"), "无需额外开销的"),
    (re.compile(r"白捡"), "额外获得"),
    (re.compile(r"配角"), "附属环节"),
    (re.compile(r"[「“\"]教练[」”\"]"), "「辅助监督信号」"),
    (re.compile(r"[「“\"]教师[」”\"]"), "「辅助监督信号」"),
    (re.compile(r"死结"), "关键瓶颈"),
    (re.compile(r"性价比极高"), "综合收益显著"),
    (re.compile(r"最吸引人"), "最值得关注"),
    (re.compile(r"惊人"), "显著"),
    (re.compile(r"将表格数字翻译成结论"), "将表中结果归纳如下"),
    (re.compile(r"(?<!-)TITS(?![A-Za-z0-9-])"), "T-ITS"),
    (re.compile(r"\s*deepest\s+feature\s+map\s*", re.I), "最深层特征图"),
    (re.compile(r"[「“\"]免费[」”\"]"), "零成本"),
)

_WORDING_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"抄作业|抄走|照抄|抄袭|抄(?!送)"), "借鉴"),
    (re.compile(r"套用"), "迁移"),
    (re.compile(r"搬运|白嫖"), "复用"),
)

# 业界模块的夸张词替换：Prompt 已禁用，这里兜一道。只换口径，不动事实与数字。
_INDUSTRY_HYPE_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"震惊|炸裂|王炸|封神|史诗级|吊炸天|燃爆"), "值得注意"),
    (re.compile(r"彻底改变(?:了)?世界|重新定义(?:了)?行业|改写(?:了)?行业规则"), "改变了行业的成本结构"),
    (re.compile(r"遥遥领先"), "明显领先"),
    (re.compile(r"史上最强"), "当前最强"),
    (re.compile(r"颠覆性(?:的)?"), "结构性的"),
    (re.compile(r"碾压|吊打|秒杀|完爆"), "显著优于"),
)

# 赛事 / Baseline 模块的学术化替换：赛事稿过去只过通用禁用词，实测成稿里出现
# 「结论互相打架」「这个指标太卷了」「大佬们」「碾压」这类街头表达。整句改写给不出
# 通用规则，所以只做高频短语的等价替换——不改事实、不动数字，只换口径。
_CONTEST_ACADEMIC_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    # 「两个结论互相打架」→ 定量权衡矛盾（用户给的对照口径）
    (re.compile(r"(?:两边|两者|几组|多组)?(?:结论|指标|结果|榜单)(?:之间)?(?:互相|相互|彼此)?(?:打架|掐架)"
                r"|(?:两边|两者|几组)?(?:结论|指标)(?:之间)?(?:明显)?(?:互相)?矛盾(?:得很|得厉害)"),
     "不同评估指标之间存在明显的定量权衡矛盾（Quantitative Trade-off）"),
    (re.compile(r"(?:这个|该|这项)?(?:指标|榜单|赛道|方向)[^。，；]{0,4}"
                r"(?:太|有点|挺|特别|非常)?卷(?:了|得厉害|到不行|起来)?"),
     "该指标上的竞争已趋于饱和"),
    (re.compile(r"各位大佬|大佬们|大佬"), "头部选手"),
    # 被动式先处理：「被 SOTA 碾压」→「明显落后于 SOTA」，直接换成「显著优于」会读成病句
    (re.compile(r"被碾压"), "处于明显劣势"),
    (re.compile(r"被([^。，；]{1,10}?)碾压"), r"明显落后于\1"),
    (re.compile(r"碾压|吊打|秒杀|完爆|暴打"), "显著优于"),
    (re.compile(r"一顿操作猛如虎"), "多轮叠加的工程改造"),
    (re.compile(r"(?:特别|非常|超|很)能打"), "表现突出"),
    (re.compile(r"(?:特别|非常|超|很)猛(?!兽)"), "表现突出"),
)

# ---------------------------------------------------------------- 叙述纪律
# 本号定位是「只挑值得读的那几篇」：推送即已筛选，正文不能把「读不读」的选择题
# 推回给读者。Prompt 里已明令禁止，这里再兜一道——命中即改写或整句删除。
_READER_BURDEN_RE = re.compile(
    r"值不值得读|值不值得看|要不要读|读不读|"
    r"读者(?:自行)?(?:判断|取舍|决定)|自行判断(?:是否)?(?:值得|要读)"
)
_READER_RECAST_RE = re.compile(
    r"(?:它)?(?:到底)?值不值得(?:读|看)\s*[？?]?\s*[，,：:]?\s*取决于\s*"
    r"(?:你|读者|读者们|大家)?\s*"
)
# 素材处理过程的旁白：表格错位、原值呈现、抓取受限……属于内部工序，读者不该看到
_PROCESS_NARRATION_RE = re.compile(
    r"行列对齐|原值呈现|不做重排|重新对齐|素材表格|"
    r"表格[^。！？]{0,12}(?:错位|失真|杂乱|不齐|参差)|"
    r"(?:抓取|解析|抽取)[^。！？]{0,6}(?:限制|受限|失败|异常|错位|偏差)|"
    r"素材(?:有限|不足|缺失|不全)|"
    r"素材(?:中|里)?[^。！？]{0,8}(?:未给出|未提供|未披露|未列出|未找到|没有给出|没有提供)"
)


def _edit_sentences(spec: dict, edit) -> int:
    """按句改写正文（p/quote/highlight 与 bullets/numbered），edit 返回新句或 ""(删除)。

    返回被改动的句数；删空的段落/bullet 整块拿下，避免留下空行。
    """
    hits = 0
    sections = spec.get("sections") or []
    kept: list = []
    for block in sections:
        if not isinstance(block, dict):
            kept.append(block)
            continue
        btype = block.get("type")
        if btype in ("p", "quote", "highlight"):
            text = str(block.get("text") or "")
            if not text.strip():
                kept.append(block)
                continue
            sents = [s for s in _SENT_SPLIT_RE.findall(text) if s.strip()]
            new = [edit(s) for s in sents]
            hits += sum(1 for a, b in zip(sents, new) if a != b)
            body = "".join(x for x in new if x).strip()
            if not body:
                continue
            block["text"] = body
            kept.append(block)
        elif btype in ("bullets", "numbered"):
            items = block.get("items") or []
            new_items = []
            for it in items:
                if not isinstance(it, str):
                    new_items.append(it)
                    continue
                sents = [s for s in _SENT_SPLIT_RE.findall(it) if s.strip()]
                if not sents:
                    continue
                new = [edit(s) for s in sents]
                hits += sum(1 for a, b in zip(sents, new) if a != b)
                joined = "".join(x for x in new if x).strip()
                if joined:
                    new_items.append(joined)
            if not new_items:
                continue
            block["items"] = new_items
            kept.append(block)
        else:
            kept.append(block)
    if hits:
        spec["sections"] = kept
    return hits


def scrub_reader_burden(spec: dict) -> int:
    """清掉「值不值得读 / 读者自行判断」这类把筛选责任推给读者的话。

    有「取决于 X」的取舍信息 → 改写成客观陈述（「它的应用价值，依赖于人们是否…」）；
    只有一句反问、没有信息量的 → 整句删掉。lead 与 takeaway 一并处理。
    """
    def edit(sent: str) -> str:
        if not _READER_BURDEN_RE.search(sent):
            return sent
        m = _READER_RECAST_RE.search(sent)
        if m:
            return sent[:m.start()] + "它的应用价值，依赖于人们" + sent[m.end():]
        return ""

    hits = _edit_sentences(spec, edit)
    lead = spec.get("lead")
    if isinstance(lead, list):
        new_lead = []
        for para in lead:
            if not isinstance(para, str):
                new_lead.append(para)
                continue
            sents = [s for s in _SENT_SPLIT_RE.findall(para) if s.strip()]
            new = [edit(s) for s in sents]
            hits += sum(1 for a, b in zip(sents, new) if a != b)
            joined = "".join(x for x in new if x).strip()
            if joined:
                new_lead.append(joined)
        spec["lead"] = new_lead
    take = spec.get("takeaway")
    if isinstance(take, str) and take.strip():
        sents = [s for s in _SENT_SPLIT_RE.findall(take) if s.strip()]
        new = [edit(s) for s in sents]
        hits += sum(1 for a, b in zip(sents, new) if a != b)
        spec["takeaway"] = "".join(x for x in new if x).strip()
    return hits


def scrub_process_narration(spec: dict) -> int:
    """删掉「素材表格行列错位 / 按原值呈现 / 抓取受限」这类素材处理过程的旁白。

    数据本身照旧输出（表格块不动），只是不再向读者交代工序。
    """
    def edit(sent: str) -> str:
        return "" if _PROCESS_NARRATION_RE.search(sent) else sent

    hits = _edit_sentences(spec, edit)
    lead = spec.get("lead")
    if isinstance(lead, list):
        new_lead = []
        for para in lead:
            if not isinstance(para, str):
                new_lead.append(para)
                continue
            sents = [s for s in _SENT_SPLIT_RE.findall(para) if s.strip()]
            new = [edit(s) for s in sents]
            hits += sum(1 for a, b in zip(sents, new) if a != b)
            joined = "".join(x for x in new if x).strip()
            if joined:
                new_lead.append(joined)
        spec["lead"] = new_lead
    return hits


# ---------------------------------------------------------------- 业界主题纯粹性
# 实测事故：一篇「OpenAI 融资目标下调」的商业快讯，01 节里夹了一条
# 「ECCV 2026 论文 SparkVSR 开源，仓库 745 stars，最近更新 2026-08-03」。
# 它来自同批候选里的另一条素材——模型把「一期多条动态」写成了简报流水账。
# Prompt 已立「单一事件」铁律，这里按句/按条做确定性兜底：
#   ① 论文 / 仓库 / 星标 / 更新日期这类学术与代码库噪声，命中即删；
#   ② 提到「同批其它候选的主体」的句子，若带「同期/另有/此外」这类拼接词，整句删；
#   ③ 清单（bullets）是拼盘高发区，出现第二家公司就整条删，不给「顺带一提」留口子。
_INDUSTRY_NOISE_RE = re.compile(
    r"(?:arxiv|cvpr|iccv|eccv|neurips|iclr|icml|aaai|siggraph|tpami)\s*(?:20\d{2})?\s*(?:论文|paper)"
    r"|论文\s*[《「\"']?[A-Za-z][A-Za-z0-9\-]{2,}"
    r"|\d[\d,]*\s*个?\s*stars?\b"
    r"|stars?\s*[:：]\s*\d"
    r"|最近更新\s*20\d{2}\s*[-./年]\s*\d{1,2}"
    r"|github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+",
    re.I,
)
# 「同期 / 另有 / 此外」——出现这些词又在讲第二家公司，就是拼盘句式
_SECOND_NEWS_RE = re.compile(r"同期|与此同时|另有|此外|另外|同一时间|同一天|同日|顺带|另一方面")
# 对比语境：句子里同时出现主线主体和第二家公司、又用了这些词，属于有意义的横向比较，保留
_COMPARE_RE = re.compile(r"对标|相比|对比|相较|相比之下|高于|低于|领先|落后|远超|差距")
# 导语/摘要里的「还有另一件事」：单事件稿里出现就是拼盘，不必等第二个主体露名字
_ANOTHER_NEWS_RE = re.compile(
    r"同期还有|与此同时|另有|此外|另外|另一条|另一则|顺带|同一天还|同日还|另一方面")


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _main_blob(main_item) -> str:
    """主线的全部文本：标题 + 摘要 + 回抓正文。判断「这句话是不是素材外内容」的基准。"""
    extra = (getattr(main_item, "extra", {}) or {}) if main_item is not None else {}
    parts = [getattr(main_item, "title", "") or "", getattr(main_item, "summary", "") or "",
             str(extra.get("full_text") or "")]
    for src in extra.get("same_event_sources") or []:
        parts.append(str(src.get("title") or ""))
        parts.append(str(src.get("summary") or ""))
    return _plain(" ".join(parts))


def _main_names(main_item) -> set[str]:
    """主线自己的主体名：句子里没出现它、却在讲别的公司，就是串题。"""
    if main_item is None:
        return set()
    try:
        from .filters.events import entities, latin_tokens

        blob = f"{getattr(main_item, 'title', '') or ''} {getattr(main_item, 'summary', '') or ''}"
        names = set(entities(blob)) | set(latin_tokens(blob))
    except Exception:
        return set()
    return {str(n) for n in names if len(str(n)) >= 2}


def _mentions(text: str, names: set[str]) -> bool:
    low = _plain(text)
    return any(_plain(n) and _plain(n) in low for n in names)


def _rival_tokens(allowed: str, rivals) -> set[str]:
    """同批候选里「不是主线」的主体名：出现在正文里就是串题。"""
    out: set[str] = set()
    for item in rivals or []:
        if item is None:
            continue
        try:
            from .filters.events import entities, latin_tokens

            blob = f"{item.title or ''} {item.summary or ''}"
            names = set(entities(blob)) | set(latin_tokens(blob))
        except Exception:      # 实体抽取只是启发式，抽不出来就不参与串题判定
            continue
        for name in names:
            token = str(name).strip()
            if len(token) < 2:
                continue
            if _plain(token) and _plain(token) in allowed:
                continue          # 主线自己就提到过，不算串题
            out.add(token)
    return out


def _has_rival(text: str, rival_tokens: set[str]) -> str:
    low = _plain(text)
    for token in rival_tokens:
        if _plain(token) and _plain(token) in low:
            return token
    return ""


def _drop_empty_sections(spec: dict) -> int:
    """删掉删空的 h2（连同空标题一起），返回删掉的节数。"""
    blocks = spec.get("sections") or []
    kept: list = []
    dropped = 0
    for i, block in enumerate(blocks):
        if isinstance(block, dict) and block.get("type") == "h2":
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is None or (isinstance(nxt, dict) and nxt.get("type") in ("h2", "divider")):
                dropped += 1
                continue
        kept.append(block)
    if dropped:
        spec["sections"] = kept
    return dropped


def prune_industry_offtopic(spec: dict, main_item, rivals=None) -> dict:
    """业界稿的「零噪声」兜底：删掉跨界混入的论文/仓库/其它公司动态。

    返回各类删除计数（noise / rivals / cards / sources / sections），
    total 为 0 表示这篇本来就干净。只删不改——不动数字、不补句子。
    """
    stats = {"noise": 0, "rivals": 0, "cards": 0, "sources": 0, "sections": 0}
    allowed = _main_blob(main_item)
    rival_tokens = _rival_tokens(allowed, rivals)

    main_names = _main_names(main_item)

    def edit(sent: str) -> str:
        if _INDUSTRY_NOISE_RE.search(sent):
            stats["noise"] += 1
            return ""
        if not rival_tokens or not _has_rival(sent, rival_tokens):
            return sent
        # 拼接词（同期/此外）→ 删；整句只讲第二家公司、主线主体没露面 → 删；
        # 同时提到主线且在做横向比较 → 保留（那是有信息的判断，不是串题）
        if _SECOND_NEWS_RE.search(sent):
            stats["rivals"] += 1
            return ""
        if not _mentions(sent, main_names) and not _COMPARE_RE.search(sent):
            stats["rivals"] += 1
            return ""
        return sent

    _edit_sentences(spec, edit)

    # 清单式罗列：出现第二家公司/第二个项目就整条删
    kept_blocks: list = []
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") not in ("bullets", "numbered"):
            kept_blocks.append(block)
            continue
        items = [i for i in (block.get("items") or []) if isinstance(i, str)]
        kept = []
        for item_text in items:
            if _INDUSTRY_NOISE_RE.search(item_text):
                stats["noise"] += 1
                continue
            if rival_tokens and _has_rival(item_text, rival_tokens):
                stats["rivals"] += 1
                continue
            kept.append(item_text)
        if not kept:
            continue
        block["items"] = kept
        kept_blocks.append(block)
    spec["sections"] = kept_blocks

    # 信息卡：夹带「论文/仓库」的行直接删，卡片只留主线事件的事实
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "meta_card":
            continue
        rows = [r for r in (block.get("rows") or []) if isinstance(r, (list, tuple))]
        kept_rows = [list(r) for r in rows
                     if not _INDUSTRY_NOISE_RE.search(" ".join(str(c) for c in r))]
        if len(kept_rows) != len(rows):
            stats["cards"] += len(rows) - len(kept_rows)
            block["rows"] = kept_rows

    # 来源列表：github 仓库链接、以及主线没提过的其它项目，不属于本篇的引用
    sources = spec.get("sources")
    if isinstance(sources, list) and sources:
        kept_sources = []
        for entry in sources:
            if not isinstance(entry, dict):
                kept_sources.append(entry)
                continue
            blob = f"{entry.get('name') or ''} {entry.get('url') or ''}"
            if _INDUSTRY_NOISE_RE.search(blob):
                stats["sources"] += 1
                continue
            if rival_tokens and _has_rival(blob, rival_tokens):
                stats["sources"] += 1
                continue
            kept_sources.append(entry)
        spec["sources"] = kept_sources

    stats["sections"] = _drop_empty_sections(spec)

    # 导语 / 摘要 / takeaway 只清学术噪声：这三处若被整段误删，文章开头就没了
    def edit_head(sent: str) -> str:
        if _INDUSTRY_NOISE_RE.search(sent):
            stats["noise"] += 1
            return ""
        if _ANOTHER_NEWS_RE.search(sent):
            stats["rivals"] += 1
            return ""
        return sent

    lead = spec.get("lead")
    if isinstance(lead, list):
        kept_lead = []
        for para in lead:
            if not isinstance(para, str):
                kept_lead.append(para)
                continue
            sents = [s for s in _SENT_SPLIT_RE.findall(para) if s.strip()]
            joined = "".join(edit_head(s) for s in sents).strip()
            if joined:
                kept_lead.append(joined)
        spec["lead"] = kept_lead
    for key in ("digest", "takeaway"):
        if isinstance(spec.get(key), str):
            sents = [s for s in _SENT_SPLIT_RE.findall(spec[key]) if s.strip()]
            spec[key] = "".join(edit_head(s) for s in sents).strip()

    stats["total"] = sum(v for k, v in stats.items() if k != "total")
    return stats


# ---------------------------------------------------------------- 观点内化
# 赛事 / Baseline 稿的素材里混着社区复盘（CSDN / 知乎 / 公开复现记录）。Prompt 已立铁律：
# 外部解读必须消化成主笔判断，正文不许出现「社区复盘指出」「CSDN 博主总结」这类转述。
# 模型仍会漏一两句，这里做句子级兜底：**剥掉转述外壳、保留事实本体**，不改数字与判断。
_PLATFORM_RE = re.compile(r"CSDN|知乎|掘金|博客园|简书|Medium", re.I)

# 转述主语 + 转述动词：「社区复盘指出，」「CSDN 博主总结：」「检索结果显示，」
_ATTRIB_PREFIX_RES: tuple[re.Pattern, ...] = (
    re.compile(
        r"^\s*(?:据|根据|从|在)?\s*"
        r"(?:社区复盘|社区里|社区|技术社区|公开复现记录|赛后复盘|公开资料|检索结果|"
        r"论坛|帖子|网上|有人|有博主|有作者|有选手|不少选手|部分选手|"
        r"CSDN|知乎|掘金|博客园|简书)"
        r"[^，,。；;：:]{0,8}?"
        r"(?:博主|作者|文章|帖子|回答|用户|网友|专栏|问答|选手|队伍)?"
        r"[^，,。；;：:]{0,6}?"
        r"(?:指出|提到|总结|认为|发现|表明|强调|给出|表示|显示|说明|写道|称|说)"
        r"\s*[，,：:]?\s*"
    ),
    # 「正如 CSDN 上那篇高赞回答所说，」这类引述外壳
    re.compile(r"^\s*(?:正如|就像|正像|如同)[^，。；:：]{0,26}?(?:所说|所言|提到的|写到的|总结的)"
               r"\s*[，,：:]?\s*"),
    # 「据公开复现记录报道/披露，」
    re.compile(r"^\s*(?:据|根据)[^，。；:：]{0,20}?(?:报道|复盘|总结|介绍|披露|统计)\s*[，,：:]?\s*"),
)

# 「社区把频域自适应细化描述为低高频分离优化恢复」→「频域自适应的核心在于低高频分离优化恢复」
_REPHRASE_RE = re.compile(
    r"^(?P<pre>.*?)(?:社区复盘|社区|有博主|公开复现记录|CSDN\s*博主|知乎|不少选手|部分选手)"
    r"[^，。；]{0,6}?把(?P<obj>.{2,26}?)(?:细化)?(?:描述|总结|概括|归纳)为(?P<val>.+)$"
)
# 残留的平台名（句子中段没被前缀规则带走）：换成中性表述，绝不把平台名留在正文里。
# 直接删会留下「在 的复现贴里」这种语法空洞；保留介词与方位词，读起来仍是通顺的中文。
_PLATFORM_LEFTOVER_RE = re.compile(
    r"(?P<pre>据|根据|从|在)?\s*(?:CSDN|知乎|掘金|博客园|简书)(?:网|平台)?\s*"
    r"(?P<loc>上|中|里)?\s*(?:的)?\s*(?:博主|作者|文章|帖子|回答|用户|网友|专栏|问答|复现贴|技术贴)?",
    re.I,
)


def _neutralize_platform(match: re.Match) -> str:
    """把平台名换成中性主语，同时保留原句的介词与方位词（在…上 / 据…）。"""
    return f"{match.group('pre') or ''}公开复现记录{match.group('loc') or ''}"


def _tidy_sentence(text: str) -> str:
    """清掉剥壳后留下的悬空标点与双空格。"""
    out = re.sub(r"\s{2,}", " ", text or "").strip()
    out = re.sub(r"^[，,、；;：:\s]+", "", out)
    out = re.sub(r"([，,、；;：:])\1+", r"\1", out)
    return out


def _internalize_sentence(sent: str) -> str:
    """单句内化：反复剥转述外壳（最多 3 轮），直到句子变成直述。"""
    out = sent
    for _ in range(3):
        before = out
        m = _REPHRASE_RE.match(out)
        if m:
            out = f"{m.group('pre')}{m.group('obj')}的核心在于{m.group('val')}"
            continue
        for pat in _ATTRIB_PREFIX_RES:
            new = pat.sub("", out, count=1)
            if new != out:
                out = new
                break
        out = _PLATFORM_LEFTOVER_RE.sub(_neutralize_platform, out)
        if out == before:
            break
    return _tidy_sentence(out)


def _edit_lead_takeaway(spec: dict, edit) -> int:
    """lead 与 takeaway 也要过同一套句级改写（它们不属于 sections）。"""
    hits = 0
    lead = spec.get("lead")
    if isinstance(lead, list):
        new_lead = []
        for para in lead:
            if not isinstance(para, str):
                new_lead.append(para)
                continue
            sents = [s for s in _SENT_SPLIT_RE.findall(para) if s.strip()]
            new = [edit(s) for s in sents]
            hits += sum(1 for a, b in zip(sents, new) if a != b)
            joined = "".join(x for x in new if x).strip()
            if joined:
                new_lead.append(joined)
        spec["lead"] = new_lead
    take = spec.get("takeaway")
    if isinstance(take, str) and take.strip():
        sents = [s for s in _SENT_SPLIT_RE.findall(take) if s.strip()]
        new = [edit(s) for s in sents]
        hits += sum(1 for a, b in zip(sents, new) if a != b)
        spec["takeaway"] = "".join(x for x in new if x).strip()
    return hits


def scrub_community_attribution(spec: dict) -> int:
    """把社区转述改写成主笔直述，返回被改写的句数。

    两类命中：
    1. 转述外壳（「社区复盘指出，X」「CSDN 博主总结：X」「正如某篇回答所说，X」）→ 只留 X；
    2. 二手改写（「社区把 A 描述为 B」）→「A 的核心在于 B」。
    改完之后正文里不应再出现 CSDN / 知乎等平台名（它们只属于 sources）。
    """
    hits = _edit_sentences(spec, _internalize_sentence)
    hits += _edit_lead_takeaway(spec, _internalize_sentence)
    # 兜底：万一平台名藏在 table/caption/highlight 的角落里，直接抹掉（正文不留平台名）
    for block in spec.get("sections") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("caption"), str) and _PLATFORM_RE.search(block["caption"]):
            block["caption"] = _PLATFORM_LEFTOVER_RE.sub(_neutralize_platform, block["caption"])
            hits += 1
    return hits


def normalize_wording(spec: dict, module: str | None = None) -> int:
    """把禁用措辞替换成规范说法，返回命中次数。只改用词，不动事实与结构。

    module="paper" 时追加论文口语比喻的学术化替换；
    module="contest" 时追加赛事 / Baseline 的街头表达替换（打架 / 太卷 / 大佬 / 碾压）；
    其余模块只过通用禁用词。
    """
    hits = 0
    fixes = _WORDING_FIXES
    if module == "paper":
        fixes = fixes + _ACADEMIC_WORDING_FIXES
    elif module == "contest":
        fixes = fixes + _CONTEST_ACADEMIC_FIXES
    elif module == "industry":
        fixes = fixes + _INDUSTRY_HYPE_FIXES

    def _fix(text: str) -> str:
        nonlocal hits
        out = text
        for pat, repl in fixes:
            out, n = pat.subn(repl, out)
            hits += n
        return out

    for block in spec.get("sections") or []:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("text"), str):
            block["text"] = _fix(block["text"])
        if isinstance(block.get("items"), list):
            block["items"] = [_fix(i) if isinstance(i, str) else i for i in block["items"]]
        if isinstance(block.get("caption"), str):
            block["caption"] = _fix(block["caption"])
    if isinstance(spec.get("lead"), list):
        spec["lead"] = [_fix(p) if isinstance(p, str) else p for p in spec["lead"]]
    if isinstance(spec.get("takeaway"), str):
        spec["takeaway"] = _fix(spec["takeaway"])
    return hits


def normalized_spec(spec: dict, module: str | None = None) -> dict:
    """返回「标题已规范、编号已连续、信息卡已收敛、元时间已剔除」的浅拷贝。

    module 传业界时会顺带把产业事件卡的主体/事件行合并成一行——渲染层与成稿出口
    共用这一份逻辑，避免预览与推文长得不一样。
    """
    sections = spec.get("sections") or []
    needs_numbering = any(_numbered_h2(b) for b in sections)
    needs_cards = module == "industry" and any(
        isinstance(b, dict) and b.get("type") == "meta_card" for b in sections)
    if not needs_numbering and not needs_cards and module != "contest":
        return spec
    clone = dict(spec)
    clone["sections"] = [dict(b) if isinstance(b, dict) else b for b in sections]
    if needs_cards:
        normalize_meta_cards(clone, module)
    normalize_section_titles(clone)
    renumber_sections(clone)
    strip_meta_time(clone)
    return clone


# ------------------------------------------------------------------ 信息卡
CARD_LABELS = ("PAPER INFO", "PROJECT INFO", "INDUSTRY SNAPSHOT",
               "COMPETITION INFO", "EVENT SNAPSHOT", "CONTEST INFO")
# 旧稿里写的是 CONTEST INFO，新规范拆成「赛题精读」与「赛事快照」两种，读到时映射过去
_LABEL_ALIASES = {"CONTEST INFO": "COMPETITION INFO"}
# 卡片行里出现这些词，说明这张卡在讲赛题本身（任务/数据/指标），而不是时间与奖项
_COMPETITION_HINTS = ("赛题", "任务", "数据", "指标", "评测", "赛道", "评分", "提交", "模型")

# 行内容出现这些词，说明主体是仓库/工具链，卡片标题该是 PROJECT INFO
# 注意「权重」不能单独作特征词：产业新闻里「开放权重」是高频说法，
# 会把「Runway 开源开放权重模型」这类事件卡误判成开源项目卡
_PROJECT_HINTS = (
    "github", "repo", "仓库", "开源", "stars", "star", "星标", "权重文件", "huggingface",
    "hugging face", "license", "许可", "sdk", "工具链", "项目地址", "代码仓库", "显存",
)

# 产业事件卡的行名。主体与事件原本分两行写（「主体：Runway」「事件：发布 Praxis-1」），
# 第一行只有一个公司名、第二行才见动作，卡片密度被浪费；规模/金额再单独占一行更散。
# 现在统一并成「事件：Runway 发布 Praxis-1」，金额折进括号里。
_SUBJECT_LABELS = ("主体", "公司", "机构", "厂商", "团队", "发布方", "事件主体", "当事方")
_EVENT_LABELS = ("事件", "动作", "发生了什么", "发布内容", "合作内容", "核心事件")
_AMOUNT_HINTS = ("规模", "金额", "融资", "估值", "交易额", "营收")


def resolve_card_label(block: dict, module: str) -> str:
    """信息卡标题自适应：论文 / 开源项目 / 产业快照 / 赛事。

    为什么不能写死 PAPER INFO：业界稿里一张「GitHub 仓库 + stars + 许可证」的卡
    顶着 PAPER INFO，读者第一眼就出戏。模型显式给了合法 label 就照用；
    否则按模块 + 行内容兜底——行里出现仓库/星标/许可证这类词，判为 PROJECT INFO。
    """
    explicit = str(block.get("label") or "").strip().upper()
    explicit = _LABEL_ALIASES.get(explicit, explicit)
    if explicit in CARD_LABELS:
        return explicit
    rows = [r for r in (block.get("rows") or []) if isinstance(r, (list, tuple)) and len(r) >= 2]
    text = " ".join(f"{r[0]} {r[1]}" for r in rows).lower()
    # 「主体 / 事件 / 规模」这种行结构本身就说明是产业事件卡：
    # 事件里出现「开源」「权重」的词很常见，不能因此判成开源项目卡
    is_event_card = any(_row_label(r) in _SUBJECT_LABELS + _EVENT_LABELS for r in rows)
    is_project = (not is_event_card) and any(hint in text for hint in _PROJECT_HINTS)

    if module == "industry":
        return "PROJECT INFO" if is_project else "INDUSTRY SNAPSHOT"
    if module == "paper":
        return "PAPER INFO"
    if module == "contest":
        # 赛题精读卡讲「任务/数据/指标」→ COMPETITION INFO；
        # 只写时间、奖金、入口的赛程卡 → EVENT SNAPSHOT
        is_competition = any(hint in text for hint in _COMPETITION_HINTS)
        return "COMPETITION INFO" if is_competition else "EVENT SNAPSHOT"
    return "PROJECT INFO" if is_project else "PAPER INFO"


def _row_label(row) -> str:
    return str(row[0] or "").strip().rstrip("：:")


def _is_amount_label(label: str) -> bool:
    return any(hint in label for hint in _AMOUNT_HINTS)


def _join_subject_event(subject: str, event: str) -> str:
    """「Runway」+「发布 Praxis-1」→「Runway 发布 Praxis-1」。

    英文主体与中文动作之间补一个空格：不补会出现「Runway发布」，读起来像拼写错误。
    """
    a, b = str(subject or "").strip(), str(event or "").strip()
    if not a:
        return b
    if not b or b.startswith(a):
        return b or a
    if a[-1].isascii() and a[-1].isalnum() and not b[0].isascii():
        return f"{a} {b}"
    return f"{a}{b}"


def merge_event_rows(rows: list) -> tuple[list, bool]:
    """把产业事件卡合并成「事件」单行，去掉独立的规模/金额行。

    只处理「主体 + 事件」这种两行结构；金额/规模行的数字不丢，折进合并句的括号里
    （「Praxis-1 开放权重（0.95 相关性）」），读者照样看得到量级。
    """
    valid = [list(r) for r in (rows or []) if isinstance(r, (list, tuple)) and len(r) >= 2]
    if not valid:
        return rows, False
    subject = next((r for r in valid if _row_label(r) in _SUBJECT_LABELS), None)
    event = next((r for r in valid if _row_label(r) in _EVENT_LABELS), None)
    amounts = [r for r in valid if _is_amount_label(_row_label(r))]
    if not amounts and not (subject and event):
        return rows, False

    base = ""
    if subject and event:
        base = _join_subject_event(subject[1], event[1])
    elif event:
        base = str(event[1]).strip()
    if not base:
        return rows, False

    merged = base
    for row in amounts:
        value = str(row[1] or "").strip()
        if value and value not in merged:
            merged = f"{merged}（{value}）"

    out: list = []
    placed = False
    for row in valid:
        label = _row_label(row)
        if _is_amount_label(label):
            continue
        if row is subject or row is event:
            if not placed:
                out.append(["事件", merged])
                placed = True
            continue
        out.append(row)
    if not placed:
        return rows, False
    return out, True


def normalize_meta_cards(spec: dict, module: str) -> int:
    """成稿兜底：产业事件卡合并主体/事件行、去掉规模行。返回被改动的卡数。"""
    if module != "industry":
        return 0
    changed = 0
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "meta_card":
            continue
        if resolve_card_label(block, module) != "INDUSTRY SNAPSHOT":
            continue  # 开源项目卡（PROJECT INFO）保留原有字段，不参与合并
        new_rows, ok = merge_event_rows(block.get("rows") or [])
        if ok:
            block["rows"] = new_rows
            changed += 1
    return changed


# ------------------------------------------------------------------ 期刊/会议简称
# 标题里写期刊全称既冗长又不像学术圈内部说话：国际通用做法是直接写简称。
# 全称留给信息卡（可检索、可溯源），标题只出现简称。
# 键统一小写、连字符统一为「-」；按长度降序匹配，避免「Pattern Recognition Letters」
# 先被「Pattern Recognition」截断。
_VENUE_ABBREV: dict[str, str] = {
    "ieee transactions on pattern analysis and machine intelligence": "TPAMI",
    "ieee transactions on circuits and systems for video technology": "TCSVT",
    "ieee transactions on neural networks and learning systems": "TNNLS",
    "ieee transactions on geoscience and remote sensing": "TGRS",
    "ieee transactions on computational imaging": "TCI",
    "ieee transactions on medical imaging": "TMI",
    "ieee transactions on image processing": "TIP",
    "ieee transactions on intelligent transportation systems": "T-ITS",
    "ieee transactions on multimedia": "TMM",
    "international journal of computer vision": "IJCV",
    "computer vision and image understanding": "CVIU",
    "pattern recognition letters": "PRL",
    "pattern recognition": "PR",
    "knowledge-based systems": "KBS",
    "medical image analysis": "MedIA",
}
_VENUE_KEYS = sorted(_VENUE_ABBREV, key=len, reverse=True)
# 已经是简称的顶会/顶刊：标题里保持原样，不再做任何替换
_SHORT_VENUES = {v.upper() for v in _VENUE_ABBREV.values()} | {
    "CVPR", "ICCV", "ECCV", "NEURIPS", "NIPS", "ICML", "ICLR", "AAAI", "IJCAI",
    "SIGGRAPH", "MM", "BMVC", "WACV", "MICCAI", "ICIP", "ICASSP", "3DV",
}
_VENUE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
# 标题开头的「简称 + 年份 + 分隔符」：用于把「PR 2026｜X」统一成「PR 2026 | X」
_LEAD_VENUE_LABEL_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z&.\-]{1,14})\s*[,，]?\s*((?:19|20)\d{2})?\s*[|｜:：]\s*")


def venue_abbrev(venue: str) -> str:
    """期刊/会议全称 → 国际通用简称；没把握的 venue 返回空串（调用方保留全称）。"""
    text = re.sub(r"[\u2010-\u2015]", "-", str(venue or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(",，.。;；")
    text = re.sub(r"\s+", " ", _VENUE_YEAR_RE.sub(" ", text)).strip(" ,，")
    if not text:
        return ""
    if text.upper() in _SHORT_VENUES:
        return text.upper()
    for key in _VENUE_KEYS:
        if key in text.lower():
            return _VENUE_ABBREV[key]
    return ""


def venue_short_label(venue: str, year: str = "") -> str:
    """「Pattern Recognition 2026」→「PR 2026」；无映射的 venue 原样保留。"""
    raw = str(venue or "").strip()
    if not raw:
        return ""
    m = _VENUE_YEAR_RE.search(raw)
    yr = m.group(0) if m else str(year or "").strip()
    base = re.sub(r"[\s,，]+$", "", _VENUE_YEAR_RE.sub(" ", raw)).strip(" ,，")
    name = venue_abbrev(base) or base
    return f"{name} {yr}".strip() if yr else name


def venue_card_label(venue: str, year: str = "") -> str:
    """信息卡的期刊/会议行：全称在前、简称跟括号里，兼顾专业度与可检索性。"""
    raw = str(venue or "").strip()
    if not raw:
        return str(year or "").strip()
    ab = venue_abbrev(raw)
    if not ab:
        return f"{raw} {year}".strip() if year and not _VENUE_YEAR_RE.search(raw) else raw
    if re.search(rf"(?<![A-Za-z]){re.escape(ab)}(?![A-Za-z])", raw, re.I):
        return raw
    return f"{raw}（{ab}）"


def abbrev_title_venue(title: str) -> str:
    """标题里的期刊/会议全称换成国际通用简称，顺带统一分隔符。

    「Pattern Recognition 2026｜X」→「PR 2026 | X」；已用简称的标题只规范分隔符；
    没有 venue 标签的标题（「无惧复杂失真！…」）原样返回。
    """
    out = str(title or "")
    if not out.strip():
        return out
    lowered = out.lower()
    for key in _VENUE_KEYS:
        idx = lowered.find(key)
        if idx < 0:
            continue
        # 只替换「后面紧跟年份或分隔符」的独立出现，避免误伤正文里的同名短语；
        # 「Pattern Recognition (PR) 2026」这种自带括注的写法也认
        tail = out[idx + len(key):]
        if tail and not re.match(
                r"^\s*[,，]?\s*(?:[（(]\s*[A-Za-z&.\- ]{1,20}\s*[)）])?\s*"
                r"(?:(?:19|20)\d{2})?\s*(?:[|｜:：]|$)", tail):
            continue
        out = out[:idx] + _VENUE_ABBREV[key] + tail
        # 「PR（PR）2026」这类全称+括注替换后的冗余括注直接去掉
        out = re.sub(
            rf"^(\s*{re.escape(_VENUE_ABBREV[key])})\s*[（(]\s*"
            rf"{re.escape(_VENUE_ABBREV[key])}\s*[)）]", r"\1", out, flags=re.I)
        break
    m = _LEAD_VENUE_LABEL_RE.match(out)
    if m and m.group(1).upper() in _SHORT_VENUES:
        label = m.group(1).upper()
        if m.group(2):
            label = f"{label} {m.group(2)}"
        out = f"{label} | {out[m.end():].strip()}"
    return out


# ------------------------------------------------------------------ 论文元数据卡
# 论文稿开篇必须能溯源：论文原名（英文照抄）/ 发表期刊或会议 / 作者团队。
# 行名的历史写法五花八门（论文标题、期刊/会议、作者…），统一收敛到下面三个正式行名。
_PAPER_META_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("论文名称", ("论文名称", "论文标题", "论文题目", "论文名", "标题")),
    ("发表期刊/会议", ("发表期刊/会议", "期刊/会议", "发表期刊", "发表会议", "期刊", "会议", "收录", "venue")),
    ("作者团队", ("作者团队", "作者", "论文作者", "作者列表", "作者信息")),
)


_META_LABEL_PREFIX_RE = re.compile(r"^[0-9０-９一二三四五六七八九十]+\s*[、.．|｜)）:：]?\s*")


# 信息卡里的占位值（「官方未公布」之流）不算信息，整行删掉：读者要的是能检索的事实。
_PLACEHOLDER_VALUE_RE = re.compile(
    r"^(?:官方)?\s*(?:未公布|未披露|未提供|未注明|未提及|未知|待定|待补充|暂无|无|n/?a|[-—–…]+)\s*[。.]?$",
    re.I,
)


def _meta_kind(label: str) -> str:
    key = str(label or "").strip().strip("：: 　|｜")
    key = _META_LABEL_PREFIX_RE.sub("", key).strip().strip("：: 　|｜").lower()
    for canonical, aliases in _PAPER_META_LABELS:
        if key == canonical.lower() or key in (a.lower() for a in aliases):
            return canonical
    return ""


def _authors_line(authors: list) -> str:
    """信息卡的作者团队行：固定写成「由 A、B、C 等研究人员共同完成」。

    裸人名列表读起来像通讯录，且模型偶尔写成「作者团队来自 A、B」（人名挂「来自」后
    是语法硬伤）；这里直接落成唯一合法句式，超过 6 人只列前 6 位。
    """
    names = [str(a).strip() for a in (authors or []) if str(a).strip()]
    if not names:
        return ""
    return "由 " + "、".join(names[:6]) + " 等研究人员共同完成"


def paper_meta_values(item) -> dict[str, str]:
    """从素材里取三行元数据的值；取不到的留空，由调用方跳过该行。"""
    if item is None:
        return {}
    extra = getattr(item, "extra", None) or {}
    pub = extra.get("publication") or {}
    venue = str(pub.get("venue") or getattr(item, "venue", "") or "").strip()
    if not venue:
        haystack = f"{getattr(item, 'url', '')} {getattr(item, 'source', '')}".lower()
        venue = "arXiv 预印本" if "arxiv" in haystack else "以原文标注为准"
    return {
        "论文名称": str(getattr(item, "title", "") or "").strip(),
        "发表期刊/会议": venue_card_label(venue, getattr(item, "year", "") or ""),
        "作者团队": _authors_line(getattr(item, "authors", None)),
    }


def ensure_paper_meta_card(spec: dict, item) -> int:
    """成稿兜底：论文正文开篇必须有信息卡，且三行必选项齐全、置顶、行名规范。

    模型实测会把信息卡整块漏掉（正文直接进 01 节），读者无法检索溯源；
    这里按素材补卡、补行、并把三行排到最前。返回改动处数。
    """
    if item is None:
        return 0
    values = paper_meta_values(item)
    if not values.get("论文名称"):
        return 0
    sections = spec.get("sections")
    if not isinstance(sections, list):
        return 0
    card = next((b for b in sections if isinstance(b, dict) and b.get("type") == "meta_card"), None)
    if card is None:
        rows = [[k, values[k]] for k, _aliases in _PAPER_META_LABELS if values.get(k)]
        extra_rows = []
        code = str((getattr(item, "code_url", "") or "")).strip()
        if code:
            extra_rows.append(["开源代码", code])
        sections.insert(0, {"type": "meta_card", "label": "PAPER INFO",
                            "rows": rows + extra_rows})
        return 1

    rows = [list(r) for r in (card.get("rows") or [])
            if isinstance(r, (list, tuple)) and len(r) >= 2]
    kept = [r for r in rows if not _PLACEHOLDER_VALUE_RE.match(str(r[1]).strip())]
    changed = len(rows) - len(kept)
    rows = kept
    for canonical, _aliases in _PAPER_META_LABELS:
        found = next((r for r in rows if _meta_kind(r[0]) == canonical), None)
        if found is None:
            if values.get(canonical):
                rows.append([canonical, values[canonical]])
                changed += 1
            continue
        if str(found[0]).strip() != canonical:
            found[0] = canonical
            changed += 1
        truth = values.get(canonical) or ""
        if truth and truth != "以原文标注为准" and str(found[1]).strip() != truth:
            found[1] = truth
            changed += 1
    head = [r for canonical, _a in _PAPER_META_LABELS
            for r in rows if _meta_kind(r[0]) == canonical][:len(_PAPER_META_LABELS)]
    ordered = head + [r for r in rows if all(r is not h for h in head)]
    if [id(r) for r in ordered] != [id(r) for r in rows]:
        changed += 1
    if changed:
        card["rows"] = ordered
        if card.get("label") in (None, ""):
            card["label"] = "PAPER INFO"
    return changed


# ------------------------------------------------------------------ 图文混排硬约束
_ARCH_CAPTION_HINTS = ("架构", "框架", "流程", "总体", "概览", "overview", "architecture",
                       "pipeline", "framework", "flowchart", "schematic", "workflow")


def _section_bounds(sections: list, needles: tuple[str, ...]) -> tuple[int, int]:
    start = None
    for i, block in enumerate(sections):
        if not isinstance(block, dict) or block.get("type") != "h2":
            continue
        text = str(block.get("text") or "")
        if any(n in text for n in needles):
            start = i
            break
    if start is None:
        return -1, -1
    end = len(sections)
    for j in range(start + 1, len(sections)):
        if isinstance(sections[j], dict) and sections[j].get("type") == "h2":
            end = j
            break
    return start, end


def _item_figure_refs(item, figures: dict) -> list[str]:
    if item is None or not figures:
        return []
    key = f"{getattr(item, 'module', 'paper')}:{getattr(item, 'fingerprint', '')}"
    return [ref for ref, meta in figures.items() if meta.get("item_id") == key]


def _placed_figures(sections: list) -> int:
    return sum(1 for b in sections if isinstance(b, dict) and b.get("type") == "figure")


def _resolved_figure(block, figures: dict) -> bool:
    if not isinstance(block, dict) or block.get("type") != "figure":
        return False
    return bool(figures) and str(block.get("ref") or "") in figures


def scrub_dangling_figures(spec: dict, figures: dict) -> int:
    """删掉指向不存在素材的 figure 块。

    模型会自己写一个 `{"ref": "Fig.1"}` 占位，素材里根本没有这张图——渲染出来
    是 `![]()` 空图，比不放图更糟。返回删掉的块数。
    """
    sections = spec.get("sections")
    if not isinstance(sections, list):
        return 0
    kept = [b for b in sections if not (
        isinstance(b, dict) and b.get("type") == "figure"
        and not _resolved_figure(b, figures))]
    dropped = len(sections) - len(kept)
    if dropped:
        spec["sections"] = kept
    return dropped


def ensure_paper_arch_figure(spec: dict, item, figures: dict) -> int:
    """「核心创新与方法拆解」节必须有 figure 块；模型漏引时按素材自动补一张架构图。

    返回补入的图数（0/1）。素材里没有可用的架构图时什么都不做——绝不凭空造图。
    """
    sections = spec.get("sections")
    if not isinstance(sections, list):
        return 0
    start, end = _section_bounds(sections, ("核心创新", "方法拆解", "模型与", "架构"))
    if start < 0:
        return 0
    if any(_resolved_figure(b, figures) for b in sections[start + 1:end]):
        return 0
    refs = _item_figure_refs(item, figures)
    if not refs:
        return 0
    ref = next((r for r in refs
                if any(h in str((figures.get(r) or {}).get("caption") or "").lower()
                       for h in _ARCH_CAPTION_HINTS)), refs[0])
    raw = str((figures.get(ref) or {}).get("caption") or "").strip()
    number = _placed_figures(sections) + 1
    caption = raw if re.match(r"^图\s*\d", raw) else f"图 {number}：{raw or '论文方法总体架构。'}"
    insert_at = start + 1
    for i in range(start + 1, end):
        if isinstance(sections[i], dict) and sections[i].get("type") in ("p", "bullets"):
            insert_at = i + 1
            break
    sections.insert(insert_at, {"type": "figure", "ref": ref, "caption": caption})
    return 1


def ensure_paper_result_table(spec: dict, item) -> int:
    """「关键实验结果解读」节必须有 table 块；模型漏写时按素材原表补一张。

    只补素材里真实存在的表（数字可溯源），不做任何改写。返回补入的表数（0/1）。
    """
    sections = spec.get("sections")
    if not isinstance(sections, list):
        return 0
    start, end = _section_bounds(sections, ("实验结果", "实验与结果", "实验验证", "结果解读"))
    if start < 0:
        return 0
    if any(isinstance(b, dict) and b.get("type") == "table" for b in sections[start + 1:end]):
        return 0
    tables = [t for t in ((getattr(item, "extra", None) or {}).get("tables") or [])
              if isinstance(t, dict)]
    if not tables:
        return 0
    table = compact_table(tables[0])
    header = table.get("header") or []
    rows = table.get("rows") or []
    if not header or not rows:
        return 0
    sections.insert(end, {"type": "table", "header": header, "rows": rows})
    return 1


def paper_asset_gaps(spec, figures) -> dict:
    """成稿红线：核心创新节必须真有结构图，实验结果节必须真有对比表。"""
    sections = spec.get("sections") if isinstance(spec, dict) else None
    if not isinstance(sections, list):
        return {"figure": True, "table": True}

    def _filled(value) -> bool:
        """header / rows 是否真有内容：dict 形式的表头按内容判，不让类型差异抛错。"""
        if isinstance(value, dict):
            return any(_filled(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(_filled(v) for v in value)
        return bool(str(value).strip())

    fig_start, fig_end = _section_bounds(sections, ("核心创新", "方法拆解", "模型与", "架构"))
    figure_gap = fig_start < 0 or not any(
        _resolved_figure(b, figures) for b in sections[fig_start + 1:fig_end])

    tbl_start, tbl_end = _section_bounds(sections, ("实验结果", "实验与结果", "实验验证", "结果解读"))
    table_gap = tbl_start < 0 or not any(
        isinstance(b, dict) and b.get("type") == "table"
        and _filled(b.get("header")) and _filled(b.get("rows"))
        for b in sections[tbl_start + 1:tbl_end])

    return {"figure": figure_gap, "table": table_gap}


# ------------------------------------------------------------------ 作者表述语法
# 「作者团队来自 Weihang Kong、Jienan Shen…」是人名直接挂在「来自」后面的语法硬伤。
# 「来自」后面只能接机构；接人名必须写成「由 A、B 等人组成」。
_AUTHOR_ORIGIN_RE = re.compile(
    r"(作者团队|本文作者|论文作者|作者们|作者|研究团队|研究组|团队)"
    r"(?:均|分别|共同)?来自\s*([^。；！？\n]{2,120})")
_AFFILIATION_HINTS = ("大学", "学院", "研究所", "研究院", "实验室", "公司", "集团", "中心",
                      "university", "institute", "laboratory", "lab", "inc", "ltd",
                      "college", "school", "academy")
_PERSON_LIST_RE = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z][\w.\-']+)+")


def recast_author_origin(sent: str) -> str:
    """把「作者团队来自 A、B」改写成「作者团队由 A、B 等人组成」；机构名不动。"""
    def sub(m: re.Match) -> str:
        team, tail = m.group(1), m.group(2).strip()
        if any(h in tail.lower() for h in _AFFILIATION_HINTS):
            return m.group(0)
        names = re.sub(r"[，,]?\s*等(?:人|研究人员)?\s*$", "", tail).strip(" ，,")
        if not names:
            return m.group(0)
        if not (_PERSON_LIST_RE.search(names) or ("、" in names and len(names) <= 80)):
            return m.group(0)
        return f"{team}由 {names} 等人组成"

    return _AUTHOR_ORIGIN_RE.sub(sub, sent)


def scrub_paper_author_grammar(spec: dict) -> int:
    """成稿兜底：正文与导语里的「来自 + 人名」全部改写成合规表述。"""
    hits = _edit_sentences(spec, recast_author_origin)
    lead = spec.get("lead")
    if isinstance(lead, list):
        for i, para in enumerate(lead):
            if not isinstance(para, str) or not para.strip():
                continue
            new = recast_author_origin(para)
            if new != para:
                lead[i] = new
                hits += 1
    return hits


# ------------------------------------------------------------------ 表格压缩
# 指标字典：关键词 → (类别, 类别内优先级)。类别用于「精度 / 误差 / 成本」轮转取样，
# 保证压缩后的表同时覆盖效果与代价，而不是留下一堆同质指标。
_METRIC_ORDER: dict[str, tuple[str, int]] = {
    # 保真与感知质量
    "psnr": ("quality", 0),
    "ws-psnr": ("quality", 1),
    "ssim": ("quality", 2),
    "ws-ssim": ("quality", 3),
    "ms-ssim": ("quality", 4),
    "lpips": ("quality", 5),
    "dists": ("quality", 6),
    "fid": ("quality", 7),
    "maniqa": ("quality", 8),
    "musiq": ("quality", 9),
    "clipiqa": ("quality", 10),
    "niqe": ("quality", 11),
    "vmaf": ("quality", 12),
    "hdr-vdp2": ("quality", 13),
    # 几何/重建误差（球面/插帧/光流类论文的核心指标）
    "sepe": ("error", 0),
    "epe": ("error", 1),
    "ae": ("error", 2),
    "mae": ("error", 3),
    "rmse": ("error", 4),
    "mse": ("error", 5),
    # 计算代价
    "latency": ("cost", 0),
    "fps": ("cost", 1),
    "params": ("cost", 2),
    "param": ("cost", 3),
    "flops": ("cost", 4),
    "gflops": ("cost", 5),
    "memory": ("cost", 6),
    "bpp": ("cost", 7),
    "time": ("cost", 8),
    "runtime": ("cost", 9),
}
# 长关键词优先匹配："ws-psnr" 必须先于 "psnr" 命中，否则两个指标会混成同一类同一档
_METRIC_KEYS = sorted(_METRIC_ORDER, key=len, reverse=True)
_CATEGORY_CYCLE = ("quality", "error", "cost")

_OURS_RE = re.compile(r"(?:ours|proposed|our\s+method|本文|本方法)", re.I)
_NUM_CELL_RE = re.compile(
    r"^\s*[~≈<>≤≥]?\s*[-+]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*±\s*\d+(?:\.\d+)?)?\s*[%]?\s*$"
)
_ARROW_RE = re.compile(r"[↑↓⇑⇓▲▼]")


def _norm(text: str) -> str:
    return _ARROW_RE.sub("", str(text or "")).lower().replace("（", "(").strip()


def _metric(text: str) -> tuple[str, int] | None:
    """表头/行名 → (类别, 档位)。认不出返回 None（视为文本列）。"""
    norm = _norm(text)
    if not norm:
        return None
    for kw in _METRIC_KEYS:
        if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", norm):
            return _METRIC_ORDER[kw]
    return None


def _is_numeric_cell(cell: str) -> bool:
    return bool(_NUM_CELL_RE.match(str(cell or "")))


def _normalize(table: dict) -> tuple[list[str], list[list[str]]]:
    """表头定宽：行不足补空、超出截断，保证 header 与每一行等长。"""
    header = [str(h or "") for h in (table.get("header") or [])]
    rows = [[str(c or "") for c in (r or [])] for r in (table.get("rows") or [])]
    if not header:
        return header, rows
    width = len(header)
    rows = [(r + [""] * width)[:width] for r in rows]
    return header, rows


def _is_metric_table(header: list[str], rows: list[list[str]]) -> bool:
    """是否是「实验指标对比表」。

    两种形态都算：
    - 常规表：表头里直接是 PSNR/SSIM/Latency 等指标名；
    - 转置表：首列是指标名（Dataset/Metric + 指标行），表头是方法名。
    只有这类表才允许裁列——赛事时间线、数据集统计之类的文本表裁了会丢信息。
    """
    if sum(1 for h in header if _metric(h)) >= 2:
        return True
    # 转置表：指标名写在左侧一到两列的行标签里（Dataset | Metrics | 各方法）
    per_row_hits = sum(1 for r in rows if any(_metric(c) for c in r[:2]))
    if per_row_hits >= 2:
        return True
    return bool(_metric(header[0])) and len(header) > MAX_TABLE_COLS


def _label_columns(header: list[str], rows: list[list[str]], limit: int = 2) -> list[int]:
    """从左侧识别标签列（方法名/Dataset 这类文本列），最多保留 limit 列。"""
    labels: list[int] = []
    for ci, h in enumerate(header):
        if len(labels) >= limit:
            break
        if _metric(h):
            break
        values = [r[ci] for r in rows if ci < len(r)]
        numeric = sum(1 for v in values if v and _is_numeric_cell(v))
        if values and numeric * 2 > len(values):  # 多数单元格是数字 → 不是标签列
            break
        labels.append(ci)
    return labels or [0]


def _round_robin(metrics: list[tuple[int, str, int]], budget: int) -> list[int]:
    """按「质量 → 误差 → 成本」轮转取指标，让一张表同时说清效果与代价。"""
    buckets: dict[str, list[tuple[int, int]]] = {c: [] for c in _CATEGORY_CYCLE}
    for ci, cat, rank in metrics:
        buckets[cat].append((rank, ci))
    for cat in buckets:
        buckets[cat].sort()
    picks: list[int] = []
    while len(picks) < budget and any(buckets.values()):
        for cat in _CATEGORY_CYCLE:
            if buckets[cat] and len(picks) < budget:
                picks.append(buckets[cat].pop(0)[1])
    return picks


def _pick_columns(header: list[str], rows: list[list[str]]) -> list[int] | None:
    """返回要保留的列下标；无需改动时返回 None。"""
    n = len(header)
    useful = [ci for ci in range(n)
              if header[ci].strip() or any(r[ci].strip() for r in rows)]
    if len(useful) <= MAX_TABLE_COLS and len(useful) == n:
        return None
    if len(useful) <= MAX_TABLE_COLS:
        return useful  # 只是去掉了几列全空列

    labels = _label_columns(header, rows)
    metric_cols: list[tuple[int, str, int]] = []
    for ci in useful:
        if ci in labels:
            continue
        parsed = _metric(header[ci])
        if parsed:
            metric_cols.append((ci, parsed[0], parsed[1]))
    budget = max(1, MAX_TABLE_COLS - len(labels))

    if metric_cols:
        keep = set(labels) | set(_round_robin(metric_cols, budget))
        return sorted(keep)

    # 转置/方法对比表：表头是方法名，保标签列 + 本文方法列 + 最右侧（最新 SOTA）邻居
    others = [ci for ci in useful if ci not in labels]
    ours = [ci for ci in others if _OURS_RE.search(header[ci])]
    picks = list(ours[:1]) or ([others[-1]] if others else [])
    for ci in reversed(others):
        if len(picks) >= budget:
            break
        if ci not in picks:
            picks.append(ci)
    return sorted(set(labels) | set(picks))


def compact_table(table: dict) -> dict:
    """按移动端宽度压列。非指标对比表原样返回（只做等宽规整）。"""
    header, rows = _normalize(table)
    if not header:
        return table
    if not _is_metric_table(header, rows):
        if header == table.get("header") and rows == table.get("rows"):
            return table
        out = dict(table)
        out["header"], out["rows"] = header, rows
        return out
    keep = _pick_columns(header, rows)
    if keep is None or len(keep) == len(header):
        if header == table.get("header") and rows == table.get("rows"):
            return table
        out = dict(table)
        out["header"], out["rows"] = header, rows
        return out
    out = dict(table)
    out["header"] = [header[ci] for ci in keep]
    out["rows"] = [[r[ci] for ci in keep] for r in rows]
    return out


def compact_tables(tables: list[dict]) -> list[dict]:
    return [compact_table(t) for t in tables or [] if isinstance(t, dict)]


def prune_spec_tables(spec: dict) -> int:
    """成稿兜底：把 spec 里所有实验表压到 MAX_TABLE_COLS 列，返回被改动张数。"""
    changed = 0
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        new = compact_table(block)
        if new is block:
            continue
        if new.get("header") != block.get("header") or new.get("rows") != block.get("rows"):
            block["header"] = new.get("header") or []
            block["rows"] = new.get("rows") or []
            changed += 1
    return changed
