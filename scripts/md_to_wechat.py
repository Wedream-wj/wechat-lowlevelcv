"""本地 Markdown → 微信公众号富文本：正文逐字不动，只换图片地址。

为什么单独写一条链路，而不是走 `llm.write` + `render_body`：
1. 那条链路是「素材 → 模型成稿」，模型必然改写文字；本任务的第一铁律是
   一个字都不许改，所以只能做纯机械的 Markdown → HTML 映射；
2. `render_body` 会加 kicker（栏目 + 日期）和固定尾部，属于「新增内容」，
   同样违反零篡改；这里只取它的排版函数（h2/h3/paragraph/quote/table），
   保证视觉风格与全平台一致，但不加任何页眉页脚。

用法：
    python -m scripts.md_to_wechat "D:/path/a.md"                # 渲染 + 本地预览
    python -m scripts.md_to_wechat "D:/path/a.md" --upload        # 图片传微信 CDN
    python -m scripts.md_to_wechat "D:/path/a.md" --push          # 传图并建草稿

图片策略：
- 远程图（CSDN 等）先下载到 `out/assets/mdimg/`，校验格式；
- 微信正文图只能走 `media/uploadimg`（返回 mmbiz.qpic.cn），外链在微信端
  会被防盗链拦成裂图，所以「换图床」的正确解法就是传微信自己的 CDN；
- 上传失败（典型是 40164 IP 白名单）时保留本地相对路径，预览照常可看，
  并打印需要加入白名单的 IP，绝不静默产出裂图稿。
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.render import wechat as W  # noqa: E402
from app.config import settings  # noqa: E402
from app.publish.attrs import PublishAttrs, build_draft_payload, assert_draft_payload  # noqa: E402

OUT_DIR = settings.out_dir / "md2wx"

# ---------------------------------------------------------------- 行内语法
MATH_STYLE = (
    "font-family:Georgia,'Times New Roman',serif;font-style:italic;"
    "color:#1B1D22;letter-spacing:0.2px;"
)


def esc(text: str) -> str:
    return _html.escape(str(text or ""), quote=False)


def inline_md(text: str) -> str:
    """**粗体** / *斜体* / `代码` / $公式$ / [文字](链接)。

    与 `wechat.inline` 的差别只有两条：补了斜体与行内公式。公式不转码、不重排，
    原样保留 LaTeX 字符（只去掉 $ 定界符——它和 ** 一样属于标记，不是正文）。
    """
    s = esc(text)
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: (
            f'<span style="color:{W.PRIMARY};border-bottom:1px solid #C7D3FF;'
            f'word-break:normal;overflow-wrap:break-word;hyphens:none;">{m.group(1)}</span>'
        ),
        s,
    )
    s = re.sub(
        r"\$([^$\n]+)\$",
        lambda m: f'<span style="{MATH_STYLE}">{m.group(1)}</span>',
        s,
    )
    s = re.sub(
        r"\*\*(.+?)\*\*",
        r'<strong style="font-weight:700;color:#0F1218;">\1</strong>',
        s,
    )
    s = re.sub(
        r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
        r'<span style="font-style:italic;">\1</span>',
        s,
    )
    s = re.sub(
        r"`([^`]+?)`",
        r'<code style="background:#F1F3F8;border-radius:3px;padding:1px 5px;'
        r'font-size:13px;color:#3C4252;font-family:Menlo,Consolas,monospace;">\1</code>',
        s,
    )
    return s.replace("\n", "<br />")


# ---------------------------------------------------------------- 块解析
@dataclass
class Block:
    kind: str
    text: str = ""                       # h/p/quote
    level: int = 0                       # heading level
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)   # list
    src: str = ""                        # img
    width: str = ""
    alt: str = ""
    ordered: bool = False

    def plain(self) -> str:
        """该块的「纯文本」：用于与渲染结果逐字比对，标记符号不算正文。"""
        if self.kind in {"h", "p", "quote"}:
            return strip_md(self.text)
        if self.kind == "table":
            return " ".join(" ".join(c for c in row) for row in [self.header] + self.rows)
        if self.kind == "list":
            return " ".join(strip_md(i) for i in self.items)
        return ""


def strip_md(text: str) -> str:
    """去掉内联标记，留下正文；与 inline_md 的替换一一对应。"""
    s = str(text or "")
    s = re.sub(r"\$([^$\n]+)\$", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]+?)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1", s)
    return s


_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$")
_IMG_RE = re.compile(r"<img\b([^>]*)/?>", re.I)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def parse_blocks(markdown: str) -> list[Block]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        head = _HEAD_RE.match(line)
        if head:
            blocks.append(Block(kind="h", level=len(head.group(1)), text=head.group(2).strip()))
            i += 1
            continue
        if line.lstrip().startswith("<img"):
            m = _IMG_RE.search(line)
            attrs = dict(_ATTR_RE.findall(m.group(1))) if m else {}
            blocks.append(Block(kind="img", src=attrs.get("src", ""),
                                width=attrs.get("width", ""), alt=attrs.get("alt", "")))
            i += 1
            continue
        if line.strip() in {"---", "***", "___"}:
            blocks.append(Block(kind="hr"))
            i += 1
            continue
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(Block(kind="quote", text="\n".join(buf).strip()))
            continue
        if line.lstrip().startswith("|"):
            rows: list[list[str]] = []
            while i < n and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append(Block(kind="table", header=rows[0], rows=rows[1:]))
            continue
        listed = _LIST_RE.match(line)
        if listed:
            items, ordered = [], bool(listed.group(2))
            while i < n:
                m = _LIST_RE.match(lines[i])
                if not m or bool(m.group(2)) != ordered:
                    break
                items.append(m.group(3).strip())
                i += 1
            blocks.append(Block(kind="list", items=items, ordered=ordered))
            continue
        buf = []
        while i < n and lines[i].strip() and not (
            _HEAD_RE.match(lines[i]) or lines[i].lstrip().startswith((">", "|", "<img"))
            or lines[i].strip() in {"---", "***", "___"} or _LIST_RE.match(lines[i])
        ):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(Block(kind="p", text="\n".join(buf)))
    return blocks


# ---------------------------------------------------------------- 块渲染
def render_hr() -> str:
    """分隔线只用一条 1px 细线，不写「· · ·」——那三个点原文没有，属于新增字符。"""
    return ('<section style="margin:28px 0;height:1px;background:#E6E8EE;'
            'line-height:0;font-size:0;">&nbsp;</section>')


def render_img(src: str, width: str, alt: str) -> str:
    w = width.strip() if width.strip() else "100%"
    if not re.fullmatch(r"\d+(?:\.\d+)?%", w):
        w = "100%"
    return (
        f'<section style="margin:26px 0;text-align:center;">'
        f'<img src="{esc(src)}" alt="{esc(alt)}" '
        f'style="width:{w};max-width:100%;display:block;margin:0 auto;border-radius:6px;" />'
        f"</section>"
    )


def h2_html(text: str) -> str:
    """与 `wechat.h2` 样式逐项一致，只把 inline 换成 inline_md（认斜体/公式）。"""
    raw = str(text or "")
    m = re.match(r"^(\d{2})\s*[|｜]\s*(.+)$", raw)
    if m:
        num, title = m.group(1), m.group(2)
        return (
            f'<section style="margin:38px 0 18px;padding-left:12px;border-left:4px solid {W.PRIMARY};">'
            f'<section style="font-size:11px;color:{W.PRIMARY};letter-spacing:2px;font-weight:700;'
            f'line-height:1.4;margin-bottom:2px;">SECTION {num}</section>'
            f'<section style="font-size:17px;font-weight:700;color:#12151B;line-height:1.55;'
            f'letter-spacing:0.2px;">{inline_md(title)}</section>'
            f"</section>"
        )
    return (
        f'<section style="margin:34px 0 16px;padding-left:12px;border-left:4px solid {W.PRIMARY};">'
        f'<section style="font-size:17px;font-weight:700;color:#12151B;line-height:1.55;">'
        f"{inline_md(raw)}</section></section>"
    )


def h3_html(text: str) -> str:
    return (
        f'<section style="margin:26px 0 12px;font-size:15px;font-weight:700;color:{W.PRIMARY};'
        f'line-height:1.7;">'
        f'<span style="display:inline-block;width:6px;height:6px;background:{W.ACCENT};'
        f'border-radius:50%;margin-right:8px;vertical-align:middle;"></span>{inline_md(text)}</section>'
    )


def paragraph_html(text: str) -> str:
    return (
        f'<p style="margin:0 0 17px;font-size:15px;line-height:1.9;color:#2B2E36;'
        f'text-align:justify;letter-spacing:0.3px;">{inline_md(text)}</p>'
    )


def render_block(b: Block) -> str:
    if b.kind == "h":
        if b.level == 1:
            return ""                       # H1 进标题字段，正文不重复
        return h2_html(b.text) if b.level == 2 else h3_html(b.text)
    if b.kind == "p":
        return paragraph_html(b.text)
    if b.kind == "quote":
        return (
            f'<section style="margin:22px 0;padding:15px 16px 15px 18px;background:{W.QUOTE_BG};'
            f'border-left:3px solid #C6CEDF;border-radius:0 6px 6px 0;">'
            f'<p style="margin:0;font-size:14px;line-height:1.85;color:#5A6070;'
            f'text-align:justify;">{inline_md(b.text)}</p></section>'
        )
    if b.kind == "table":
        return render_table(b.header, b.rows)
    if b.kind == "list":
        rows = []
        for idx, item in enumerate(b.items, 1):
            if b.ordered:
                rows.append(
                    f'<section style="margin:0 0 12px;padding-left:28px;text-indent:-28px;'
                    f'font-size:15px;line-height:1.85;color:#2B2E36;text-align:justify;">'
                    f'<span style="display:inline-block;width:22px;text-indent:0;color:{W.PRIMARY};'
                    f'font-weight:700;font-size:13px;font-family:Georgia,serif;">{idx:02d}</span>'
                    f"{inline_md(item)}</section>"
                )
            else:
                rows.append(
                    f'<section style="margin:0 0 10px;padding-left:17px;text-indent:-17px;'
                    f'font-size:15px;line-height:1.85;color:#2B2E36;text-align:justify;">'
                    f'<span style="display:inline-block;width:6px;height:6px;background:{W.PRIMARY};'
                    f'border-radius:50%;margin-right:9px;vertical-align:middle;text-indent:0;"></span>'
                    f"{inline_md(item)}</section>"
                )
        return f'<section style="margin:4px 0 20px;">{"".join(rows)}</section>'
    if b.kind == "img":
        return render_img(b.src, b.width, b.alt)
    if b.kind == "hr":
        return render_hr()
    return ""


def render_table(header: list[str], rows: list[list[str]]) -> str:
    """复用系统表格样式（表头主色、斑马纹、首列不换行），只把 inline 换成 inline_md。

    不直接调 `W.table`，因为它的 `inline()` 不认斜体/公式；本稿表格里虽没有，
    但保持一条链路上只有一种内联渲染，避免以后加内容时两种口径打架。
    """
    ncols = len(header)
    rows = [r[:ncols] for r in rows]
    if ncols >= 5:
        pad, head_size, cell_size = "8px 5px", "11px", "11.5px"
    elif ncols == 4:
        pad, head_size, cell_size = "9px 7px", "11.5px", "12px"
    else:
        pad, head_size, cell_size = "9px 8px", "12px", "12.5px"
    ths = "".join(
        f'<th style="padding:{pad};font-size:{head_size};font-weight:600;color:#FFFFFF;'
        f'text-align:left;background:{W.PRIMARY};border-right:1px solid rgba(255,255,255,0.18);'
        f'line-height:1.5;{W._nowrap_if_first(ci)}">{inline_md(h)}</th>'
        for ci, h in enumerate(header)
    )
    trs = []
    for idx, row in enumerate(rows):
        bg = "#FFFFFF" if idx % 2 == 0 else "#FAFBFE"
        tds = "".join(
            f'<td style="padding:{pad};font-size:{cell_size};color:#33373F;line-height:1.55;'
            f'vertical-align:middle;border-bottom:1px solid #EDEFF4;background:{bg};'
            f'{W._nowrap_if_first(ci)}">{inline_md(c)}</td>'
            for ci, c in enumerate(row)
        )
        trs.append(f"<tr>{tds}</tr>")
    return (
        f'<section style="margin:22px 0;overflow-x:auto;">'
        f'<table style="width:100%;max-width:100%;border-collapse:collapse;table-layout:auto;">'
        f'<thead><tr>{ths}</tr></thead><tbody>{"".join(trs)}</tbody></table></section>'
    )


# ---------------------------------------------------------------- 保真校验
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


_BLOCK_CLOSE_RE = re.compile(r"(?i)(</p>|</section>|</h[1-6]>|</li>|<br\s*/?>)")


def html_text(fragment: str) -> str:
    """HTML 片段 → 可见文本（不含 alt：alt 是属性，不是正文）。

    块级闭合标签与 <br> 记作换行：源 Markdown 里的段内换行/条目边界本来就是
    「空白」，两侧都按空白归一，逐字比对才不会被排版结构骗到。
    """
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", fragment)
    s = _BLOCK_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return _WS_RE.sub(" ", s).strip()


def _table_cells(fragment: str) -> list[str]:
    return [_WS_RE.sub(" ", html_text(c)).strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", fragment, re.S)]


def verify_fidelity(blocks: list[Block]) -> list[str]:
    """逐块比对「源文本」与「渲染文本」，返回不一致明细（空表示一字未改）。"""
    problems: list[str] = []
    for idx, b in enumerate(blocks):
        if b.kind == "h" and b.level == 1:
            continue
        if b.kind == "table":
            want = [_WS_RE.sub(" ", c).strip() for c in b.header] + \
                   [_WS_RE.sub(" ", c).strip() for row in b.rows for c in row]
            got = _table_cells(render_block(b))
            if want != got:
                problems.append(f"块#{idx}（table）单元格不一致：{want} vs {got}")
            continue
        want = _WS_RE.sub(" ", b.plain()).strip()
        got = html_text(render_block(b))
        if want != got:
            problems.append(f"块#{idx}（{b.kind}）源={want[:60]!r} 渲染={got[:60]!r}")
    return problems


# ---------------------------------------------------------------- 图片
IMG_EXTS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def _guess_ext(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


async def fetch_images(blocks: list[Block], md_dir: Path, cache_dir: Path) -> dict[str, Path]:
    """把每张图落到本地：远程图下载、本地相对路径按 md 所在目录解析。"""
    import httpx

    cache_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, Path] = {}
    async with httpx.AsyncClient(timeout=120, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Referer": "https://blog.csdn.net/"}) as client:
        for b in blocks:
            if b.kind != "img" or not b.src:
                continue
            src = b.src
            if src.startswith(("http://", "https://")):
                digest = hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]
                cand = list(cache_dir.glob(f"{digest}.*"))
                if cand:
                    mapping[src] = cand[0]
                    continue
                resp = await client.get(src)
                resp.raise_for_status()
                data = resp.content
                path = cache_dir / f"{digest}{_guess_ext(data)}"
                path.write_bytes(data)
                mapping[src] = path
            else:
                path = (md_dir / src).resolve()
                if path.exists():
                    mapping[src] = path
    return mapping


async def upload_images(mapping: dict[str, Path]) -> tuple[dict[str, str], str]:
    """上传到微信正文图床；返回 (src→URL, 失败原因)。"""
    from app.publish.wechat_api import WeChatClient, WeChatError

    out: dict[str, str] = {}
    async with WeChatClient() as wx:
        for src, path in mapping.items():
            try:
                out[src] = await wx.upload_content_image(path)
            except WeChatError as exc:
                return out, str(exc)
            except Exception as exc:                      # noqa: BLE001
                return out, f"{type(exc).__name__}: {exc}"
    return out, ""


def rewrite_images(blocks: list[Block], urls: dict[str, str]) -> list[str]:
    """返回替换后的图片 src 列表（按块顺序），供 HTML 与 Markdown 两份产物共用。"""
    out = []
    for b in blocks:
        if b.kind != "img" or not b.src:
            continue
        out.append(urls.get(b.src) or b.src)
    return out


def reuse_uploaded(src: Path) -> dict[str, str]:
    """复用上一次上传成功的 CDN 地址：同一张图重复上传只会白占素材库。"""
    report = OUT_DIR / "report.json"
    if not report.exists():
        return {}
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return {}
    if Path(str(data.get("source") or "")).resolve() != src.resolve():
        return {}
    return {str(k): str(v) for k, v in (data.get("image_urls") or {}).items() if v}


def local_preview_srcs(
    blocks: list[Block], mapping: dict[str, Path], dest_dir: Path, rel_prefix: str = ""
) -> dict[str, str]:
    """预览用图：把落地的图再拷一份到预览目录，返回「原地址 → 相对路径」。

    WHY：微信图床（mmbiz.qpic.cn）有防盗链，本地预览带着 Referer 打开时它回的是
    「此图片来自微信公众平台 未经允许不可引用」的 2KB 占位图（实测无 Referer 才是
    真图）。预览页因此永远不该引用 CDN 地址，一律指向本地副本；正文稿
    （推给公众号的那份）才用 CDN。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for b in blocks:
        if b.kind != "img" or not b.src:
            continue
        path = Path(mapping.get(b.src) or "")
        if not path.exists():
            continue
        if path.parent.resolve() == dest_dir.resolve():
            name = path.name                    # 已经在预览目录里，直接引用，不重复拷贝
        else:
            name = f"img{len(out) + 1:02d}{path.suffix.lower() or '.png'}"
            target = dest_dir / name
            if not target.exists() or target.stat().st_size != path.stat().st_size:
                shutil.copyfile(path, target)
        out[b.src] = f"{rel_prefix}{name}" if rel_prefix else name
    return out


