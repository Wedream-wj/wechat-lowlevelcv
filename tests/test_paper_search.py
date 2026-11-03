"""指定论文名称检索与素材提取的离线单测。

全部不联网：检索适配器的匹配/合并逻辑用伪造记录测；JATS 与表格校验用
内联 XML / 构造数据测；PDF 插图区域用 pymupdf 现场生成的最小 PDF 测。
"""
from __future__ import annotations

import unittest

from app.models import Item
from app.sources import paper_pdf as pp
from app.sources import paper_search as ps


def _rec(**kw) -> ps.PaperRecord:
    defaults = dict(title="Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN",
                    venue="Sensors", year="2021", doi="10.3390/s21093251",
                    source="openalex", score=1.0)
    defaults.update(kw)
    return ps.PaperRecord(**defaults)


class TestTitleMatch(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(ps.title_similarity(
            "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN",
            "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN"), 1.0)

    def test_same_paper_different_punctuation(self):
        q = "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN"
        # 副标题丢失 / 破折号差异：仍应高于合并阈值
        self.assertGreaterEqual(ps.title_similarity(q, q.replace("-", " ")), 0.90)

    def test_different_paper_low_score(self):
        q = "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN"
        self.assertLess(ps.title_similarity(q, "TinySR: Diffusion-Based Video Super-Resolution"), 0.4)

    def test_threshold_blocks_partial_overlap(self):
        # 同领域相似标题（另一篇猪只识别论文）：不许冒充目标
        q = "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN"
        other = "Automatic Detection of Group-Housed Pigs Using Multi-Feature Template Matching"
        self.assertLess(ps.title_similarity(q, other), ps.MATCH_THRESHOLD)


class TestMergeAndRank(unittest.TestCase):
    def test_merge_fills_missing_fields(self):
        base = _rec(abstract="", pdf_url="", pmcid="")
        other = _rec(source="crossref", venue="", abstract="foo", pdf_url="https://x/y.pdf",
                     pmcid="PMC8125813", citations=99)
        base.merge(other)
        self.assertEqual(base.venue, "Sensors")          # 不覆盖已有
        self.assertEqual(base.abstract, "foo")
        self.assertEqual(base.pdf_url, "https://x/y.pdf")
        self.assertEqual(base.pmcid, "PMC8125813")
        self.assertEqual(base.citations, 99)

    def test_rank_puts_best_first(self):
        good = _rec(score=0.95)
        bad = _rec(title="Something Else Entirely About Robots", source="openalex", score=0.2)
        ranked = ps._rank_candidates(
            "Automatic Detection and Segmentation for Group-Housed Pigs Based on PigMS R-CNN",
            [bad, good])
        self.assertIs(ranked[0], good)

    def test_merge_records_prefers_openalex(self):
        oa = _rec(source="openalex", venue="Sensors", year="2021")
        cr = _rec(source="crossref", venue="", year="", publisher="MDPI")
        merged = ps._merge_records([cr, oa])
        self.assertEqual(merged.venue, "Sensors")
        self.assertEqual(merged.publisher, "MDPI")

    def test_build_item(self):
        hit = ps.PaperHit(record=_rec(pmcid="PMC8125813", pdf_url="https://x/y.pdf",
                                      citations=38, abstract="Abs"),
                          query="q", traces=[ps.SearchTrace(source="openalex", ok=True, count=8)],
                          code_url="https://github.com/a/b")
        item = ps.build_item(hit)
        self.assertEqual(item.module, "paper")
        self.assertEqual(item.score, 100.0)
        self.assertTrue(item.extra["targeted"])
        self.assertEqual(item.extra["pmcid"], "PMC8125813")
        self.assertEqual(item.code_url, "https://github.com/a/b")


JATS = """<article><front><abstract><p>Instance segmentation of pigs is critical.</p></abstract></front>
<body>
<sec><label>4</label><title>Results</title>
<p>The MS R-CNN framework with traditional NMS obtains an F1 of 0.9228 on 130 images.</p>
<p>By setting the soft-NMS threshold to 0.7, PigMS R-CNN achieves an F1 of 0.9374.</p>
<p>Introduction section background text without numbers worth mentioning here.</p>
</sec></body>
<floats-group>
<table-wrap id="t1"><label>Table 1</label><caption><p>Detection results based on MS R-CNN.</p></caption>
<table><thead><tr><th>Method</th><th>Recall (%)</th><th>F1</th></tr></thead>
<tbody><tr><td>MS R-CNN</td><td>92.76</td><td>0.9228</td></tr>
<tr><td>PigMS R-CNN</td><td>93.37</td><td>0.9374</td></tr></tbody></table></table-wrap>
<table-wrap id="t2"><label>Table 3</label><caption><p>Comparison of IoU thresholds.</p></caption>
<table><thead><tr><th rowspan="2">Method</th><th colspan="3">0</th><th colspan="3">0.5</th></tr>
<tr><th>R</th><th>P</th><th>F1</th><th>R</th><th>P</th><th>F1</th></tr></thead>
<tbody><tr><td>MS R-CNN</td><td>94.68%</td><td>86.33%</td><td>0.9031</td><td>93.27%</td><td>90.19%</td><td>0.9170</td></tr></tbody></table></table-wrap>
<fig id="f1"><label>Figure 2</label><caption><p>Flowchart of the detection and segmentation algorithm based on PigMS R-CNN.</p></caption></fig>
</floats-group></article>"""


class TestJatsAssets(unittest.TestCase):
    def setUp(self):
        self.assets = pp.jats_assets(JATS)

    def test_tables_clean(self):
        tables = self.assets["tables"]
        self.assertEqual(len(tables), 2)
        t1 = tables[0]
        self.assertEqual(t1["header"], ["Method", "Recall (%)", "F1"])
        self.assertIn("0.9374", t1["rows"][1])

    def test_two_row_header_merged(self):
        t3 = self.assets["tables"][1]
        self.assertEqual(t3["header"][0], "Method")
        self.assertIn("R@0", t3["header"])
        self.assertIn("F1@0.5", t3["header"])
        # 数据行从第三行开始：MS R-CNN 的 94.68% 不再被当成表头
        self.assertEqual(t3["rows"][0][0], "MS R-CNN")

    def test_snippets_numeric(self):
        joined = " ".join(self.assets["snippets"])
        self.assertIn("0.9374", joined)
        self.assertIn("0.9228", joined)

    def test_fig_captions(self):
        self.assertIn(2, self.assets["fig_captions"])
        self.assertIn("Flowchart", self.assets["fig_captions"][2])

    def test_full_text_extracts_paragraphs(self):
        self.assertIn("F1 of 0.9374", self.assets["full_text"])

    def test_bad_xml_returns_empty(self):
        self.assertEqual(pp.jats_assets("<not-xml"), {})


class TestPlausibleGrid(unittest.TestCase):
    def test_clean_table_passes(self):
        grid = [["Method", "Recall", "F1"],
                ["MS R-CNN", "92.76", "0.9228"],
                ["PigMS R-CNN", "93.37", "0.9374"]]
        self.assertTrue(pp._plausible_grid(grid))

    def test_garbled_vertical_split_fails(self):
        # 实测事故：PDF find_tables 把「84.09」竖排拆成「8409\n.」
        grid = [["ImageType", "Recall", "F1"],
                ["Frontview", "8409\n.", "08211\n."],
                ["Topview", "9660\n.", "09691\n."]]
        self.assertFalse(pp._plausible_grid(grid))

    def test_all_text_table_fails(self):
        grid = [["A", "B"], ["x y z very long description here", "another long one"], ["c", "d"]]
        self.assertFalse(pp._plausible_grid(grid))


class TestFigureRole(unittest.TestCase):
    def test_flowchart_is_arch(self):
        self.assertEqual(pp._figure_role("Flowchart of the detection and segmentation algorithm based on PigMS R-CNN.", 2), "arch")

    def test_architecture_is_arch(self):
        self.assertEqual(pp._figure_role("The architecture of feature pyramid network.", 3), "arch")

    def test_qualitative_is_result(self):
        self.assertEqual(pp._figure_role("Detection and instance segmentation of the pig objects.", 10), "result")


class TestRegionFigures(unittest.TestCase):
    """用 pymupdf 现场造一页「图 + 图注」的 PDF，验证按图注区域整块渲染。"""

    def test_caption_region_renders_figure(self):
        fitz = pp._fitz()
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.draw_rect(fitz.Rect(80, 200, 500, 320), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        page.insert_text((90, 230), "ResNet-101 + FPN", fontsize=14)
        page.insert_text((80, 345), "Figure 2. Flowchart of the proposed architecture.", fontsize=10)
        data = doc.tobytes()
        doc.close()
        figs = pp._caption_regions(data)
        self.assertTrue(figs)
        fig = next(f for f in figs if f.number == 2)
        self.assertEqual(fig.role, "arch")
        self.assertGreater(fig.width, 300)
        self.assertGreater(fig.height, 60)
        # 渲染出的 PNG 可被 PIL 解码
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(fig.png))
        self.assertEqual(img.format, "PNG")


class TestSciHubGuard(unittest.TestCase):
    def test_disabled_by_default(self):
        import asyncio

        from app.sources.base import HttpClient

        async def main():
            async with HttpClient() as client:
                trace = ps.SearchTrace(source="sci_hub")
                url = await ps._from_sci_hub(client, "10.3390/s21093251", trace)
                return url, trace

        url, trace = asyncio.run(main())
        self.assertEqual(url, "")
        self.assertFalse(trace.ok)
        self.assertIn("禁用", trace.note)


if __name__ == "__main__":
    unittest.main()


class TestCrossTableDiffs(unittest.TestCase):
    """同名指标跨表差值（含中文指标名）放行、跨指标蒙差值拦截。"""

    TABLES = [
        {"header": ["Image Type", "Total", "Recall (%)", "Precision (%)", "F1"],
         "rows": [["Front view", "352", "84.09", "80.22", "0.8211"],
                  ["Top view", "795", "96.60", "97.22", "0.9691"],
                  ["Total", "1147", "92.76", "91.80", "0.9228"]]},
        {"header": ["Image Type", "Total", "Recall (%)", "Precision (%)", "F1"],
         "rows": [["Front view", "352", "83.52", "84.00", "0.8376"],
                  ["Top view", "395", "97.74", "98.60", "0.9817"],
                  ["Total", "1147", "93.37", "94.11", "0.9374"]]},
        # 两行表头合并后的紧凑列名：R@0 / P@0 / F1@0 …
        {"header": ["Method", "R@0", "P@0", "F1@0", "R@0.3", "P@0.3", "F1@0.3"],
         "rows": [["MS R-CNN", "94.68%", "86.33%", "0.9031", "93.55%", "91.59%", "0.9256"],
                  ["PigMS R-CNN", "94.85%", "92.77%", "0.9380", "93.85%", "92.99%", "0.9342"]]},
    ]

    def setUp(self):
        from app.llm import prompts as pr

        self.item = Item(module="paper", title="x", url="u", source="s",
                         extra={"tables": self.TABLES})

    def _check(self, text: str) -> list[str]:
        from app.llm.prompts import unsupported_prose_numbers

        spec = {"sections": [{"type": "h2", "text": "03 | 关键实验结果解读"},
                             {"type": "p", "text": text}]}
        return unsupported_prose_numbers(spec, [self.item])

    def test_cross_table_metric_diff_passes(self):
        # 3.78 = 84.00（本文表）- 80.22（基线表），同名指标跨表
        self.assertEqual(self._check("精确率由 80.22% 升至 84.00%，提高 3.78 个百分点。"), [])

    def test_compact_header_same_column_diff_passes(self):
        # 6.44 = 92.77 - 86.33，IoU 表 P@0 列内跨行差
        self.assertEqual(self._check("精确率从 86.33% 升到 92.77%，提升 6.44 个百分点。"), [])

    def test_all_real_diffs_pass(self):
        text = ("总召回率由 92.76% 升至 93.37%，提高 0.61 个百分点；精确率由 91.80% 升至 "
                "94.11%，提高 2.31 个百分点；F1 由 0.9228 升至 0.9374，提高 0.0146；"
                "俯视图 F1 由 0.9691 升至 0.9817，提高 0.0126；落差 0.1441。")
        self.assertEqual(self._check(text), [])

    def test_fabricated_diff_still_caught(self):
        bad = self._check("精确率提高了 8.88 个百分点、F1 提升 7.77。")
        self.assertIn("8.88", bad)
        self.assertIn("7.77", bad)


class TestMetricAttribution(unittest.TestCase):
    """指标归属的三种语序：句内在前 / 句内在后 / 承前句继承。"""

    TABLES = TestCrossTableDiffs.TABLES

    def _check(self, text: str) -> list[str]:
        from app.llm.prompts import unsupported_prose_numbers

        spec = {"sections": [{"type": "h2", "text": "03 | 关键实验结果解读"},
                             {"type": "p", "text": text}]}
        return unsupported_prose_numbers(spec, [self.item_of()])

    def item_of(self):
        return Item(module="paper", title="x", url="u", source="s",
                    extra={"tables": self.TABLES})

    def test_metric_after_number(self):
        # 「以极小的改动换取 0.0146 的 F1 提升」——指标在数字后面
        self.assertEqual(self._check("它以极小的结构改动换取粘连场景下 0.0146 的 F1 提升。"), [])

    def test_metric_inherited_from_previous_sentence(self):
        # 「前视图 F1 为 0.8376…；俯视图上由 0.9691 升至 0.9817，提升 0.0126」——
        # 第二个分句没有指标名，继承前句 F1
        self.assertEqual(self._check(
            "前视图上，该方法的 F1 为 0.8376，高于基线的 0.8211，提升 0.0165；"
            "俯视图上由 0.9691 升至 0.9817，提升 0.0126。"), [])

    def test_scope_does_not_leak_across_paragraphs(self):
        # 跨指标蒙差值仍然要拦：0.0821 是任何两个素材 F1 值都凑不出的 4 位小数
        bad = self._check("F1 由 0.9228 升至 0.9374。 它确实是编的：0.0821。")
        self.assertIn("0.0821", bad)
