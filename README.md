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
| 图像 | **Pillow + PyMuPDF** | 封面优先用素材真图（架构图 / teaser / 官方视觉），PyMuPDF 从论文 PDF 抠图；没有合格图就本地绘制 1080×460（2.35:1）品牌底图 |

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
- `WX_ORIGINAL_TYPE / WX_COLLECTION`：推草稿的发布属性，默认 `文字原创` / `每日推送`，每次推送必带（别名「原创」「图文原创」自动归一，写错值在推送前报错）；
- `WX_DRAFT_EXTRA_FIELDS`：把上面两项直接写进 `draft/add` 载荷（微信当前不支持这两个字段，默认 0；接口开放后置 1 即生效）；
- `WX_UI_FILL / WX_STORAGE_STATE`：用 Playwright 回后台自动勾原创、选合集。需 `pip install playwright`（复用系统 Edge/Chrome，无需下 chromium）+ 一份已登录的 `storage_state`；未开启或没登录时推送照常，只是在控制台列出待办；
- `KAGGLE_USERNAME / KAGGLE_KEY`、`SERPER_API_KEY / TAVILY_API_KEY`：竞赛模块的可选增强源，不配则自动跳过；
  NTIRE 报告的社区复盘检索**不依赖这两个 key**（CSDN 官方搜索接口 + Bing site: 检索都免 key）；
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
│   │   ├── cvf.py           #   CVF Open Access · NTIRE 官方报告 + 冠军方案定向检索
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
│   │   ├── cover.py         # 1080×460（2.35:1）封面渲染：素材图 + 左暗蒙版 / 品牌底图
│   │   └── cover_select.py  # ★ 封面选图：按模块差异化挑图、体检、打分，不达标退底图
│   └── publish/           # 微信草稿箱：attrs.py（原创/合集固化）· mp_ui.py（后台补勾）· wechat_api.py
│       ├── attrs.py        # ★ 发布属性：原创声明「文字原创」+ 合集「每日推送」的固化/断言/审计
│       ├── mp_ui.py        # 可选 Playwright 回后台勾原创、选合集（缺依赖/缺合集都有明确日志）
│       └── wechat_api.py   # token 缓存 / uploadimg / add_material / draft/add
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
- **指定论文名称检索成稿**（2026-11-03 新增）：控制台论文卡片的「按论文名精读」输入框 / `python -m scripts.demo paper --query "<论文名>"` / `POST /api/run/paper {"query": "..."}`。链路（`sources/paper_search.py`）：**OpenAlex → Crossref → Europe PMC → Semantic Scholar → Google Scholar → ScienceDirect → IEEE Xplore** 七路并发，各自留 trace（成功/被反爬/超时都点名）；归一化标题相似度（SequenceMatcher + 词级 Jaccard）≥ 0.72 才算命中——宁可报「未找到」，绝不把同领域的相似标题论文当成目标；多源记录按 DOI/标题合并（OpenAlex 元数据最全、Crossref 期刊名最权威，只补缺不覆盖）。全文链：OA pdf → 出版商 DOI 落地页 `citation_pdf_url` → Semantic Scholar OA 镜像（`.env` 配 `S2_API_KEY` 走独立配额，匿名池实测长期 429）→ **Sci-Hub 默认禁用**（`academic.paper_search.scihub_enabled`，镜像分发未授权副本，默认只在开放获取/授权渠道内工作）。素材提取（`sources/paper_pdf.py`）双通道：Europe PMC 全文 XML 给干净表格（含「IoU 分组 × R/P/F1」两行表头的自动合并 `R@0.5`）、数值结论句与 32000 字全文；PDF 只干「按图注区域整块渲染插图」——架构图/流程图常由几十个图块拼成，取图注上方位图+矢量绘图并集以 2.5x+ 缩放整块渲染（`Flowchart/architecture` 判 arch，定性示例判 result），表格若走 PDF 通道必须过单元格合理性校验（竖排断行如「8409\n.」整表判废，宁缺毋假）。成稿 100% 继承论文模块全套规范：venue 信誉规则（Crossref 核实的期刊名上标题）、数字逐字核对、总结纯粹性、推草稿「文字原创 + 每日推送」。同名指标（含中文指标名→英文表头）的跨表差值已加入数字核对白名单（`_METRIC_ALIASES`），「精确率 80.22→84.00，提高 3.78」不再误判为编造，跨指标蒙差值（PSNR 差冒充 MUSIQ 差）仍然拦截。
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
- **选题三闸（防重复 / 防串题 / 防编造）**：实测踩过两个坑——同一天连出两篇 OpenAI 融资稿（「砍掉三分之一：300 亿变 200 亿」与「砍掉 100 亿美元，估值降至 1.25 万亿」），以及在一篇融资快讯里夹进「ECCV 2026 论文 SparkVSR 开源，仓库 745 stars」。三条硬闸现在固化在代码里：
  - **闸门一 · 标题相似度**（`filters/scoring.dedupe_history`）：与近 90 天出稿标题归一化比对，`SequenceMatcher >= 0.85` 直接挡；
  - **闸门二 · 事件级查重**（`filters/events.py`）：标题相似度拦不住「同一事件换个数字再写一篇」，所以按 **核心实体 + 事件类型 + 时间窗口**（`industry.event_dedupe_days`，默认 14 天）判重。事件类型覆盖融资/并购/上市退市/监管/财报/人事/芯片算力/模型发布/战略合作/技术研究；实体 = 品牌词表 + 拉丁专名 + 中文机构后缀。防误杀的两条细则：宽松类型（模型发布/技术研究）要求共享产品专名或标题高度相似；两边各自点名了不同对象（`OpenAI 与 ServiceNow 合作` vs `OpenAI 与 Oracle 合作`）直接判为两件事。
  - **闸门三 · 同批合并**（`collapse_same_event`）：同一事件的多篇报道只留最高分那条做主条，其余挂成 `same_event_sources` 仅用于交叉核对，不再各出一篇。
  - **单一事件收敛**：`select` 只取 3 条「备战」，回抓正文后用 `pick_subject` 收敛为 **1 条主角**（优先有 `full_text` 的一手报道），其余候选根本不进 Prompt——一期多条动态正是串题的根源。
  - **成稿零噪声**（`spec_tools.prune_industry_offtopic`）：按句/按条/按信息卡行删除「论文·仓库·stars·更新日期」噪声、带「同期/此外」的第二家公司动态，以及不属于主线主体的来源链接；删空的章节连同标题一起移除，编号重排。只删不改，不动任何数字。
  - **实事求是**（`prompts.unsupported_industry_numbers`）：金额/比例/算力/千级量级必须能在主线素材原文（含回抓正文）里逐字找到；同一句素材里两数相减得到的结果算合法换算。命中即定向重写一次，仍不过则保留原稿并打日志待人工核对。
  - **标题自审**（`llm/title_audit.industry_title_issues`）：查「句式绕弯（把 X 从 A 到 B 里解出来）」「空话营销词」「缺主体」「夹带论文/仓库」；不过则只重出标题，再不过退回素材原题（会先剥掉「刚刚，/重磅/官宣」这类媒体营销前缀）。
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

