"""FastAPI 服务：控制台 + 出稿 API。

接口设计围绕「运营能自助操作」：
- POST /api/run/{module}   触发一期（后台任务，返回 job_id）
- GET  /api/jobs/{job_id}  轮询进度
- GET  /preview/{issue_id} 手机画框预览
- GET  /markdown/{issue_id} 该期 Markdown 原文（text/markdown，便于直接复制/另存）
- POST /api/issues/{id}/push 推草稿箱
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db
from .config import ROOT, force_utf8_stdio, settings, topics
from .llm.client import usage
from .llm.prompts import PROMPTS
from .pipeline import PipelineError, push_issue, run_module
from .scheduler import build_scheduler
from .sources import COLLECTORS, MODULE_LABEL
from .sources import industry

# 模块导入即生效：无论 `python -m app.main` 还是 `uvicorn app.api:app` 启动都覆盖
force_utf8_stdio()

WEB_DIR = ROOT / "web"
JOBS: dict[str, dict] = {}
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    db.init_db()
    _scheduler = build_scheduler()
    if _scheduler:
        _scheduler.start()
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="底层视觉论文笔记 · 内容中台", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
app.mount("/out", StaticFiles(directory=str(settings.out_dir)), name="out")


def _check_admin(request: Request) -> None:
    token = os.getenv("LLCV_ADMIN_TOKEN", "")
    if not token:
        return
    if request.headers.get("x-admin-token") != token:
        raise HTTPException(status_code=401, detail="缺少或错误的 X-Admin-Token")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "llm": {"mode": "live" if settings.llm_ready else "mock", "model": settings.llm_model,
                "base_url": settings.llm_base_url},
        "wechat": {"ready": settings.wechat_ready, "author": settings.wx_author},
        "schedule": topics.schedule,
        "scheduler_running": bool(_scheduler and _scheduler.running),
    }


@app.get("/api/stats")
async def stats() -> dict:
    data = db.stats()
    data["usage"] = {
        "calls": usage.calls,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cost_cny": round(usage.cost_cny, 3),
    }
    return data


@app.get("/api/sources")
async def sources() -> dict:
    return {
        "modules": [
            {
                "key": module,
                "label": MODULE_LABEL.get(module, module),
                "sources": [{"name": name, "fn": f"{fn.__module__}.{fn.__name__}"} for name, fn in entries],
            }
            for module, entries in COLLECTORS.items()
        ],
        "rss": topics.rss_feeds,
        # 最近一次抓取的逐源体检（跑过才有 count，没跑过为 None）
        "rss_health": industry.feed_health(),
    }


@app.get("/api/prompts/{module}")
async def prompt(module: str) -> dict:
    if module not in PROMPTS:
        raise HTTPException(404, "未知模块")
    return {"module": module, "prompt": PROMPTS[module]}


async def _run_job(job_id: str, module: str, publish: bool | None, top_n: int | None,
                   variant: str | None = None, query: str | None = None,
                   pdf_path: str | None = None) -> None:
    job = JOBS[job_id]
    job.update({"status": "running", "started_at": datetime.now().isoformat()})
    # 进度与里程碑事件由 run_module 的回调实时写入，前端每 1.5s 轮询展示
    job["progress"] = {"stage": "启动", "pct": 1, "msg": "任务已启动"}
    job["events"] = []

    def on_progress(stage: str, pct: int, msg: str, event: bool = False) -> None:
        job["progress"] = {"stage": stage, "pct": int(pct), "msg": msg}
        if event:
            job["events"].append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "stage": stage,
                "msg": msg,
            })
            # 事件只留最近 200 条，防止超长任务把内存拖爆
            if len(job["events"]) > 200:
                del job["events"][: len(job["events"]) - 200]

    try:
        result = await run_module(module, publish=publish, top_n=top_n, variant=variant,
                                  paper_query=query if module == "paper" else None,
                                  paper_path=pdf_path if module == "paper" else None,
                                  progress=on_progress)
        if result.article:
            job.update({
                "status": "done",
                "issue_id": result.article.issue_id,
                "title": result.article.title,
                "stats": result.stats,
                "errors": result.errors,
            })
            job["progress"] = {"stage": "完成", "pct": 100,
                               "msg": f"已生成：{result.article.title}"}
        else:
            fatal = result.errors[-1] if result.errors else {"stage": "?", "error": "未知错误"}
            job.update({"status": "failed", "errors": result.errors, "stats": result.stats})
            job["progress"] = {"stage": "失败",
                               "pct": job["progress"].get("pct", 0),
                               "msg": f"{fatal.get('stage')}：{fatal.get('error')}"}
    except Exception as exc:  # 兜底，避免任务静默消失
        job.update({"status": "failed", "errors": [{"stage": "runner", "error": str(exc)}]})
        job["progress"] = {"stage": "失败", "pct": job["progress"].get("pct", 0), "msg": str(exc)}


@app.post("/api/run/{module}")
async def run(module: str, request: Request, background: BackgroundTasks) -> dict:
    _check_admin(request)
    if module not in COLLECTORS:
        raise HTTPException(404, "未知模块")
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    # 本地论文解读：body.pdf 携带本地 PDF 绝对路径（或上传端点返回的服务器路径）
    pdf = str(body.get("pdf") or "").strip() or None
    if pdf and module == "paper":
        if not Path(pdf).is_file():
            raise HTTPException(400, f"本地 PDF 不存在：{pdf}（请检查盘符与路径分隔符）")
        if not pdf.lower().endswith(".pdf"):
            raise HTTPException(400, f"只支持 PDF 文件：{pdf}")
    elif pdf:
        pdf = None  # pdf 参数仅对 paper 模块生效，其他模块静默忽略
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "queued", "module": module, "created_at": datetime.now().isoformat()}
    background.add_task(_run_job, job_id, module, body.get("publish"), body.get("top_n"),
                        body.get("variant"), (str(body.get("query") or "").strip() or None), pdf)
    return {"job_id": job_id, "module": module}


@app.post("/api/upload/pdf")
async def upload_pdf(request: Request, file: UploadFile = File(...)) -> dict:
    """本地论文解读的文件选择器桥接。

    浏览器安全模型拿不到本地完整路径（只给 C:\\fakepath\\xx.pdf），所以真
    「选择文件」必须把文件传到服务端再跑解析——服务器就在本机，上传等价于
    原地读取。保存到 out/uploads/，带时间戳前缀防同名覆盖。
    """
    _check_admin(request)
    name = Path(file.filename or "paper.pdf").name or "paper.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "只支持 .pdf 文件")
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "文件内容不是合法 PDF（缺少 %PDF 头）")
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(400, "PDF 超过 200MB 上限")
    dest_dir = settings.out_dir / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{name}"
    dest.write_bytes(data)
    return {"path": str(dest.resolve()), "name": name, "size": len(data)}


@app.get("/api/jobs/{job_id}")
async def job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "job 不存在")
    return JOBS[job_id]


@app.get("/api/issues")
async def issues(module: str | None = None, limit: int = 30) -> list[dict]:
    return db.list_articles(module, limit)


@app.get("/api/issues/{issue_id}")
async def issue(issue_id: str) -> dict:
    article = db.get_article(issue_id)
    if not article:
        raise HTTPException(404, "期号不存在")
    row = article.to_row()
    row["items"] = [
        {"title": i.title, "url": i.url, "source": i.source, "venue": i.venue, "score": i.score}
        for i in article.items
    ]
    return row


@app.get("/preview/{issue_id}", response_class=HTMLResponse)
async def preview(issue_id: str) -> HTMLResponse:
    path = settings.out_dir / f"preview_{issue_id}.html"
    if not path.exists():
        raise HTTPException(404, "预览不存在，请先生成")
    html = path.read_text(encoding="utf-8")
    # 预览页里的图片是文件系统相对路径（assets/xxx.png），直接经 HTTP 返回时会被
    # 解析成 /preview/assets/xxx.png 而 404。这里只在响应时注入 <base href="/out/">
    # 把相对资源锚定到静态目录 /out 上；磁盘上的 preview_*.html 保持原样，
    # 以便运维直接双击打开时仍是同目录相对路径，两种打开方式互不干扰。
    if "<base " not in html:
        html = html.replace("<head>", '<head>\n<base href="/out/">', 1)
    return HTMLResponse(html)


@app.get("/markdown/{issue_id}")
async def markdown(issue_id: str) -> Response:
    """返回该期 Markdown 原文，控制台「复制 Markdown」和浏览器另存都走这里。"""
    # 期号来自 URL，只接受纯文件名，挡住 ..\ 与 ../ 之类的目录穿越
    if Path(issue_id).name != issue_id:
        raise HTTPException(404, "期号不合法")
    path = settings.out_dir / f"{issue_id}.md"
    if not path.exists():
        raise HTTPException(404, "Markdown 不存在，请先生成")
    return Response(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@app.delete("/api/issues/{issue_id}")
async def delete_issue(issue_id: str, request: Request) -> dict:
    """删除本地草稿：数据库记录 + 本地三个产物文件（md / html / preview）。

    封面图按「模块+日期」命名、可能被同天多期复用，不在此清理。
    已推送进微信草稿箱的内容不受影响——这里只管本地。
    """
    _check_admin(request)
    # 与 /markdown 同款防护：期号只接受纯文件名，挡住目录穿越
    if Path(issue_id).name != issue_id or issue_id in {".", ".."}:
        raise HTTPException(404, "期号不合法")
    if not db.get_article(issue_id):
        raise HTTPException(404, "期号不存在")
    removed: list[str] = []
    for name in (f"{issue_id}.md", f"{issue_id}.html", f"preview_{issue_id}.html"):
        path = settings.out_dir / name
        try:
            path.unlink()
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise HTTPException(500, f"删除 {name} 失败：{exc}") from exc
    db.delete_article(issue_id)
    return {"ok": True, "issue_id": issue_id, "removed_files": removed}


@app.post("/api/issues/{issue_id}/push")
async def push(issue_id: str, request: Request) -> dict:
    _check_admin(request)
    try:
        article = await push_issue(issue_id)
    except PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "issue_id": article.issue_id, "media_id": article.wx_media_id,
            "status": article.status, "attrs": article.wx_attrs}


@app.get("/api/wechat/check")
async def wechat_check() -> dict:
    """微信连接体检：验证配置 → 凭据/白名单 → 草稿箱权限，不产生任何草稿。"""
    from .publish.wechat_api import WeChatClient

    async with WeChatClient() as wx:
        return await wx.diagnose()


@app.get("/api/orphans")
async def orphans() -> dict:
    """素材池里长期未被使用的条目，方便判断选题是否过窄。"""
    out = {}
    for module in COLLECTORS:
        items = db.recent_items(module, limit=200, unused_only=True)
        out[module] = len(items)
    return out


@app.exception_handler(PipelineError)
async def pipeline_error_handler(request: Request, exc: PipelineError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
