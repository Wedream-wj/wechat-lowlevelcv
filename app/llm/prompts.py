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
from ..spec_tools import MAX_TABLE_COLS, compact_tables

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

【表达纪律（最高优先级，违反即整篇不合格）】
1. **严禁输出任何心路历程、内心独白或自我指涉**。以下句式一律禁止：
   「但这里不给猜测数字」「笔者猜测」「我推测」「原文未写但我认为」「由于素材有限」
   「需要说明的是」「受限于资料」——以及任何把「我怎么想、我为什么这么写」打印出来的句子。
   读者只看得到提炼后的结论，看不到写作过程：所有判断都以第三人称客观陈述，
   像已发表的评论，而不是边想边写的草稿。
2. **严禁「未披露/未提及/未给出/素材没有/无法量化」这类机械化声明**。
   论文没写到的次要对比维度（推理时延、显存占用、某组消融），**直接省略不提**，
   不要为了显得全面而强行声明「原文未披露 X」——那是机器味最重的写法。
   确实缺失的信息，用行文自然绕过（只写手上有依据的部分），而不是贴一条免责声明。
3. 数字纪律不变：素材里没有的数字一个都不写；不能因为不写「未披露」就去编数字。

【硬性约束】
- 所有数字（PSNR、参数量、FPS、奖金、日期）必须能在给定素材中找到。找不到就不写，
  也**不要**写「论文未披露」之类的说明——直接不提这一项。
- 素材里给了实验表格或带数字的结论句时，正文必须引用其中至少 3 个具体数字并写出比较（谁比谁高多少）；
  禁止用「素材未提供具体数值」「无法量化」这类说明搪塞——先把手上的数字用完。
