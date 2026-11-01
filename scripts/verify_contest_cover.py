"""赛事 / Baseline 封面「四级渐进检索」的实跑验证。

三个场景各跑一遍，验证用户铁律：
  A 有官方图  —— 赛事官网 / 平台横幅（og:image、Banner、主视觉、官方 Logo）直接上封面；
  B 有相关图  —— 官网没有主视觉时，退数据集样例 / baseline 框架图 / 社区复盘图；
  C 无图降级  —— 穷尽检索仍无图：返回空选图结果，走品牌底图，流水线照常出稿。

用法：
    python scripts/verify_contest_cover.py            # 三个场景全跑
    python scripts/verify_contest_cover.py --scenario a

产物：out/_contest_cover_<场景>.jpg / .png（封面图）+ 控制台选图结论。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                       # noqa: E402
from app.models import Item                           # noqa: E402
from app.render import cover as cover_mod             # noqa: E402
from app.render import cover_select                   # noqa: E402
from app.sources import competitions, contest_visual, cvf   # noqa: E402
from app.sources.base import HttpClient               # noqa: E402

TITLE = "NTIRE 2026 低光增强挑战赛：冠军方案把噪声当成信息"
SUBTITLE = "官方报告 + 冠军方案拆解"


def _report(tag: str, item: Item, pick, out: str) -> None:
    print(f"  · 条目：{item.title[:52]}")
    print(f"  · 入口：{item.url[:88]}")
    if pick:
        print(f"  · 选图：✔ {pick.describe()}")
        print(f"  · 版式：{pick.fit}")
        for note in pick.notes[:3]:
            print(f"      - {note}")
    else:
        print("  · 选图：✘ 无可用的官方/相关视觉 → 品牌底图（按「不强求」）")
    print(f"  · 封面：{out}")


async def scenario_a(client: HttpClient) -> None:
    """A：赛事官网 / 平台横幅 —— 第一优先级「官方视觉直采」。"""
    print("\n== A. 有官方图（官方视觉直采）==")
    items = await competitions.fetch_tianchi(client)
    items = [i for i in items if i.image_url][:3]
    if not items:
        print("  ⚠ 天池接口无可用条目（网络/接口变更），跳过本场景")
        return
    item = items[0]
    print(f"  平台横幅：{item.image_url[:80]}")
    detail = await competitions.fetch_contest_detail(client, item)
    item.extra["contest_detail"] = detail
    visuals = detail.get("visuals") or []
    print(f"  官方页视觉候选：{len(visuals)} 个"
          + ("（" + "、".join(v["role"] or "待判" for v in visuals[:4]) + "）" if visuals else ""))
    pick = await cover_select.pick_cover(client, [item], "contest")
    out = cover_mod.make_cover({"title": TITLE, "subtitle": SUBTITLE}, "contest",
                               "2026.11.01", hero=pick.path if pick else None,
                               hero_fit=pick.fit if pick else "cover",
                               out_path=settings.out_dir / "_contest_cover_official.jpg")
    _report("A", item, pick, out)


async def scenario_b(client: HttpClient) -> None:
    """B：数据集样例 / baseline 框架图 / 社区复盘图 —— 第二优先级「相关匹配」。"""
    print("\n== B. 有相关图（数据集样例 / 框架图 / 社区图）==")
    reports = await cvf.fetch_reports(client)
    if not reports:
        print("  ⚠ CVF NTIRE 报告源不可达，改用离线样本验证第二优先级")
        item = Item(module="contest", title="NTIRE 2026 低光增强挑战赛（离线样本）",
                    url="https://www.cvlai.net/ntire/2026/", source="CVF NTIRE 报告",
                    category="学习资源")
        item.extra.update({
            "origin": "ntire", "is_learning_resource": True,
            "contest_detail": {"fetched": True, "visuals": [
                {"url": "https://example.invalid/samples.jpg", "role": "",
                 "caption": "Dataset samples across scenes", "source": "official",
                 "body_no": 0, "referer": item.url}]},
        })
        pick = await cover_select.pick_cover(client, [item], "contest")
        out = cover_mod.make_cover({"title": TITLE, "subtitle": SUBTITLE}, "contest",
                                   "2026.11.01", out_path=settings.out_dir / "_contest_cover_related.png")
        _report("B", item, pick, out)
        return

    item = reports[0]
    print(f"  报告：{item.title[:60]}")
    detail = await cvf.enrich_report(client, item)
    item.extra["ntire_report"] = detail
    if detail.get("cover_candidates"):
        item.extra["cover_candidates"] = detail["cover_candidates"]
    print(f"  报告抠图：{len(detail.get('cover_candidates') or [])} 张 | "
          f"方法小节 {len(detail.get('sections') or [])} 个 | "
          f"社区复盘 {len(detail.get('community') or [])} 篇")
    pick = await cover_select.pick_cover(client, [item], "contest")
    out = cover_mod.make_cover({"title": TITLE, "subtitle": SUBTITLE}, "contest",
                               "2026.11.01", hero=pick.path if pick else None,
                               hero_fit=pick.fit if pick else "cover",
                               out_path=settings.out_dir / "_contest_cover_related.jpg")
    _report("B", item, pick, out)


async def scenario_c(client: HttpClient) -> None:
    """C：穷尽无图 —— 第三优先级「尽力而为，不强求」。"""
    print("\n== C. 无图降级（不报错、不卡死、不硬凑）==")
    item = Item(module="contest", title="某仅有文字说明的赛题页面",
                url="https://example.com/", source="Web 检索", category="综合赛事",
                summary="页面只有文字说明，没有任何配图。")
    item.extra.update({"origin": "search", "platform": "example.com"})
    detail = await competitions.fetch_contest_detail(client, item)
    item.extra["contest_detail"] = detail
    print(f"  官方页视觉候选：{len(detail.get('visuals') or [])} 个（预期 0）")
    pick = await cover_select.pick_cover(client, [item], "contest")
    out = cover_mod.make_cover({"title": TITLE, "subtitle": SUBTITLE}, "contest",
                               "2026.11.01",
                               out_path=settings.out_dir / "_contest_cover_fallback.png")
    _report("C", item, pick, out)
    assert pick is None, "无图页面不该硬凑出封面"
    print("  ✔ 降级路径正常：选图为空，品牌底图已出，流水线未中断")


async def main(only: str = "") -> int:
    only = (only or "").lower()
    async with HttpClient() as client:
        if only in ("", "a"):
            await scenario_a(client)
        if only in ("", "b"):
            await scenario_b(client)
        if only in ("", "c"):
            await scenario_c(client)
    print("\n完成。封面产物在 out/ 下：_contest_cover_{official,related,fallback}.*")
    return 0


if __name__ == "__main__":
    arg = ""
    if "--scenario" in sys.argv:
        idx = sys.argv.index("--scenario")
        arg = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
    raise SystemExit(asyncio.run(main(arg)))
