"""赛事「时效性闸门」现场体检：过期赛事到底有没有被拦住。

用法：
    python -m scripts.verify_contest_recency          # 联网抓真实候选，只跑到选题（不调大模型、不写库）
    python -m scripts.verify_contest_recency --offline # 只跑回归用例（不联网）

它回答四个问题：
1. 回归用例：2019-11-18 截止的「FlyAI 超分挑战赛」，在**保留原有 confirmed_active /
   evergreen 标记**的前提下，是否仍被判定过期？（这条以前必然漏）
2. 真实抓取汇合后，有多少条赛事被时效闸门剔除、原因分布如何？
3. 留下来的候选，各自的截止时间与剩余天数是否都在窗口内？
4. 抓取阶段放行、但官方页核验时才暴露过期的场次，能否被二次判定拦住？

成稿侧的最终保障（候选全过期时直接报错、不出稿）由 tests/test_contest_recency.py
与 pipeline 的 veto 分支覆盖。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta

from app.filters import recency
from app.models import Item
from app.sources import collect


def _legacy_flyai_item() -> Item:
    """完全复刻修复前的 FlyAI 条目：evergreen + confirmed_active 双标记。"""
    return Item(
        module="contest",
        title="FlyAI 超分挑战赛（图像四倍超分）",
        url="https://static.flyai.com/",
        source="FlyAI",
        summary="报名截止 2019-11-18 23:59:59，基于 DIV2K 训练集做四倍超分。",
        extra={"platform": "FlyAI", "deadline": "2019-11-18 23:59:59",
               "evergreen": True, "confirmed_active": True,
               "entry_url": "https://static.flyai.com/"},
    )


def _check_regression() -> int:
    item = _legacy_flyai_item()
    verdict = recency.evaluate(item)
    kept, rejected = recency.gate([item])
    ok = (not verdict.ok) and not kept and bool(rejected)
    print(f"① 回归用例：{item.title}")
    print(f"   判定：{verdict.status} —— {verdict.reason}")
    print(f"   闸门结果：保留 {len(kept)} 条 / 剔除 {len(rejected)} 条 → "
          f"{'PASS（已拦截）' if ok else 'FAIL（又漏了）'}")

    # 官方页核验才发现过期的场景
    late = Item(module="contest", title="某挑战赛", url="https://e.com", source="AI 赛事雷达",
                summary="图像复原赛道。",
                extra={"platform": "AI 赛事雷达", "deadline": "", "confirmed_active": True})
    detail = {"page_excerpt": "报名截止 2019-11-18 23:59:59，比赛已结束，获奖名单见公告。",
              "timeline": []}
    late_verdict = recency.evaluate(late, detail=detail)
    late_ok = not late_verdict.ok
    print(f"② 官方页二次判定：{late.title} → {late_verdict.status}（{late_verdict.reason}）"
          f" → {'PASS' if late_ok else 'FAIL'}")
    return 0 if (ok and late_ok) else 1


def _print_live(items: list[Item]) -> None:
    now = datetime.now()
    by_source: dict[str, list[Item]] = {}
    for item in items:
        by_source.setdefault(item.source, []).append(item)
    print("③ 按来源分布（有明确截止 / 靠数据源在办标记 / 学习资源）：")
    for source, group in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        dated = sum(1 for i in group if i.extra.get("deadline"))
        unverified = sum(1 for i in group
                         if not (i.extra.get("recency") or {}).get("verified", True))
        learning = sum(1 for i in group if i.extra.get("is_learning_resource"))
        print(f"   · {source:<18} {len(group):>4} 场（截止字段 {dated} / 在办标记 {unverified}"
              f" / 学习资源 {learning}）")

    print("④ 最近截止的候选（按剩余天数升序）：")
    rows = []
    for item in items:
        left = recency.evaluate(item, now).days_left
        dl = recency.parse_date(str(item.extra.get("deadline") or ""))
        rows.append((left if left is not None else 10 ** 6, item, dl))
    for left, item, dl in sorted(rows, key=lambda r: (r[0], r[1].title))[:12]:
        left_txt = "—" if left >= 10 ** 6 else f"{left} 天"
        dl_txt = f"{dl:%Y-%m-%d}" if dl else "无截止字段"
        print(f"   · {item.title[:34]:<34} {dl_txt:<12} 剩 {left_txt:<6} "
              f"{(item.extra.get('recency') or {}).get('status', ''):<9} {item.source}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="赛事时效闸门体检")
    parser.add_argument("--offline", action="store_true", help="只跑回归用例，不联网")
    args = parser.parse_args()

    code = _check_regression()
    if args.offline:
        return code

    items, errors = await collect("contest")
    gate_warns = [e for e in errors if e.get("source") == "时效闸门"]
    print(f"④ 实时抓取：保留 {len(items)} 场，源告警 {len(errors)} 个"
          + (f"（时效闸门告警 {len(gate_warns)} 条）" if gate_warns else ""))
    for e in gate_warns:
        print(f"   闸门：{e['error']}")
    if not items:
        print("[warn] 本轮没有留在窗口内的候选——这正是闸门该有的结果（宁可不写，也不写旧比赛）")
        return code
    _print_live(items)

    # 窗口自检：留下场的截止时间必须都在 now ~ now+upcoming_days 之间
    upcoming = int((recency._cfg() or {}).get("upcoming_days", recency.UPCOMING_DAYS))
    bad = []
    for item in items:
        dl = recency.parse_date(str(item.extra.get("deadline") or ""))
        if dl and not (datetime.now() < dl <= datetime.now() + timedelta(days=upcoming)):
            bad.append(item.title)
    print(f"⑤ 窗口自检：{len(items)} 场里 {len(bad)} 场超出 {upcoming} 天窗口"
          f"{'（' + '、'.join(t[:20] for t in bad[:3]) + '）' if bad else ' → PASS'}")
    return code if not bad else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