- 不得编造作者单位、开源地址、成绩排名。
- 不得引用素材之外的图片 URL。需要配图时只引用给定的 F 编号。
- 输出必须是合法 JSON，不要 Markdown 代码块包裹，不要任何解释性文字。
"""

# ---------------------------------------------------------------- 输出规格
SPEC_GUIDE = """
【输出 JSON 规格】
{
  "title": "公众号标题，<= 34 字。学术论文按【标题生成规则】写顶会风格；业界按科技产业媒体风格（主体 + 冲突动作 + 标志性结果）；竞赛按赛题/节点写实，不要标题党",
  "subtitle": "副标题，<= 40 字，一句话点出这篇的核心矛盾或价值",
  "digest": "摘要，<= 100 字，用于分享卡片",
  "lead": ["导语段 1", "导语段 2"],
  "sections": [ ...块序列... ],
  "takeaway": "一句话总结，70–140 字：长单句或带分号的复合句，完整交代「核心动机 + 关键创新/方法 + 带来的性能或范式突破」，要有判断，不要复述标题，不要空话",
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
{"type":"meta_card", "label":"PAPER INFO", "rows":[["论文标题","..."],["期刊/会议","..."],["作者团队","..."],["研究机构","..."],["开源代码","..."]]}
// 业界产业事件卡（label 写 INDUSTRY SNAPSHOT）：主体与事件并成一行，不设规模/金额行
{"type":"meta_card", "label":"INDUSTRY SNAPSHOT", "rows":[["事件","Runway 开源世界动作模型 Praxis-1"],["时间","2026-10-26 发布，数月内开放权重"],["影响面","机器人策略训练与评测"]]}
{"type":"figure", "ref":"F1", "caption":"图 1：整体架构。左边是…"}
{"type":"table", "header":["方法","PSNR↑","WS-PSNR↑","SEPE↓","Latency↓"], "rows":[["Ours","38.68","37.73","15.22","365"]]}
{"type":"contest_card", "name":"赛事名", "track":"赛题方向", "prize":"奖金池", "deadline":"报名/截止", "entry":"入口链接", "desc":"一句话点评"}
{"type":"code", "lang":"python", "caption":"train.py · 训练主循环", "code":"代码原文（原样粘贴，不得改写）"}
{"type":"divider"}

【信息卡标签（label）】
- 论文 → `PAPER INFO`；开源项目 / 工具链 / 技术对比 → `PROJECT INFO`；
  产业事件（融资、合作、发布、退市） → `INDUSTRY SNAPSHOT`；赛事 → `CONTEST INFO`。
- 业界稿里**严禁**把信息卡写成 PAPER INFO。省略 label 时排版层会按模块与行内容兜底推断，
  但显式写对更稳。

【表格规格（手机端硬约束）】
- 表格最多 5 列：1 列方法名 + 最多 4 列核心指标。超过 5 列手机端必然要横向滚动，禁止。
- 指标列按「精度 + 误差 + 代价」各取最关键的一两项，例如
  Method / PSNR / WS-PSNR / SEPE / Latency；素材表列多时由你主动挑列，
  被舍弃的次要指标（参数量、FLOPs、次要数据集）改用正文一句话交代，不要塞回表格。
- 列名保留 ↑/↓ 方向标记；行数控制在 8 行以内，只留最强对手与本文方法。

【章节编号（硬约束）】
- h2 编号必须从 01 开始、按出现顺序连续递增，**严禁跳号、重号**。
- 省略某一节（例如单篇素材时省略「总结」）后，其后各节编号要整体前移：
  原来准备写 06 的「写在最后」必须改写为 05，不能留下 04 → 06 的断层。

篇幅：论文 1800–2600 字；业界 1400–2000 字；竞赛 900–1400 字（不含表格）。
"""

# ---------------------------------------------------------------- 通用片段
FACT_RULES = """
【事实纪律】
- 素材里没有的数字，一个都不要写。
- 不确定的信息（如作者单位、奖金池）写「官方未公布」，不要猜。
- 素材的 tables / result_snippets 里有数字时，必须引用至少 3 个并写出比较差值
  （「25.26 vs 24.10，高 1.16 dB」这种），不得用「论文未提供具体数值」「无法量化」搪塞。
- 某个次要指标没给时，**直接省略这一维度**，不要写「论文未披露 X」——
  把已经给出的数字写足，缺项自然略过即可。
- 评价要落在具体设计上：说「用熵路由把平滑区和高频区分开，避免了教师伪影污染」，
  而不是「设计非常巧妙」。
- 如果素材质量不足以支撑一整篇，就换个角度写深已有的部分，不要注水，
  也不要在正文里交代「素材不足」。
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
   （必须裁到 5 列以内：方法名 + 4 个核心指标，数字照抄、不得改写），表格前后各写一段解读：
   提升集中在哪些指标、比最强对手高/低多少（写成「PSNR 高 0.42 dB」「LPIPS 低 0.019」这样的差值，
   差值必须拿表里两个数逐位现算），
   代价是什么。正文合计至少引用 3 个来自 tables 或 result_snippets 的具体数字。
   **禁止自己另拼一张表**：表格里每个数字都必须来自素材 tables，素材没有的数字一个都不许写。
   素材里有第二张 figure（结果对比图）时引用它，图注写「图 X：…」。
   **禁止**出现「素材未提供具体数值」「无法量化」「表格数据缺失」这类内部说明。
6. h2 "04 | 与 SOTA 的对比与分析"：把表格数字翻译成结论。点名最强对手（方法名 + 它在哪项指标多少），
   写清领先/落后多少；参数量与推理开销只在素材给出时展开，没给就跳过这层，不要声明「未披露」。
   再写清什么场景下会被反超，并给出优势与局限的平衡判断：它真正赢在哪一环、在哪类输入上会失手。
   作者结论与你的判断用「值得注意的是」区分。
7. h2 "05 | 总结"（仅当一期有多篇候选）：逐篇用 1 句话归纳「问题—核心原理—最亮结果」（bullets），
   随后用一段话收束整期判断。**纯粹性规则（硬约束）**：本节只能谈本期选读的论文本身，
   严禁横向对比、严禁扯入其他论文/赛事/未在本文出现的任何工作——总结是收束，不是综述。
   **互斥规则**：本节承担整期收束时，不要再输出 highlight「核心结论」块，takeaway 只写一句
   整期点睛、不得复述本节内容；**单篇素材时整节省略**（连标题一起不要），
   此时下一节「写在最后」的编号顺延为 05，正文不得出现 05 空缺。
8. h2 "06 | 写在最后"（单篇素材省略上一节时为 "05 | 写在最后"）：全文的思想落点，必须写出**高阶 Insight**，三个角度缺一不可：
   - **痛点延伸**：这项工作真正解决了该领域哪个历史局限？（此前十年/几年的主流做法卡在哪，
     它靠什么设计打通的——落到具体机制，不落「效果更好」）
   - **底层启发**：它给后续研究者带来了什么思维转变或范式信号？（哪类问题该换思路了、
     哪种「想当然」的假设被它证伪）
   - **潜在局限与破局点**：客观指出方法的盲区（哪类输入/场景会失效、哪个组件还是权宜之计），
     以及未来最可能被攻克的下一步方向。
   全部依据必须是本篇论文自身的方法与实验，**不得引入论文之外的任何其他工作**；
   严禁「总而言之，这篇论文很有意义，未来可期」这类空话套话——每句话都要有具体的技术指向，
   说不出具体依据的判断不要写。
9. takeaway（一句话总结）：70–140 字，允许长单句或带分号的复合句，但必须完整承载三段信息——
   **核心动机**（它要解决什么死结）+ **关键创新/方法**（靠什么设计解决的）+ **性能或范式突破**
   （拿到了什么结果、改变了什么做法）；不要压缩成一句干瘪的口号，也不要复述标题；
   不要「总之/未来可期/值得关注」这类空话。

【标题生成规则（发表信誉度 + 动机/原理驱动）】
**第一条：发表状态以素材 publication 字段为准（系统已联网查证），标题信誉度硬约束：**
- publication.accepted=true：标题 venue 前缀必须用 publication.venue 的会议/期刊名
  （如「CVPR 2026」「TIP 2025」），信息卡的「期刊/会议」行同样更正，不得再写 arXiv；
- publication.accepted=false（检索确认纯预印本）或没有 publication 字段时：
  标题**严禁出现 "arXiv" 字样**（正文仍可提及 arXiv 预印本），venue 要素整体省略；
- 未经过同行评议的预印本不打 arXiv 标签是本号铁律，无论模型多想写都不得违反。

**第二条：从动机/原理切入提炼吸引力，禁止纯数值堆砌：**
- ❌「迭代从 500 降到 100，PSNR 还高 0.82 dB」——罗列实验指标与参数变化的跑分型标题一律禁止；
- ✔️「如何打破 PnP 迭代瓶颈？HP-PnP-Flow 引入流模型实现高效底层视觉重建」——
  第一钩子必须是核心动机（此前卡在哪）、方法原理（靠什么解）或范式转变（哪种思路被推翻），
  数字最多保留一处最亮的做佐证、且只能放在模型名之后的辅助位置，不得作为标题开头或主钩子，
  不得唱主角。

其余要素与格式（任选参考其一，不得照抄）：
- 【CVPR26-西北工业大学】YOLO 真的需要在每个训练轮次都查看所有训练图像吗？
- CVPR 2025 | AdcSR：扩散反演实现任意步数真实世界超分
- 无惧复杂失真！基于 LoRA 的高鲁棒图像鉴伪新方案
- venue 只用 publication 查证过的名称，机构/作者只在素材给出时写，绝不编造；
- 模型名照抄论文标题中的方法名（如 AdcSR / Freqformer）；
- 长度 <= 34 字；疑问句式用于「反直觉/重新审视」类论文，陈述句式用于「方法+突破」类；
- 禁止「炸裂/封神/王炸/吊打/遥遥领先」等词，吸引力来自动机反差与原理巧思，不来自夸张词。

【写作要求】
- 【结构建议】里的 h2 小节标题必须**逐字照用**（含「01 |」「02 |」编号与措辞，
  包括「写在最后」的标题文字），不得自创或改写章节名；小节顺序可按素材微调，标题本身不许动。
  唯一允许的编号变化是**省略小节后的整体前移**：单篇素材省略「05 | 总结」时，
  「写在最后」必须写成 05；其余情况编号严格连续。
- 至少引用 1 张图；图注要写「图 X：…」，并解释图中哪一部分对应正文哪句话。
- 不要按论文章节顺序流水账。按「问题—方法—证据—对比—边界」重组。
- 「总结」（如有）与「写在最后」都必须严格收敛于本期选读论文自身，
  不得引入任何论文之外的工作做对比或延伸。
""" + FACT_RULES + SPEC_GUIDE

# ---------------------------------------------------------------- 模块 B
INDUSTRY_PROMPT = """
【任务】写一篇 AI 产业观察，发在「底层视觉论文笔记」。读者是算法工程师、技术产品经理与在读研究生，
他们不缺新闻，缺的是「这条消息跟我有什么关系、值不值得改计划」。

【赛道范围（已放宽，不要自我设限）】
- 底层视觉、计算机视觉、深度学习、人工智能；
- Physical AI / 具身智能 / 人形机器人 / 自动驾驶；
- 大模型与基础模型、多模态、智能体、开源模型与工具链；
- 芯片、算力、端侧推理、传感器与影像硬件生态；
- 融资、IPO、收购、退市、战略合作等商业动作背后的技术路线。
判断标准只有一条：这条动态会不会改变一个技术人的研究选题、工程选型或职业判断。

【开篇 Hook（全篇最高优先级，写不好整篇作废）】
lead 第一段的第一句必须是**冲突句**，从下面四种反差里选一种，且必须落到素材里的具体对象/数字：
1. **理想 vs 现实**：发布会/论文/官方口径承诺的东西，与一线真正在用的东西之间的落差；
   例：「当学术界还在卷 0.1 dB 的 PSNR 时，真正让几万用户日夜使用的，其实是一个个把十几个模型缝合起来的桌面端壳子。」
2. **学术卷指标 vs 工业卷落地**：benchmark 上的胜负，与部署时的显存、延迟、维护成本不成正比；
3. **热度 vs 工程价值**：估值/星标/流量很高，但可复现性、许可证、交付能力存疑（或反过来）；
4. **官方叙事 vs 反面证据**：素材里同时存在官方数据与第三方实测/历史教训时，把矛盾摆出来。
写法要求：
- 第一句不许以时间状语或主语宣告开头。**禁用**「近日」「随着」「在…背景下」「XX 宣布」「让我们」「众所周知」「毫无疑问」；
- 不许用设问句凑钩子（「这意味着什么？」这类）；
- 反差必须能在后文被兑现：第二节要解释反差从哪来，第四节要交代它的边界；
- 示例只示范句式，**不得照抄**，也不得把示例里的事实搬进正文。

【结构建议】
1. lead（2 段）：第一段抛 Hook（冲突 + 具体场景/数字）；第二段用两三句把事件事实交代清楚，
   并给出你的核心判断（这篇真正要回答的问题）。
2. meta_card：信息卡。主体是开源项目/工具链 → label 写 `PROJECT INFO`，
   行用「项目 / 仓库 / 热度（stars）/ 许可证 / 硬件或显存门槛 / 上手方式」；
   主体是产业事件 → label 写 `INDUSTRY SNAPSHOT`，行用「事件 / 时间 / 影响面」三行：
   - 第一行 label 写「事件」，内容**一句话说完「谁 + 干了什么」**，例：
     「Runway 开源世界动作模型 Praxis-1」「康佳 4 年亏损 203 亿后正式退市」；
   - **不要**把「主体」和「事件」拆成两行，**也不要**单列「规模 / 金额 / 估值 / 融资额」一行——
     数字只出现在这句事和标题里，不另占行；读者要的是动作，不是一个孤零零的公司名加一个数字；
   - 剩下只留「时间」（发布时间或计划节点）和「影响面」（谁受影响）两行。
   **业界稿严禁使用 PAPER INFO**；素材没给的行直接省略，不要写「未披露」。
3. h2 "01 | 发生了什么"：只讲事实，不评价。谁、什么时候、做了什么、关键数字是多少。
   一期有多条动态时，主线一条展开写，其余用 bullets 各一句带过（标明来源），不要平均用力。
4. h2 "02 | 技术上看，真正的变化在哪"：主体段。回答三件事——
   - 它绕开了此前哪条走不通的路（要有具体机制，不是「更高效」）；
   - 变化发生在架构、数据、训练范式还是成本结构上；
   - 代价是什么（算力、标注、延迟、许可证、维护人力），素材给了就写数字，没给就定性描述。
   素材的 figures 非空时，**本节必须插 1 个 figure 块**（用 ref 引用，不要新造编号），
   图注写「图里画的是什么 + 对应本节哪句判断」。
5. h2 "03 | 对读者意味着什么"：落到可执行判断。分三类人写（做研究的 / 做工程的 / 做产品的），
   每类给一条具体建议；不要写「值得关注」这种没有动作的建议。
6. h2 "04 | 写在最后"：全篇的落点，也是 Insight 密度最高的一节（350–500 字，2–3 段连贯行文，
   不列 bullets）。这一节不复述事件、不做零散补充质疑，只回答一个问题：
   **这件事把行业的成本结构或演进路径，往哪推了一步。** 三步收束：
   - **成本结构**：它把哪一项成本压下去了（算力 / 数据采集 / 标注 / 推理延迟 / 维护人力），
     又把哪一项抬了上来；两者相抵之后，谁真正受益、谁被挤出局。素材给了金额就写金额，
     没给就只做结构判断，**不要编量级**。
   - **演进路径**：如果这条路线成立，未来 6–18 个月行业会怎么走——哪条技术路线被加速、
     哪条被跳过、哪个环节会成为新瓶颈（数据供给、评测标准、许可证、供应链、算力分配）。
     判断要落到具体环节，不要写「行业将加速发展」。
   - **终极判断**：收在一句可被检验的结论上，形如「如果 X 成立，Y 会在 Z 之前发生」，
     而不是「值得关注」「拭目以待」「时间会给出答案」。
   信息边界仍要交代（官方 benchmark 打几折、厂商口径打几折、什么条件下整条判断会变成伪信号），
   但必须**嵌进判断里**说，例如「0.95 的相关性只在自建数据集上成立，这就是这条路线目前的天花板」；
   不许单独起一段罗列「本文数据来自官方，第三方复现待观察」。
   本节禁止：流水账式罗列各方观点、个人心路历程（「笔者一开始以为…后来发现…」）、
   脱离素材的宏大叙事（「AI 正在重塑一切」）、对前文论点的重复总结。
7. takeaway：一句判断句（70–140 字），交代「这条动态改变了什么 + 对谁有用 + 前提条件」，
   不要给情绪，不要复述标题，也不要与「写在最后」的终局结论重复。

【标题生成规则（科技产业媒体风格）】
目标：在公众号列表里，让工程师一眼看出「谁、干了什么、为什么反常」。长度 <= 34 字。
四种可用结构（任选，不得照抄示例）：
- A 主体 + 共建/绑定 + 落地细节：`荣耀与广东共建AI实验室：粤语语料入库`
- B 昔日地位 + 反向事件 + 数据冲击：`昔日"彩电一哥"正式退市！康佳4年亏了203亿`
- C 主体 + 深度绑定对象 + 赛道判断：`MiniMax 深度绑定中国电信，大模型政企赛道掀起"生态明牌"战`
- D 新主体 + 估值/定位 + 野心：`息壤开物刚成立即估值35亿，奋力打造Physical AI"世界模型"`
硬约束：
- 主体必须是素材里出现过的公司/机构/模型/项目名，不得用「某大厂」「国内团队」代指；
- 动词要有冲突感（共建、绑定、退市、开源、砍到、首度、反超、掀桌、下注），
  但事实必须成立——素材没说的动作不许加；
- 金额、估值、参数、排名等数字只能来自素材，一个都不许编；
- 可用一个「！」或「：」制造节奏，全标题最多一处，不要连用；
- 禁止「震惊/炸裂/封神/王炸/吊打/遥遥领先/史上最强/彻底改变世界」这类夸张词；
- 禁止纯数字堆砌（如「融资 5 亿，估值 35 亿，增长 300%」），数字最多保留一处最有力的；
- 标题里不写「AI 资讯」「行业观察」「本周动态」这类栏目名。

【写作要求】
- **章节名（硬约束）**：收尾节标题逐字写作「04 | 写在最后」；**禁止**写成「冷静一下」「结语」
  「尾声」「小结」等其他叫法，也禁止在后面加副标题（不要「写在最后：几点担忧」）。
- 不要写成产品发布会通稿或新闻摘要，每段都要有「所以呢」。
- 开源项目：优先写清能力边界、显存/速度门槛、许可证限制、与现有方案怎么选。
- 融资/商业新闻：重点写技术路线与商业化之间的张力，不要罗列投资方名单。
- 厂商 benchmark 一律标注「官方数据，第三方复现待观察」，不得当成定论。
- 多赛道混合的一期（芯片 + 机器人 + 大模型）不要硬凑成一条主线：
  用「共同的压力」把它们串起来（例如都在为推理成本或数据供给买单），串不起来就只写一条主线。
- 素材的 full_text 是媒体正文，可引用其中的事实与直接引述，但要注明来源；
  媒体观点不等于你的独立验证，需要判断时用「值得注意的是」区分开。
- 素材条目带 figures（如 F1/F2）时，正文**至少插 1 个 figure 块**（选最能说明问题的那张，
  通常放「02」里），图注写清「图里画的是什么 + 对应正文哪句判断」；没有配图就用文字交代，不要硬凑。
- **数字纪律（业界稿补充）**：金额、估值、参数量、百分比、相关性系数必须能在素材原文里逐字找到。
  素材只给了 Token 数、没给金额，就不要折算成人民币/美元；素材没说成本量级，
  就不要写「几十万美元」「上千万」这类量级词——宁可写「遥操作数据采集成本高」这种定性表述。
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
- 素材里的 category=学习资源 条目（往届 baseline 仓库）**不属于本期赛事速递**：
  不要写进赛事列表、时间线表格或任何小节；它们由独立的「baseline 精读」成稿变体处理。
""" + FACT_RULES + SPEC_GUIDE

# ---------------------------------------------------------------- 模块 C 变体：竞赛 baseline 精读
CONTEST_BASELINE_PROMPT = """
【任务】写一篇「竞赛 baseline 精读」，发在「底层视觉论文笔记」。
你是常年在各大数据竞赛刷进前十的竞赛 Grandmaster，正以专业选手带练的视角，
拆解一份往届竞赛的开源 baseline 复现：它的数据洞察、值得借鉴的代码亮点，
以及从这份起点往上打的专业升级路线。
读者读完要能回答三个问题：这类赛题的痛点在哪、这份 baseline 有哪些逻辑值得借鉴复用、接下来按什么路线升级。

【重要边界】
- **不要强行联系当前正在进行的其他赛事**：全文不出现「本场赛事」「正在报名的 XX 赛」这类嫁接，
  不写报名入口、奖金、截止倒计时。只讲原赛题本身与这份 baseline 的解法。
- **赛题信息必须查证后确定性撰写**：extra.baseline_detail.search_context 里是联网检索到的
  赛事官方介绍片段（标题、摘要、链接）。赛题定义、业务背景、评测口径**优先以检索结果为准**，
  用确定性语气直述；检索结果里的数字可以引用。
- **严禁任何推测性话术**：「按目录名推断」「按代码推断」「猜测可能为」「大概率是」「应该是」
  一律禁止，也不得向读者解释「信息是从哪推断来的」。检索与代码证据足以支撑的结论，直接写成事实；
  检索没覆盖的部分，用「本赛题的核心在于……」「该方案重点解决了……」这类确定性句式补足。

【措辞禁令（最高优先级，逐条硬约束）】
1. **严禁暴露素材缺陷与代码缺陷**。以下表述一律禁止：
   「代码没写完」「仓库未完成」「不完整」「只有/仅仅有 EDA」「README 为空/摘录为空/很短」
   「未注明」「未知」「素材未提供」「素材没写」「无法量化」「信息缺失」「没有给出」「留白」等。
2. 素材薄、代码基础，一律包装为定位与空间：
   「该 Baseline 聚焦于基础框架与数据洞察，为后续模型迭代留下了极大的调优空间」这类句式。
   EDA 为主的仓库要写成「数据洞察型 baseline」，并明确说清这正是它的价值所在——
   数据结论越扎实，后面的模型迭代越有的放矢。
3. **严禁推测性词汇**：「按目录名推断」「按代码推断」「推断为」「猜测」「大概率」「可能为」
   「应该是」「多半是」等一律禁止；赛题定义直接用确定性句式陈述。
4. **严禁直白粗糙的口语**：「抄走」「套用」「偷懒」「搬运」「白嫖」等一律不用，
   统一替换为「复用」「借鉴」「参考」「迁移」。对照示例：
   ❌「先抄走它的数据管道」
   ✔️「可优先借鉴并迁移其高效的数据处理 Pipeline，在此基础上融入 7/14/28 天滑动窗口统计特征
   与 LightGBM 模型，并结合时序交叉验证（Time-Series Split）进行效果迭代。」
5. **严禁露出信息缺失的不确定性**。范式级补足必须落在可执行的专业动作上
   （指标怎么选、CV 怎么切、特征怎么挖、模型怎么升级），不得写成空洞的免责声明。
6. 唯一保留的数字纪律：**具体数字（样本量、成绩、名次、超参数值）只能来自素材或检索结果**，
   不得编造；范式建议里的方法名（LightGBM、StratifiedKFold 等）不是数字，可以写。

【素材说明】
- 素材只有一条 category=学习资源 条目：Datawhale「竞赛Baseline」开源仓库里的一个赛题目录。
  extra.baseline_detail 里有：
  * files：仓库文件清单（真实文件名）；
  * readme_excerpt：README 摘录（可能很短——此时以 search_context 的检索结果为准，通篇不要提 README）；
  * code_samples：仓库代码文件的真实片段（[{name, code}]，notebook 已抽取 code cell）；
  * search_context：联网检索到的赛事官方介绍片段（赛题定义、业务背景的事实依据）；
  * paradigm_hint：范式级背景提示（检索没覆盖时的补足参考）。
- code_samples 是代码解读的核心素材：正文至少用 1 个 code 块原样粘贴片段（最多 3 个），
  逐段讲解；code 素材里没有的代码不要凭空编造。
- EDA notebook 的代码（describe / value_counts / groupby / 可视化）本身就是数据分布结论的来源：
  从这些代码提炼数据特征（字段类型、类别数、不平衡程度、时序/空间属性），用确定性语言写进 01 节。

【结构建议】
1. lead（2 段）：第一段以确定性语气点出这份 baseline 对应的原赛题
   （赛题定义与业务背景以 search_context 为准直述），以及为什么值得读一份基础型 baseline——
   起点越干净，调优路线越清晰；第二段预告本文路线：数据痛点 → baseline 亮点 → 升级 Roadmap。
2. meta_card：信息卡（原赛题、赛事平台、任务类型、baseline 仓库、代码入口）。
   任务类型等字段按检索结果与代码证据确定性填写，不写「推断」字样。
3. h2 "01 | 数据分布与赛题痛点速览"：用确定性语言总结数据特征（数据类型、字段构成、
   类别数与不平衡、时序/空间/文本属性、噪声与极值），依据是 EDA 代码与检索到的赛题信息；
   随后点出这类赛题的通用痛点（指标陷阱、分布漂移、样本不平衡）。
4. h2 "02 | baseline 亮点提炼（含关键代码解读）"：萃取现有 EDA / 数据清洗 / 特征工程 /
   训练框架里值得借鉴与复用的逻辑；code 块原样粘贴 code_samples 片段（单个不超过 30 行，
   超长取连续节选并注明「节选」），每段代码**前**写清它处于流程哪一步，**后**写清
   它在做什么、为什么这么写、改动空间在哪。
5. h2 "03 | 专业级升级 Roadmap"：核心干货段，三条线展开（用 h3 分小节）：
   * 特征工程：具体到手法——Target Encoding、时序滑动窗口统计、交叉特征、聚合统计、
     embedding 编码等，结合 01 节的数据特征说哪几招优先、为什么；
   * 模型选择：从 LightGBM / XGBoost / CatBoost 的表格三件套起步，到何时值得上深度模型，
     每一级给预期收益与代价；
   * 交叉验证策略：按数据特征推荐切分（StratifiedKFold / GroupKFold / 时序滚动切分），
     说清为什么这种切分能对齐线上分布、避免泄漏。
6. takeaway：一句话判断（这份 baseline 的真正价值 + 第一步升级动作）。

【写作要求】
- 全文 1600–2400 字（不含代码），技术密度优先；通篇「专业选手带练」口吻，有判断、有优先级。
- 不得罗列仓库目录；要解释每个组件为什么在那。
- 不要写「本文将介绍」这类废话，直接开讲。
""" + FACT_RULES + """
【本变体措辞修正（优先级高于上方事实纪律的措辞条款）】
- 「不确定的信息写『官方未公布/仓库未注明』」这条在本变体中停用：改以 search_context 检索结果
  与确定性句式补足，通篇不得出现「未公布/未注明/未知/推断/猜测/大概率/抄走/套用」等任何一种。
- 数字纪律保留：素材与检索结果里都没有的具体数字一个都不要写；范式建议的方法名不是数字，可以写。
- 成稿里出现「素材未提供/未注明/未知/为空/没写完/不完整/按目录名推断/猜测/抄走」任何一种都算不合格。
""" + SPEC_GUIDE

PROMPTS = {"paper": PAPER_PROMPT, "industry": INDUSTRY_PROMPT, "contest": CONTEST_PROMPT,
           "contest_baseline": CONTEST_BASELINE_PROMPT}

MODULE_TITLE = {"paper": "论文精读", "industry": "业界观察", "contest": "赛事速递"}
# 素材里有数字、成稿却没引用时的补写要求（见 thin_evidence 判定）
QUANT_RETRY = (
    "这一稿的实验解读缺少数值证据，读起来没有说服力。请保持原有的结构、标题与已写的分析，"
    "但务必重写「关键实验结果解读」与「与 SOTA 的对比与分析」两节：\n"
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
    "与「与 SOTA 的对比与分析」两节：\n"
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


def drop_redundant_summary(spec: dict) -> bool:
    """「总结」节与「核心结论」highlight 互斥（收束二选一，并存必重复）。

    Prompt 里有这条规则，但模型偶尔不遵守——这里在代码层强制执行：
    出现标题含「总结」的 h2 时，移除 title 含「核心结论」的 highlight 块。
    返回是否发生了删改。
    """
    sections = spec.get("sections") or []
    has_summary = any(
        isinstance(b, dict) and b.get("type") == "h2" and "总结" in str(b.get("text", ""))
        for b in sections
    )
    if not has_summary:
        return False
    kept = [
        b for b in sections
        if not (isinstance(b, dict) and b.get("type") == "highlight"
                and "核心结论" in str(b.get("title", "")))
    ]
    changed = len(kept) != len(sections)
    if changed:
        spec["sections"] = kept
    return changed


_ARXIV_TITLE_RE = re.compile(r"\s*arxiv\b[:：]?\s*(20\d{2})?\s*", re.I)


def enforce_title_rules(spec: dict, module: str, items: list | None = None) -> bool:
    """论文标题信誉度硬约束：标题严禁出现 arXiv 字样 + venue 必须与查证结论一致。

    规则依据：纯预印本未经同行评议，标题打 arXiv 标签会拉低权威感；已正式收录的
    论文，venue 以发表状态查证（publication 字段）为准——模型偶尔写错或编造
    会议名（实测把 OpenReview 查到的 ICML 写成 CVPR），这里在代码层强制对齐。
    返回是否发生了修改。
    """
    title = str(spec.get("title") or "")
    changed = False

    # 1) 清洗 arXiv 字样（仅论文模块；正文仍可提及 arXiv）
    if module == "paper" and "arxiv" in title.lower():
        cleaned = _ARXIV_TITLE_RE.sub(" ", title)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        cleaned = re.sub(r"^[\s|｜\-—·]+|[\s|｜\-—·]+$", "", cleaned)
        if cleaned and cleaned != title:
            title = spec["title"] = cleaned
            changed = True

    # 2) venue 对齐查证结论：先定位标题实际写的是哪篇（头条不一定 selected[0]——
    #    模型按素材价值自选头条），再拿那篇的查证结论强制对齐。
    if module == "paper" and items:
        featured = _featured_item(title, items)
        pub = (featured.extra.get("publication") or {}) if featured else {}
        venue = str(pub.get("venue") or "")
        short = venue.split()[0] if venue else ""
        if pub.get("accepted") and venue and short not in title:
            # 已正式收录：剥掉开头错误/编造的 venue 标签，换上查证过的名称
            stripped = re.sub(
                r"^\s*(?:【[^】]{0,24}】|[A-Za-z][A-Za-z/ &]{0,20}\s*20\d{2}\s*[|｜：:]?\s*)",
                "", title)
            spec["title"] = f"{venue} | {stripped}"
            changed = True
        elif pub.get("accepted") is False:
            # 检索确认仅预印本：标题里的任何 venue 前缀都是编造的（模型给预印本
            # 贴 CVPR/ICML 标签比漏写更伤信誉），剥掉
            stripped = _TITLE_VENUE_PREFIX_RE.sub("", title).strip()
            if stripped and stripped != title:
                spec["title"] = stripped
                changed = True
    return changed


_FLAT_LEAD_OPENERS = (
    "近日", "日前", "最近", "随着", "据悉", "据报道", "据消息", "众所周知",
    "毫无疑问", "让我们", "在当前", "近年来", "这段时间",
)


def industry_lead_is_flat(spec: dict) -> bool:
    """业界稿开篇是不是「通稿式」：时间状语开头，或前 12 字里就出现「宣布」。

    只做窄判定（不做语义判断），命中才触发一次定向重写，
    避免为了「有没有钩子」这种主观标准反复烧 token。
    """
    lead = spec.get("lead") or []
    first = str(lead[0] if lead else "").strip()
    if not first:
        return True
    if first.startswith(_FLAT_LEAD_OPENERS):
        return True
    head = first[:12]
    return "宣布" in head or "发布消息" in head


INDUSTRY_HOOK_RETRY = (
    "上一稿的 lead 第一段是通稿式开头（时间状语或「XX 宣布」起手），没有钩子。"
    "请保持事实、章节结构、信息卡与标题不变，只重写 lead 第一段：\n"
    "1) 第一句必须是反直觉结论或冲突句，落到素材里的具体对象与数字；\n"
    "2) 三种反差任选：理想 vs 现实、学术卷指标 vs 工业卷落地、热度/估值 vs 工程价值；\n"
    "3) 不得以「近日/日前/随着/据悉/据报道/众所周知/让我们」开头，不得用设问句；\n"
    "4) 第二段继续交代事件事实与核心判断，全篇字数不变。"
)


def _featured_item(spec_title: str, items: list):
    """按标题相似度找出成稿头条对应的素材条目（模型会自选头条，未必是 selected[0]）。"""
    from difflib import SequenceMatcher

    clean = re.sub(
        r"^\s*(?:【[^】]{0,24}】|[A-Za-z][A-Za-z/ &]{0,20}\s*20\d{2}\s*[|｜：:]?\s*)",
        "", spec_title).lower()[:80]
    best, best_r = None, -1.0
    for it in items:
        ratio = SequenceMatcher(None, clean, (it.title or "").lower()[:80]).ratio()
        if ratio > best_r:
            best, best_r = it, ratio
    return best


_TITLE_VENUE_PREFIX_RE = re.compile(
    r"^\s*(?:【[^】]{0,24}】|[A-Za-z][A-Za-z/ &]{0,20}\s*20\d{2}\s*[|｜：:]?\s*)")


def title_is_number_led(spec: dict) -> bool:
    """跑分型标题检测：剥掉 venue 前缀后，正文前 12 个字符内出现数字即判定。

    「迭代从 500 降到 100：…」「从500步到100步：…」都是数字开头的平庸标题；
    「如何打破 PnP 迭代瓶颈？…」这类动机钩子不含数字，正常放行。
    """
    title = str(spec.get("title") or "")
    body = _TITLE_VENUE_PREFIX_RE.sub("", title)
    return bool(re.search(r"\d", body[:12]))


def build_title_rewrite_messages(spec: dict, featured_item) -> list[dict[str, str]]:
    """跑分型标题的定向重写 Prompt：只改标题，动机/原理驱动，venue 前缀原样保留。"""
    title = str(spec.get("title") or "")
    prefix_m = _TITLE_VENUE_PREFIX_RE.match(title)
    prefix = prefix_m.group(0) if prefix_m else ""
    paper_title = (getattr(featured_item, "title", "") or "")[:120]
    summary = (getattr(featured_item, "summary", "") or "")[:800]
    return [
        {"role": "system", "content": (
            "你是顶会论文解读公众号的标题编辑。只输出 JSON：{\"title\": \"...\"}，"
            "不要任何解释。")},
        {"role": "user", "content": f"""重写下面这条公众号标题。当前标题是跑分型的——以实验数字/参数变化开头，必须改为动机/原理驱动。

当前标题：{title}

论文原题：{paper_title}
论文摘要：{summary}

重写规则：
1. 第一钩子必须是核心动机（此前方法卡在哪）、方法原理（靠什么解）或范式转变，禁止以数字/参数变化开头；
2. {"必须以 " + prefix.strip() + " 开头（含分隔符），venue 前缀一字不改" if prefix else "不得出现 arXiv 字样"}；
3. 模型名照抄论文标题中的方法名；数字最多保留一处最亮的、放在模型名之后的辅助位置；
4. 长度 <= 34 字；疑问句式用于反直觉类，陈述句式用于方法+突破类；禁止夸张词。
正例：如何打破 PnP 迭代瓶颈？HP-PnP-Flow 引入流模型实现高效底层视觉重建
反例：迭代从 500 降到 100：HP-PnP-Flow 用混合时间调度重构 PnP 重建（数字开头，禁止）"""}]


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
    if item.published_at:
        payload["published"] = item.published_at.strftime("%Y-%m-%d")
    # 业界稿的正文（选中后回抓）：量子位首页只有一句摘要，没有这段写不出事实密度
    if item.extra.get("full_text"):
        payload["full_text"] = str(item.extra["full_text"])[:3000]
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
        # 素材表先压到移动端口径（表头 + 4 个核心指标列）再给模型：
        # 模型会照抄输入表的形态，喂 9 列它就吐 9 列。数字校验仍用未裁剪的原始表，
        # 所以正文引用被裁掉的指标数值也能核对通过。
        payload["tables"] = compact_tables(tables[:3])
    snippets = item.extra.get("result_snippets") or []
    if snippets:
        payload["result_snippets"] = snippets
    if item.extra.get("days_left") is not None:
        payload["days_left"] = item.extra["days_left"]
    if item.extra.get("is_learning_resource"):
        payload["is_learning_resource"] = True
        detail = item.extra.get("baseline_detail") or {}
        payload["baseline_detail"] = {
            "files": [f for f in (detail.get("files") or []) if f][:15],
            "readme_excerpt": (detail.get("readme_excerpt") or "")[:3000],
        }
        # README 过短时自动生成的范式推演提示，供成稿做专业推断兜底
        if detail.get("paradigm_hint"):
            payload["baseline_detail"]["paradigm_hint"] = str(detail["paradigm_hint"])[:1200]
        # 联网检索到的赛事官方背景：赛题信息查证后确定性撰写的事实依据
        ctx = detail.get("search_context") or []
        if ctx:
            payload["baseline_detail"]["search_context"] = [
                {"title": str(c.get("title") or "")[:120],
                 "snippet": str(c.get("snippet") or "")[:400],
                 "url": str(c.get("url") or "")}
                for c in ctx[:4]
            ]
        # 关键代码片段是 baseline 精读的核心素材，必须原样传给模型
        samples = detail.get("code_samples") or []
        if samples:
            payload["baseline_detail"]["code_samples"] = [
                {"name": str(s.get("name") or ""), "code": str(s.get("code") or "")[:2200]}
                for s in samples[:3] if s.get("code")
            ]
    # 论文发表状态（检索查证结论）：标题与信息卡的 venue 以此为准
    pub = item.extra.get("publication")
    if pub:
        payload["publication"] = pub
    return payload


def build_user_prompt(module: str, items: list, figures: dict[str, dict], date_str: str,
                      variant: str | None = None) -> str:
    """把素材整理成模型友好的 JSON，附带图片编号表。variant 用于模块内成稿变体。"""
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
        "variant": variant or "default",
        "brand": {"name": BRAND_NAME, "slogan": SLOGAN},
        "materials": material,
        "figures": figure_table,
    }
    prompt_key = f"{module}_{variant}" if (module == "contest" and variant == "baseline") else module
    return (
        PROMPTS[prompt_key]
        + "\n\n【本期素材】\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\n请严格按上述 JSON 规格输出，只输出 JSON。"
    )
