"""ArticleSpec → 微信公众号富文本 HTML（全内联样式）。

为什么不用 CSS 类 / flex：
公众号编辑器会过滤 <style>、class、部分 flex 与定位属性。稳妥做法只有三条：
1. 全部样式内联，用 section/p/table 这些它认的标签；
2. 布局优先用 table + padding，而不是 flex / grid；
3. 图片宽度 100%，不写死 px 高度，避免不同机型错位。
"""
from __future__ import annotations

import html as _html
import re
from datetime import date

from ..config import topics

T = topics.raw.get("theme", {})
INK = T.get("ink", "#1B1D22")
SUB = T.get("sub", "#6B7280")
PRIMARY = T.get("primary", "#2A4BD7")
PRIMARY_SOFT = T.get("primary_soft", "#EEF2FF")
ACCENT = T.get("accent", "#00A88F")
WARM = T.get("warm", "#E8833A")
QUOTE_BG = T.get("quote_bg", "#F7F8FA")
LINE = T.get("line", "#E6E8EE")
FONT = T.get("font", '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif')

MODULE_LABEL = {"paper": "论文精读", "industry": "业界观察", "contest": "赛事速递"}
MODULE_COLOR = {"paper": PRIMARY, "industry": ACCENT, "contest": WARM}
MODULE_SOFT = {"paper": PRIMARY_SOFT, "industry": "#E9F8F5", "contest": "#FFF3E9"}


def esc(text: str) -> str:
    return _html.escape(str(text or ""), quote=False)


def inline(text: str) -> str:
    """极简内联语法：**粗体**、==高亮==、`代码`、[文字](链接)。"""
    escaped = esc(text)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: (
            f'<span style="color:{PRIMARY};border-bottom:1px solid #C7D3FF;'
            f'word-break:break-all;">{m.group(1)}</span>'
        ),
        escaped,
    )
    escaped = re.sub(
        r"\*\*(.+?)\*\*",
        r'<strong style="font-weight:700;color:#0F1218;">\1</strong>',
        escaped,
    )
    escaped = re.sub(
        r"==(.+?)==",
        r'<span style="background:linear-gradient(transparent 58%, #FFEFC2 58%);'
        r'font-weight:600;">\1</span>',
        escaped,
    )
    escaped = re.sub(
        r"`([^`]+?)`",
        r'<code style="background:#F1F3F8;border-radius:3px;padding:1px 5px;'
        r'font-size:13px;color:#3C4252;font-family:Menlo,Consolas,monospace;">\1</code>',
        escaped,
    )
    return escaped.replace("\n", "<br />")


# ------------------------------------------------------------------ 基础块
def kicker(module: str, date_str: str) -> str:
    color = MODULE_COLOR.get(module, PRIMARY)
    soft = MODULE_SOFT.get(module, PRIMARY_SOFT)
    label = MODULE_LABEL.get(module, "笔记")
    return (
        f'<section style="margin:0 0 20px;">'
        f'<section style="display:inline-block;padding:4px 11px;background:{soft};'
        f'border-radius:4px;font-size:11px;letter-spacing:1.5px;color:{color};'
        f'font-weight:600;line-height:1.6;">{label} · {date_str}</section>'
        f"</section>"
    )


def lead_block(paragraphs: list[str]) -> str:
    if not paragraphs:
        return ""
    parts = []
    for idx, para in enumerate(paragraphs):
        color = "#33373F" if idx == 0 else "#4A4F5A"
        size = "16px" if idx == 0 else "15px"
        parts.append(
            f'<p style="margin:0 0 16px;font-size:{size};line-height:1.95;color:{color};'
            f'text-align:justify;letter-spacing:0.3px;">{inline(para)}</p>'
        )
    return "".join(parts)


def h2(text: str) -> str:
    raw = str(text or "")
    m = re.match(r"^(\d{2})\s*[|｜]\s*(.+)$", raw)
    if m:
        num, title = m.group(1), m.group(2)
        return (
            f'<section style="margin:38px 0 18px;padding-left:12px;border-left:4px solid {PRIMARY};">'
            f'<section style="font-size:11px;color:{PRIMARY};letter-spacing:2px;font-weight:700;'
            f'line-height:1.4;margin-bottom:2px;">SECTION {num}</section>'
            f'<section style="font-size:17px;font-weight:700;color:#12151B;line-height:1.55;'
            f'letter-spacing:0.2px;">{inline(title)}</section>'
            f"</section>"
        )
    return (
        f'<section style="margin:34px 0 16px;padding-left:12px;border-left:4px solid {PRIMARY};">'
        f'<section style="font-size:17px;font-weight:700;color:#12151B;line-height:1.55;">'
        f"{inline(raw)}</section></section>"
    )


