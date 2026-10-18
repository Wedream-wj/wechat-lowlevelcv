"""离线降级稿 + 演示稿。

`mock_spec()`：没配 API Key 时，用素材字段拼一份「结构完整、措辞克制」的占位稿，
保证流水线、排版、草稿推送都能端到端跑通（内容会标注为自动整理）。

`DEMO_SPECS`：三份手写的示例成稿，用于本地预览排版效果，不参与线上发布。
"""
from __future__ import annotations

from datetime import date

from ..models import Item


def _brief(items: list[Item]) -> list[dict]:
    out = []
    for item in items[1:3]:
        bits = [item.venue, item.year]
        out.append({
            "title": item.title,
            "meta": " · ".join([b for b in bits if b]),
            "text": (item.summary or "")[:180] + ("…" if len(item.summary or "") > 180 else ""),
            "url": item.url,
        })
    return out


def mock_spec(module: str, items: list[Item], figures: dict[str, dict]) -> dict:
    """把素材机械地组织成 ArticleSpec，不生成任何素材之外的数字。"""
    if not items:
        raise ValueError("mock_spec 需要至少一条素材")
    main = items[0]
    date_str = date.today().strftime("%Y.%m.%d")

    if module == "paper":
        raw_title = main.extra.get("title_zh") or main.title
        short_title = raw_title if len(raw_title) <= 26 else raw_title[:26].rstrip("，、的与和及 ") + "…"
        sections: list[dict] = [
            {"type": "meta_card", "rows": [
                ["论文标题", main.extra.get("title_zh") or main.title],
                ["期刊/会议", main.venue or "arXiv"],
                ["作者团队", ", ".join(main.authors[:6]) or "未披露"],
                ["开源代码", main.code_url or "暂未开源"],
            ]},
            {"type": "h2", "text": "01 | 这篇在解决什么问题"},
            {"type": "p", "text": (main.summary or "摘要暂缺。")[:600]},
            {"type": "h2", "text": "02 | 方法拆解"},
            {"type": "p", "text": "以下内容依据论文摘要自动整理，完整的方法细节需要结合原文阅读。"},
        ]
        fids = [fid for fid, meta in figures.items() if meta.get("item_id") == f"{main.module}:{main.fingerprint}"]
        if fids:
            sections.append({"type": "figure", "ref": fids[0],
                             "caption": figures[fids[0]].get("caption") or "论文总体框架"})
        sections += [
            {"type": "h2", "text": "03 | 代价与适用边界"},
            {"type": "p", "text": "自动模式下暂不做主观评价。建议重点关注：参数量与推理开销、真实场景泛化、以及对训练数据规模的依赖。"},
        ]
        if len(items) > 1:
            sections += [{"type": "h2", "text": "04 | 顺带值得一看"},
                         {"type": "bullets", "items": [f"{i.title}（{i.venue or 'arXiv'}）" for i in items[1:3]]}]
        return {
            "title": f"{short_title}｜论文速览",
            "subtitle": f"{main.venue or 'arXiv'} · {main.category or '底层视觉'}",
            "digest": (main.summary or "")[:100],
            "lead": [f"今天看一篇 {main.venue or 'arXiv'} 的工作：{main.title}。",
                     "以下为结构化速览，配置大模型后可生成完整解读。"],
            "sections": sections,
            "takeaway": "先读摘要与架构图，再决定是否精读实验部分。",
            "sources": [{"name": main.title, "url": main.url}],
        }

    if module == "industry":
        sections: list[dict] = [
            {"type": "h2", "text": "01 | 线索清单"},
            {"type": "bullets", "items": [f"{i.title}（来源：{i.source}）" for i in items[:4]]},
        ]
        # 有配图就插进去：离线模式也要能验证「图文并茂」这条硬要求
        for fid, meta in list(figures.items())[:2]:
            sections.append({"type": "figure", "ref": fid,
                             "caption": meta.get("caption") or meta.get("title", "")})
        sections += [
            {"type": "h2", "text": "02 | 怎么读这些消息"},
            {"type": "p", "text": "建议优先看三类信号：是否开源、是否给出可复现的评测、以及推理成本是否落在可落地区间。"},
        ]
        return {
            "title": "底层视觉的工程侧，正在发生什么",
            "subtitle": f"{date_str} · 自动整理的行业线索",
            "digest": "自动模式下汇总近期与底层视觉相关的行业动态。",
            "lead": ["本期为自动整理稿，用于验证抓取与排版链路。",
                     "配置大模型后将输出完整的事件解读与判断。"],
            "sections": sections,
            "takeaway": "先分清「能力演示」和「可落地能力」。",
            "sources": [{"name": i.title, "url": i.url} for i in items[:4]],
        }

    cards: list[dict] = []
    for item in items[:3]:
        cards += [
            {"type": "contest_card",
             "name": item.title,
             "track": item.category or "底层视觉",
             "prize": str(item.extra.get("prize") or "待官方公布"),
             "deadline": str(item.extra.get("deadline") or "待官方公布"),
             "entry": item.url,
             "desc": (item.summary or "")[:120]},
            {"type": "divider"},
        ]
    return {
        "title": "本期值得打的底层视觉赛事",
        "subtitle": "按方向、奖金与截止时间整理",
        "digest": "自动汇总的赛事清单，含赛题方向、奖金与截止时间。",
        "lead": [f"本期汇总 {len(items[:3])} 场与底层视觉相关的赛事。",
                 "时间与奖金以官方页面为准，建议先跑通 baseline 再决定投入。"],
        "sections": cards + [
            {"type": "h2", "text": "01 | 参赛避坑"},
            {"type": "bullets", "items": ["先确认评测指标与提交格式，再设计模型", "注意训练集与测试集的分布差异", "预留至少 30% 时间做复现与集成"]},
        ],
        "takeaway": "选一场与手头数据最接近的赛事，胜率比奖金更重要。",
        "sources": [{"name": i.title, "url": i.url} for i in items[:3]],
    }