#### C-1 · baseline 精读：两条素材路线

控制台上「生成 baseline 精读」（绿色按钮）走的是 `variant="baseline"`，整篇只精读**一份**学习资源。素材有两条路，Prompt 也不同：

| 路线 | 素材 | 讲什么 | Prompt |
|---|---|---|---|
| Datawhale 复现仓库 | README + 文件清单 + 关键代码片段 | 代码起点、数据洞察、升级 Roadmap | `contest_baseline` |
| **NTIRE 官方挑战赛报告** | 报告正文 + 榜单 + 冠军方案 + 社区复盘 | 赛道定义、评测协议、冠军拆解、榜单差距 | `contest_ntire` |

`build_user_prompt` 按素材里有没有 `ntire_report` 自动选 Prompt，不需要额外开关。

#### C-2 · CVF Open Access / NTIRE 报告链路（`app/sources/cvf.py`）

NTIRE（CVPR/ICCV/WACV Workshop 旗下最大的底层视觉挑战赛）每年会给每条赛道发一份
challenge report：赛题定义、数据规格、评测协议、完整榜单、各队方法描述全在里面，
是这条管线里信息密度最高、也最容易溯源的素材。

1. **发现**：`openaccess.thecvf.com/menu` → 近 N 届 workshop 入口 → 该届 NTIRE 页。
   注意最新一届的 href 是 `CVPR2026_workshops`（**没有** `/menu` 后缀），漏这一种最新一届就永远抓不到。
