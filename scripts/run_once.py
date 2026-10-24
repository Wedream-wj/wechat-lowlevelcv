"""单模块一次出稿的命令行入口（服务器 cron 直接跑这个也行）。

用法：
    python -m scripts.run_once paper            # 抓取+成稿+排版，产出预览
    python -m scripts.run_once industry --push  # 顺带推草稿箱
    python -m scripts.run_once contest --mock   # 离线演示模式
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# --mock 必须在导入 app 配置之前生效
_parser = argparse.ArgumentParser(description="单模块出稿")
_parser.add_argument("module", choices=["paper", "industry", "contest"])
_parser.add_argument("--push", action="store_true", help="成稿后直接推公众号草稿箱")
_parser.add_argument("--mock", action="store_true", help="离线模式（不调大模型）")
_parser.add_argument("--top-n", type=int, default=None, help="本次最多选用几条素材")
_parser.add_argument("--variant", default=None, choices=["baseline"],
                     help="成稿变体：contest 支持 baseline（整篇赛事×baseline 精读）")
_args = _parser.parse_args()
if _args.mock:
    os.environ["LLCV_MOCK_LLM"] = "1"

from app.pipeline import run_module  # noqa: E402


async def main() -> int:
    result = await run_module(_args.module, publish=_args.push or None, top_n=_args.top_n,
                              variant=_args.variant)
    if result.article:
        print(f"[ok] {result.article.title}")
        print(f"     预览: {result.stats.get('preview')}")
        print(f"     统计: {result.stats}")
        return 0
    print("[fail]", result.errors)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