# ---------------------------------------------------------------- 产物
PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<!-- 预览页可能残留外链图：微信 mmbiz.qpic.cn 带 Referer 访问会返回
     「此图片来自微信公众平台 未经允许不可引用」的占位图（实测 <3KB），
     去掉 Referer 才给真图。正文里的图已经换成本地副本，这条只是兜底。 -->
<meta name="referrer" content="no-referrer" />
<title>{title} · 微信预览</title>
<style>
  body {{ margin:0; padding:28px 0 60px; background:#EDEFF3;
         font-family:{font}; }}
  .meta {{ max-width:375px; margin:0 auto 14px; display:flex; gap:10px; align-items:center;
          font-size:12px; color:#6B7280; }}
  .meta b {{ color:#2A4BD7; font-weight:600; }}
  .phone {{ width:375px; margin:0 auto; background:#fff; border-radius:14px;
           box-shadow:0 8px 30px rgba(20,28,60,.10); overflow:hidden; }}
  .topbar {{ padding:13px 16px; border-bottom:1px solid #EFF1F5; font-size:13px;
            color:#111827; font-weight:600; }}
  .title {{ padding:22px 20px 0; font-size:22px; line-height:1.42; font-weight:700;
           color:#111318; letter-spacing:.2px; }}
  .byline {{ padding:10px 20px 4px; font-size:12px; color:#9AA0AE; }}
  .body {{ padding:14px 20px 34px; }}
  .body p a {{ text-decoration:none; }}
  .warn {{ max-width:375px; margin:14px auto 0; padding:10px 14px; background:#FFF7E8;
          border:1px solid #F3E1BE; border-radius:8px; font-size:12px; color:#8A6A2F;
          line-height:1.7; }}
</style>
</head>
<body>
  <div class="meta"><b>微信预览</b><span>375px</span><span>·</span><span>{stats}</span></div>
  <div class="phone">
    <div class="topbar">公众号 · {author}</div>
    <div class="title">{title}</div>
    <div class="byline">{author} · 2026.11.18</div>
    <div class="body">
{body}
    </div>
  </div>
  {warn}
</body>
</html>
"""


def build_markdown(blocks: list[Block], urls: dict[str, str], title: str) -> str:
    """把原文里的图片地址换掉，其余字符一个不动（按块重建，块内原文直出）。"""
    lines: list[str] = [f"# {title}", ""]
    img_index = 0
    for b in blocks:
        if b.kind == "h" and b.level == 1:
            continue
        if b.kind == "h":
            lines.append(f"{'#' * b.level} {b.text}")
        elif b.kind == "p":
            lines.append(b.text)
        elif b.kind == "quote":
            lines.extend(f"> {ln}" for ln in b.text.split("\n"))
        elif b.kind == "table":
            width = len(b.header)
            lines.append("| " + " | ".join(b.header) + " |")
            lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
            for row in b.rows:
                lines.append("| " + " | ".join(row) + " |")
        elif b.kind == "list":
            for idx, item in enumerate(b.items, 1):
                lines.append(f"{idx}. {item}" if b.ordered else f"- {item}")
        elif b.kind == "img":
            src = urls.get(b.src) or b.src
            img_index += 1
            tag = f'<img src="{src}"'
            if b.width:
                tag += f' width="{b.width}"'
            if b.alt:
                tag += f' alt="{b.alt}"'
            lines.append(tag + ' align="center" />')
        elif b.kind == "hr":
            lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 主流程
async def run(args: argparse.Namespace) -> int:
    src = Path(args.markdown).expanduser().resolve()
    if not src.exists():
        print(f"找不到文件：{src}")
        return 2
    text = src.read_text(encoding="utf-8")
    blocks = parse_blocks(text)
    title = next((b.text for b in blocks if b.kind == "h" and b.level == 1), src.stem)

    problems = verify_fidelity(blocks)
    if problems:
        print("✗ 正文保真校验未通过，已中止（不允许带着改动出稿）：")
        for p in problems[:10]:
            print("   ", p)
        return 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mapping = await fetch_images(blocks, src.parent, OUT_DIR / "images")

    urls: dict[str, str] = {}
    upload_error = ""
    if args.upload or args.push:
        urls = reuse_uploaded(src) if args.reuse else {}
        pending = {k: v for k, v in mapping.items() if k not in urls}
        if pending:
            fresh, upload_error = await upload_images(pending)
            urls.update(fresh)
        if upload_error:
            print("! 微信图床上传失败，先保留本地图：", upload_error.split("｜")[0])
            print("  →", upload_error)
            if args.push:
                print("✗ 图片没上床就不建草稿（微信端会裂图），已中止推送")
                return 4

    body = "".join(render_block(b) for b in blocks)
    # 正文稿：图片指向微信 CDN（外链在微信端会被防盗链拦成裂图）
    content_body = _replace_img_src(body, blocks, urls) if urls else body
    if urls:
        # 只换 src 属性，不碰任何文本节点——保真校验已在上面对「无图版本」跑过一遍，
        # 这里再做一遍反向确认：替换前后可见文本必须完全相同
        if html_text(content_body) != html_text(body):
            print("✗ 图片地址替换改动了正文，已中止")
            return 5
    content = (f'<section style="font-family:{W.FONT};font-size:15px;line-height:1.9;'
               f'color:{W.INK};">{content_body}</section>')

    content_path = OUT_DIR / "content.html"
    content_path.write_text(content, encoding="utf-8")

    # 预览页：图片一律指向本地副本。微信 CDN 带 Referer 访问会回
    # 「此图片来自微信公众平台 未经允许不可引用」的占位图（实测 2KB vs 真图 800KB），
    # 预览页不该依赖它；本地副本还能离线看、双击打开也不会裂。
    local_urls = local_preview_srcs(blocks, mapping, OUT_DIR / "images", rel_prefix="images/")
    preview_body = _replace_img_src(body, blocks, local_urls) if local_urls else body

    stats = f"{len(html_text(content).replace(' ', ''))} 字 · {len(mapping)} 图"
    warn = ""
    if upload_error:
        warn = (f'<div class="warn"><b>图片尚未上微信图床</b>（{esc(upload_error.split("｜")[0])}）：'
                f'预览用的是本地图，正文稿里的 src 仍是本地/外链，需白名单后重跑 --upload。</div>')
    preview = PREVIEW_TEMPLATE.format(
        title=esc(title), font=W.FONT, stats=stats, author=esc(settings.wx_author),
        body=preview_body, warn=warn,
    )
    preview_path = OUT_DIR / "preview.html"
    preview_path.write_text(preview, encoding="utf-8")

    md_out = OUT_DIR / f"{src.stem}.wechat.md"
    md_out.write_text(build_markdown(blocks, urls, title), encoding="utf-8")

    report = {
        "source": str(src),
        "title": title,
        "blocks": len(blocks),
        "images": {k: str(v) for k, v in mapping.items()},
        "image_urls": urls,
        "upload_error": upload_error,
        "fidelity": "ok" if not problems else problems,
        "chars": len(html_text(content).replace(" ", "")),
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    print(f"✓ 正文保真：{len(blocks)} 块逐字一致")
    print(f"✓ 图片：{len(mapping)} 张已落地，{len(urls)} 张已上微信 CDN")
    print(f"·  正文稿：{content_path}")
    print(f"·  预览页：{preview_path}")
    print(f"·  改址稿：{md_out}")

    if args.push:
        media_id = await push_draft(content, title, blocks, mapping)
        print(f"✓ 草稿已创建：media_id={media_id}")
        print(f"  发布属性：{PublishAttrs.from_settings().describe()}")
    return 0


def _replace_img_src(fragment: str, blocks: list[Block], urls: dict[str, str]) -> str:
    for b in blocks:
        if b.kind != "img" or not b.src or b.src not in urls:
            continue
        fragment = fragment.replace(f'src="{esc(b.src)}"', f'src="{esc(urls[b.src])}"')
    return fragment


async def push_draft(content: str, title: str, blocks: list[Block], mapping: dict[str, Path]) -> str:
    """建草稿：封面用现有 thumb_media_id，正文图必须已在 CDN（run 里已保证）。"""
    from app.publish.wechat_api import WeChatClient

    digest = ""
    for b in blocks:
        if b.kind == "p" and len(b.plain()) > 40:
            digest = b.plain()
            break
    attrs = PublishAttrs.from_settings()
    async with WeChatClient() as wx:
        thumb = settings.wx_thumb_media_id
        if not thumb and mapping:
            thumb = await _cover_media_id(wx, next(iter(mapping.values())))
        return await wx.add_draft(
            title=title, author=settings.wx_author, digest=digest[:120],
            content=content, thumb_media_id=thumb, attrs=attrs,
        )


async def _cover_media_id(wx, path: Path) -> str:
    return await wx.upload_cover_material(path)


def main(argv: list[str] | None = None) -> int:
    import asyncio

    ap = argparse.ArgumentParser(description="Markdown → 微信公众号富文本（正文零篡改）")
    ap.add_argument("markdown", help="Markdown 文件路径")
    ap.add_argument("--upload", action="store_true", help="把图片上传到微信正文图床并改写 src")
    ap.add_argument("--reuse", action="store_true",
                    help="复用上次上传成功的 CDN 地址（只补传新图，不重复占素材库）")
    ap.add_argument("--push", action="store_true", help="上传图片并创建草稿（文字原创 / 每日推送）")
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