2. **建条**：NTIRE 页有 60+ 篇 workshop 论文，只留标题同时含 `NTIRE + 年份 + challenge` 的官方报告；
   实测 CVPR 2026 有 38 份。赛道名会去掉「The 3rd」「The Eleventh」这类序数，但**保留 Track 号**——
   社区检索是否命中同一赛道全靠它。
3. **深挖**（`enrich_report`）：
   - 摘要页取 `#abstract` 与 PDF 链接；
   - PDF **分段下载**前 `pdf_bytes`（默认 0.8MB）：整本 2–3MB 在慢网络下要三分钟，前 0.8MB 已经覆盖
     摘要、赛道定义、榜单表与全部方法小节，剩下的是参考文献；PyMuPDF 打开不完整 PDF 也能出前 20 页文本；
   - `parse_sections` 先**定位到方法章节**再按 `3.x` 切——报告前半部分是 2.1 训练集 / 2.2 验证集 / 2.3 指标，
     直接切会把「训练集划分」写成参赛方案；`Submitted Methods`、`Quantitative Comparison` 这类导语型小节跳过；
   - `parse_leaderboard` 认「Award Ranking / Leaderboard」表（名次 + 队名 + 分数三元组）；
     另一种「团队名 + 一串数字」的多列排版抽不成结构，改由 `results_table` 原样交给模型自己读数；
   - `report_stats` 只把 `participants/registrations` 计为人数，`submissions` 是提交次数，混用会让正文写错规模。
4. **冠军方案定向检索**（`search_community`）：
   - 查询词按 `NTIRE + 年份 + 赛道/方法名 + 冠军/方案` 组合；
   - CSDN 走官方搜索接口（免 key，JSON，摘要质量高）；知乎站内接口要签名，改走 Bing 的 `site:zhihu.com`；
   - 相关性硬门槛：标题/摘要里必须有 `NTIRE`，否则要求「年份 + 赛道词」同时命中——CSDN 常年混进
     「低照度图像处理综述」这类泛文；
   - `same_track`：命中结果若写的是同届**另一条赛道**（Track 2 vs Track 1），保留但降权并标记，
     Prompt 明令它只能佐证整届趋势，不能说成这条赛道的冠军方案。
5. **排序**：底层视觉赛道词命中的报告优先（超分/去噪/低光/RAW/HDR…），
   「画质评价 / 异常检测 / 审美评测」这类评审型赛道降权，让栏目主赛道先入选。

---

## 5. Prompt 设计（app/llm/prompts.py）

三套 prompt 共享一个 System（人设 + 文风 + 硬性约束 + JSON 规格）+ 模块化任务卡：

- **统一文风约束**：拒绝摘要腔；「问题—做法—代价」叙事；段落 ≤ 4 行；术语中英对照；禁用词表（震惊/炸裂/王炸…）；
- **四套任务卡**：`PAPER_PROMPT` / `INDUSTRY_PROMPT` / `CONTEST_PROMPT`（单场精读）+
  `CONTEST_BASELINE_PROMPT`（Datawhale 仓库精读）/ `CONTEST_NTIRE_PROMPT`（官方报告精读）；
