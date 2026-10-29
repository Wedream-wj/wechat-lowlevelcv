# 底层视觉论文笔记 · 内容中台

面向公众号「底层视觉论文笔记」的全自动内容生产系统：**抓取 → 筛选 → 大模型成稿 → 微信富文本排版 →（可选）草稿箱推送**，覆盖三条内容管线：学术论文（A）、业界进展（B）、赛事竞赛（C）。

![status](https://img.shields.io/badge/python-3.11%2B-blue) ![fastapi](https://img.shields.io/badge/FastAPI-0.110%2B-05998b)

---

## 1. 系统架构

```
                       ┌──────────────────────────────────────────────┐
                       │              Web 控制台 (web/index.html)      │
                       │   触发出稿 / 预览 / 推草稿 / 看 Prompt / 日志   │
                       └───────────────┬──────────────────────────────┘
                                       │ REST (FastAPI, app/api.py)
        ┌──────────────────────────────┼───────────────────────────────┐
        │ 模块 A 学术论文                │ 模块 B 业界进展                 │ 模块 C 赛事竞赛   │
        │ lowlevelcv.com 精选解析        │ RSS/Atom 通用解析              │ Kaggle API       │
        │ arXiv API + 插图提取          │ （机器之心/PaperWeekly/将门…）   │ 天池（宽松解析）    │
        │ Papers With Code（降级容错）   │ 关键词+权重过滤                 │ Serper/Tavily 检索│
        └──────────────┬───────────────┴───────────────┬───────────────┘
                       │  Item（统一素材模型）           │
                       ▼                               │
        ┌────────────────────────────┐                 │
        │ 筛选引擎 app/filters        │  关键词加权 + 会议等级 + 时效衰减 + 相似去重 + 多样性
        └──────────────┬─────────────┘
                       ▼
        ┌────────────────────────────┐
        │ 大模型写作 app/llm          │  三套 Prompt → ArticleSpec JSON（结构化稿，只写内容不写 HTML）
        └──────────────┬─────────────┘
                       ▼
        ┌────────────────────────────────────────────┐
        │ 排版引擎 app/render                          │  ArticleSpec → 微信内联样式 HTML / Markdown / 封面图
        └──────────────┬─────────────────────────────┘
                       ▼
        ┌────────────────────────────┐     ┌──────────────────────────┐
        │ SQLite 入库（app/db.py）    │ ──▶ │ 微信草稿箱（publish/）     │  图片上传换 URL → draft/add
        └────────────────────────────┘     └──────────────────────────┘
```

### 技术选型（及理由）

| 层 | 选型 | 理由 |
|---|---|---|
| 服务框架 | **FastAPI + Uvicorn** | 异步原生，抓取是 IO 密集；自带 OpenAPI 文档 |
| 抓取 | **httpx (async) + BeautifulSoup/lxml + feedparser** | 异步并发 + 容错重试（tenacity）；RSS 解析交给 feedparser 稳 |
| 调度 | **APScheduler (CronTrigger)** | 单机够用，cron 表达式写在 YAML 里；要扩展时把 `run_module` 抽成 Celery/Arq 任务即可 |
| 大模型 | **OpenAI 兼容客户端** | DeepSeek / 通义 / 智谱 / Moonshot / 自建 vLLM 都能接，只改环境变量 |
| 存储 | **SQLite（标准库）** | 素材池 + 成稿 + 运行日志，单文件零运维；量大可平滑换 Postgres |
| 排版 | **自研 ArticleSpec → 内联 HTML** | 公众号编辑器会过滤 `<style>`/class/部分 flex，只有全内联样式可控；结构化稿保证每期版式 100% 一致 |
| 图像 | **Pillow** | 本地生成 1200×675 封面（三套配色随模块切换），可选 PyMuPDF 渲染 PDF 首页兜底配图 |

### 关键设计决策

1. **模型只写内容，不写排版。** LLM 输出 `ArticleSpec` JSON（标题/导语/块序列/表格/卡片），HTML 由模板渲染。好处：版式永不跑偏、可对 JSON 做事实校验、运营可直接改 Markdown 后重排。
2. **两级筛选。** 规则（关键词加权 + 会议等级 + 时效指数衰减 + 相似标题去重 + 方向多样性）先砍掉 ~80% 噪声，大模型只做最后一公里的判断，省 token 且稳定。
3. **图文并茂的兜底链。** 熵视自带示意图 → arXiv HTML 版按 caption 挑架构图 → PDF 首页渲染 → 「配图位」占位块，任何一级失败不阻断出稿。
4. **可离线运行。** 没有 LLM Key 时自动降级为结构完整的占位稿（`LLCV_MOCK_LLM=1`），`python -m scripts.demo` 不联网即可预览三套版式。

---

## 2. 快速开始

```bash
cd wechat-lowlevelcv
python -m venv .venv && .venv\Scripts\activate      # Windows；mac/linux 用 source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # 填 LLM_API_KEY；要推草稿再填 WX_APPID/WX_SECRET

# 三条验证路径
python -m scripts.demo                    # ① 离线看排版：out/demo_*.html
python -m scripts.run_once paper --mock   # ② 离线跑全流程（占位稿）
uvicorn app.api:app --reload              # ③ 起控制台 http://127.0.0.1:8000

# 换网络/换机器后先体检：逐路打印哪路源能用、哪路被挡
python -m scripts.check_sources
```

控制台里点「生成一期」→ 完成后自动打开手机画框预览 → 确认后点「推草稿」，文章即出现在公众号后台草稿箱。

### 环境变量

见 `.env.example`。要点：

- `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`：任意 OpenAI 兼容端点；
- `LLM_REASONING_EFFORT`：推理模型（DeepSeek-R1/V4 等）建议设 `none` 关思考——思维链与正文共享 `max_tokens`，不关会出现「模型返回为空 / JSON 被截断 / 单篇数分钟」；留空则不下发该参数，非推理模型无需设置；
- `WX_APPID / WX_SECRET`：公众号后台「基本配置」获取；**需把服务器出口 IP 加白名单**，且账号需有草稿箱权限（认证订阅号/服务号）；
- `KAGGLE_USERNAME / KAGGLE_KEY`、`SERPER_API_KEY / TAVILY_API_KEY`：竞赛模块的可选增强源，不配则自动跳过；
- `LLCV_ADMIN_TOKEN`：设置后，触发出稿/推送的接口要求 `X-Admin-Token` 头。
- `HEADLESS_BROWSER`：可选，无头浏览器可执行文件路径。不配时自动找系统 Edge/Chrome（Windows 找 `msedge.exe` / `chrome.exe`，macOS 找 `/Applications/Google Chrome.app/...`）。赛事模块的 Codabench 与 Vue SPA 赛站正文依赖它；找不到时该源降级为「不可用」并进源告警，不影响其他模块。

---

## 3. 目录结构

```
wechat-lowlevelcv/
├── app/
│   ├── config.py            # 环境变量 + topics.yaml 加载
│   ├── models.py            # Item / Article / Figure
│   ├── db.py                # SQLite：items / issues / runs
│   ├── pipeline.py          # 全流程编排
│   ├── spec_tools.py        # ★ 章节编号连续性 + 移动端表格列压缩
│   ├── imaging.py           # ★ 图片格式归一（webp→png/jpeg，压到微信 1MB 内）
│   ├── api.py               # FastAPI 路由 + 后台任务
│   ├── scheduler.py         # APScheduler cron
│   ├── sources/             # 抓取层（每源一个模块，见下）
│   │   ├── base.py          #   异步 HTTP、重试、图片落盘（webp→png 归一）、HTML 清洗
│   │   ├── headless.py      #   无头 Edge/Chrome 渲染（绕过 iocaine 蜜罐与 Vue SPA）+ 蜜罐识别
│   │   ├── lowlevelcv.py    #   熵视精选（页面结构实测解析）
│   │   ├── arxiv.py         #   arXiv API + 架构图提取（HTML→PDF 兜底）
│   │   ├── paperswithcode.py#   PWC API（已做静默降级）
│   │   ├── industry.py      #   RSS/Atom 通用 + 逐源体检
│   │   ├── chinese_media.py #   量子位 / HackCV 直连（中文产业媒体，入选后回抓正文）
│   │   ├── github_trending.py #  GitHub 开源源（RSS 兜底 + 开源选题）
│   │   ├── contest_feeds.py #   AI 赛事雷达 / ML Contests / Codabench / FlyAI
│   │   └── competitions.py  #   Kaggle / 天池 / 搜索 / 策展页 / 官方详情核验
│   ├── filters/scoring.py   # 评分、去重、多样性选择
│   ├── llm/
│   │   ├── prompts.py       # ★ 三套写作 Prompt + ArticleSpec 规格
│   │   ├── client.py        # OpenAI 兼容客户端、JSON 修复、用量统计
│   │   └── mock.py          # 离线降级稿 + 演示稿
│   ├── render/
│   │   ├── wechat.py        # ★ ArticleSpec → 微信内联样式 HTML
│   │   ├── markdown.py      # ArticleSpec → Markdown（可编辑中间稿）
│   │   ├── preview.py       # 手机画框预览页（一键复制 Markdown / 富文本）
│   │   └── cover.py         # 1200×675 封面生成
│   └── publish/wechat_api.py# token 缓存 / uploadimg / add_material / draft/add
├── config/topics.yaml       # ★ 选题词表、数据源、阈值、排期、主题色——改这里不改代码
├── web/index.html           # 运营控制台
├── scripts/{demo,run_once,check_sources}.py
├── tests/                   # unittest：解析器用真实页面夹具
└── out/                     # 成稿 HTML/Markdown/封面/预览
```

---

## 4. 三条数据管线

### 模块 A · 学术论文

- **数据源**：`lowlevelcv.com/papers-selection`（最新 3 页，含分类/摘要/示意图/代码链接，`?current=N` 翻页）；arXiv API（`cat:cs.CV` + 关键词，按提交时间倒序）；Papers With Code（API 不稳定，静默降级）。
- **筛选**：核心词（low-level vision / super-resolution…）3 分、任务词（去雾/去噪/压缩…）2 分、外围词 1 分；会议等级（CVPR/ICCV/ECCV/NeurIPS/TPAMI…）与时效指数衰减加权；相似标题去重 + 同方向最多 2 条的多样性约束。阈值在 `topics.yaml → academic.filter`。
- **配图**：熵视示意图 / arXiv HTML 按 caption 关键词（architect/overview/pipeline…）挑架构图 / PyMuPDF 渲染 PDF 首页，三级兜底。
- **版式**：导语 → 论文信息卡（meta_card）→ 01 问题 → 02 方法拆解（架构图 + 模块讲解）→ 03 实验（表格）→ 04 代价与边界 → 05 顺带值得一看。

### 模块 B · 业界进展

- **数据源（四通道，互为兜底）**：
  - **中文产业媒体直连**（`app/sources/chinese_media.py`）：量子位首页列表 + HackCV 每日简报。HackCV 的一期简报会被拆成多条独立动态（跳过 arXiv 栏目，避免与论文模块撞题），量子位条目入选后回抓正文（`full_text`），否则只有一句摘要撑不起一篇解读。
  - **RSS**：`topics.yaml → industry.feeds`，官方博客（OpenAI News、Berkeley BAIR、HF、Google Research）为主。公共 RSSHub 实例在本机网络被代理拦截（实测全部 502），相关线路已置 `enabled: false`，自建实例后改回即可；单源最多取 `feed_max_items` 条（OpenAI 一次能吐 1300+ 条，不截断会淹没其他源）。
  - **GitHub 开源**（`app/sources/github_trending.py`）：按关键词 + `stars>=80` + 近 240 天有 push 搜索仓库，星标量折算成关注度分。它同时解决两个问题：RSS 被限流时模块不至于空转；开源工具/算法更新本身就是本模块的正式选题。
  - **赛道范围**：不限于底层视觉——CV / 深度学习 / AI / Physical AI / 大模型产业的模型发布、企业落地、开源工具、芯片算力、资本动作都覆盖。`industry.keywords`（core/boost/weak 三档）与 `industry.categories`（大模型、具身、芯片、开源、影像终端、资本、AI 应用）只作用于本模块；`max_per_source: 2` 防止一家聚合站包圆整期。
- **配图**：GitHub 仓库自带稳定的 OpenGraph 预览图（`opengraph.githubassets.com/1/{owner}/{repo}`，1200×630，官方素材），直接拿来当插图；媒体条目取列表缩略图（量子位的原图是全尺寸）。
- **源体检（重要）**：RSS 被反爬时 `feedparser` 不报错、只返回 0 条——这是这类系统最典型的静默故障。代码把每路源的 `count / error / 返回内容前 120 字` 记进 `industry.LAST_REPORT`，并通过 `/api/sources → rss_health` 与控制台的「源告警」暴露，例如：
  `RSS·机器之心 → ProxyError: 502 Bad Gateway`。
- **写作立场**：开篇必须用「理想 vs 现实 / 学术卷指标 vs 工业卷落地 / 热度 vs 工程价值」的反差做 Hook，禁止「近日/随着/XX 宣布」式流水账；标题走科技产业媒体结构（主体 + 冲突动作 + 标志性结果，数字只来自素材）。
- **收尾节**：统一叫「**04 | 写在最后**」（旧的「冷静一下」已弃用，`spec_tools.normalize_section_titles` 会在渲染前把别名收敛回正式名）。这一节承担全篇最高的 Insight 密度，按「成本结构 → 演进路径 → 可检验的终极判断」三步收束，信息边界要嵌进判断里说，禁止流水账、心路历程与宏大叙事。
- **信息卡自适应**：`meta_card` 的标题按主体切换为 `PAPER INFO` / `PROJECT INFO` / `INDUSTRY SNAPSHOT` / `COMPETITION INFO` / `EVENT SNAPSHOT`——业界稿里开源项目卡写 `PAPER INFO` 会直接出戏，渲染层（`resolve_card_label`）会按模块与行内容兜底（旧别名 `CONTEST INFO` 会被收敛到 `COMPETITION INFO`）。
- **产业事件卡的行**：只保留「事件 / 时间 / 影响面」三行。主体和事件并成一句「谁干了什么」（`Runway 开源世界动作模型 Praxis-1`），**不单列「规模或金额」**——数字写进这句事或标题里；模型漏改时 `spec_tools.normalize_meta_cards` / `merge_event_rows` 会在成稿与渲染两处兜底（金额折进事件句的括号内，不丢信息），开源项目卡（`PROJECT INFO`）不参与合并。

### 模块 C · 赛事竞赛

- **数据源（六通道）**：
  - **Kaggle** 官方 API（结构化最好，`teamCount` 可判断赛事量级）；**天池**（非公开接口，宽松解析 + 降级）；
  - **AI 赛事雷达**（`app/sources/contest_feeds.py::fetch_radar`）：`ye4wzp.github.io/ai-contest-radar` 的 Astro 页面内嵌 `window.__DATA__`，一次吐 800+ 条赛事，是目前覆盖面最广的一路（含天智杯、CCF、地方赛、企业赛）；
  - **ML Contests**（`fetch_mlcontests`）：`data-competitions` 属性里是一段 HTML 转义过的 JSON，`html.unescape` 后直接 `json.loads`；
  - **Codabench**（`fetch_codabench`）：DRF 可浏览 API 页里 `pre.prettyprint` 内嵌完整 JSON；该站直连会命中 iocaine 蜜罐（返回《1984》随机段落 + 状态码 200），需无头浏览器兜底；
  - **FlyAI**（`fetch_flyai`）：主站上瑞数验证码（无头浏览器同样拦），改走静态镜像；这类常年开赛的练习型赛事标 `evergreen` 并降权；
  - Serper/Tavily 搜索补 CVPR/ECCV Workshop、CCF 赛事；NTIRE/MIPI 策展页兜底。
- **赛道范围**：不限于底层视觉，覆盖 CV / 深度学习 / 大模型 / 具身 / 多模态 / AI for Science。`competitions.keywords`（core/boost/weak 三档）只在打分阶段叠加权重，不做硬过滤——底层视觉的赛事数量撑不起日更，泛 AI 赛事里反而更容易挑到有方法论的题。
- **强实时性过滤**：`filter_active` 丢掉已过截止、超出 120 天窗口、无任何时间线索的条目；`registration_closed()` 用双证据判报名状态——命中「报名已结束/已截止」等状态词即关，否则取文本里最早的「报名截止 <日期>」，已过即关。**只看「决赛在未来」不算还能报名**（实测天池踩过的坑：报名 10-26 已闭、决赛 11-16 仍未到）。
- **页面倒计时优先**（`registration_status`）：天池在报名开着时会渲染「报名剩 03 日 14 时」，而同一页的「2026-05-31 报名与资格赛结束」讲的是**资格赛阶段**。带计数的倒计时一旦出现就直接判「报名开着」（「立即报名」这种按钮文案不算证据），并作为 `registration_hint` 原句进素材与卡片「报名」行——否则模型会把资格赛结束写成「报名已结束」。
- **质量门槛**：奖金为 Kudos/积分且队伍 < 200 的 Kaggle 课堂练习赛直接丢弃——它们以 18.9 的高分霸榜，会挤掉真赛事；真金白银 +2 分，队伍 ≥200 +1.5 分。
- **单场精读（核心变化）**：**一期只写一场赛事**。pipeline 先取 5 条候选，逐条抓官方详情页核验报名状态，遇 `registration_closed` 顺延；全截止则取最高分那场并切到「赛题拆解 + 复盘」视角。最终 `selected = [chosen]`，禁止一篇推文盘点多场比赛。
- **七个维度**：赛事背景 / 赛事任务 / 数据说明 / 评估指标 / 赛事周期 / 奖项设置 / 专业解题思路与 Baseline 引导；其中破局思路（Grandmaster 视角）篇幅不少于三成。**缺失字段一律写「待官方公布」，绝不编造。**
- **官方详情页抓取**（`competitions.fetch_contest_detail`）：普通请求 → 静态文本 <1200 字则走无头渲染兜底；产出 `official_brief`（官方赛题正文 3500 字）/ `page_excerpt`（2000 字）/ `timeline` / `prize_hint` / `metrics_hint` / `registration_closed`，一并塞进 LLM 素材。
- **信息卡**：按行内容自适应 `COMPETITION INFO`（赛事名/主办/赛题方向/报名状态/奖金池/时间节点/入口）或 `EVENT SNAPSHOT`（复盘视角下的关键节点快照）。
- **收尾节**：统一「**06 | 写在最后**」，落到赛题背后的方法论迁移，而不是战绩总结。
- **成稿兜底（代码层，不靠模型自觉）**：
  - `strip_meta_time()`：删掉「当前日期 2026-10-28，距报名关闭只剩 3 天」这类元信息句——读者不需要知道编辑哪天写稿，跨天推送后这句话立刻自相矛盾；命中即整句删，删空的块直接移除。
  - `strip_countdown_title()`：报名已关时摘掉标题里的「N 天报名倒计时/距报名截止只剩 N 天」，删完不足 8 字则放弃（宁可不改也不把标题削成半个词）。
  - `days_left` 只按 `extra.deadline` 算，未必等于报名截止；`registration_closed` 为真时不下发该字段。

---

## 5. Prompt 设计（app/llm/prompts.py）

三套 prompt 共享一个 System（人设 + 文风 + 硬性约束 + JSON 规格）+ 模块化任务卡：

- **统一文风约束**：拒绝摘要腔；「问题—做法—代价」叙事；段落 ≤ 4 行；术语中英对照；禁用词表（震惊/炸裂/王炸…）；
- **事实纪律**：所有数字必须来自素材；次要指标没给就直接省略（不写「未披露/无法量化」）；评价要落在具体设计上；
- **结构化输出**：`ArticleSpec`，块类型 `h2/h3/p/bullets/numbered/quote/highlight/meta_card/figure/table/contest_card/divider`；
- **容错**：强制 JSON mode + 本地括号配平修复 + 失败把上次输出回传让模型自修，两次失败才报错；
- 控制台「看 Prompt」按钮可直接查看完整 Prompt。

## 6. 微信排版转换器（app/render/wechat.py）

- 全部样式内联（公众号过滤 `<style>` 与 class）；布局用 `section/p/table`，不用 flex/grid；
- 主色随模块切换（学术蓝 / 青色 / 暖橙），小节编号胶囊、引用块、高亮卡、信息卡（标签按主体自适应为 PAPER INFO / PROJECT INFO / INDUSTRY SNAPSHOT / COMPETITION INFO / EVENT SNAPSHOT）、赛事卡、深色「一句话总结」卡片；
- 图片 `width:100%` 自适应；表格 `table-layout:auto` + 首列方法名不换行，列数上限 5 列（见 `app/spec_tools.py`：组稿前把素材表压到「方法 + 4 个核心指标」，被裁指标由正文文字承担），5 列以上自动收窄内边距与字号，手机端无需横向滚动；
- 小节编号在代码层强制连续：`renumber_sections()` 按出现顺序重排 01,02,03…，单篇素材省略「总结」也不会出现 04 → 06 的断层；
- 预览页两个复制入口（Clipboard API + execCommand 兜底）：**「复制 Markdown」**（首选——公众号编辑器支持 Markdown 粘贴，另带「查看 Markdown」面板可手动全选复制）与「复制富文本」；后者粘贴进公众号编辑器即保留内联样式，公众号不认 Markdown 时用它兜底；
- 推草稿时本地图片自动走 `media/uploadimg` 换成微信 CDN URL（同一张图只传一次）；
- **图片格式归一**（`app/imaging.py`）：微信只收 jpg/png、正文图 ≤1MB，量子位/HackCV 大量使用 webp，直接推会回 `40005 invalid file type hint`。下载落盘时先把 webp/gif/avif 转 PNG（带透明）或 JPEG（照片，质量 88 起逐级降），上传前再兜一次并压到体积上限，所以历史旧稿也能推。

## 7. API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 运营控制台 |
| GET | `/api/health` | 模型/微信/调度状态 |
| GET | `/api/stats` | 素材池、成稿、token 用量 |
| POST | `/api/run/{module}` | 触发出稿（后台任务），body 可传 `publish`、`top_n` |
| GET | `/api/jobs/{id}` | 任务进度 |
| GET | `/api/issues` · `/api/issues/{id}` | 成稿列表 / 详情 |
| GET | `/preview/{issue_id}` | 手机画框预览 |
| GET | `/markdown/{issue_id}` | 该期 Markdown 原文（`text/markdown`，可直接复制/另存） |
| POST | `/api/issues/{id}/push` | 推公众号草稿箱 |
| GET | `/api/prompts/{module}` | 查看完整 Prompt |
| GET | `/api/sources` · `/api/orphans` | 数据源清单 / 未消耗素材 |

## 8. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖（146 例）：lowlevelcv 解析器（用真实页面夹具）、评分与筛选、JSON 修复、Spec 校验、微信渲染（内联样式/转义/内联语法）、Markdown 导出、图片格式归一、四个赛事源的解析与蜜罐识别、报名状态判定与练习赛门槛。

## 9. 部署建议

- **单机**：`uvicorn app.api:app --host 0.0.0.0 --port 8000` + systemd/NSSM 守护；cron 已内置；
- **容器**：装好依赖后 `CMD uvicorn app.api:app`，挂载 `./out`、`./data`、`.env`；
- **扩展**：量上去后把 `run_module` 移入 Celery/Arq worker，Web 层只投递；SQLite 换 Postgres 只需改 `db.py`；
- **合规提醒**：公众号内容版权属于原作者，业界模块务必基于 RSS 线索做二次解读并注明来源；自动发布的文章建议保持「草稿箱 → 人工确认 → 群发」的最后一道人工闸门。

## 10. 已知边界

- Papers With Code 公开 API 2025 年后极不稳定，代码里做了静默降级，主源不受影响；
- 天池无公开 API，接口变更时会自动跳过（看运行日志的 `errors` 字段）；
- 公众号 RSS 依赖 RSSHub 可用性，建议自建实例；公共实例挂了会自动落到 GitHub 开源源，但「公众号原文」这一路就缺了；
- GitHub 搜索接口未鉴权时限流 10 次/分钟，代码已按 6.5 秒间隔串行；填 `industry.github.token` 可提速到 30 次/分钟；
- 本机实测（2026-10 网络环境）：`openai.com/news/rss.xml`、`bair.berkeley.edu`、`api.github.com` 可访问；`jiqizhixin.com`、`huggingface.co`、`research.google` 及 RSSHub 公共实例被代理拦截（502）。这类地域性差异应通过 `/api/sources` 的体检结果确认，不要凭代码假设；
- 草稿箱接口要求认证订阅号/服务号；未认证账号请用预览页的「复制 Markdown」或「复制富文本」手动粘贴。
- 赛事源的时效性天然衰减快：雷达条目里 `signup=closed` 的直接丢，但「报名已结束」常常只写在详情页正文里，所以精读前必须逐条抓官方页核验（`fetch_contest_detail`），这一步单条约 3–8 秒；候选全部截止时会退化成复盘稿，而非硬凑一场。
- FlyAI 主站上瑞数（验证码 + 动态 JS），无头浏览器同样被拦，只走静态镜像；镜像更新有延迟，新赛事可能晚几天出现。
- Codabench 对无 `User-Agent` 的直连返回蜜罐页（《1984》随机段落、状态码 200），必须靠 `looks_like_honeypot` 识别后转无头渲染，否则会把小说段落当赛题喂给模型。
