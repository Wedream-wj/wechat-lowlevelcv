"""数据对齐契约测试：实验表格零错位 + 人机协作最小改动。

背景：《ICME 2024 | 图像复原模块引导的恶劣天气人群计数与定位》多轮润色暴露
实验对比表格错位（TABLE II 被拆成 13 列、Fig.3 柱状图轴刻度混进表体）。
本文件把两条铁律固化成可回归的断言：

1. pdf_tables() 的词坐标重建——数字锚定列、跨列分组表头折叠、图表残渣截断、
   数字回源校验（表格里每个数字必须能在表注辖域的原始词序列里逐字找回）；
2. 编辑守卫——保存链路的表格行列等宽与渲染往返不变量，错位的表进不了库；
   changed_scope() 让「只改指定位置」变成可校验的 diff 报告。

合成 PDF 全部用 pymupdf 现画现测，离线确定性；真实论文 PDF 缺席时跳过。
"""
import os
import unittest
from pathlib import Path

from app.edit import assert_tables_stable, changed_scope
from app.llm import prompts
from app.render.md_parse import parse_markdown
from app.sources import paper_pdf as pp

REAL_PDF = Path(__file__).resolve().parents[1] / "out" / "uploads" / \
    "20261107-143948_Crowd_Counting_and_Localization_in_Haze_and_Rain.pdf"