def h3(text: str) -> str:
    return (
        f'<section style="margin:26px 0 12px;font-size:15px;font-weight:700;color:{PRIMARY};'
        f'line-height:1.7;">'
        f'<span style="display:inline-block;width:6px;height:6px;background:{ACCENT};'
        f'border-radius:50%;margin-right:8px;vertical-align:middle;"></span>{inline(text)}</section>'
    )


def paragraph(text: str) -> str:
    return (
        f'<p style="margin:0 0 17px;font-size:15px;line-height:1.9;color:#2B2E36;'
        f'text-align:justify;letter-spacing:0.3px;">{inline(text)}</p>'
    )


def bullets(items: list[str]) -> str:
    rows = []
    for item in items:
        if not item:
            continue
        rows.append(
            f'<section style="margin:0 0 10px;padding-left:17px;text-indent:-17px;'
            f'font-size:15px;line-height:1.85;color:#2B2E36;text-align:justify;">'
            f'<span style="display:inline-block;width:6px;height:6px;background:{PRIMARY};'
            f'border-radius:50%;margin-right:9px;vertical-align:middle;text-indent:0;"></span>'
            f"{inline(item)}</section>"
        )
    return f'<section style="margin:4px 0 20px;">{"".join(rows)}</section>'


def numbered(items: list[str]) -> str:
    rows = []
    for i, item in enumerate(items, 1):
        if not item:
            continue
        rows.append(
            f'<section style="margin:0 0 12px;padding-left:28px;text-indent:-28px;'
            f'font-size:15px;line-height:1.85;color:#2B2E36;text-align:justify;">'
            f'<span style="display:inline-block;width:22px;text-indent:0;color:{PRIMARY};'
            f'font-weight:700;font-size:13px;font-family:Georgia,serif;">{i:02d}</span>'
            f"{inline(item)}</section>"
        )
    return f'<section style="margin:4px 0 20px;">{"".join(rows)}</section>'


def quote(text: str) -> str:
    return (
        f'<section style="margin:22px 0;padding:15px 16px 15px 18px;background:{QUOTE_BG};'
        f'border-left:3px solid #C6CEDF;border-radius:0 6px 6px 0;">'
        f'<p style="margin:0;font-size:14px;line-height:1.85;color:#5A6070;'
        f'text-align:justify;">{inline(text)}</p></section>'
    )


def highlight(title: str, text: str) -> str:
    return (
        f'<section style="margin:24px 0;padding:16px 18px;background:{PRIMARY_SOFT};'
        f'border:1px solid #DCE4FF;border-radius:8px;">'
        f'<section style="font-size:10px;letter-spacing:2px;color:{PRIMARY};font-weight:700;'
        f'margin-bottom:7px;line-height:1.5;">{esc(title or "核心结论")}</section>'
        f'<section style="font-size:15px;font-weight:600;color:#1B2A4A;line-height:1.8;'
        f'text-align:justify;">{inline(text)}</section></section>'
    )


def meta_card(rows: list) -> str:
    trs = []
    valid = [r for r in rows or [] if isinstance(r, (list, tuple)) and len(r) >= 2 and r[1]]
    for idx, (label, value) in enumerate(valid):
        border = "" if idx == len(valid) - 1 else "border-bottom:1px dashed #EFF1F6;"
        value_html = inline(str(value))
        trs.append(
            f'<tr>'
            f'<td style="width:76px;padding:9px 8px 9px 0;font-size:12px;color:{SUB};'
            f'vertical-align:top;line-height:1.75;white-space:nowrap;{border}">{esc(label)}</td>'
            f'<td style="padding:9px 0;font-size:13px;color:#2B2E36;vertical-align:top;'
            f'line-height:1.75;{border}">{value_html}</td>'
            f"</tr>"
        )
    if not trs:
        return ""
    return (
        f'<section style="margin:24px 0;border:1px solid {LINE};border-radius:10px;'
        f'overflow:hidden;">'
        f'<section style="padding:10px 16px;background:#F8FAFF;border-bottom:1px solid {LINE};">'
        f'<span style="font-size:11px;letter-spacing:2px;color:{PRIMARY};font-weight:700;">'
        f"PAPER INFO</span></section>"
        f'<section style="padding:4px 16px;"><table style="width:100%;border-collapse:collapse;">'
        f'<tbody>{"".join(trs)}</tbody></table></section></section>'
    )


