"""FastAPI 服务：控制台 + 出稿 API。

接口设计围绕「运营能自助操作」：
- POST /api/run/{module}   触发一期（后台任务，返回 job_id）
- GET  /api/jobs/{job_id}  轮询进度
- GET  /preview/{issue_id} 手机画框预览
- POST /api/issues/{id}/push 推草稿箱
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import ROOT, settings, topics
from .llm.client import usage
from .llm.prompts import PROMPTS
from .pipeline import PipelineError, push_issue, run_module
from .scheduler import build_scheduler
from .sources import COLLECTORS, MODULE_LABEL
from .sources import industry

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


async def _run_job(job_id: str, module: str, publish: bool | None, top_n: int | None) -> None:
    JOBS[job_id].update({"status": "running", "started_at": datetime.now().isoformat()})
    try:
        result = await run_module(module, publish=publish, top_n=top_n)
        if result.article:
            JOBS[job_id].update({
                "status": "done",
                "issue_id": result.article.issue_id,
                "title": result.article.title,
                "stats": result.stats,
                "errors": result.errors,
            })
        else:
            JOBS[job_id].update({"status": "failed", "errors": result.errors, "stats": result.stats})
    except Exception as exc:  # 兜底，避免任务静默消失
        JOBS[job_id].update({"status": "failed", "errors": [{"stage": "runner", "error": str(exc)}]})


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
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "queued", "module": module, "created_at": datetime.now().isoformat()}
    background.add_task(_run_job, job_id, module, body.get("publish"), body.get("top_n"))
    return {"job_id": job_id, "module": module}


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
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.post("/api/issues/{issue_id}/push")
async def push(issue_id: str, request: Request) -> dict:
    _check_admin(request)
    try:
        article = await push_issue(issue_id)
    except PipelineError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "issue_id": article.issue_id, "media_id": article.wx_media_id, "status": article.status}


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
