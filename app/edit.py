"""人工润色落库：Markdown 是唯一事实源，HTML 一律服务端重新排版。

为什么不直接存前端富文本的 HTML：微信草稿箱只认这套内联样式，而浏览器
contenteditable 吐出来的是语义标签（p / ul / table），直接推过去就变成公众号里
没有任何样式的裸文本。所以编辑器的契约是单向的：

    前端只提交 Markdown（唯一可编辑事实源）
        → md_parse 解析成 spec
        → wechat.render_body 重新排版成微信 HTML
        → 入库 + 落盘（{id}.md / {id}.html / preview_{id}.html）

推草稿流程（pipeline.push_issue）读的就是库里的 HTML，因此「推出去的永远
是最后一次保存的润色稿」，不需要额外同步动作。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db
from .config import settings
from .models import Article
from .render.md_parse import parse_markdown
from .render.preview import render_preview
from .render import wechat
from .sources import MODULE_LABEL

MAX_MARKDOWN = 200_000  # 一期推文 1 万字级别，20 万字符足够挡住误粘贴的整本书


# ---------------------------------------------------------------- 协作纪律（最小改动与数据对齐）
def _table_blocks(spec: dict) -> list[dict]:
    return [b for b in spec.get("sections") or []
            if isinstance(b, dict) and b.get("type") == "table"]


def assert_tables_stable(spec: dict, figures: dict) -> None:
    """表格不变量（数据对齐契约的保存侧闸门）：排版往返绝不失真。

    md → spec →（spec_to_markdown）→ md → spec 的两轮解析里，每张表的
    表头、行数、每格内容必须逐字一致。不一致只可能是两种情况：
    单元格里出现了未转义的「|」（列会当场错位），或渲染链路悄悄改写了
    表格数据——两种都按契约拒绝保存，把错位挡在入库之前。
    """
    from .render.markdown import spec_to_markdown

    tables = _table_blocks(spec)
    if not tables:
        return
    # 行列等宽校验：某行比表头多/少一格 = 有未转义的「|」或断行混进来了，
    # 这正是「表格错位」的直接特征，在入库前就拦下
    for idx, table in enumerate(tables, start=1):
        width = len(table.get("header") or [])
        for r_i, row in enumerate(table.get("rows") or [], start=2):
            if len(row) != width:
                raise ValueError(
                    f"第 {idx} 张表格第 {r_i} 行有 {len(row)} 格，与表头 {width} 列不一致"
                    "（通常是有未转义的「|」混进了单元格），已按数据对齐契约拒绝保存；"
                    "请把正文里的竖线改写为「\\|」或直接删除。")
    try:
        spec2, _ = parse_markdown(spec_to_markdown(spec, figures))
        tables2 = _table_blocks(spec2)
    except Exception as exc:  # 往返自身崩溃 = 渲染链路有问题，同样拒绝保存
        raise ValueError(f"表格数据在排版往返中解析失败，已拒绝保存：{exc}") from exc
    for idx, (a, b) in enumerate(zip(tables, tables2), start=1):
        if a.get("header") != b.get("header") or a.get("rows") != b.get("rows"):
            raise ValueError(
                f"第 {idx} 张表格在排版往返中发生了数据变化（行列错位或未转义的「|」），"
                "已按数据对齐契约拒绝保存：请检查该表的竖线是否都需要转义为「\\|」。")


def changed_scope(old_markdown: str, new_markdown: str) -> dict:
    """人机协作审计：两版稿子之间改了什么（块级 diff + 表格级 diff）。

    「只改指定位置」纪律的可校验形态：润色（无论人手改还是 AI 改）之后，
    据此核对改动是否落在用户点名的范围——没被点名的表格必须逐字原样。
    """
    from itertools import zip_longest

    old_blocks = [b for b in _parse_for_audit(old_markdown).get("sections") or []
                  if isinstance(b, dict)]
    new_blocks = [b for b in _parse_for_audit(new_markdown).get("sections") or []
                  if isinstance(b, dict)]
    old_tables = [b for b in old_blocks if b.get("type") == "table"]
    new_tables = [b for b in new_blocks if b.get("type") == "table"]
    return {
        "total_blocks": max(len(old_blocks), len(new_blocks)),
        "changed_blocks": sum(1 for a, b in zip_longest(old_blocks, new_blocks) if a != b),
        "total_tables": max(len(old_tables), len(new_tables)),
        "tables_changed": [i for i, (a, b) in
                           enumerate(zip_longest(old_tables, new_tables), start=1)
                           if a != b],
    }


def _parse_for_audit(markdown: str) -> dict:
    text = (markdown or "").replace("\r\n", "\n").strip()
    if not text:
        return {}
    spec, _figures = parse_markdown(text + "\n")
    return spec


@dataclass
class EditResult:
    issue_id: str
    html: str
    markdown: str
    spec: dict = field(default_factory=dict)
    figures: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return str(self.spec.get("title") or "")

    @property
    def subtitle(self) -> str:
        return str(self.spec.get("subtitle") or "")

    @property
    def digest(self) -> str:
        return str(self.spec.get("digest") or "")

    @property
    def words(self) -> int:
        text = "".join(str(x) for x in (self.spec.get("lead") or []))
        for block in self.spec.get("sections") or []:
            text += str(block.get("text") or "")
            text += "".join(str(i) for i in block.get("items") or [])
            for row in block.get("rows") or []:
                text += "".join(str(c) for c in row if not isinstance(c, (list, tuple)))
        return len("".join(text.split()))

    @property
    def sections(self) -> int:
        return sum(1 for b in self.spec.get("sections") or [] if b.get("type") in {"h2", "h3"})

    def to_dict(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "html": self.html,
            "markdown": self.markdown,
            "title": self.title,
            "subtitle": self.subtitle,
            "digest": self.digest,
            "words": self.words,
            "sections": self.sections,
        }


def _date_str(article: Article) -> str:
    created = article.created_at or datetime.now()
    return created.strftime("%Y.%m.%d")


def cover_relative(article: Article) -> str:
    """封面在预览页里要用「相对 out 目录」的路径，和流水线出稿保持一致。"""
    raw = str(article.cover_path or "")
    if not raw:
        return ""
    path = Path(raw)
    try:
        return path.resolve().relative_to(settings.out_dir.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def render_edit(article: Article, markdown: str) -> EditResult:
    """只排版不落库（编辑器实时预览用）。markdown 为空直接报错，不写空稿。"""
    text = (markdown or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("Markdown 内容为空，拒绝用空稿覆盖原文")
    if len(text) > MAX_MARKDOWN:
        raise ValueError(f"Markdown 超过 {MAX_MARKDOWN} 字符上限，请分批整理")
    spec, figures = parse_markdown(text + "\n")
    assert_tables_stable(spec, figures)  # 数据对齐契约：错位的表进不了库
    body = wechat.render_body(spec, figures, article.module, _date_str(article))
    return EditResult(issue_id=article.issue_id, html=body, markdown=text + "\n",
                      spec=spec, figures=figures)


def save_edit(article: Article, markdown: str) -> dict:
    """保存润色稿：重新排版 → 入库（markdown/html/标题/摘要/rev）→ 落盘三件套。"""
    result = render_edit(article, markdown)
    stamp = datetime.now()
    meta = db.update_article_content(
        article.issue_id,
        markdown=result.markdown,
        html=result.html,
        title=result.title,
        subtitle=result.subtitle,
        digest=result.digest,
        edited_at=stamp,
    )

    out = settings.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{article.issue_id}.md").write_text(result.markdown, encoding="utf-8")
    (out / f"{article.issue_id}.html").write_text(result.html, encoding="utf-8")
    preview = render_preview(
        result.spec,
        result.html,
        result.markdown,
        article.module,
        cover_relative(article),
        article.issue_id,
        _date_str(article),
        MODULE_LABEL.get(article.module, article.module),
    )
    (out / f"preview_{article.issue_id}.html").write_text(preview, encoding="utf-8")

    payload = result.to_dict()
    payload.update({
        "ok": True,
        "rev": meta.get("rev", 0),
        "edited_at": (meta.get("edited_at") or stamp).isoformat(timespec="seconds"),
        "preview": f"/preview/{article.issue_id}",
        "status": meta.get("status", article.status),
    })
    return payload