def _synth_pdf(caption: str, lines: list[list[tuple[str, float]]]) -> bytes:
    """合成单栏 PDF：一行 caption + 若干行 (text, x) 词，行距 14pt。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=420, height=600)
    page.insert_text((40, 40), caption, fontsize=8)
    y = 62.0
    for row in lines:
        for text, x in row:
            page.insert_text((x, y), text, fontsize=8)
        y += 14.0
    data = doc.tobytes()
    doc.close()
    return data


class TestSyntheticGrid(unittest.TestCase):
    """词坐标重建的合成用例：残渣截断 / 引用合并 / 数字回源。"""

    def _extract(self, caption, lines):
        tables = pp.pdf_tables(_synth_pdf(caption, lines), limit=5)
        self.assertEqual(len(tables), 1, f"应恰好提取 1 张表，实际 {len(tables)}")
        return tables[0]

    def test_chart_residue_is_cut(self):
        """柱状图轴刻度与图例行必须整段截断，绝不混进表体。"""
        rows = [
            [("Methods", 40), ("Year", 100), ("MAE", 140), ("RMSE", 200)],
            [("CSRNet", 40), ("2016", 100), ("79.11", 140), ("123.23", 200)],
            [("Ours", 40), ("2024", 100), ("65.37", 140), ("101.3", 200)],
            # —— 以下全是 Fig 柱状图残渣：纵轴刻度 + 图例 ——
            [("35", 40)],
            [("30", 40)],
            [("25.01", 140), ("23.33", 200)],
            [("CHSNet", 60), ("PET", 120), ("Ours", 180)],
        ]
        table = self._extract(
            "TABLE II RESIDUE CHECK. QUANTITATIVE RESULTS ON TWO CHALLENGING BENCHMARKS.",
            rows)
        self.assertEqual(table["header"], ["Methods", "Year", "MAE", "RMSE"])
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["rows"][0], ["CSRNet", "2016", "79.11", "123.23"])
        self.assertEqual(table["rows"][1], ["Ours", "2024", "65.37", "101.3"])
        joined = str(table["rows"])
        for junk in ("35", "30", "25.01", "23.33", "CHSNet", "PET"):
            self.assertNotIn(f"'{junk}'", joined)

    def test_citation_fuses_into_method_label(self):
        """「KDMG (VGG19) [2]」这类带引用的方法名必须是一格，不许拆列。"""
        rows = [
            [("Methods", 40), ("Year", 130), ("MAE", 170), ("RMSE", 230)],
            [("KDMG (VGG19) [2]", 40), ("2020", 130), ("80.27", 170), ("124.19", 230)],
            [("PET [7]", 40), ("2023", 130), ("71.12", 170), ("112.03", 230)],
            [("Ours", 40), ("2024", 130), ("65.37", 170), ("101.3", 230)],
        ]
        table = self._extract(
            "TABLE I CITATION CHECK. QUANTITATIVE COMPARISON WITH BASELINES AND SOTA.",
            rows)
        self.assertEqual(table["header"], ["Methods", "Year", "MAE", "RMSE"])
        self.assertEqual(table["rows"][0][0], "KDMG (VGG19) [2]")
        self.assertEqual(table["rows"][1][0], "PET [7]")
        self.assertEqual(table["rows"][2], ["Ours", "2024", "65.37", "101.3"])

    def test_all_numbers_trace_back_to_band_text(self):
        """数字回源校验：表格里出现辖域词序列里没有的数字必须被判废。"""
        table = {"header": ["M", "V"], "rows": [["a", "9.99"]]}
        self.assertEqual(pp._unverified_numbers(table, "M 8.88 V"), ["9.99"])
        self.assertEqual(pp._unverified_numbers(table, "M 8.88 9.99 V"), [])

    def test_grid_score_prefers_aligned_grid(self):
        """质量分必须让「满格等宽的干净网格」稳赢「列爆 + 表注入头」的残网格。"""
        clean = (["Methods", "Year", "MAE"],
                 [["CSRNet", "2016", "79.11"], ["Ours", "2024", "65.37"]])
        junk = (["", "", "", "", "DATASET (DH-SHT).", "", "", "", "", "", "", "", ""],
                [["CSRNet", "[1]", "", "", "2016", "79.11", "", "123.23", "", "", "", "", ""]])
        self.assertGreater(pp._grid_score(*clean), pp._grid_score(*junk))


@unittest.skipIf(not REAL_PDF.exists(), "真实论文 PDF 不在本机")
class TestRealPdfAlignment(unittest.TestCase):
    """真实论文集成测试：Crowd Counting in Haze and Rain (ICME 2024)。"""

    @classmethod
    def setUpClass(cls):
        cls.tables = pp.pdf_tables(REAL_PDF.read_bytes(), limit=10)

    def _by_caption(self, prefix: str) -> dict:
        matches = [t for t in self.tables
                   if str(t.get("caption", "")).upper().startswith(prefix)]
        self.assertTrue(matches, f"未提取到 {prefix}")
        return matches[0]

    def test_all_five_tables_extracted(self):
        self.assertEqual(len(self.tables), 5)

    def test_table2_spanner_header_folded(self):
        """TABLE II：跨列分组表头折叠成单行，行列 100% 对应。"""
        t2 = self._by_caption("TABLE II")
        self.assertEqual(t2["header"], ["Methods", "Year", "DH-SHT PartA MAE",
                                        "DH-SHT PartA RMSE", "DH-SHT PartB MAE",
                                        "DH-SHT PartB RMSE"])
        self.assertEqual(t2["rows"][0], ["CSRNet [1]", "2016", "79.11",
                                         "123.23", "21.45", "33.64"])
        self.assertEqual(t2["rows"][-1], ["Ours", "2024", "65.37",
                                          "101.3", "7.72", "13.45"])
        # Fig.3 柱状图的轴刻度与图例绝不允许出现在表体里
        joined = str(t2["rows"])
        for junk in ("GAME", "'35'", "'30'", "'25'", "'20'", "'15'", "'10'",
                     "'21.24'", "'33.45'"):
            self.assertNotIn(junk, joined)

    def test_every_table_is_rectangular_and_verified(self):
        band_cache: dict[int, list] = {}

        def _rect(tables):
            for t in tables:
                width = len(t["header"])
                for row in t["rows"]:
                    self.assertEqual(len(row), width,
                                     f"{t['caption'][:20]} 行列不等宽: {row}")
        _rect(self.tables)
        # 逐张表做数字回源校验（真实几何）
        import pymupdf
        doc = pymupdf.open(stream=REAL_PDF.read_bytes(), filetype="pdf")
        try:
            for pno in range(len(doc)):
                page = doc.load_page(pno)
                for cap in pp._table_captions(page):
                    band = pp._table_band(page, cap)
                    if band:
                        band_cache[(pno, cap[4][:20])] = pp._band_text(page, band)
        finally:
            doc.close()
        for t in self.tables:
            for key, text in band_cache.items():
                if key[1] and str(t["caption"]).startswith(key[1][:12]):
                    self.assertEqual(pp._unverified_numbers(t, text), [],
                                     f"{t['caption'][:20]} 有来历不明的数字")
                    break

    def test_table4_five_columns(self):
        """TABLE IV：Separate*/Separate/Direct/Union 五列各自归位。"""
        t4 = self._by_caption("TABLE IV")
        self.assertEqual(t4["header"], ["Metric", "Separate*", "Separate",
                                        "Direct", "Union"])
        self.assertEqual(t4["rows"][0], ["MAE", "115.28", "82.82", "65.90", "62.96"])


class TestEditGuards(unittest.TestCase):
    """编辑守卫：错位的表进不了库；最小改动可被审计。"""

    BASE = """# 标题