- **措辞兜底**：`spec_tools.normalize_wording()` 在成稿出口把「抄作业 / 抄走 / 照抄 / 套用 / 搬运 / 白嫖」
  替换为「借鉴 / 迁移 / 复用」——Prompt 有禁令，但模型会换个说法回来（实测「最值得抄作业的一处」）；
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
- **发布属性固化**（`app/publish/attrs.py`）：每次推草稿必带 `原创声明=文字原创`、`合集=每日推送`。微信 `draft/add` / `freepublish/submit` **没有**这两个字段（开放社区官方回复「不支持」），所以分三层落地：① `WX_ORIGINAL_TYPE` / `WX_COLLECTION` 固化默认值（别名「原创」「图文原创」自动归一，写错值在推送前就报错，不会带着错声明发出去）；② 推送前 `assert_draft_payload` 断言必填字段与属性一致，`WX_DRAFT_EXTRA_FIELDS=1` 时把两个字段直接并进载荷（接口今后开放即生效，无需改代码）；③ `WX_UI_FILL=1` 时用 Playwright 回后台点选（`mp_ui.py`，按「草稿箱 → 标题 → 编辑 → 文字原创 → 每日推送」文本定位，缺合集/未登录/没装依赖都只降级成待办清单，绝不把成功的推送标成失败）。每次推送的属性快照落在 `out/assets/draft_attrs_<issue>.json`；
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