# ------------------------------------------------------------------ 演示稿
DEMO_SPECS: dict[str, dict] = {
    "paper": {
        "title": "把摩尔纹拆成高频和低频：Freqformer 的双分支复原思路",
        "subtitle": "ECCV 2026｜用紧凑模型拆解纹理破坏与色偏的纠缠",
        "digest": "摩尔纹的难点在于纹理破坏与颜色失真相互纠缠。Freqformer 用一次频率分解把两者分开，再交给双分支网络分别处理。",
        "lead": [
            "拍屏幕、拍织物，摩尔纹总会以两种面貌同时出现：一层是细密的彩色条纹，另一层是被破坏的织物纹理。"
            "大多数复原网络把两者当成同一种退化，用同一个 decoder 一起修，结果往往是色偏压住了、纹理却糊了。",
            "ECCV 2026 的 Freqformer 换了个思路：先把摩尔纹拆成高频的空间局部纹理和低频的尺度鲁棒颜色失真，"
            "再用两条分支分别处理，最后用一个可学习的频率合成模块缝回去。",
        ],
        "sections": [
            {"type": "meta_card", "rows": [
                ["论文标题", "基于有效频率分解的图像去摩尔纹 Transformer（Freqformer）"],
                ["期刊/会议", "European Conference on Computer Vision (ECCV) 2026"],
                ["作者团队", "Xiaoyang Liu, Bolin Qiu, Zheng Chen, Libo Zhu, Zihan Zhou, Kai Liu, Jiezhang Cao, Yulun Zhang"],
                ["开源代码", "论文注明将公开，链接以官方仓库为准"],
            ]},
            {"type": "h2", "text": "01 | 摩尔纹难在哪"},
            {"type": "p", "text": "现有方法大致分两类。一类是直接的图像到图像复原，把摩尔纹当成普通噪声；另一类引入频域先验，"
                                    "用 Haar 小波或分块 DCT 做变换。前者的问题是无法解耦两种伪影，后者的麻烦在于离散变换本身："
                                    "**空间不连续、通道冗余**，固定的逆变换还会把误差一步步累积回来。"},
            {"type": "quote", "text": "摩尔纹图案导致的纹理破坏与颜色失真之间存在复杂的相互作用，直接做图像到图像复原往往无法有效解耦。"},
            {"type": "p", "text": "这句话基本点出了这篇论文的出发点：不是「去得更干净」，而是「先把问题拆开」。"},
            {"type": "h2", "text": "02 | 方法拆解"},
            {"type": "p", "text": "Freqformer 的整体流程可以概括为三步：分解、分治、合成。"},
            {"type": "figure", "ref": "F1", "caption": "图 1：Freqformer 总体架构。输入经频率分解后进入双分支，分别处理高频纹理与低频色偏，最后由 FCT 模块融合。"},
            {"type": "h3", "text": "频率分解：把纠缠的两种退化摊开"},
            {"type": "p", "text": "作者把摩尔纹分解为**高频的空间局部化纹理**与**低频的尺度鲁棒颜色失真**。"
                                    "这个划分符合直觉：细密条纹对应高频，而大面积的偏色对应低频。"
                                    "相比小波或 DCT，这里的分解是为后续分支量身定制的，避免了固定逆变换带来的误差累积。"},
            {"type": "h3", "text": "双分支 + 非对称训练"},
            {"type": "p", "text": "两条分支的职责不同，训练策略也不对称——高频分支更需要保结构，低频分支更需要保色彩一致性。"
                                    "论文为两种特性分别定制了处理方式，而不是简单地复制同一套 block。"},
            {"type": "h3", "text": "FCT 与 SA-CA：缝合与聚焦"},
            {"type": "numbered", "items": [
                "**FCT（Frequency Composition Transform）**：可学习的频率合成模块，自适应融合两个分支的输出，保证重建的连贯性与高保真度。",
                "**SA-CA（Spatial-Aware Channel Attention）**：在通道注意力里加入空间感知，把注意力压到摩尔纹敏感区域，同时控制计算开销。",
            ]},
            {"type": "highlight", "title": "一句话理解", "text": "不是把摩尔纹当噪声抹掉，而是先判断它属于哪一类退化，再交给对应的专家网络处理。"},
            {"type": "h2", "text": "03 | 实验说明了什么"},
            {"type": "p", "text": "论文在多个去摩尔纹基准上做了对比，结论是「以紧凑的模型尺寸达到 SOTA」。"
                                    "这句话里真正值得注意的不是 SOTA，而是**紧凑**：在移动端与相机 ISP 流水线里，模型体积往往比零点几个 dB 更关键。"},
            {"type": "table", "header": ["关注点", "论文给出的信号", "读的时候要追问什么"],
             "rows": [
                 ["重建质量", "多个基准上达到 SOTA", "提升集中在合成数据还是真实拍摄？"],
                 ["模型尺寸", "紧凑模型", "参数量、MACs、端侧帧率具体是多少？"],
                 ["泛化性", "覆盖屏幕与织物两类摩尔纹", "未见过的传感器/显示面板是否仍有效？"],
             ]},
            {"type": "p", "text": "由于摘要未披露逐项指标，这里不抄具体数字。真正要确认的是：**它的增益是否来自频率分解本身**。"
                                    "一个可靠的判断方法是看消融——去掉 FCT 或换成单分支后，性能掉多少。"},
            {"type": "h2", "text": "04 | 代价与适用边界"},
            {"type": "bullets", "items": [
                "**双分支意味着两次前向**：相比单分支网络，显存与延迟都会上去，端侧部署需要重新评估预算。",
                "**频率分解依赖退化假设**：如果真实场景的摩尔纹不满足「高频纹理 + 低频色偏」的划分，分支的职责就会错位。",
                "**合成数据到真实拍摄的鸿沟**：摩尔纹数据集长期依赖合成，织物这类半周期图案尤其难合成。",
                "**与 ISP 的耦合**：相机内部去摩尔纹通常在 RAW 域做，本文方法在 sRGB 域验证，落地时还要过一遍链路适配。",
            ]},
            {"type": "quote", "text": "值得注意的是，论文强调的是紧凑模型尺寸下的 SOTA，而非绝对指标领先——这两件事在工程选型里权重完全不同。"},
            {"type": "h2", "text": "05 | 顺带值得一看"},
            {"type": "bullets", "items": [
                "**织物图像去摩尔纹基准**（ECCV 2026）：首个针对织物的基准，含 16,050 对合成图像，解决了真实配对数据难获取的问题。",
                "**RawJPEG Adapter**（ECCV 2026）：让 Raw 数据塞进标准 JPEG，用注释字段存变换参数，做端侧 Raw 管线的人可以关注。",
            ]},
        ],
        "takeaway": "如果你在做屏幕/织物拍摄的复原，这套「先分解再分治」的思路值得试；但要先算清双分支的端侧延迟账。",
        "sources": [
            {"name": "Freqformer: 基于有效频率分解的图像去摩尔纹 Transformer (ECCV 2026)", "url": "https://arxiv.org/abs/2505.19120"},
            {"name": "熵视 · 底层视觉论文精选", "url": "https://lowlevelcv.com/papers-selection"},
        ],
    },
    "industry": {
        "title": "复原模型开始卷「真实场景」，但真正的分水岭在数据",
        "subtitle": "从刷榜到落地，底层视觉这一年变了什么",
        "digest": "当合成数据上的指标越来越难拉开差距，底层视觉的竞争正在转向真实退化建模、推理成本与端侧部署。",
        "lead": [
            "如果你最近在刷 arXiv 的 cs.CV，会有一种明显的体感：单纯在合成退化上刷 PSNR 的论文变少了，"
            "标题里带 real-world、in-the-wild、efficient 的变多了。",
            "这不是文风变化，而是评价体系在变。当一条赛道的第一名和第二十名只差零点几个 dB，"
            "继续卷指标就很难说明问题，大家开始换问题问：**在真实拍摄的退化上，它还成立吗？**",
        ],
        "sections": [
            {"type": "h2", "text": "01 | 发生了什么"},
            {"type": "p", "text": "过去一年，底层视觉的几个方向出现了同一个转向：从「造更复杂的网络」转向「造更可信的退化」。"},
            {"type": "bullets", "items": [
                "**数据集成为主战场**：织物摩尔纹、真实低光、复合退化（雨滴 + 反光）等此前缺基准的场景，开始出现成对的真实数据与系统评测。",
                "**生成式先验进入复原流水线**：扩散模型不再只做生成，而是作为先验约束重建过程，用来补回高频细节。",
                "**效率被摆上台面**：端侧、相机 ISP、视频会议等场景开始要求实时，参数量与延迟成为论文里的必报项。",
            ]},
            {"type": "p", "text": "这三件事指向同一个底层逻辑：**复原任务正在从「算法问题」变成「数据 + 系统问题」**。"},
            {"type": "h2", "text": "02 | 技术上看，真正的变化在哪"},
            {"type": "p", "text": "以往的做法是先假设退化模型（ bicubic、加性高斯噪声、大气散射模型），再在仿真数据上训练。"
                                    "这条路的天花板很明確：真实退化往往不服从这些假设。"},
            {"type": "numbered", "items": [
                "**退化建模从手工假设转向数据驱动**：不再假设噪声是高斯分布，而是从真实配对数据里学退化分布。",
                "**从单任务走向复合退化**：真实场景里雨滴和反光同时出现、摩尔纹伴随色偏，单任务模型会互相打架。",
                "**从保真度单目标走向保真 + 感知的多目标**：PSNR 高的结果往往过平滑，感知指标与下游任务被一起纳入评估。",
            ]},
            {"type": "highlight", "title": "核心判断", "text": "接下来的差距不在网络结构，而在「谁能拿到成对的真实退化数据，并把评测做得可信」。"},
            {"type": "h2", "text": "03 | 对底层视觉意味着什么"},
            {"type": "p", "text": "对做研究的人，选题逻辑要调整：**先找到评测缺失的场景，再谈方法创新**。"
                                    "一个能被复现的新基准，影响力往往大于在旧基准上再涨 0.1 dB。"},
            {"type": "bullets", "items": [
                "**做算法的**：把消融做扎实，尤其是「增益来自哪个设计」这一层，审稿人和面试官都在看这个。",
                "**做工程的**：先测端侧延迟再选模型，双分支、扩散先验这类设计在移动端的代价经常被低估。",
                "**做产品的**：区分「演示可用」与「全天候可用」，真实场景的长尾退化才是用户投诉的来源。",
            ]},
            {"type": "h2", "text": "04 | 冷静一下"},
            {"type": "p", "text": "也要警惕另一种泡沫：把「用了扩散模型」当成创新本身。生成式先验确实能补细节，"
                                    "但它同时带来幻觉风险——重建出的纹理可能完全不存在于真实场景。"
                                    "在医学影像、司法取证这类场景，这是不可接受的。"},
            {"type": "quote", "text": "评估一套复原方案时，除了看它修出了什么，更要看它「编」出了什么。"},
            {"type": "p", "text": "另一个需要打折的是厂商口径的对比数据。官方 benchmark 通常挑了最有利的测试集与基线，"
                                    "第三方复现之前，最好把结论当成「上限」而不是「预期」。"},
        ],
        "takeaway": "底层视觉的下一轮竞争，属于能把真实退化说清楚、把成本算明白的团队。",
        "sources": [
            {"name": "熵视 · 底层视觉论文精选（ECCV 2026 专题）", "url": "https://lowlevelcv.com/papers-selection"},
            {"name": "arXiv cs.CV 最新论文", "url": "https://arxiv.org/list/cs.CV/recent"},
        ],
    },
    "contest": {
        "title": "本期值得打的底层视觉赛事：3 场，2 场偏真实场景",
        "subtitle": "赛题方向、奖金与截止时间一次看完",
        "digest": "本期整理了 NTIRE、MIPI 与天池的三场底层视觉赛事，重点标注赛题方向、时间节点与参赛门槛。",
        "lead": [
            "本期挑了 3 场与底层视觉直接相关的赛事：两场聚焦真实场景复原，一场偏移动端影像。"
            "共同点是都提供 baseline 与标准评测，适合作为阶段性的练手目标。",
            "如果你只有一份算力预算，建议优先选数据模态与手头工作最接近的那场——**迁移成本比奖金更值得算**。",
        ],
        "sections": [
            {"type": "h2", "text": "01 | 本期值得关注的赛事"},
            {"type": "contest_card", "name": "NTIRE 2026 · 图像复原系列挑战赛",
             "track": "超分 / 去噪 / 去模糊 / 真实场景复原",
             "prize": "以官方页面公布为准",
             "deadline": "待官方公布（通常于 CVPR Workshop 前截止）",
             "entry": "https://www.cvlai.net/ntire/2026/",
             "desc": "底层视觉领域最成体系的赛事之一，赛道多、baseline 完整，适合作为第一次参赛的起点。"},
            {"type": "p", "text": "NTIRE 的赛道通常按退化类型切分，报名时先确认评测指标是 PSNR 导向还是感知导向——"
                                    "两者的最优解往往相反，别用同一套模型打所有赛道。"},
            {"type": "divider"},
            {"type": "contest_card", "name": "MIPI 2026 · 移动端影像挑战赛",
             "track": "RAW 域去噪 / 去马赛克 / 低光增强",
             "prize": "以官方页面公布为准",
             "deadline": "待官方公布",
             "entry": "https://mipi-challenge.org/",
             "desc": "偏移动端与 ISP 链路，数据是真实拍摄的 RAW，工程价值高，对端侧效率有隐性要求。"},
            {"type": "p", "text": "这类比赛的隐藏门槛在数据管线：RAW 到 sRGB 的转换、黑电平、镜头阴影校正，任何一步处理错了，"
                                    "离线指标都会莫名其妙地低。"},
            {"type": "divider"},
            {"type": "contest_card", "name": "天池 · 真实场景图像复原赛",
             "track": "真实退化建模 / 图像增强",
             "prize": "以官方页面公布为准",
             "deadline": "以官网赛程为准",
             "entry": "https://tianchi.aliyun.com/competition/gameList/activeList",
             "desc": "国内平台，中文赛题与社区支持友好；真实场景数据往往带有明显的长尾退化，适合练鲁棒性。"},
            {"type": "h2", "text": "02 | 时间线与怎么选"},
            {"type": "table", "header": ["赛事", "方向", "截止", "奖金", "难度"],
             "rows": [
                 ["NTIRE 2026", "超分/去噪/去模糊", "待公布", "以官网为准", "中"],
                 ["MIPI 2026", "RAW 域影像", "待公布", "以官网为准", "中高"],
                 ["天池 · 真实复原", "真实退化", "以赛程为准", "以官网为准", "中"],
             ]},
            {"type": "bullets", "items": [
                "**想快速出成绩**：选赛道多、baseline 完善的 NTIRE，先复现再改进。",
                "**想做工程沉淀**：选 MIPI，RAW 域经验可以直接迁移到手机影像团队。",
                "**想练鲁棒性**：选真实场景数据的天池赛，长尾退化更接近线上问题。",
            ]},
            {"type": "h2", "text": "03 | 参赛避坑"},
            {"type": "numbered", "items": [
                "**先读评测脚本，再读赛题**：指标实现里常藏着裁剪边界、色彩空间、归一化方式这些致命细节。",
                "**留出本地验证集**：官方测试集提交次数有限，别把它当调参集用。",
                "**别忽视推理约束**：部分赛道会限制参数量或运行时间，训练时就要把预算考虑进去。",
                "**组队优先补短板**：一个懂数据管线的队友，通常比多一个调模型的更有用。",
            ]},
            {"type": "highlight", "title": "一句提醒", "text": "所有时间与奖金以官方页面为准，本文只做线索整理；报名前务必核对赛程与规则版本。"},
        ],
        "takeaway": "先选与手头数据最接近的一场，跑通 baseline，再谈创新。",
        "sources": [
            {"name": "NTIRE 2026", "url": "https://www.cvlai.net/ntire/2026/"},
            {"name": "MIPI Challenge", "url": "https://mipi-challenge.org/"},
            {"name": "天池竞赛", "url": "https://tianchi.aliyun.com/competition/gameList/activeList"},
        ],
    },
}