> 副标题

## 01 | 方法

方法段落文字。

| Methods | MAE | RMSE |
|---|---|---|
| Baseline | 10.10 | 20.20 |
| Ours | 9.90 | 18.80 |

## 02 | 结论

结论段落。
"""

    def test_clean_markdown_passes_invariant(self):
        spec, figures = parse_markdown(self.BASE)
        assert_tables_stable(spec, figures)  # 不抛即通过

    def test_ragged_table_is_rejected(self):
        """行内多出一格（未转义的「|」）= 错位特征，必须拒绝保存。"""
        spec, figures = parse_markdown(self.BASE.replace(
            "| Baseline | 10.10 | 20.20 |", "| Baseline | 10.10 | 20.20 | 30.30 |"))
        with self.assertRaises(ValueError) as ctx:
            assert_tables_stable(spec, figures)
        self.assertIn("数据对齐契约", str(ctx.exception))

    def test_changed_scope_reports_minimal_edit(self):
        """只改一段文字时，diff 报告必须显示表格逐字未动。"""
        edited = self.BASE.replace("方法段落文字。", "方法段落文字，补充了动机。")
        report = changed_scope(self.BASE, edited)
        self.assertEqual(report["changed_blocks"], 1)
        self.assertEqual(report["tables_changed"], [])
        self.assertEqual(report["total_tables"], 1)

    def test_changed_scope_flags_table_edits(self):
        edited = self.BASE.replace("| Ours | 9.90 | 18.80 |",
                                   "| Ours | 9.80 | 18.80 |")
        report = changed_scope(self.BASE, edited)
        self.assertEqual(report["tables_changed"], [1])


class TestPromptDiscipline(unittest.TestCase):
    """铁律进 Prompt：行列对齐契约与协作纪律必须能被模型读到。"""

    def test_row_alignment_rule_in_prompts(self):
        import inspect
        src = inspect.getsource(prompts)
        self.assertIn("行列对齐铁律", src)
        self.assertIn("禁止移动、交换、重排任何单元格", src)

    def test_edit_collab_rules_wired_into_retry(self):
        self.assertIn("只改指定位置", prompts.EDIT_COLLAB_RULES)
        self.assertIn("人工意图至上", prompts.EDIT_COLLAB_RULES)
        self.assertIn("表格数据零失真", prompts.EDIT_COLLAB_RULES)
        self.assertIn("人机协作润色纪律", prompts.QUANT_RETRY)


if __name__ == "__main__":
    unittest.main()
