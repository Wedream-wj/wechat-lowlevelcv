"""数据源注册表。新增一个源只要：写模块 → 在 COLLECTORS 里挂上。"""
from __future__ import annotations

import asyncio
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
    ],
}

# 单源硬超时（秒）：宁可少一个源，也不能让流水线卡死
SOURCE_TIMEOUTS: dict[str, float] = {
    "熵视 lowlevelcv": 60.0,
    "arXiv": 75.0,
    "Papers With Code": 40.0,
    "RSS 订阅": 60.0,
    "GitHub 开源": 60.0,
    "赛事聚合": 90.0,
}

MODULE_LABEL = {"paper": "学术论文", "industry": "业界进展", "contest": "赛事竞赛"}


async def collect(module: str, with_figures: bool = True) -> tuple[list[Item], list[dict]]:
    """并发跑某个模块下的所有源，返回 (去重后的素材, 错误列表)。

    每个源都有硬超时：慢网络下宁可少一个源，也不能让整条流水线卡死。
    """
    if module not in COLLECTORS:
        raise ValueError(f"未知模块：{module}")

    errors: list[dict] = []
    async with HttpClient() as client:
        async def run(name: str, fn: Collector) -> list[Item]:
            try:
                items = await asyncio.wait_for(fn(client), timeout=SOURCE_TIMEOUTS.get(name, 60.0))
            except asyncio.TimeoutError:
                errors.append({"source": name, "error": f"超时（>{SOURCE_TIMEOUTS.get(name, 60.0):.0f}s）"})
                return []
            except Exception as exc:  # 单源失败可观测，但不致命
                errors.append({"source": name, "error": f"{type(exc).__name__}: {exc}"})
                return []
            # 抓到了但一条没有，同样要报出来——静默的空结果最难查
            if not items:
                errors.append({"source": name, "error": "0 条（源可能被反爬、限流或已停更）"})
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

        groups = await asyncio.gather(*(run(name, fn) for name, fn in COLLECTORS[module]))

        merged: dict[str, Item] = {}
        for group in groups:
            for item in group:
                current = merged.get(item.fingerprint)
                if current is None or item.score > current.score:
                    merged[item.fingerprint] = item
        items = list(merged.values())

        if module == "paper" and with_figures:
            # 只给最有希望的候选补图，避免抓取过重
            try:
                await asyncio.wait_for(arxiv.enrich_figures(client, items, limit=3), timeout=90.0)
            except asyncio.TimeoutError:
                errors.append({"source": "arXiv 配图", "error": "补图总超时（>90s），跳过"})

    return items, errors
