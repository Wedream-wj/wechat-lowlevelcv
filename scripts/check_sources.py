"""数据源体检 CLI：逐路探测可用性，输出一张表。

用途：换网络环境 / 换成自建 RSSHub / 换机器部署后，先跑这个确认「到底哪路能通」，
不要靠猜。示例：

    python -m scripts.check_sources            # 只测 RSS 与平台源
    python -m scripts.check_sources --module industry
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import topics
from app.sources import COLLECTORS, SOURCE_TIMEOUTS
from app.sources.base import HttpClient
from app.sources import industry


async def probe_rss() -> int:
    print("== RSS 订阅 ==")
    async with HttpClient() as client:
        try:
            await asyncio.wait_for(industry.fetch(client), timeout=SOURCE_TIMEOUTS["RSS 订阅"])
        except asyncio.TimeoutError:
            print("  整组超时")
    rows = industry.feed_health()
    ok = 0
    for row in rows:
        count = row.get("count")
        state = "OK" if count else "FAIL"
        if count:
            ok += 1
        note = row.get("error") or ""
        print(f"  [{state:4}] {str(row.get('feed'))[:22]:<24} {str(count):>5} 条  {note}")
    return ok


async def probe_module(module: str) -> None:
    print(f"== 模块 {module} ==")
    async with HttpClient() as client:
        for name, fn in COLLECTORS[module]:
            try:
                items = await asyncio.wait_for(fn(client), timeout=SOURCE_TIMEOUTS.get(name, 60.0))
            except asyncio.TimeoutError:
                print(f"  [FAIL] {name:<22} 超时（>{SOURCE_TIMEOUTS.get(name, 60):.0f}s）")
                continue
            except Exception as exc:
                print(f"  [FAIL] {name:<22} {type(exc).__name__}: {exc}")
                continue
            print(f"  [{'OK' if items else 'EMPTY':4}] {name:<22} {len(items):>5} 条")

    if module == "industry":
        # RSS 汇总看着是 OK，但可能只有 1 路在供料，必须展开到每一路
        print("  -- RSS 逐路明细 --")
        for row in industry.feed_health():
            count = row.get("count") or 0
            note = row.get("error") or ""
            print(f"     [{'OK' if count else 'FAIL':4}] {str(row.get('feed'))[:22]:<24} {count:>5} 条  {note}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="数据源体检")
    parser.add_argument("--module", choices=list(COLLECTORS), help="只测某个模块")
    args = parser.parse_args()

    print(f"站点数 = {len(topics.rss_feeds)}　模块 = {list(COLLECTORS)}\n")
    if args.module:
        await probe_module(args.module)
    else:
        await probe_rss()
        print()
        for module in COLLECTORS:
            await probe_module(module)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
