"""数据源注册表。新增一个源只要：写模块 → 在 COLLECTORS 里挂上。"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from ..models import Item
from .base import HttpClient
from . import arxiv, competitions, github_trending, industry, lowlevelcv, paperswithcode

Collector = Callable[[HttpClient], Awaitable[list[Item]]]

COLLECTORS: dict[str, list[tuple[str, Collector]]] = {
    "paper": [
        ("熵视 lowlevelcv", lowlevelcv.fetch),
        ("arXiv", arxiv.fetch),
        ("Papers With Code", paperswithcode.fetch),
    ],
    "industry": [
        ("RSS 订阅", industry.fetch),
        ("GitHub 开源", github_trending.fetch),
    ],
    "contest": [
        ("赛事聚合", competitions.fetch),
        # 独立源而非塞在 fetch() 里拼：失败/限流要进 collect() 的源告警，
        # 让运营看见「baseline 源 0 条」，而不是静默吞掉后让筛选层报出误导性错误
        ("Datawhale baseline", competitions.fetch_baseline),
    ],
}

# 单源硬超时（秒）：宁可少一个源，也不能让流水线卡死
SOURCE_TIMEOUTS: dict[str, float] = {
    "熵视 lowlevelcv": 60.0,
    "arXiv": 75.0,
    "Papers With Code": 40.0,
    "RSS 订阅": 60.0,
    "GitHub 开源": 60.0,
    "赛事聚合": 120.0,
    "Datawhale baseline": 45.0,
}

MODULE_LABEL = {"paper": "学术论文", "industry": "业界进展", "contest": "赛事竞赛"}


async def collect(module: str,
                  progress: Callable[[int, str], None] | None = None) -> tuple[list[Item], list[dict]]:
    """并发跑某个模块下的所有源，返回 (去重后的素材, 错误列表)。

    每个源都有硬超时：慢网络下宁可少一个源，也不能让整条流水线卡死。
    progress(pct, msg)：可选进度回调，pct 是本模块内部的抓取进度（0~100），
    由流水线负责映射到全局刻度；各源并发，进度按「完成的源数」推进。
    """
    if module not in COLLECTORS:
        raise ValueError(f"未知模块：{module}")

    def _report(pct: int, msg: str) -> None:
        if progress is None:
            return
        try:
            progress(max(0, min(100, pct)), msg)
        except Exception:  # 展示回调不允许影响抓取
            pass

    entries = COLLECTORS[module]
    state = {"done": 0}

    def _done_one(msg: str) -> None:
        state["done"] += 1
        _report(int(state["done"] * 100 / len(entries)), msg)

    _report(0, f"开始抓取 {len(entries)} 个数据源")

    errors: list[dict] = []
    async with HttpClient() as client:
        async def run(name: str, fn: Collector) -> list[Item]:
            started = time.monotonic()
            try:
                items = await asyncio.wait_for(fn(client), timeout=SOURCE_TIMEOUTS.get(name, 60.0))
            except asyncio.TimeoutError:
                errors.append({"source": name, "error": f"超时（>{SOURCE_TIMEOUTS.get(name, 60.0):.0f}s）"})
                print(f"[sources:{module}] {name} 超时（>{SOURCE_TIMEOUTS.get(name, 60.0):.0f}s）", flush=True)
                _done_one(f"{name} 超时（>{SOURCE_TIMEOUTS.get(name, 60.0):.0f}s）")
                return []
            except Exception as exc:  # 单源失败可观测，但不致命
                errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[sources:{module}] {name} 失败：{type(exc).__name__}: {exc}", flush=True)
                _done_one(f"{name} 失败：{type(exc).__name__}")
                return []
            # 抓到了但一条没有，同样要报出来——静默的空结果最难查
            if not items:
                errors.append({"source": name, "error": "0 条（源可能被反爬、限流或已停更）"})
            print(f"[sources:{module}] {name} 完成：{len(items)} 条，用时 {time.monotonic() - started:.1f}s", flush=True)
            _done_one(f"{name} 完成：{len(items)} 条")
            if name == "RSS 订阅":
                # 单路 RSS 挂掉不影响整体，但必须让运营看见是哪一路
                for row in industry.feed_health():
                    if row.get("error"):
                        errors.append({
                            "source": f"RSS·{row.get('feed')}",
                            "error": row["error"],
                            "level": "warn",
                        })
            return items

        groups = await asyncio.gather(*(run(name, fn) for name, fn in entries))

        merged: dict[str, Item] = {}
        for group in groups:
            for item in group:
                current = merged.get(item.fingerprint)
                if current is None or item.score > current.score:
                    merged[item.fingerprint] = item
        items = list(merged.values())

    return items, errors