def figure(block: dict, figures: dict[str, dict]) -> str:
    meta = figures.get(str(block.get("ref")), {})
    src = meta.get("path") or meta.get("url") or ""
    caption = str(block.get("caption") or meta.get("caption") or "")
    if not src:
        return (
            f'<section style="margin:24px 0;padding:26px 12px;background:#F7F8FA;'
            f'border:1px dashed #D9DEE9;border-radius:8px;text-align:center;">'
            f'<section style="font-size:12px;color:#9AA0AE;">配图位 · {esc(caption or "架构图")}</section>'
            f"</section>"
        )
    return (
        f'<section style="margin:26px 0;">'
        f'<img src="{esc(src)}" style="width:100%;display:block;border-radius:6px;" />'
        f'<section style="margin-top:9px;padding-left:2px;font-size:12px;color:#8A90A0;'
        f'line-height:1.7;text-align:left;">{inline(caption)}</section></section>'
    )


def table(header: list, rows: list) -> str:
    if not header:
        return ""
    ths = "".join(
        f'<th style="padding:9px 8px;font-size:12px;font-weight:600;color:#FFFFFF;'
        f'text-align:left;background:{PRIMARY};border-right:1px solid rgba(255,255,255,0.18);'
        f'line-height:1.6;">{inline(str(h))}</th>'
        for h in header
    )
    trs = []
    for idx, row in enumerate(rows):
        bg = "#FFFFFF" if idx % 2 == 0 else "#FAFBFE"
        tds = "".join(
            f'<td style="padding:9px 8px;font-size:12.5px;color:#33373F;line-height:1.65;'
            f'vertical-align:top;border-bottom:1px solid #EDEFF4;background:{bg};">{inline(str(c))}</td>'
            for c in row
        )
        trs.append(f"<tr>{tds}</tr>")
    return (
        f'<section style="margin:22px 0;overflow-x:auto;">'
        f'<table style="width:100%;border-collapse:collapse;table-layout:fixed;">'
        f'<thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table></section>'
    )


def contest_card(block: dict) -> str:
    rows = [
        ("赛题方向", block.get("track", "")),
        ("奖金池", block.get("prize", "")),
        ("时间节点", block.get("deadline", "")),
        ("参赛入口", block.get("entry", "")),
    ]
    trs = []
    for label, value in rows:
        if not value:
            continue
        trs.append(
            f'<tr><td style="width:70px;padding:7px 8px 7px 0;font-size:12px;color:{SUB};'
            f'vertical-align:top;white-space:nowrap;line-height:1.7;">{label}</td>'
            f'<td style="padding:7px 0;font-size:13px;color:#2B2E36;vertical-align:top;'
            f'line-height:1.7;word-break:break-all;">{inline(str(value))}</td></tr>'
        )
    return (
        f'<section style="margin:22px 0;border:1px solid #F2E0CE;border-radius:10px;'
        f'overflow:hidden;background:#FFFCF8;">'
        f'<section style="padding:13px 16px 11px;background:#FFF4E9;'
        f'border-bottom:1px solid #F2E0CE;">'
        f'<span style="display:inline-block;padding:2px 7px;background:{WARM};color:#fff;'
        f'font-size:10px;letter-spacing:1px;border-radius:3px;vertical-align:middle;">'
        f'CHALLENGE</span>'
        f'<section style="margin-top:8px;font-size:16px;font-weight:700;color:#241A12;'
        f'line-height:1.6;">{inline(block.get("name", ""))}</section></section>'
        f'<section style="padding:10px 16px 4px;"><table style="width:100%;border-collapse:collapse;">'
        f'<tbody>{"".join(trs)}</tbody></table></section>'
        + (
            f'<section style="padding:6px 16px 16px;font-size:13px;color:#5A5148;line-height:1.8;">'
            f'{inline(block.get("desc", ""))}</section>'
            if block.get("desc") else ""
        )
        + f"</section>"
    )


def divider() -> str:
    return (
        f'<section style="margin:26px 0;text-align:center;color:#C9CEDB;font-size:12px;'
        f'letter-spacing:8px;line-height:1;">· · ·</section>'
    )


def _code_html(code: str) -> str:
    """代码原文 → 公众号安全的 HTML：换行用 <br />，行首缩进用 &nbsp; 保真。

    公众号编辑器不可靠地保留 white-space:pre-wrap，靠标签硬保换行与缩进。
    """
    lines = []
    for line in esc(code.replace("\t", "    ")).split("\n"):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        lines.append("&nbsp;" * indent + stripped)
    return "<br />".join(lines)


