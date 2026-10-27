"""ArticleSpec → Markdown。

Markdown 在这里的角色是「可编辑的中间稿」：
- 运营同学可以直接改文字，再用 `python -m scripts.render_md` 重新排版；
- 也是入库留档的格式，比 HTML 更容易 diff。
"""
from __future__ import annotations

from ..spec_tools import normalized_spec


def _meta_rows(rows: list) -> str:
    lines = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        lines.append(f"- **{row[0]}**：{row[1]}")
    return "\n".join(lines)


def spec_to_markdown(spec: dict, figures: dict[str, dict], module: str | None = None) -> str:
    # 与 HTML 渲染共用同一套规范（编号 + 业界事件卡合并），避免预览页与可编辑稿对不上
    spec = normalized_spec(spec, module)
    out: list[str] = [f"# {spec.get('title', '')}", ""]
    if spec.get("subtitle"):
        out += [f"> {spec['subtitle']}", ""]
    if spec.get("digest"):
        out += [f"**导读**：{spec['digest']}", ""]

    for para in spec.get("lead", []) or []:
        out += [str(para), ""]

    for block in spec.get("sections", []) or []:
        btype = block.get("type")
        if btype == "h2":
            out += [f"## {block.get('text', '')}", ""]
        elif btype == "h3":
            out += [f"### {block.get('text', '')}", ""]
        elif btype == "p":
            out += [str(block.get("text", "")), ""]
        elif btype == "bullets":
            out += [f"- {item}" for item in block.get("items", []) if item] + [""]
        elif btype == "numbered":
            out += [f"{i}. {item}" for i, item in enumerate(block.get("items", []), 1) if item] + [""]
        elif btype == "quote":
            out += [f"> {block.get('text', '')}", ""]
        elif btype == "highlight":
            out += [f"> **{block.get('title', '核心结论')}**：{block.get('text', '')}", ""]
        elif btype == "meta_card":
            out += [_meta_rows(block.get("rows", [])), ""]
        elif btype == "figure":
            meta = figures.get(str(block.get("ref")), {})
            src = meta.get("path") or meta.get("url") or ""
            out += [f"![{block.get('caption', '')}]({src})", ""]
        elif btype == "table":
            header = block.get("header", [])
            rows = block.get("rows", [])
            if header:
                out.append("| " + " | ".join(str(h) for h in header) + " |")
                out.append("|" + "|".join(["---"] * len(header)) + "|")
                for row in rows:
                    out.append("| " + " | ".join(str(c) for c in row) + " |")
                out.append("")
        elif btype == "contest_card":
            out += [f"### {block.get('name', '')}", ""]
            for label, key in (("赛题方向", "track"), ("奖金池", "prize"), ("时间节点", "deadline")):
                if block.get(key):
                    out.append(f"- **{label}**：{block[key]}")
            if block.get("entry"):
                out.append(f"- **参赛入口**：{block['entry']}")
            if block.get("desc"):
                out += ["", str(block["desc"])]
            out.append("")
        elif btype == "code":
            caption = block.get("caption") or ""
            lang = block.get("lang") or "python"
            if caption:
                out += [f"**{caption}**", ""]
            out += [f"```{lang}", str(block.get("code", "")).rstrip(), "```", ""]
        elif btype == "divider":
            out += ["---", ""]

    if spec.get("takeaway"):
        out += ["## 一句话总结", "", f"**{spec['takeaway']}**", ""]
    if spec.get("sources"):
        out += ["### 参考来源", ""]
        for src in spec["sources"]:
            if isinstance(src, dict):
                out.append(f"- [{src.get('name', '')}]({src.get('url', '')})")
        out.append("")
    return "\n".join(out).strip() + "\n"
