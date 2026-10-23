"""演示入口：走真实流水线（联网抓取 + 排版），生成最新样稿。

用法：
    python -m scripts.demo               # 默认生成 paper + contest 两期
    python -m scripts.demo paper         # 只生成论文模块
    python -m scripts.demo contest       # 只生成竞赛模块
    python -m scripts.demo paper contest # 指定多个（可选 paper/industry/contest）

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
OUT = settings.out_dir


async def run(module: str) -> bool:
    print(f"[demo] ===== {module} 开始 =====")
    result = await run_module(module, demo=True)
    stats = result.stats or {}

    if not result.article:
        print(f"[demo] {module} 失败（耗时 {stats.get('seconds', '?')}s）：")
        for e in result.errors or [{"source": "?", "error": "未知错误"}]:
            print(f"  - [{e.get('source') or e.get('stage')}] {e.get('error')}")
        return False

    issue_id = stats.get("issue_id", "")
    copies = [
        (OUT / f"preview_{issue_id}.html", OUT / f"demo_{module}.html"),
        (OUT / f"{issue_id}.html", OUT / f"demo_{module}_body.html"),
        (OUT / f"{issue_id}.md", OUT / f"demo_{module}.md"),
    ]
    copied = []
    for src, dst in copies:
        if src.exists():
            shutil.copyfile(src, dst)
            copied.append(dst)

    print(
        f"[demo] {module} 完成（期号 {issue_id}）：抓取 {stats.get('fetched', 0)} 条 / "
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
    mods = [m for m in args if m in VALID] or ["paper", "contest"]
    bad = [m for m in args if m not in VALID]
    if bad:
        print(f"[demo] 忽略未知模块：{','.join(bad)}（可选：{','.join(VALID)}）")

    ok = True
    for m in mods:
        ok = await run(m) and ok

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
