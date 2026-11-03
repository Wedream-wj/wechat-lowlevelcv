"""演示入口：走真实流水线（联网抓取 + 排版），生成最新样稿。

用法：
    python -m scripts.demo               # 默认生成 paper + contest 两期
    python -m scripts.demo paper         # 只生成论文模块
    python -m scripts.demo contest       # 只生成竞赛模块
    python -m scripts.demo baseline      # 只生成「竞赛 baseline 精读」（NTIRE 报告 / Datawhale 仓库）
    python -m scripts.demo paper contest # 指定多个（可选 paper/industry/contest/baseline）
    python -m scripts.demo paper --query "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN"
                                         # 指定论文名称全网检索成稿（学术论文模块）

与正式运行（scripts/run_once.py）的区别：
- demo=True：不写库（不记 seen 指纹、不登记文章与运行日志），可反复重跑；
- 不推送公众号（auto_publish 一律不生效）；
- 产物固定复制为 out/demo_{module}.html / demo_{module}_body.html /
  demo_{module}.md，覆盖旧样稿，方便直接对比本次重构效果。
"""
from __future__ import annotations

import asyncio
import shutil
import sys

from app.config import settings
from app.pipeline import run_module

VALID = ("paper", "industry", "contest")
# baseline 是 contest 的成稿变体：整篇只精读一份 baseline（NTIRE 官方报告或 Datawhale 复现仓库）
VARIANT_TOKENS = {"baseline": ("contest", "baseline")}
OUT = settings.out_dir


async def run(module: str, variant: str | None = None,
              query: str | None = None) -> bool:
    label = f"{module}:{variant}" if variant else module
    if query:
        label += "·指定检索"
    print(f"[demo] ===== {label} 开始 =====")
    result = await run_module(module, demo=True, variant=variant, paper_query=query)
    stats = result.stats or {}

    if not result.article:
        print(f"[demo] {label} 失败（耗时 {stats.get('seconds', '?')}s）：")
        for e in result.errors or [{"source": "?", "error": "未知错误"}]:
            print(f"  - [{e.get('source') or e.get('stage')}] {e.get('error')}")
        return False

    issue_id = stats.get("issue_id", "")
    stem = f"{module}_{variant}" if variant else module
    copies = [
        (OUT / f"preview_{issue_id}.html", OUT / f"demo_{stem}.html"),
        (OUT / f"{issue_id}.html", OUT / f"demo_{stem}_body.html"),
        (OUT / f"{issue_id}.md", OUT / f"demo_{stem}.md"),
    ]
    copied = []
    for src, dst in copies:
        if src.exists():
            shutil.copyfile(src, dst)
            copied.append(dst)

    print(
        f"[demo] {label} 完成（期号 {issue_id}）：抓取 {stats.get('fetched', 0)} 条 / "
        f"选用 {stats.get('selected', 0)} 条 / 配图 {stats.get('figures', 0)} 张 / "
        f"LLM 模式 {stats.get('llm_mode', '?')} / 耗时 {stats.get('seconds', '?')}s"
    )
    for e in result.errors:
        print(f"[demo]   源告警：[{e.get('source') or e.get('stage')}] {e.get('error')}")
    for dst in copied:
        print(f"[demo]   产物 -> {dst}")
    return True


async def main() -> None:
    args = sys.argv[1:]
    # --query <论文名>：指定论文检索（与 paper 模块搭配）
    query: str | None = None
    if "--query" in args:
        pos = args.index("--query")
        if pos + 1 < len(args):
            query = args[pos + 1]
            del args[pos:pos + 2]
    jobs: list[tuple[str, str | None]] = [(m, None) for m in args if m in VALID]
    jobs += [VARIANT_TOKENS[t] for t in args if t in VARIANT_TOKENS]
    if not jobs:
        jobs = [("paper", None), ("contest", None)]
    bad = [a for a in args if a not in VALID and a not in VARIANT_TOKENS]
    if bad:
        print(f"[demo] 忽略未知参数：{','.join(bad)}"
              f"（可选：{','.join(VALID + tuple(VARIANT_TOKENS))}）")

    ok = True
    for m, v in jobs:
        ok = await run(m, v, query if m == "paper" else None) and ok

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
