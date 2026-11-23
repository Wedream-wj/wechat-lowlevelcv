"""历史出稿·论文信息卡的两类整理：行名归一 + venue 对齐。

背景：早期论文稿是「边写边定格式」攒下来的，信息卡里留着两类不一致——

1. **行名**：`论文标题` / `期刊/会议` / `发表状态` 等旧写法，与现行的
   `论文名称` / `发表期刊/会议` / `作者团队` 混用。前端看着差不多，但检索、
   字段抽取、后续脚本都按正式行名命中，旧行名等于漏检。
2. **venue**：《InvSR》卡片只写「CVPR」缺年份，《DORNet》标题写 ECCV 2026、
   卡片写 CVPR 2025；还有把 Crossref 的卷期/DOI 尾巴直接搬上卡（挤成两行）、
   预印本挂着会议名（等于替论文发假录用通知）这类绑定错误。

跑法：

    python -m scripts.normalize_paper_meta            # 只报告（默认）
    python -m scripts.normalize_paper_meta --fix      # 落库（rev+1，备份 out/_meta_card_backup/）

改动范围被 `_scope_problem` 锁死：只允许 H1 标题的 venue 前缀与信息卡块，
导语、正文、表格、配图一个字都不许动；写库前还要过 `edit.save_edit` 的
表格对齐闸门。venue 的权威值来自**正文实际精读的那篇论文**（与信息卡绑定同一判据），
不猜、不从别的候选借。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, edit                                   # noqa: E402
from app.config import settings                             # noqa: E402
from app.filters import grounding                           # noqa: E402
from app.render.md_parse import parse_markdown              # noqa: E402
from app.spec_tools import (                                # noqa: E402
    _SHORT_VENUES,
    is_preprint_venue,
    item_venue_label,
    item_venue_short,
)

# 标题开头的 venue 标签：「CVPR 2025 | 」「Sensors | 」「NEURIPS 2026 | 」
_TITLE_VENUE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z&.\-]{0,20})\s*(?P<year>(?:19|20)\d{2})?\s*\|\s*")
_VENUE_ROW_RE = re.compile(r"^(- \*\*发表期刊/会议\*\*：).*$", re.M)


def _fix_title(title: str, short: str) -> tuple[str, str]:
    """标题 venue 前缀对齐权威结论；返回 (新标题, 变更说明)。"""
    m = _TITLE_VENUE_RE.match(title)
    if not short:
        # 预印本不许挂 venue：只剥已知会议/期刊的标签，别的开头（方法名）不动
        if m and (m.group("name").upper() in _SHORT_VENUES or m.group("year")):
            body = title[m.end():].lstrip()
            return body, f"标题剥掉预印本 venue 前缀「{m.group(0).strip()}」"
        return title, ""
    if not m:
        return title, ""          # 本来就没前缀：补前缀不在本次范围（另属标题格式）
    want = f"{short} |"
    if m.group(0).strip() == want:
        return title, ""
    body = title[m.end():]
    return f"{want} {body}", f"标题 venue「{m.group(0).strip()}」→「{want}」"


def _rewrite_venue_row(markdown: str, label: str) -> tuple[str, str]:
    m = _VENUE_ROW_RE.search(markdown)
    if not m:
        return markdown, ""
    old = markdown[m.start():m.end()]
    current = old.split("：", 1)[-1].strip()
    # 卡片已经明确写了预印本（如「发表状态：arXiv 预印本」）时，不拿素材里的会议名把它
    # 抬成正式收录——录用状态要联网复核，不能靠字段推断（UPSR 就被误标过 preprint）。
    if "预印本" in current or is_preprint_venue(current):
        return markdown, ""
    new = m.group(1) + label
    if old == new:
        return markdown, ""
    return markdown[:m.start()] + new + markdown[m.end():], f"venue「{current}」→「{label}」"


def _scope_problem(old: str, new: str, want_title: str) -> str:
    """改动范围审计：除 H1 与信息卡块之外必须逐字相同。"""
    ol, nl = old.splitlines(), new.splitlines()
    osp, nsp = edit._paper_meta_span(ol), edit._paper_meta_span(nl)
    if osp is None or nsp is None:
        return "定位不到信息卡块"
    if ol[1:osp[0]] != nl[1:nsp[0]]:
        return "导语/导读被改动"
    if ol[osp[1]:] != nl[nsp[1]:]:
        return "正文或图注被改动"
    if nl[0] != f"# {want_title}":
        return f"H1 与预期不一致：{nl[0][:40]}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="落库（rev+1）")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    db.init_db()
    rows = db.search_articles(module="paper", page=1, page_size=args.limit)["items"]
    print(f"体检 {len(rows)} 期论文稿（判据：信息卡行名 / 行序 / 标题 venue / 卡片 venue）")

    label_hits = order_hits = venue_hits = title_hits = fixed = 0
    problems = 0
    for row in rows:
        article = db.get_article(row["issue_id"])
        if not article or not article.markdown:
            continue
        spec, _ = parse_markdown(article.markdown)
        target, info = grounding.resolve_meta_subject(spec, article.items)
        if target is None:
            continue
        if info.get("mismatch"):
            print(f"  ⚠ {article.issue_id} 信息卡指向的不是正文主体，跳过"
                  f"（先跑 scripts.audit_paper_meta --fix）")
            problems += 1
            continue

        new_md, changes = edit.normalize_meta_card_lines(article.markdown, "paper")
        label_changes = [c for c in changes if c.startswith("行名")]
        if label_changes:
            label_hits += 1
        if any("排序" in c for c in changes):
            order_hits += 1

        venue_label = item_venue_label(target)
        venue_note = ""
        if venue_label:
            new_md, venue_note = _rewrite_venue_row(new_md, venue_label)
        if venue_note:
            venue_hits += 1

        title = article.title or new_md.splitlines()[0].lstrip("# ").strip()
        new_title, title_note = _fix_title(title, item_venue_short(target))
        if title_note:
            title_hits += 1
            new_md = re.sub(r"^\s*#\s*.*$", f"# {new_title}", new_md, count=1, flags=re.M)

        notes = changes + [c for c in (venue_note, title_note) if c]
        if not notes:
            continue
        print(f"\n· {article.issue_id}  {(article.title or '')[:40]}")
        for note in notes:
            print(f"   - {note}")

        bad = _scope_problem(article.markdown, new_md, new_title)
        if bad:
            print(f"   ⚠ 范围校验未通过（{bad}），跳过")
            problems += 1
            continue
        if not args.fix:
            continue

        backup = settings.out_dir / "_meta_card_backup"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / f"{article.issue_id}.md").write_text(article.markdown, encoding="utf-8")
        edit.save_edit(article, new_md)
        meta = db.edit_meta(article.issue_id)
        fixed += 1
        print(f"   ✅ 已修复，rev={meta.get('rev', '?')}，状态 {meta.get('status', '?')}"
              f"（正文未改动，原稿在 out/_meta_card_backup/）")

    print(f"\n结果：行名不规范 {label_hits} 期、三行顺序不规范 {order_hits} 期、"
          f"venue 不一致 {venue_hits} 期、标题 venue 不符 {title_hits} 期"
          + (f"；已修复 {fixed} 期" if args.fix else "（--fix 可自动修复）"))
    if problems:
        print(f"⚠ {problems} 期需要人工确认（见上）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
