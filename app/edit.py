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
