"""PDF 兜底链路单测：候选排序去重 / 403 降级到 OA 副本 / 非 PDF 响应。

背景：`app/sources/paper_pdf.py` 刚把「逐渠道下载」从 enrich_target 抽成
`acquire_pdf()`（返回 `(字节, list[PdfAttempt])`）。这里的重点是三条成稿红线：
1. 直链 403 不能静默降级——必须换 OA 副本并把失败原因写进 attempts；
2. 候选顺序必须稳定（直链 → arXiv → 各元数据源），且同一 URL 只试一次；
3. 200 + text/html 的验证码页不能当 PDF 收下。

全部走假 HTTP 客户端，绝不触网；S2 / Sci-Hub / ScienceDirect 一律离线化。
"""
import asyncio
import contextlib
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.sources import paper_pdf as pp
from app.sources import paper_search as ps
from app.sources.paper_search import Item
from app.spec_tools import paper_asset_gaps

DOI = "10.1016/j.patcog.2026.115188"
PII = "S0031320326021515"
LANDING = f"https://www.sciencedirect.com/science/article/pii/{PII}"
DIRECT = f"{LANDING}/pdfft"
OA_PDF = "https://example.org/repo/weacount.pdf"
OA_JSON = f"https://api.openalex.org/works/doi:{DOI}"

PDF_BODY = b"%PDF-1.4\n" + b"0" * 2048
HTML_BODY = b"<html><body>Are you a robot? captcha challenge</body></html>"


class _Resp:
    """最小 httpx.Response 替身：_fetch_pdf_once 只读 status_code/content/headers。"""

    def __init__(self, status_code: int = 200, content: bytes = b"", headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


class _Raw:
    """httpx.AsyncClient 替身：只实现 get，未预置的 URL 一律 404 text/html。"""

    def __init__(self, owner: "_FakeHttp"):
        self._owner = owner

    async def get(self, url, **kwargs):
        return self._owner.respond(url)


class _FakeHttp:
    """HttpClient 替身：记录每一次请求，按 URL 返回预置响应 / JSON。

    `responses` 精确匹配 URL（下载层），`json_map` 按 URL 子串匹配（元数据层），
    两者都没配就返回 404 / {}，这样「某个源落空」也能被测到。
    """

    def __init__(self, responses: dict | None = None, json_map: dict | None = None):
        self._sem = asyncio.Semaphore(1)
        self._client = _Raw(self)
        self.responses = responses or {}
        self.json_map = json_map or {}
        self.requests: list[tuple[str, str]] = []      # (层, url)，get/json/text

    # ---------------- httpx 层 ----------------
    def respond(self, url: str) -> _Resp:
        self.requests.append(("get", url))
        return self.responses.get(url, _Resp(404, b"", {"content-type": "text/html"}))

    # ---------------- HttpClient 层 ----------------
    async def json(self, url: str, params=None, **kwargs):
        self.requests.append(("json", url))
        for needle, payload in self.json_map.items():
            if needle in url:
                return copy.deepcopy(payload)
        return {}

    async def text(self, url: str, **kwargs) -> str:
        self.requests.append(("text", url))
        return self.responses.get(url, _Resp(404, b"", {})).text

    def urls(self, layer: str | None = None) -> list[str]:
        return [u for m, u in self.requests if layer is None or m == layer]


def _quiet(*_args, **_kwargs) -> None:
    """log 替身：acquire_pdf / resolve_pdf_urls 的日志不污染测试输出。"""


@contextlib.contextmanager
def _offline(out_dir: Path):
    """把落盘目录与所有「可能真发请求 / 依赖本机凭据」的渠道离线化。"""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(pp.settings, "out_dir", out_dir))
        stack.enter_context(patch.dict(os.environ, {
            "SCIENCEDIRECT_COOKIE": "",
            "ELSEVIER_COOKIE": "",
            "UNPAYWALL_EMAIL": "",
        }))
        # S2 匿名限流会 4 次 × 5s 重试；Sci-Hub 由 paper_search 侧开关控制
        stack.enter_context(patch.object(
            pp, "semantic_scholar_pdf", AsyncMock(return_value="")))
        stack.enter_context(patch.object(
            ps, "scihub_pdf_url", AsyncMock(return_value=""), create=True))
        yield