def code_block(block: dict) -> str:
    code = str(block.get("code") or "").rstrip()
    caption = str(block.get("caption") or block.get("lang") or "代码")
    if not code:
        return ""
    return (
        f'<section style="margin:22px 0;border:1px solid #2A3040;border-radius:8px;'
        f'overflow:hidden;">'
        f'<section style="padding:7px 14px;background:#262C3A;font-size:11px;color:#9FB0D0;'
        f'letter-spacing:1px;font-family:Menlo,Consolas,monospace;line-height:1.6;">'
        f"{esc(caption)}</section>"
        f'<section style="padding:14px 16px;background:#14181F;'
        f'font-family:Menlo,Consolas,monospace;font-size:12.5px;line-height:1.75;'
        f'color:#DCE3F0;word-break:break-all;">{_code_html(code)}</section></section>'
    )


def footer(spec: dict, module: str) -> str:
    takeaway = spec.get("takeaway") or ""
    block = ""
    if takeaway:
        block = (
            f'<section style="margin:34px 0 0;padding:20px 18px;background:#151A24;'
            f'border-radius:10px;">'
            f'<section style="font-size:10px;letter-spacing:2.5px;color:#7E8CB0;'
            f'font-weight:700;margin-bottom:8px;">写在最后</section>'
            f'<section style="font-size:15px;color:#F3F5FA;line-height:1.85;font-weight:600;'
            f'text-align:justify;">{inline(takeaway)}</section></section>'
        )

    sources = ""
    src_list = [s for s in (spec.get("sources") or []) if isinstance(s, dict) and s.get("name")]
    if src_list:
        items = "".join(
            f'<section style="margin:0 0 6px;font-size:11.5px;color:#9AA0AE;line-height:1.7;'
            f'padding-left:14px;text-indent:-14px;">'
            f'<span style="color:#C3C8D4;">{i:02d}</span>　{esc(s.get("name", ""))}'
            f'</section>'
            for i, s in enumerate(src_list, 1)
        )
        sources = (
            f'<section style="margin:28px 0 0;padding-top:16px;border-top:1px solid {LINE};">'
            f'<section style="font-size:10px;letter-spacing:2px;color:#B0B5C2;'
            f'font-weight:700;margin-bottom:9px;">SOURCES</section>{items}</section>'
        )

    brand = topics.raw.get("brand", {})
    return (
        f"{block}{sources}"
        f'<section style="margin:30px 0 6px;text-align:center;">'
        f'<section style="display:inline-block;width:26px;height:2px;background:{MODULE_COLOR.get(module, PRIMARY)};'
        f'margin-bottom:13px;"></section>'
        f'<section style="font-size:13px;color:#3A3F4B;font-weight:600;letter-spacing:1px;">'
        f'{esc(brand.get("name", "底层视觉论文笔记"))}</section>'
        f'<section style="margin-top:5px;font-size:11px;color:#A6ABB8;letter-spacing:0.5px;">'
        f'{esc(brand.get("slogan", ""))}</section></section>'
    )


# ------------------------------------------------------------------ 入口
def render_body(spec: dict, figures: dict[str, dict], module: str, date_str: str | None = None) -> str:
    date_str = date_str or date.today().strftime("%Y.%m.%d")
    parts = [
        f'<section style="font-family:{FONT};font-size:15px;line-height:1.9;color:{INK};'
        f'letter-spacing:0.3px;word-break:break-word;background:#FFFFFF;padding:2px 0;">',
        kicker(module, date_str),
        lead_block(spec.get("lead") or []),
    ]
    for block in spec.get("sections") or []:
        btype = block.get("type")
        if btype == "h2":
            parts.append(h2(block.get("text", "")))
        elif btype == "h3":
            parts.append(h3(block.get("text", "")))
        elif btype == "p":
            parts.append(paragraph(block.get("text", "")))
        elif btype == "bullets":
            parts.append(bullets(block.get("items") or []))
        elif btype == "numbered":
            parts.append(numbered(block.get("items") or []))
        elif btype == "quote":
            parts.append(quote(block.get("text", "")))
        elif btype == "highlight":
            parts.append(highlight(block.get("title", ""), block.get("text", "")))
        elif btype == "meta_card":
            parts.append(meta_card(block.get("rows") or []))
        elif btype == "figure":
            parts.append(figure(block, figures))
        elif btype == "table":
            parts.append(table(block.get("header") or [], block.get("rows") or []))
        elif btype == "contest_card":
            parts.append(contest_card(block))
        elif btype == "code":
            parts.append(code_block(block))
        elif btype == "divider":
            parts.append(divider())
    parts.append(footer(spec, module))
    parts.append("</section>")
    return "".join(parts)
