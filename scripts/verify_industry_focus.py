"""业界进展「选题去重 + 主题收敛 + 实事求是」三闸门的现场体检。

用法：
    python -m scripts.verify_industry_focus            # 联网抓取真实候选，只跑到选题收敛（不调大模型）
    python -m scripts.verify_industry_focus --days 14  # 指定事件查重窗口

它不写库、不推草稿，只回答三个问题：
1. 真实候选池里，有多少条被「事件级查重」挡掉？挡的是不是同一事件？
2. 同批候选里有多少条重复事件被合并到主条？合并后主条还剩几条？
3. 收敛出的「唯一主角」是谁，它的事件签名是什么？

成稿侧的验证（正文不再串题、数字可核对）由 `python -m scripts.demo industry` 出稿后人工核对，
以及 tests/test_industry_focus.py 的确定性用例覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from app import db
from app.config import settings, topics
from app.filters import events as ev
from app.filters import scoring
from app.sources import collect


async def main() -> int:
    parser = argparse.ArgumentParser(description="业界选题三闸门体检")
    parser.add_argument("--days", type=int, default=None, help="事件查重窗口（默认取配置）")
    args = parser.parse_args()
    window = args.days or int(topics.industry.get("event_dedupe_days", 14) or 14)

    items, errors = await collect("industry")
    print(f"[抓取] {len(items)} 条候选，源告警 {len(errors)} 个")
    if not items:
        print("[fail] 没抓到候选，检查网络或源配置")
        return 1

    seen = db.seen_fingerprints("industry", days=45)
    history_titles = db.recent_history_titles("industry", days=90)
    entries = db.recent_history_entries("industry", days=max(window, 30))
    print(f"[历史] 近期标题 {len(history_titles)} 条 / 事件条目 {len(entries)} 条 / "
          f"已用指纹 {len(seen)} 条")

    # 诊断：只按事件判重（不看标题相似度）能挡住哪些——这是标题闸门拦不住的「换数字重写」
    _kept_only_event, event_only = ev.dedupe_events(list(items), entries, days=window)
    print(f"[诊断·仅事件] 原始候选里 {len(event_only)} 条属于「同一事件换个说法」")
    for d in event_only[:6]:
        print(f"        · {d['title'][:46]}")
        print(f"          事件={d['event']} ← 已出《{d['matched_title'][:38]}》")

    # 闸门 1：标题相似度（旧口径，仍然保留）
    items, dup_titles = scoring.dedupe_history(items, history_titles)
    print(f"[闸门1·标题] 挡掉 {len(dup_titles)} 条")
    for t in dup_titles[:5]:
        print(f"        - {t[:56]}")

    # 闸门 2：事件级（谁 + 干了什么），拦「同一事件换数字再写一篇」
    items, event_dups = ev.dedupe_events(items, entries, days=window)
    print(f"[闸门2·事件] 挡掉 {len(event_dups)} 条（窗口 {window} 天）")
    for d in event_dups[:8]:
        print(f"        - {d['title'][:44]}")
        print(f"          事件={d['event']} ← 已出《{d['matched_title'][:40]}》({d['matched_at']})")

    # 闸门 3：同批合并（待出稿队列内部不许撞题）
    before = len(items)
    items, merged = ev.collapse_same_event(items)
    print(f"[闸门3·同批] {before - len(items)} 条并入主条，剩 {len(items)} 个独立事件")
    for title, others in list(merged.items())[:5]:
        print(f"        - {title[:40]}")
        for o in others:
            print(f"          ← {o.title[:44]}（{o.source}）")

    selected = scoring.select(items, topics, "industry", top_n=3, seen=seen)
    if not selected:
        print("[fail] 三道闸门之后没有可写候选（这是「宁可不发也不重复」的正常结果）")
        return 2
    print(f"[候选] 按分数取 {len(selected)} 条备战：")
    for i in selected:
        print(f"        · {i.score:6.1f}  {i.title[:50]}")
    subject = ev.pick_subject(selected)
    sig = ev.item_signature(subject)
    print(f"[主角] {subject.title}")
    print(f"       事件签名：{sig.describe()} / 实体 {sorted(sig.entities)}")
    others = ev.describe_sources(subject)
    if others:
        print(f"       同事件交叉信源：{others}")
    print(f"[产物] 预览目录 {settings.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