class TestAcquirePdf(unittest.IsolatedAsyncioTestCase):
    """acquire_pdf：直链失败 → 换渠道 → 落盘 + 逐渠道尝试记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "assets").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _item(self, **extra) -> Item:
        item = Item(module="paper", source="science_direct", url=LANDING,
                    title="WeaCount Spatial-Aware Decoupling Crowd Counting")
        item.extra.update({"doi": DOI, **extra})
        return item

    async def test_openalex_copy_used_when_direct_link_is_403(self):
        client = _FakeHttp(
            responses={
                DIRECT: _Resp(403, HTML_BODY, {"content-type": "text/html"}),
                OA_PDF: _Resp(200, PDF_BODY, {"content-type": "application/pdf"}),
            },
            json_map={OA_JSON: {"open_access": {"oa_url": OA_PDF}}})
        item = self._item(pdf_url=DIRECT)

        with _offline(self.tmp):
            body, attempts = await pp.acquire_pdf(client, item, log=_quiet)

        self.assertTrue(body.startswith(b"%PDF-"))
        self.assertEqual(item.extra["pdf_source"], "OpenAlex")
        self.assertEqual(item.extra["pdf_failure"], "")

        saved = list((self.tmp / "pdfs").glob("*.pdf"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_bytes(), body)

        first = attempts[0]
        self.assertEqual((first.channel, first.url), ("直链", DIRECT))
        self.assertIn("HTTP 403", first.detail)
        self.assertFalse(first.ok)
        winners = [a for a in attempts if a.ok]
        self.assertEqual([a.channel for a in winners], ["OpenAlex"])

    async def test_direct_link_first_and_deduped(self):
        client = _FakeHttp()                       # 所有 JSON 源落空，只剩本地候选
        item = self._item(pdf_url=DIRECT,
                          pdf_urls=[DIRECT, f" {DIRECT} ", "ftp://ignored/x.pdf"])

        with _offline(self.tmp):
            pairs = await pp.resolve_pdf_urls(client, item, log=_quiet)

        urls = [u for _c, u in pairs]
        self.assertEqual(pairs[0], ("直链", DIRECT))
        self.assertEqual(urls, [DIRECT])           # 去重 + 非 http 候选丢弃
        self.assertEqual(client.urls("get"), [])   # 解析阶段不下载

    async def test_arxiv_id_candidate_before_openalex(self):
        client = _FakeHttp(json_map={OA_JSON: {"open_access": {"oa_url": OA_PDF}}})
        item = self._item(pdf_url=DIRECT, arxiv_id="2410.01234")

        with _offline(self.tmp):
            pairs = await pp.resolve_pdf_urls(client, item, log=_quiet)

        channels = [c for c, _u in pairs]
        self.assertEqual(channels[:2], ["直链", "arXiv"])
        self.assertLess(channels.index("arXiv"), channels.index("OpenAlex"))
        self.assertIn("https://arxiv.org/pdf/2410.01234", [u for _c, u in pairs])

    async def test_arxiv_abs_url_candidate_before_openalex(self):
        client = _FakeHttp(json_map={OA_JSON: {"open_access": {"oa_url": OA_PDF}}})
        item = self._item()
        item.url = "https://arxiv.org/abs/2410.01234"

        with _offline(self.tmp):
            pairs = await pp.resolve_pdf_urls(client, item, log=_quiet)

        channels = [c for c, _u in pairs]
        self.assertEqual(channels[0], "arXiv")     # 无直链时 arXiv 排第一
        self.assertLess(channels.index("arXiv"), channels.index("OpenAlex"))

    async def test_scihub_disabled_by_default_makes_no_request(self):
        from app.sources.paper_search import scihub_pdf_url

        if ps._scihub_enabled():
            self.skipTest("配置已显式启用 Sci-Hub，默认禁用语义不适用")
        client = _FakeHttp()

        self.assertEqual(await scihub_pdf_url(client, "10.1016/j.patcog.2026.115188"), "")
        self.assertEqual(client.requests, [])

    async def test_html_200_is_not_pdf(self):
        client = _FakeHttp(responses={"https://example.org/landing": _Resp(
            200, HTML_BODY, {"content-type": "text/html; charset=utf-8"})})

        body, detail = await pp._fetch_pdf_once(client, "https://example.org/landing")

        self.assertEqual(body, b"")
        self.assertIn("非 PDF", detail)
        self.assertIn("text/html", detail)


class TestRankTables(unittest.TestCase):
    def test_result_table_ranked_first(self):
        tables = [
            {"caption": "Table 1 Dataset statistics of the three benchmarks.",
             "header": ["Split", "Images"], "rows": [["train", "1000"]]},
            {"caption": "Table 4 Comparison with state-of-the-art methods",
             "header": ["Method", "MAE"], "rows": [["Ours", "11.2"]]},
        ]
        ranked = pp._rank_tables(tables)
        self.assertIn("Table 4", ranked[0]["caption"])
        self.assertIn("Table 1", ranked[1]["caption"])


FIGURES = {
    "F1": {"path": "assets/a.jpg", "url": "https://example.org/a.jpg",
           "caption": "图 1：WeaCount 总体架构。", "title": "WeaCount",
           "item_id": "paper:abc"},
}
TABLE = {"header": ["Method", "MAE"], "rows": [["Ours", "11.2"]]}
EMPTY_TABLE = {"header": ["Method", "MAE"], "rows": []}


def _paper_spec(*, figure_ref=None, table=None) -> dict:
    """最小 paper spec：核心创新节 + 实验结果节，按需塞 figure / table 块。"""
    creative: list = [{"type": "p", "text": "解耦计数与定位两个子任务。"}]
    if figure_ref is not None:
        creative.append({"type": "figure", "ref": figure_ref,
                         "caption": "图 1：WeaCount 总体架构。"})
    results: list = [{"type": "p", "text": "在三个基准上均优于现有方法。"}]
    if table is not None:
        results.append({"type": "table", **table})
    return {"title": "WeaCount", "sections": [
        {"type": "h2", "text": "02 | 核心创新与方法拆解"}, *creative,
        {"type": "h2", "text": "03 | 关键实验结果解读"}, *results,
    ]}


class TestPaperAssetGaps(unittest.TestCase):
    """成稿红线四象限：结构图 / 对比表 各自「有 / 缺」。"""

    def test_complete_spec_has_no_gaps(self):
        gaps = paper_asset_gaps(_paper_spec(figure_ref="F1", table=TABLE), FIGURES)
        self.assertFalse(gaps["figure"])
        self.assertFalse(gaps["table"])

    def test_missing_figure_block_flags_figure(self):
        gaps = paper_asset_gaps(_paper_spec(table=TABLE), FIGURES)
        self.assertTrue(gaps["figure"])
        self.assertFalse(gaps["table"])

    def test_table_without_rows_flags_table(self):
        gaps = paper_asset_gaps(_paper_spec(figure_ref="F1", table=EMPTY_TABLE), FIGURES)
        self.assertFalse(gaps["figure"])
        self.assertTrue(gaps["table"])

    def test_missing_both_flags_both(self):
        gaps = paper_asset_gaps(_paper_spec(), FIGURES)
        self.assertTrue(gaps["figure"])
        self.assertTrue(gaps["table"])


if __name__ == "__main__":
    unittest.main()