覆盖（406 例）：lowlevelcv 解析器（用真实页面夹具）、评分与筛选、JSON 修复、Spec 校验、微信渲染（内联样式/转义/内联语法）、Markdown 导出、图片格式归一、四个赛事源的解析与蜜罐识别、报名状态判定与练习赛门槛、赛事/业界封面选图、标题自审、观点内化、发布属性断言、业界「事件级查重 / 同批合并 / 主题纯粹性清洗 / 数字核对」（`tests/test_industry_focus.py`）、以及指定论文检索的「标题匹配 / 多源合并 / JATS 表格 / 图注区域渲染 / 单元格校验 / Sci-Hub 合规默认」（`tests/test_paper_search.py`）。

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
- 封面图（`app/render/cover_select.py`）只从「本条素材自己的页面」取图，三模块策略不同：论文走 arXiv HTML → PDF 抠图（teaser/架构/对比图）→ 列表页缩略图；业界走仓库 README（raw 被掐时自动换 gcore.jsdelivr 镜像）→ 项目官网 og:image；赛事只认官方视觉 / 数据集样例 / baseline 框架图，**分数不到阈值就退品牌底图，绝不硬凑**。所有候选过体检（尺寸 / 比例 / 空白图 / badge 头像），logo 走 contain 版式不裁切。**封面只出本期主角素材**（一期选 4 条时，绝不借第 3 条的配图当封面——文章写 A、封面是 B 是硬伤）；主角没有合格图就退品牌底图。阈值与画布尺寸在 `topics.yaml` 的 `cover:` 段可调。
- `raw.githubusercontent.com` 在部分网络下 TLS 握手直接失败（本机实测如此），README 相对路径图已挂 gcore / cdn 两级 jsDelivr 镜像兜底；CVF 的报告 PDF 与摘要页分目录（`/html/` ↔ `/papers/`），`pdf_url` 已按此重写。
- 成稿校验的 title 降级提取（`app/llm/client.py → recover_title`）：模型偶尔输出 5000+ 字符完整正文却漏写/改名 `title` 键（实测写过「标题」别名、把规格包进子对象），此前一票否决两次重写全灭。现在按可靠度降级四层：JSON 别名键 → 子对象解包 → JSON 外残留文本的 markdown 一级标题 / 加粗标题行 → 素材元数据标题；Prompt（SYSTEM 与 SPEC_GUIDE）同步加了「第一个键必须是 title」的强约束，重试消息也点名该字段。
- 论文封面优先级（2026-11-01 定稿）：**核心架构图（首选）> 亮眼定性效果图（次选）> teaser / SOTA 对比图（第三）**；来源侧穷尽 arXiv HTML → PDF 抠图（无架构图就去 PDF 再挖一趟）→ 列表页缩略图，全部体检合格的候选被阈值拦下时按最高分「救场」上封面，只有确实一张合适的图都找不到才退品牌底图。「本期主角」按**成稿标题**定位（模型会自选头条，未必是 selected[0]），主角对齐后 `order==0` 才真正等于「文章写谁、封面就是谁」。
- 业界封面强保障（2026-11-01 定稿，针对「封面空洞/无图」）：**核心产品图/芯片架构/车端实拍（首选）> 报道主视觉（正文首图 body-hero，多为发布会现场）> logo > 截图**。来源侧：enrich 阶段随正文抽取配图存 `cover_candidates`（量子位 og:image 实测是全站 logo、列表缩略图只有 ~200px，不抓正文图几乎必然退底图）；候选不足时选图阶段对原文链接现场回抓一次正文图（覆盖 HackCV/外部源）。**救场规则泛化**：industry 任何体检过关的实景图都允许低分救场（role=other 需横幅比例 ≥560px 宽），`pick_cover` 对业界稿实际不会返回 None——真正一张图都找不到才走科技感底图。角色词表新增 `product`（芯片/板卡/实车/样机…）与 `venue`（发布会/展台/现场…），业界稿封面主角同样按成稿标题对齐。
- 赛事封面四级渐进检索（2026-11-01 定稿，铁律「官方优先、相关匹配、尽力而为、不强求」）：**① 官方直采**——`contest_visual.py` 在赛事详情页抓取的同一次请求/渲染里抽官方视觉（og:image / JSON-LD image / banner·hero·keyvisual 容器 / 官方 Logo），SPA 站点（Codabench / 天池）静态页只有壳时由无头渲染兜底；**② 相关匹配**——数据集样例（图注判 dataset）、baseline 框架图（arch）、赛事现场（venue）、知乎/CSDN 冠军复盘里的方案图（related，仅前两级都无着落时才去抓）；**③ 尽力而为**——全无图时 `pick_cover` 返回 None，走品牌底图，**不报错、不卡死**；**④ 不强求**——判不出角色的正文图只有官方页第一张升格 related（且必须横幅比例 ≥560px），其余一律淘汰。角色权重：official 5.0 > logo 4.4 > dataset 4.2 > arch 4.0 > venue 3.8 > related 2.8；救场只限主题正当角色。**身份校验**挡赞助商 logo（logo 文件名必须对上域名/标题的实义词，NTIRE 页的 OPPO/快手 logo 实测全数拦下）、人物照（/speakers/ 等路径）与 GitHub 文字卡（opengraph.githubassets.com）一律不进候选。赛事词表（挑战赛/大赛/冠军/低光/基线…）已并入 `cn_words.py`，标题断行零拆词。验证脚本 `scripts/verify_contest_cover.py` 覆盖「有官方图 / 有相关图 / 无图降级」三场景。
- 赛事 / Baseline 稿「观点内化 + 去口语化」（2026-11-01 定稿，针对「社区复盘指出…」「CSDN 博主总结…」式搬运腔与「结论互相打架 / 这个指标太卷了 / 大佬们 / 碾压」式街头表达）：SYSTEM 与三个赛事 Prompt （CONTEST / BASELINE / NTIRE）写入**观点内化铁律**——外部复盘（CSDN / 知乎 / 公开复现记录）是内部参考资料，不是引用对象：消化后用主笔口吻直述；转述词（社区指出 / 有博主总结 / 检索结果显示…）与平台名一律不进正文，平台名只属于 sources 列表；段落按「数据 → 指标 → 结构 → 训练」重组，不按来源条目重组。NTIRE 稿 05 节由「社区复盘：冠军选手怎么调」更名为「**复现要点：冠军方案的工程细节**」（spec_tools 章节别名同步收敛旧名）。代码层 `scrub_community_attribution` 按句剥转述外壳（保留事实本体，平台名换成「公开复现记录」中性表述），`_CONTEST_ACADEMIC_FIXES` 把街头短语等价替换为学术口径（打架 → 定量权衡矛盾（Quantitative Trade-off）、太卷 → 竞争已趋于饱和、大佬 → 头部选手、碾压 → 显著优于/明显落后于）。实测 deepseek-chat 新 Prompt 直出即合规（05 节三条工程决策零转述、CSDN 只出现在参考来源），代码兜底零触发。
- 赛事标题「先自审再动笔」（2026-11-01 定稿，针对「76 个生物任务、9 大领域：BioXArena 把 LLM 智能体拉进湿实验门口」这类黑话标题）：Prompt（CONTEST_PROMPT / BASELINE / NTIRE 变体）写入**三闸自审 + 爆款公式**——① 黑话闸（禁生造隐喻「把 X 拉进 Y 门口」与论文术语「拓扑空间/流形/端到端对齐」）；② 空话闸（禁门口/之巅/元年/新纪元/前夜/里程碑/盛宴，判据：删掉后意思不变就是废话）；③ 可读闸（不做这个方向的人要能复述出「这场比赛让 AI 干什么」）。句式三选一：规模+首秀感 / 设问+规模 / 痛点+动作，标题 30–40 字（硬上限 56），前 20 字必须露 Hook。
  代码层 `app/llm/title_audit.py` 与发布前自审 `_sanity_check_contest_title`（pipeline）：自审不过 → 只重出标题的定向重写（自审报告原样喂给模型）→ 仍不过则确定性兜底（`polish_title` 删空洞修饰 / `compose_contest_title` 按「规模数字 + 赛事名 + 这场在测什么」重拼）；**每一级只接受自审通过的结果，没有合格候选就保留原题，绝不把标题改坏**。实测踩出的两个误报已修：量词表补「队/万/亿元」等（「1727 队抢 2 万奖金」曾因缺量词被判「没数字 Hook」）；赛事锚点放宽到两字重叠 + 平台/主办方名（「天池这场表格赛」曾对不上四字重叠被判「没有赛事名」）。mock 降级稿的赛事标题同样改走 `compose_contest_title`，不再用「XX 赛道的门槛在哪」模板句。验证：`tests/test_contest_title.py` 24 例；`scripts/verify_contest_title.py` 真调模型出 BioXArena 稿（deepseek-chat 实测产出「76 个真实实验、9 大领域，BioXArena 开赛：LLM 智能体能不能真做生物实验」，自审零问题）。
