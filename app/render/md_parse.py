"""Markdown → ArticleSpec：人工润色后的稿子重新排版入库的解析层。

Markdown 在本项目里一直是「可编辑的中间稿」（见 render/markdown.py 的模块注释），
但此前只有「spec → md」单向出口；人工改完 md 没法再排版回去。本模块补上反向通道：

    md  ──parse_markdown──▶  spec + figures  ──wechat.render_body──▶  微信 HTML

支持的块与 spec_to_markdown 的产出严格对齐，保证「改完文字还能原样重排」：

- `# 标题` / `> 副标题` / `**导读**：…`
- 正文段落、`## 小节` / `### 小标题`
- `- 列表` / `1. 编号列表`（整块都是 `- **字段**：值` 时还原成信息卡）
- `> 引用`、`> **结论**：…`（高亮块）
- `| 表格 |`、`![图注](assets/x.png)`、``` 代码 ```、`---` 分隔线
- 尾部的 `## 一句话总结` 与 `### 参考来源`

安全口径：md 里的原文一律当纯文本转义（走 wechat.inline 的 esc），
裸 HTML 标签不会被执行，也不会进入最终推送的 HTML。
"""
from __future__ import annotations

import re

from . import wechat

_TABLE_SEP = re.compile(r"^\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_IMAGE = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.+)$")
_NUMBER = re.compile(r"^\d+\.\s+(.+)$")
_QUOTE_LINE = re.compile(r"^>\s?(.*)$")
_META = re.compile(r"^\*\*(.+?)\*\*\s*[：:]\s*(.+)$")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_TAKEAWAY_TITLES = {"一句话总结", "写在最后的一句话"}
_SOURCE_TITLES = {"参考来源", "参考链接", "Sources"}


def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def _strip_emphasis(text: str) -> str:
    text = text.strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        return text[2:-2].strip()
    return text


