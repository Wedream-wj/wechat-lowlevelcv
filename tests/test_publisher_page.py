"""出版商通道单测：ScienceDirect 图床大图 + 落地页 <table> 结构化提取。

背景：正文页对非订阅 IP 恒 403，但图床 ars.els-cdn.com 可直连，所以这里
重点覆盖「PII 解析 → _lrg 升级 → 挑图顺序」与「验证码页不当真表」两条线。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.sources import paper_pdf as pp
from app.sources import publisher_page as pub
from app.sources.paper_search import Item

PII = "S0031320326021515"
LANDING = f"https://www.sciencedirect.com/science/article/pii/{PII}"


class TestElsevierLink(unittest.TestCase):
    def test_pii_from_article_path(self):
        self.assertEqual(pub.elsevier_pii(LANDING), PII)

    def test_pii_from_query_and_lowercase(self):
        self.assertEqual(
            pub.elsevier_pii("https://linkinghub.elsevier.com/retrieve?pii=s0031320326021515"),
            PII)

    def test_pii_from_pdfft_link(self):
        self.assertEqual(
            pub.elsevier_pii(f"https://www.sciencedirect.com/science/article/pii/{PII}/pdfft"),
            PII)

    def test_non_elsevier_link_has_no_pii(self):
        self.assertEqual(pub.elsevier_pii("https://arxiv.org/abs/2410.01234"), "")

    def test_small_image_upgraded_to_large(self):
        self.assertEqual(
            pub._lrg(f"https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr3.jpg"),
            f"https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr3_lrg.jpg")

    def test_inline_and_sml_variants_upgraded(self):
        for name in (f"1-s2.0-{PII}-gr3.sml", f"1-s2.0-{PII}-gr3__inline.jpg"):
            self.assertTrue(pub._lrg(f"https://ars.els-cdn.com/content/image/{name}")
                            .endswith("-gr3_lrg.jpg"))

    def test_large_image_is_idempotent(self):
        url = f"https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr1_lrg.jpg"
        self.assertEqual(pub._lrg(url), url)

    def test_other_publisher_original_kept(self):
        url = "https://ieeexplore.ieee.org/ielx7/6287639/1/01234567-fig1.png"
        self.assertEqual(pub._lrg(url), url)


class TestNumericCell(unittest.TestCase):
    def test_metric_values_accepted(self):
        for value in ("93.4%", "12.3 ± 0.4", "1,234", "93.4/91.2", "-12.3", "0.9374"):
            self.assertTrue(pub._NUM_LOOSE_RE.match(value), value)

    def test_words_rejected(self):
        for value in ("Method", "Table 4", "Ours (ResNet-50)"):
            self.assertFalse(pub._NUM_LOOSE_RE.match(value), value)


TABLE_HTML = f"""
<html><body>
<table>
  <caption>Table 4. Comparison with state-of-the-art methods.</caption>
  <tr><th rowspan="2">Method</th><th colspan="2">WeaCount-8</th>
      <th colspan="2">WeaCount-16</th></tr>
  <tr><th>MAE</th><th>MSE</th><th>MAE</th><th>MSE</th></tr>
  <tr><td>Ours</td><td>11.2 [43]</td><td>34.5</td><td>10.8</td><td>31.2</td></tr>
  <tr><td>Baseline</td><td>15.4</td><td>45.1</td><td>14.9</td><td>42.7</td></tr>
</table>
<table>
  <tr><td>Navigation</td><td>Home</td></tr>
  <tr><td>About the journal</td>
      <td>This is a long paragraph of editorial boilerplate that no experiment
          table would ever contain, used here to trigger the layout-table filter
          and make sure we never feed site chrome to the model.</td></tr>
</table>
</body></html>
"""


class TestTablesFromHtml(unittest.TestCase):
    def setUp(self):
        self.tables = pub.tables_from_html(TABLE_HTML)

    def test_only_experiment_table_kept(self):
        self.assertEqual(len(self.tables), 1)

    def test_caption_and_two_row_header_merged(self):
        table = self.tables[0]
        self.assertTrue(table["caption"].startswith("Table 4"))
        self.assertEqual(table["header"][0], "Method")
        self.assertEqual(table["header"][1], "MAE(WeaCount-8)")

    def test_rows_are_clean_numbers(self):
        table = self.tables[0]
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["rows"][0], ["Ours", "11.2", "34.5", "10.8", "31.2"])

    def test_no_table_tag_returns_empty(self):
        self.assertEqual(pub.tables_from_html("<html><body>challenge</body></html>"), [])


FIGURE_HTML = f"""
<html><body>
<figure>
  <img srcset="https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr1.jpg 1x,
               https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr1_lrg.jpg 2x"
       src="https://ars.els-cdn.com/content/image/1-s2.0-{PII}-gr1.jpg">
  <figcaption>Fig. 1. Overview of the WeaCount framework.</figcaption>