- 「总结」纯粹性：总结只收束正文实际展开过的论文，严禁突然冒出正文未铺垫的其他方法并横向拉踩（横向对比只属于「04 | 与 SOTA 的对比与分析」，且对手必须是该论文实验表里的方法）。Prompt 有铁律，代码层再兜一道（`prune_summary_offtopic`）：从其他候选论文标题抽专有名词式方法名（TinySR / PeCA 这类，Toward / Diffusion 这类普通词不算），出现在总结节/takeaway 且正文未铺垫的，直接剪句剪条目、剪空整节连标题拿掉；takeaway 因乱入被清空时触发一次定向重写补回。
- 封面标题语义断行（`app/render/cover.py` + `app/render/cn_words.py`）：曾两次翻车——先有贪心逐字折行把「真实红外超分」切成「…真实红 / 外超分」，后有单字粒度把「引导」切成「…用梯度引 / 导补齐…」。根治方案：**中文进词表分词**（`cn_words.segment_cjk`，零依赖最长匹配，覆盖底层视觉术语 + 通用书面词约 400 条，未登录字退单字原子），「引导」「超分」「感知缺口」等词从此是单个不可劈开的原子；DP 在词边界间做均衡断行，行首禁字/助词（的/了/着过 +400）、行首运算符（+16 → 200）、量词与数字焊死（硬禁 `_HARD_BREAK`）、介词行尾吊空（+220）、后置量纲词（「2K | 以上」+180）各有罚则。**动态字号兜底 `_fit_title`**：先争取两行（48→36px），两行在 32px 都放不下再放开三行重试（48→24px），极端长标题用最小字号 + 省略号——任何一步都不回头劈词。全模块（论文/业界/赛事横版封面与方图缩略图）共用同一条链路。实测「CVPR 2025 | DifIISR：用梯度引导补齐红外超分的视觉与感知缺口」→「…DifIISR：用梯度引导 / 补齐红外超分的视觉与感知缺口」（两行 40px，零拆词）。
- 学术化文风：SYSTEM 与 PAPER_PROMPT 加【学术文风禁用清单】（禁对读者摊牌式开头、口语动词「一起给/赢了/让出去」、设问句小节标题）；对比对象统一称「最先进的方法（SOTA）」。`spec_tools.normalize_wording(spec, module)` 新增 `_ACADEMIC_WORDING_FIXES`（仅 paper 模块）：「代价写在效率上」→「方法的局限性主要体现在计算效率与参数量上」、「最强对手」→「最先进的方法」、「X 是让出去的」→「X 属于主动的定量取舍（Trade-off）」等，成稿出口强制替换。
- 叙述纪律（推送即已筛选）：正文严禁把「读不读」的选择题丢回读者（「值不值得读，取决于你是否…」），统一改写成客观的应用价值陈述（「它的应用价值，依赖于人们是否接受以 PSNR 换取感知质量的定量权衡（Trade-off）」）；也严禁任何素材处理过程的旁白（「表格行列对齐存在错位」「按单元格原值呈现」「不做重排」「抓取受限」）——工序细节与读者无关，表格直接以整洁格式输出。SYSTEM 加【定位】与表达纪律第 4 条；代码层 `spec_tools.scrub_reader_burden`（有取舍信息的句子改写、纯反问句删除）与 `scrub_process_narration`（整句删除）在 pipeline 成稿出口再兜一道，删空的段落/bullet 整块拿下。
- 业界「选题去重 + 主题强收敛 + 实事求是」（2026-11-01 定稿，针对两起实测事故：同日连出两篇 OpenAI 融资稿；融资快讯里夹「ECCV 2026 论文 SparkVSR 开源，仓库 745 stars」）：Prompt 写入**单一事件铁律**——全篇只写一个核心事件，其它公司动态、同日其它消息、素材里的论文与仓库统计一律不得进入标题/导语/正文/信息卡/图注/takeaway/sources；判据是「删掉这句，主线判断是否受损」；禁用「同期还有/与此同时/另一条消息」拼盘句式。代码层三道闸 + 两道兜底：事件级查重（`filters/events.py`，实体 + 事件类型 + 窗口，默认 14 天）拦「换数字重写」；`collapse_same_event` 把同批重复事件并成一条主条，其余只作交叉核对信源；`pick_subject` 把 3 条备战收敛成 1 条主角（优先有回抓正文的一手报道）；`prune_industry_offtopic` 按句/按条/按卡行删噪声（论文·仓库·stars·更新日期、第二家公司动态、不属于主线的来源链接），删空的章节连标题一起拿掉再重排编号；`unsupported_industry_numbers` 要求金额/比例/算力/千级量级能在主线素材原文（含 `full_text`）逐字找到（同一句素材内两数相减算合法换算），命中即定向重写一次。标题侧新增 `industry_title_issues`（绕弯句式 / 空话营销词 / 缺主体 / 夹带论文仓库）与 `_sanity_check_industry_title`（模型只重出标题 → 兜底退回洗过的素材原题）。**已知取舍**：事件判重是启发式——中文公司名依赖词表 + 机构后缀，标题完全不点主体的重复稿只能靠标题相似度兜底；`event_dedupe_days` 调大会更保守（少重复、也可能少选题）。验证：`tests/test_industry_focus.py` 33 例；`scripts/verify_industry_focus.py` 联网体检三闸；`python -m scripts.demo industry` 实跑出稿（实测 178 条候选 → 标题闸挡 18 条 → 收敛为 DeepSeek Super-agent 单一事件，成稿零论文/仓库噪声、零第二家公司）。

- 指定论文名称检索（2026-11-03）：「全网」的诚实口径——OpenAlex/Crossref/EuropePMC/Semantic Scholar 四路 API 稳定可用，Google Scholar 无公开 API 且反爬（本机实测代理 502）、ScienceDirect/IEEE 检索页 403，这三路保留在链路里但失败只记 trace 不阻塞；出版商（MDPI/Elsevier）直链在本机网络被 Cloudflare 拦时，全文退 Europe PMC XML（OA 期刊）、插图退 Semantic Scholar OA 镜像，两者都不可得则封面走品牌底图、表格与数值句照常成稿。Sci-Hub 链路已实现但默认禁用（合规）。相似度阈值 0.72 是启发式：改写幅度极大的译名/缩写标题（如只输「PigMS」四个字符）会报「未找到」，这是刻意行为。
