"""定时任务：默认三个栏目各自 cron，全部可在 config/topics.yaml 里改。

调度器与 Web 服务同进程（适合单机/小团队）；如果要横向扩展，
把 `run_module` 抽成独立 worker（Celery/RQ/Arq），Web 层只投递任务即可。
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings, topics
from .pipeline import run_module


def build_scheduler() -> AsyncIOScheduler | None:
    if str(topics.schedule.get("enabled", "true")).lower() not in {"1", "true", "yes"}:
        return None

    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    async def job(module: str) -> None:
        result = await run_module(module)
        status = "ok" if result.article else "failed"
        print(f"[schedule] {module} {status} {result.stats}")

    for module in ("paper", "industry", "contest"):
        expr = topics.schedule.get(module)
        if not expr:
            continue
        try:
            trigger = CronTrigger.from_crontab(expr, timezone=settings.timezone)
        except ValueError as exc:
            print(f"[schedule] {module} 的 cron 表达式非法：{expr} ({exc})")
            continue
        scheduler.add_job(job, trigger, args=[module], id=f"job_{module}",
                          max_instances=1, coalesce=True, misfire_grace_time=3600)
    return scheduler