</figure>
</body></html>
"""


class TestFiguresFromHtml(unittest.TestCase):
    def test_largest_srcset_upgraded_and_role_detected(self):
        figures, captions = pub.figures_from_html(FIGURE_HTML, LANDING)
        self.assertEqual(len(figures), 1)
        url, caption, role = figures[0]
        self.assertTrue(url.endswith("-gr1_lrg.jpg"))
        self.assertEqual(role, "arch")
        self.assertIn("WeaCount", caption)
        self.assertIn(1, captions)


class _Resp:
    def __init__(self, status_code: int = 200, text: str = "", headers=None, url: str = ""):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url


class _Raw:
    """最小 httpx 替身：只用 get（落地页 / DOI 跳转）与 head（图床探测）。"""

    def __init__(self, page: _Resp, heads: dict, redirects: dict | None = None):
        self.page = page
        self.heads = heads
        self.redirects = redirects or {}      # doi.org/{DOI} → 302 Location

    async def get(self, url, headers=None, timeout=None, follow_redirects=None):
        redirect = self.redirects.get(url)
        if redirect is None:
            return self.page
        if not follow_redirects:
            return redirect
        # 跟随重定向：真实 httpx 会把最终的 resp.url 换成落地地址
        return _Resp(200, "", {}, url=str(redirect.headers.get("location") or ""))

    async def head(self, url, timeout=None):
        return self.heads.get(url, _Resp(404, headers={"content-type": "text/xml"}))


class _Client:
    def __init__(self, tmp: Path, *, page: _Resp, heads: dict | None = None, ok: bool = True,
                 redirects: dict | None = None):
        self._client = _Raw(page, heads or {}, redirects)
        self.tmp = tmp
        self.ok = ok
        self.urls: list[tuple[str, str]] = []

    async def download_image(self, url: str, referer: str = "", timeout: float = 15.0) -> str:
        self.urls.append((url, referer))
        if not self.ok:
            return ""
        path = self.tmp / f"fig{len(self.urls)}.jpg"
        path.write_bytes(b"\xff\xd8\xff" + b"0" * 6000)   # 过 5KB 的最小体积校验
        return str(path)


DOI = "10.1016/j.patcog.2026.115188"
DOI_URL = f"https://doi.org/{DOI}"
LINKINGHUB = f"https://linkinghub.elsevier.com/retrieve/pii/{PII}"


class TestResolvePii(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_pii_hit_without_network(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"))
        self.assertEqual(asyncio.run(pub.resolve_pii(client, LANDING)), PII)

    def test_doi_location_header_carries_pii(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"),
                         redirects={DOI_URL: _Resp(302, headers={"location": LINKINGHUB})})
        self.assertEqual(asyncio.run(pub.resolve_pii(client, DOI_URL)), PII)

    def test_doi_followed_redirect_used_as_fallback(self):
        # Location 里没有 PII（例如先去第三方跳板），跟随重定向后的最终 URL 也拿不到 PII
        client = _Client(self.tmp, page=_Resp(403, "challenge"),
                         redirects={DOI_URL: _Resp(302, headers={"location": "https://x.test/go"})})
        self.assertEqual(asyncio.run(pub.resolve_pii(client, DOI_URL)), "")

    def test_non_elsevier_doi_returns_empty(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"),
                         redirects={DOI_URL: _Resp(302, headers={
                             "location": "https://link.springer.com/article/10.1007/s12345"})})
        self.assertEqual(asyncio.run(pub.resolve_pii(client, DOI_URL)), "")

    def test_link_without_doi_returns_empty(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"))
        self.assertEqual(asyncio.run(pub.resolve_pii(client, "https://arxiv.org/abs/2410.01234")), "")

    def test_doi_org_landing_still_reaches_cdn(self):
        heads = {pub._cdn_image(PII, "gr", 1): _Resp(
            200, headers={"content-type": "image/jpeg", "content-length": "469927"})}
        client = _Client(self.tmp, page=_Resp(403, "challenge"), heads=heads,
                         redirects={DOI_URL: _Resp(302, headers={"location": LINKINGHUB})})
        out = asyncio.run(pub.publisher_assets(client, DOI_URL))
        self.assertEqual(len(out["figures"]), 1)
        self.assertTrue(client.urls[0][0].endswith(f"1-s2.0-{PII}-gr1_lrg.jpg"))


class TestPublisherAssets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _heads(self, *numbers: int) -> dict:
        return {pub._cdn_image(PII, "gr", n): _Resp(
            200, headers={"content-type": "image/jpeg", "content-length": "469927"})
            for n in numbers}

    def test_challenge_page_marks_blocked(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"))
        out = asyncio.run(pub.publisher_assets(client, LANDING))
        self.assertTrue(out["blocked"])
        self.assertEqual(out["tables"], [])
        self.assertEqual(out["figures"], [])

    def test_cdn_figures_picked_by_caption_role(self):
        heads = self._heads(1, 2)
        client = _Client(self.tmp, page=_Resp(403, "challenge"), heads=heads)
        out = asyncio.run(pub.publisher_assets(
            client, LANDING, captions={2: "Fig. 2. Overview of the WeaCount framework."}))
        self.assertEqual(len(out["figures"]), 1)
        path, caption, role = out["figures"][0]
        self.assertEqual(role, "arch")
        self.assertIn("Overview", caption)
        self.assertTrue(client.urls[0][0].endswith("-gr2_lrg.jpg"))
        self.assertEqual(client.urls[0][1], LANDING)
        self.assertTrue(Path(path).exists())

    def test_first_figure_used_when_caption_unknown(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"), heads=self._heads(1))
        out = asyncio.run(pub.publisher_assets(client, LANDING))
        self.assertEqual(len(out["figures"]), 1)
        self.assertEqual(out["figures"][0][2], "arch")   # 图是真的，就不编图注

    def test_all_probes_404_yields_no_figure(self):
        client = _Client(self.tmp, page=_Resp(403, "challenge"))
        self.assertEqual(asyncio.run(pub.elsevier_cdn_figures(client, PII)), [])
        self.assertEqual(client.urls, [])

    def test_landing_page_table_used_when_pii_absent(self):
        page = _Resp(200, TABLE_HTML)
        client = _Client(self.tmp, page=page)
        out = asyncio.run(pub.publisher_assets(client, "https://example.org/paper/1"))
        self.assertFalse(out["blocked"])
        self.assertEqual(len(out["tables"]), 1)
        self.assertEqual(out["tables"][0]["header"][0], "Method")


class TestEnrichUsesPublisherChannel(unittest.TestCase):
    """论文主链全空时，「核心创新」节仍要拿到真实的架构图。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "assets").mkdir()
        self.item = Item(module="paper", source="science_direct", url=LANDING,
                         title="WeaCount Spatial-Aware Decoupling Crowd Counting")
        self.item.extra["doi"] = "10.1016/j.patcog.2026.115188"
        self.client = _Client(self.tmp, page=_Resp(403, "challenge"))

    def tearDown(self):
        self._tmp.cleanup()

    def _patches(self, publisher_result: dict, regions=None):
        return [
            patch.object(pp.settings, "out_dir", self.tmp),
            patch.object(pp, "acquire_pdf", AsyncMock(return_value=(b"", []))),
            patch.object(pp, "fetch_jats", AsyncMock(return_value=None)),
            patch.object(pp, "semantic_scholar_pdf", AsyncMock(return_value="")),
            patch.object(pp, "pdf_text", lambda *_a, **_k: ""),
            patch.object(pp, "pdf_tables", lambda *_a, **_k: []),
            patch.object(pp, "_caption_regions", lambda *_a, **_k: regions or []),
            patch.object(pp, "_repo_arch_figure", AsyncMock(return_value=("", "", ""))),
            patch.object(pp, "_arxiv_assets", AsyncMock(return_value={})),
            patch.object(pub, "publisher_assets", AsyncMock(return_value=publisher_result)),
        ]

    def test_arch_figure_from_cdn_reaches_item_images(self):
        arch = ("/tmp/out/assets/paper_gr1.jpg", "Fig. 1 论文原图（出版商图床高清大图）", "arch")
        patches = self._patches(
            {"tables": [], "fig_captions": {1: "Fig. 1 论文原图"}, "figures": [arch],
             "blocked": True})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        summary = asyncio.run(pp.enrich_target(self.client, self.item))
        self.assertEqual(self.item.images, [arch[0]])
        self.assertEqual(self.item.extra["figure_captions"], [arch[1]])
        self.assertEqual(self.item.image_url, arch[0])
        self.assertEqual(summary["figures"], 1)

    def test_publisher_tables_fill_empty_slot(self):
        table = {"caption": "Table 4", "header": ["Method", "MAE"],
                 "rows": [["Ours", "11.2"]]}
        patches = self._patches({"tables": [table], "fig_captions": {}, "figures": [],
                                 "blocked": False})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        asyncio.run(pp.enrich_target(self.client, self.item))
        self.assertEqual(self.item.extra["tables"], [table])

    def test_nothing_found_keeps_images_empty(self):
        patches = self._patches({"tables": [], "fig_captions": {}, "figures": [],
                                 "blocked": True})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        asyncio.run(pp.enrich_target(self.client, self.item))
        self.assertEqual(self.item.images, [])
        self.assertNotIn("tables", self.item.extra)   # 无源就不造表

    def test_other_role_figure_still_used(self):
        region = SimpleNamespace(number=7, caption="Qualitative outputs on the test set.",
                                 role="other", png=b"\x89PNG\r\n")
        patches = self._patches({"tables": [], "fig_captions": {}, "figures": [],
                                 "blocked": True}, regions=[region])
        patches[1] = patch.object(pp, "acquire_pdf",
                                  AsyncMock(return_value=(b"%PDF-1.4", [])))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        asyncio.run(pp.enrich_target(self.client, self.item))
        self.assertEqual(len(self.item.images), 1)
        self.assertIn("fig07.png", self.item.images[0])


if __name__ == "__main__":
    unittest.main()
