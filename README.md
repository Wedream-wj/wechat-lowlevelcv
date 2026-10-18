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
- `WX_APPID / WX_SECRET`：公众号后台「基本配置」获取；**需把服务器出口 IP 加白名单**，且账号需有草稿箱权限（认证订阅号/服务号）；
- `KAGGLE_USERNAME / KAGGLE_KEY`、`SERPER_API_KEY / TAVILY_API_KEY`：竞赛模块的可选增强源，不配则自动跳过；
- `LLCV_ADMIN_TOKEN`：设置后，触发出稿/推送的接口要求 `X-Admin-Token` 头。

---

## 3. 目录结构

```
wechat-lowlevelcv/
├── app/
│   ├── config.py            # 环境变量 + topics.yaml 加载
│   ├── models.py            # Item / Article / Figure
│   ├── db.py                # SQLite：items / issues / runs
│   ├── pipeline.py          # 全流程编排
│   ├── api.py               # FastAPI 路由 + 后台任务
│   ├── scheduler.py         # APScheduler cron
│   ├── sources/             # 抓取层（每源一个模块，见下）
│   │   ├── base.py          #   异步 HTTP、重试、图片落盘、HTML 清洗
│   │   ├── lowlevelcv.py    #   熵视精选（页面结构实测解析）
│   │   ├── arxiv.py         #   arXiv API + 架构图提取（HTML→PDF 兜底）
│   │   ├── paperswithcode.py#   PWC API（已做静默降级）
│   │   ├── industry.py      #   RSS/Atom 通用 + 逐源体检
│   │   ├── github_trending.py #  GitHub 开源源（RSS 兜底 + 开源选题）
│   │   └── competitions.py  #   Kaggle / 天池 / 搜索 / 策展页
│   ├── filters/scoring.py   # 评分、去重、多样性选择
│   ├── llm/
│   │   ├── prompts.py       # ★ 三套写作 Prompt + ArticleSpec 规格
│   │   ├── client.py        # OpenAI 兼容客户端、JSON 修复、用量统计
│   │   └── mock.py          # 离线降级稿 + 演示稿
│   ├── render/
│   │   ├── wechat.py        # ★ ArticleSpec → 微信内联样式 HTML
│   │   ├── markdown.py      # ArticleSpec → Markdown（可编辑中间稿）
│   │   ├── preview.py       # 手机画框预览页（一键复制富文本）
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

- **数据源（双通道，互为兜底）**：
  - **RSS**：`topics.yaml → industry.feeds`，默认给了 RSSHub 路由（机器之心、PaperWeekly、将门创投、量子位、CVer）+ 官方博客（OpenAI News、Berkeley BAIR、HF、Google Research）。`optional: true` 表示「锦上添花」，挂了不算故障。
  - **GitHub 开源**（`app/sources/github_trending.py`）：按关键词 + `stars>=80` + 近 240 天有 push 搜索仓库，星标量折算成关注度分。它同时解决两个问题：RSS 被限流时模块不至于空转；开源工具/算法更新本身就是本模块的正式选题。
- **配图**：GitHub 仓库自带稳定的 OpenGraph 预览图（`opengraph.githubassets.com/1/{owner}/{repo}`，1200×630，官方素材），直接拿来当插图；RSS 条目则从 `media_content` / 正文 `<img>` 里取。
- **源体检（重要）**：RSS 被反爬时 `feedparser` 不报错、只返回 0 条——这是这类系统最典型的静默故障。代码把每路源的 `count / error / 返回内容前 120 字` 记进 `industry.LAST_REPORT`，并通过 `/api/sources → rss_health` 与控制台的「源告警」暴露，例如：
  `RSS·机器之心 → ProxyError: 502 Bad Gateway`。
- **写作立场**：不复述新闻，回答「对做底层视觉的人意味着什么」；厂商 benchmark 必须标注「官方数据，第三方复现待观察」。

### 模块 C · 赛事竞赛

- **数据源**：Kaggle 官方 API（结构化最好）；天池（非公开接口，宽松解析 + 降级）；Serper/Tavily 搜索补 CVPR/ECCV Workshop、CCF 赛事；NTIRE/MIPI 策展页兜底。
- **输出**：每场一张 `contest_card`（赛题方向/奖金池/时间节点/入口）+ 难度点评 + 时间线表格 + 避坑清单。**缺失字段一律写「待官方公布」，绝不编造。**

---

## 5. Prompt 设计（app/llm/prompts.py）

三套 prompt 共享一个 System（人设 + 文风 + 硬性约束 + JSON 规格）+ 模块化任务卡：

- **统一文风约束**：拒绝摘要腔；「问题—做法—代价」叙事；段落 ≤ 4 行；术语中英对照；禁用词表（震惊/炸裂/王炸…）；
- **事实纪律**：所有数字必须来自素材，找不到写「论文未披露」；评价要落在具体设计上；
- **结构化输出**：`ArticleSpec`，块类型 `h2/h3/p/bullets/numbered/quote/highlight/meta_card/figure/table/contest_card/divider`；
- **容错**：强制 JSON mode + 本地括号配平修复 + 失败把上次输出回传让模型自修，两次失败才报错；
- 控制台「看 Prompt」按钮可直接查看完整 Prompt。

## 6. 微信排版转换器（app/render/wechat.py）

- 全部样式内联（公众号过滤 `<style>` 与 class）；布局用 `section/p/table`，不用 flex/grid；
- 主色随模块切换（学术蓝 / 青色 / 暖橙），小节编号胶囊、引用块、高亮卡、论文信息卡、赛事卡、深色「写在最后」总结卡；
- 图片 `width:100%` 自适应；表格用 `table-layout:fixed` + 斑马纹；
- 预览页带「复制富文本」按钮（Clipboard API + execCommand 兜底），粘贴进公众号编辑器即保留样式；
- 推草稿时本地图片自动走 `media/uploadimg` 换成微信 CDN URL（同一张图只传一次）。

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
| POST | `/api/issues/{id}/push` | 推公众号草稿箱 |
| GET | `/api/prompts/{module}` | 查看完整 Prompt |
| GET | `/api/sources` · `/api/orphans` | 数据源清单 / 未消耗素材 |

## 8. 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：lowlevelcv 解析器（用真实页面夹具）、评分与筛选、JSON 修复、Spec 校验、微信渲染（内联样式/转义/内联语法）、Markdown 导出。

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
- 草稿箱接口要求认证订阅号/服务号；未认证账号请用「复制富文本」手动粘贴。
