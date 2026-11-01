"""赛事标题「先自审再动笔」的实跑验证（BioXArena 生物智能体赛事）。

跑法：
    python scripts/verify_contest_title.py            # 真实调大模型出稿 + 自审 + 渲染预览

验证链路与线上完全一致：
  1) LLMClient.write('contest', ...) —— 用新 Prompt（三闸自审 + 爆款公式）出稿；
  2) contest_title_issues —— 代码层自审，打印每一条不合格理由；
  3) pipeline._sanity_check_contest_title —— 不过则定向重写 / 去装饰 / 公式化重拼；
  4) 渲染封面 + 微信排版 + 预览页，肉眼确认标题通俗、断行不拆词。

产物：out/_title_<n>_*.jpg、out/preview_demo-contest-bioxarena.html
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings                      # noqa: E402
from app.llm.client import LLMClient                # noqa: E402
from app.llm.mock import mock_spec                   # noqa: E402
from app.llm.title_audit import (                    # noqa: E402
    compose_contest_title,
    contest_title_issues,
    polish_title,
)
from app.models import Item                          # noqa: E402
from app.pipeline import _sanity_check_contest_title  # noqa: E402
from app.render import cover as cover_mod            # noqa: E402
from app.render import wechat                        # noqa: E402
from app.render.markdown import spec_to_markdown     # noqa: E402
from app.render.preview import render_preview        # noqa: E402

DATE = "2026.11.01"

# 事故样本：用户实测出的晦涩标题，用来验证「自审能拦住、重构能改好」
BAD_TITLE = "76 个生物任务、9 大领域：BioXArena 把 LLM 智能体拉进湿实验门口"

OFFICIAL_BRIEF = (
    "BioXArena is a benchmark and open challenge for LLM agents in biology. "
    "It covers 9 biological domains and 76 real lab tasks: gene perturbation design, "
    "protein structure reasoning, cell-imaging phenotype annotation, protocol planning, "
    "and wet-lab experiment scheduling. Agents must call tools, read instrument output, "
    "and produce executable protocols; submissions are scored by task success rate and "
    "wet-lab reproducibility. Registration closes 2026-11-20, final submission 2026-12-05. "
    "Total prize pool is 30,000 USD plus cloud computing credits."
)


def bioxarena_item() -> Item:
    item = Item(
        module="contest",
        title="BioXArena: 76 Tasks across 9 Domains for LLM Agents in Biology",
        url="https://bioxarena.example.org/2026",
        source="AI 赛事雷达",
        category="生物智能体",
        summary=("BioXArena 开放 76 个真实生物实验任务、覆盖 9 大领域，"
                 "考核 LLM 智能体能否读懂仪器输出、写出可执行的湿实验方案。"),
        keywords=["生物", "智能体", "benchmark"],
    )
    item.extra.update({
        "platform": "BioXArena",
        "organizer": "BioXArena 组委会",
        "prize": "总奖池 3 万美元 + 云计算算力",
        "deadline": "2026-12-05",
        "entry_url": item.url,
        "days_left": 34,
        "tags": ["生物", "智能体", "多模态", "工具调用"],
        "contest_detail": {
            "source": "official",
            "official_brief": OFFICIAL_BRIEF,
            "page_excerpt": OFFICIAL_BRIEF,
            "timeline": ["2026-11-20 报名截止", "2026-12-05 最终提交截止"],
            "prize_hint": ["Total prize pool is 30,000 USD plus cloud computing credits."],
            "metrics_hint": ["scored by task success rate", "wet-lab reproducibility"],
            "registration_closed": False,
            "registration_hint": "报名剩 19 日 06 时",
        },
    })
    return item


def show(tag: str, title: str, item: Item) -> list[str]:
    issues = contest_title_issues(title, item)
    print(f"\n[{tag}] {title}")
    print(f"      字数 {len(title)}")
    if issues:
        for i in issues:
            print(f"      ✗ {i}")
    else:
        print("      ✔ 自审通过")
    return issues


async def main() -> int:
    item = bioxarena_item()

    print("=" * 78)
    print("0. 事故样本自审（证明闸门真的能拦住）")
    print("=" * 78)
    show("事故标题", BAD_TITLE, item)
    print(f"      去装饰后：{polish_title(BAD_TITLE)}")
    print(f"      公式重拼：{compose_contest_title(item)}")

    print("\n" + "=" * 78)
    print("1. 真实调模型出稿（contest 模块 · 新 Prompt）")
    print("=" * 78)
    llm = LLMClient()
    print(f"   LLM 模式：{llm.mode}")
    if llm.enabled:
        try:
            spec = await llm.write("contest", [item], {}, DATE)
        except Exception as exc:
            print(f"   ⚠ 真实调用失败（{exc}），降级 mock 继续验证自审链路")
            spec = mock_spec("contest", [item], {}, variant=None)
    else:
        spec = mock_spec("contest", [item], {}, variant=None)

    raw_title = spec.get("title", "")
    issues = show("模型原始标题", raw_title, item)

    print("\n" + "=" * 78)
    print("2. 自审 → 通俗化重构（线上 push 前必走的那一步）")
    print("=" * 78)
    logs: list[str] = []
    changed = await _sanity_check_contest_title(spec, item, llm, logs.append)
    for line in logs:
        print(f"   · {line}")
    if not changed and not issues:
        print("   · 原题已通过自审，未做改动")
    final_title = spec.get("title", "")
    remaining = contest_title_issues(final_title, item)
    print(f"\n   最终标题：{final_title}")
    print(f"   自审结论：{'✔ 通过' if not remaining else '✗ ' + '；'.join(remaining)}")
    assert not remaining, "自审未通过，标题仍不合格"

    print("\n" + "=" * 78)
    print("3. 渲染封面 / 微信排版 / 预览页")
    print("=" * 78)
    cover_path = cover_mod.make_cover(
        spec, "contest", DATE, out_path=settings.out_dir / "_title_contest_bioxarena.jpg")
    print(f"   封面：{cover_path}")
    body = wechat.render_body(spec, {}, "contest", DATE)
    markdown = spec_to_markdown(spec, {}, "contest")
    issue_id = "demo-contest-bioxarena"
    preview_html = render_preview(
        spec, body, markdown, "contest", Path(cover_path).name,
        issue_id, DATE, "赛事速递")
    out_html = settings.out_dir / f"preview_{issue_id}.html"
    out_html.write_text(preview_html, encoding="utf-8")
    (settings.out_dir / f"{issue_id}.md").write_text(markdown, encoding="utf-8")
    print(f"   预览：{out_html}")
    print(f"   正文长度：{len(body)} 字符（HTML） / Markdown {len(markdown)} 字符")
    print("\n完成。看一下预览页的标题与封面断行是否自然。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