def parse_markdown(md: str) -> tuple[dict, dict[str, dict]]:
    """把 Markdown 还原成 (spec, figures)。

    figures 的 key 是 `F1/F2/…`（渲染层按 ref 取图），value 为
    `{"path": 相对资源路径, "caption": 图注}`——与 spec_to_markdown 的输出互逆。
    """
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    spec: dict = {"sections": []}
    sections: list[dict] = spec["sections"]
    figures: dict[str, dict] = {}
    lead: list[str] = []
    sources: list[dict] = []

    i, total = 0, len(lines)
    seen_section = False      # 是否已出现第一个 ## 小节（之前的内容算导语）
    in_takeaway = False
    in_sources = False

    def add_paragraph(text: str) -> None:
        nonlocal in_takeaway
        if in_takeaway:
            spec["takeaway"] = _strip_emphasis(text)
            in_takeaway = False
        elif not seen_section and not lead and not spec.get("digest"):
            m = re.match(r"^\*\*导读\*\*\s*[：:]\s*(.+)$", text.strip())
            if m:
                spec["digest"] = m.group(1).strip()
            else:
                lead.append(text)
        elif not seen_section:
            lead.append(text)
        else:
            sections.append({"type": "p", "text": text})

    while i < total:
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue

        # 标题
        if line.startswith("# ") and not spec.get("title"):
            spec["title"] = line[2:].strip()
            i += 1
            continue

        # 小节标题
        if line.startswith("## "):
            text = line[3:].strip()
            if text in _TAKEAWAY_TITLES:
                in_takeaway = True
            elif text in _SOURCE_TITLES:
                in_sources = True
            else:
                seen_section = True
                in_sources = False
                sections.append({"type": "h2", "text": text})
            i += 1
            continue

        if line.startswith("### "):
            text = line[4:].strip()
            if text in _SOURCE_TITLES:
                in_sources = True
            else:
                seen_section = True
                in_sources = False
                sections.append({"type": "h3", "text": text})
            i += 1
            continue

        # 代码块
        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1) or "python"
            body: list[str] = []
            i += 1
            while i < total and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1  # 跳过收尾的 ```
            seen_section = True
            sections.append({"type": "code", "lang": lang, "code": "\n".join(body).rstrip(),
                             "caption": ""})
            continue

        # 图片：![图注](assets/x.png)
        img = _IMAGE.match(line)
        if img:
            alt, src = img.group(1).strip(), img.group(2).strip()
            ref = f"F{len(figures) + 1}"
            figures[ref] = {"path": src, "caption": alt}
            seen_section = True
            sections.append({"type": "figure", "ref": ref, "caption": alt})
            i += 1
            continue

        # 表格
        if line.startswith("|"):
            block: list[str] = []
            while i < total and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = [r for r in block if not _TABLE_SEP.match(r)]
            if len(rows) >= 2:
                header = _split_row(rows[0])
                body_rows = [_split_row(r) for r in rows[1:]]
                seen_section = True
                sections.append({"type": "table", "header": header, "rows": body_rows})
            elif rows:
                add_paragraph(" ".join(_split_row(rows[0])))
            continue

        # 无序列表
        bullet = _BULLET.match(line)
        if bullet:
            items: list[str] = []
            while i < total:
                m = _BULLET.match(lines[i].strip())
                if not m:
                    break
                items.append(m.group(1).strip())
                i += 1
            if in_sources:
                for item in items:
                    link = _LINK.search(item)
                    if link:
                        sources.append({"name": link.group(1).strip() or link.group(2),
                                        "url": link.group(2)})
                    elif item:
                        sources.append({"name": item, "url": ""})
                continue
            metas = [_META.match(item) for item in items]
            if len(items) >= 2 and all(metas):
                seen_section = True
                sections.append({"type": "meta_card",
                                 "rows": [[m.group(1).strip(), m.group(2).strip()] for m in metas]})
            else:
                seen_section = True
                sections.append({"type": "bullets", "items": items})
            continue

        # 有序列表
        numbered = _NUMBER.match(line)
        if numbered:
            items = []
            while i < total:
                m = _NUMBER.match(lines[i].strip())
                if not m:
                    break
                items.append(m.group(1).strip())
                i += 1
            seen_section = True
            sections.append({"type": "numbered", "items": items})
            continue

        # 引用 / 高亮
        if line.startswith(">"):
            quotes: list[str] = []
            while i < total:
                m = _QUOTE_LINE.match(lines[i].strip())
                if not m:
                    break
                quotes.append(m.group(1).strip())
                i += 1
            text = "\n".join(quotes).strip()
            if not seen_section and not lead and not spec.get("subtitle") and len(quotes) == 1:
                spec["subtitle"] = text
                continue
            seen_section = True
            hlt = _META.match(text)
            if hlt:
                sections.append({"type": "highlight", "title": hlt.group(1).strip(),
                                 "text": hlt.group(2).strip()})
            else:
                sections.append({"type": "quote", "text": text})
            continue

        # 分隔线
        if line in {"---", "***", "___"}:
            seen_section = True
            sections.append({"type": "divider"})
            i += 1
            continue

        # 普通段落：连续非空行合并，保留内部换行（渲染时转 <br/>）
        para: list[str] = [line]
        i += 1
        while i < total:
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("#") or nxt.startswith("|") or nxt.startswith(">")
                    or _BULLET.match(nxt) or _NUMBER.match(nxt) or _IMAGE.match(nxt)
                    or _FENCE.match(nxt) or nxt in {"---", "***", "___"}):
                break
            para.append(nxt)
            i += 1
        add_paragraph("\n".join(para))

    if lead:
        spec["lead"] = lead
    if sources:
        spec["sources"] = sources
    return spec, figures


def md_to_body(markdown: str, module: str, date_str: str | None = None) -> tuple[str, dict, dict]:
    """Markdown → 微信正文 HTML（与流水线出稿同一套排版）。返回 (html, spec, figures)。"""
    spec, figures = parse_markdown(markdown)
    return wechat.render_body(spec, figures, module, date_str), spec, figures
