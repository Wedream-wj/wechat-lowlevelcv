"""从论文 PDF 里抠「封面级」插图：teaser / 架构图 / 定性对比图。

为什么不只靠 arXiv HTML 版：LaTeXML 只覆盖 2023-12 之后、且作者主动上传的论文，
不少底层视觉论文（尤其会议版）只有 PDF；而封面最需要的 teaser 图恰恰在首页。
所以补一条 PDF 通道。

实现思路刻意保守——不做版面还原，只做一件事：
  1. 找「Figure N:」图注文本块；
  2. 取它正上方那组嵌入图片的 bbox 并集（PDF 里一张 teaser 常由若干子图拼成，
     单张 xref 往往只有半张脸）；
  3. 解出 xref 的原始位图返回。

拿不到就返回空列表。**绝不按「页面大致位置」盲裁一块**：那条路会把正文段落
裁成封面，字糊成一片，比没有封面更糟。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

# 「Figure 1:」「Fig. 3.」「Figure 12 |」——排版工具有各种变体
_CAPTION_RE = re.compile(r"^\s*(?:figure|fig\.?)\s*(\d{1,2})\s*[:：|.\-–—]\s*(.*)", re.I | re.S)
# 图注关键词：决定这张图是「架构」还是「效果」
_ARCH_WORDS = ("overview", "architecture", "architectur", "pipeline", "framework",
               "overall", "network structure", "illustration of our", "our method")
_RESULT_WORDS = ("qualitative", "comparison", "compared", "visual result", "results on",
                 "samples", "visualization", "teaser")
_DATASET_WORDS = ("dataset", "data samples", "example images", "training samples")

# 体检阈值：太小的图放大到封面宽就是马赛克
MIN_W, MIN_H = 380, 150
MAX_ASPECT = 4.5      # 横长条（图例、色带）不适合做封面
MIN_ASPECT = 0.5      # 竖长条同理


@dataclass
class PdfFigure:
    data: bytes
    ext: str
    caption: str
    kind: str = "other"      # teaser | arch | result | dataset | other
    page: int = 0
    width: int = 0
    height: int = 0


def _fitz():
    try:
        import pymupdf as fitz  # PyMuPDF ≥ 2.0 的规范名
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore
    return fitz


def _kind(caption: str, number: int) -> str:
    low = (caption or "").lower()
    if any(w in low for w in _ARCH_WORDS):
        return "arch"
    if any(w in low for w in _RESULT_WORDS):
        return "result"
    if any(w in low for w in _DATASET_WORDS):
        return "dataset"
    # 「Figure 1」在绝大多数学术排版里就是 teaser：把全文主张压在一张图上
    if number == 1:
        return "teaser"
    return "other"


def _captions(page) -> list[tuple[object, str, int]]:
    """页面里的图注块：[(rect, 全文, 图号)]。"""
    out = []
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return out
    for block in blocks:
        if len(block) < 7 or block[6] != 0:      # block_type 0 = 文本
            continue
        text = re.sub(r"\s+", " ", str(block[4] or "")).strip()
        m = _CAPTION_RE.match(text)
        if not m:
            continue
        rect = _fitz().Rect(block[0], block[1], block[2], block[3])
        out.append((rect, text, int(m.group(1))))
    return out


def _images_above(page, caption_rect) -> list[dict]:
    """图注正上方的嵌入图片（gap ≤ 120pt、横向重叠、尺寸够大）。"""
    fitz = _fitz()
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        return []
    picked = []
    for info in infos:
        try:
            rect = fitz.Rect(info["bbox"])
        except Exception:
            continue
        if rect.width < 100 or rect.height < 60:
            continue
        gap = caption_rect.y0 - rect.y1
        if gap < -8 or gap > 120:
            continue
        # 横向必须与图注有交集：双栏排版里另一栏的图不能算进来
        if rect.x1 < caption_rect.x0 - 40 or rect.x0 > caption_rect.x1 + 40:
            continue
        picked.append(info)
    return picked


def _decode(doc, info: dict) -> tuple[bytes, str, int, int]:
    """把 image info 解成 (bytes, ext, w, h)；解不出返回空。"""
    xref = info.get("xref") or 0
    if not xref:
        return b"", "", 0, 0
    try:
        raw = doc.extract_image(xref)
    except Exception:
        return b"", "", 0, 0
    data = raw.get("image") or b""
    return data, str(raw.get("ext") or "png"), int(raw.get("width") or 0), int(raw.get("height") or 0)


def extract(pdf_bytes: bytes, max_pages: int = 8, limit: int = 4) -> list[PdfFigure]:
    """返回候选插图（按 teaser → arch → result → other 排序）。失败返回空列表。"""
    if not pdf_bytes:
        return []
    try:
        doc = _fitz().open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return []
    out: list[PdfFigure] = []
    seen: set[int] = set()
    try:
        pages = min(len(doc), max_pages)
        for pno in range(pages):
            page = doc[pno]
            try:
                page_area = abs(page.rect.width * page.rect.height) or 1
            except Exception:
                continue
            for cap_rect, text, number in _captions(page):
                infos = _images_above(page, cap_rect)
                if not infos:
                    continue
                kind = _kind(text, number)
                for info in infos:
                    xref = int(info.get("xref") or 0)
                    if not xref or xref in seen:
                        continue
                    data, ext, w, h = _decode(doc, info)
                    if not data or w < MIN_W or h < MIN_H:
                        continue
                    try:
                        rect = _fitz().Rect(info["bbox"])
                    except Exception:
                        rect = None
                    # 整页扫描图（封面页、扫描版 PDF）不做封面
                    if rect is not None and abs(rect.width * rect.height) > page_area * 0.8:
                        continue
                    aspect = w / max(h, 1)
                    if aspect > MAX_ASPECT or aspect < MIN_ASPECT:
                        continue
                    seen.add(xref)
                    out.append(PdfFigure(data=data, ext=ext, caption=text[:200],
                                         kind=kind, page=pno, width=w, height=h))
                if len(out) >= limit * 3:
                    break
            if len(out) >= limit * 3:
                break
    finally:
        try:
            doc.close()
        except Exception:
            pass
    # 论文封面优先级：架构图（首选）> 定性效果图（次选）> teaser（第三）。
    # limit 截断发生在排序之后，顺序决定哪些图能进候选池。
    order = {"arch": 0, "result": 1, "teaser": 2, "dataset": 3, "other": 4}
    out.sort(key=lambda f: (order.get(f.kind, 9), f.page))
    return out[:limit]


def to_png(fig: PdfFigure, max_width: int = 1600) -> bytes:
    """统一成 PNG（微信只吃 jpg/png），并按需缩到封面够用的宽度。"""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(fig.data))
        img.load()
    except Exception:
        return b""
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))), Image.LANCZOS)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
