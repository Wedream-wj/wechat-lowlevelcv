"""实验表格「本文方法零留白」测试：解析不漏列、成稿不留白、回填不串行。

事故原型：《CVPR 2025 | SFD》的「关键实验结果解读」表格里本文方法一行写成
`+Ours | — | — | — | —`。根因在 HTML 抽表：LaTeXML 的对比表用 rowspan/colspan
表达分组表头，旧实现按 <tr> 直接取 <td>，被占位的格全丢，整张表向左挤，
右侧方法列（含 +Ours）变成空列；模型照抄列名，补出一行破折号。

本文件把四条线固化：
1. HTML 网格展开（rowspan/colspan + 多行表头折叠）——方法列数值不再丢；
2. 解析侧自检——表头有 Ours、整列没数的错位表不许进素材；
3. 成稿侧断言——Ours 行/列留白必须回填，回填只能取同一方法的原文数值；
4. 保存侧闸门——人工贴进来的残缺表拒绝保存，不静默放行。
"""
import sqlite3
import unittest
from pathlib import Path

from app import tables_guard as g
from app.edit import render_edit
from app.llm import prompts
from app.models import Article, Item
from app.sources.arxiv import extract_tables
from app.spec_tools import ensure_paper_result_table

DB = Path(__file__).resolve().parents[1] / "data" / "llcv.db"


def _item(title="CVPR 2025 | SFD：用语义特征判别重塑感知超光", tables=None):
    return Item(module="paper", title=title, url="https://arxiv.org/abs/2503.19295",
                source="arXiv", extra={"tables": tables or []})


# 与 SFD Table 1 同构：两行 thead + rowspan 基准 + 两个 Ours 变体
SFD_HTML = """
<html><body>
<figure class="ltx_table">
<figcaption>Table 1: Quantitative comparison of our method vs. other SOTA methods.</figcaption>
<table class="ltx_tabular">
<thead>
<tr><th rowspan="2">Benchmark</th><th rowspan="2">Metric</th>
<th>RankSR</th><th>SRGAN</th><th>RRDB</th><th>SwinIR</th></tr>
<tr><th>GAN [79]</th><th>[29]</th><th>+Ours</th><th>+Ours</th></tr>
</thead>
<tbody>
<tr><td rowspan="2">Set5</td><td>PSNR ↑</td><td>29.65</td><td>30.52</td><td>31.63</td><td>31.78</td></tr>
<tr><td>LPIPS ↓</td><td>0.070</td><td>0.069</td><td>0.063</td><td>0.059</td></tr>
<tr><td rowspan="2">Urban100</td><td>PSNR ↑</td><td>24.47</td><td>25.02</td><td>26.18</td><td>26.57</td></tr>
<tr><td>LPIPS ↓</td><td>0.138</td><td>0.130</td><td>0.109</td><td>0.099</td></tr>
</tbody></table></figure></body></html>
"""

# 同一张表解析错位后的形态：表头还留着 +Ours，右半边数值全空
BROKEN_MATERIAL = {
    "caption": "Table 1: Quantitative comparison of our method vs. other SOTA methods.",
    "header": ["Benchmark", "Metric", "RankSR", "", "", "", "+Ours", "", "+Ours"],
    "rows": [
        ["Set5", "PSNR ↑", "29.65", "", "", "", "", "", ""],
        ["SSIM ↑", "0.838", "0.863", "", "", "", "", "", ""],
        ["LPIPS ↓", "0.070", "0.069", "", "", "", "", "", ""],
    ],
}

BROKEN_BLOCK = {
    "type": "table",
    "header": ["方法", "Set5 PSNR↑", "Set5 LPIPS↓", "Urban100 PSNR↑", "Urban100 LPIPS↓"],
    "rows": [["RankSR", "29.65", "0.069", "24.47", "0.130"], ["+Ours", "—", "—", "—", "—"]],
}


class TestHtmlGrid(unittest.TestCase):
    """HTML 表格必须按 rowspan/colspan 展开后再折叠表头。"""

    def setUp(self):
        self.tables = extract_tables(SFD_HTML, limit=3, max_cols=14)

    def test_rowspan_and_two_row_header(self):
        self.assertEqual(len(self.tables), 1, "应抽出唯一一张对比表")
        table = self.tables[0]
        self.assertIn("Benchmark Metric", table["header"][0])
        self.assertEqual(table["header"][1:], ["RankSR GAN", "SRGAN", "RRDB +Ours", "SwinIR +Ours"])
        self.assertEqual(table["rows"][0], ["Set5 PSNR ↑", "29.65", "30.52", "31.63", "31.78"])
        self.assertEqual(table["rows"][3], ["Urban100 LPIPS ↓", "0.138", "0.130", "0.109", "0.099"])

    def test_ours_column_has_values(self):
        table = self.tables[0]
        ours = [j for j, h in enumerate(table["header"]) if "Ours" in h]
        self.assertTrue(ours, "表头必须认得出 Ours 列")
        for j in ours:
            self.assertTrue(all(r[j].strip() for r in table["rows"]),
                            f"Ours 列 {j} 出现空值——解析又左移了")

    def test_blank_ours_column_is_dropped(self):
        """表头有 Ours、整列没数的错位表不许进素材（喂给模型就会被照抄）。"""
        html = SFD_HTML.replace('<td>31.63</td>', "<td></td>").replace(
            '<td>0.063</td>', "<td></td>")
        tables = extract_tables(html, limit=3, max_cols=14)
        for t in tables:
            gap = g.ours_gap(t["header"], t["rows"])
            self.assertFalse(gap and gap["kind"] == "blank_col",
                             "错位表（Ours 整列空）不应出现在素材里")


class TestGapDetection(unittest.TestCase):
    def test_blank_row(self):
        gap = g.ours_gap(BROKEN_BLOCK["header"], BROKEN_BLOCK["rows"])
        self.assertEqual(gap["kind"], "blank_row")

    def test_blank_col(self):
        header = ["Method", "PSNR", "SSIM", "SFD (Ours)"]
        rows = [["A", "30.1", "0.90", ""], ["B", "30.5", "0.91", ""]]
        self.assertEqual(g.ours_gap(header, rows)["kind"], "blank_col")

    def test_missing_when_required(self):
        header = ["方法", "PSNR", "SSIM"]
        rows = [["A", "30.1", "0.90"], ["B", "30.5", "0.91"]]
        self.assertIsNone(g.ours_gap(header, rows))
        self.assertEqual(g.ours_gap(header, rows, require=True)["kind"], "missing")

    def test_placeholder_tokens_are_blank(self):
        for token in ("—", "-", "/", "N/A", "  ", "？"):
            self.assertTrue(g.is_blank(token), f"{token!r} 应判空")
        self.assertFalse(g.is_blank("0.070"))
        self.assertFalse(g.is_blank("92.76%"))


class TestRepair(unittest.TestCase):
    """回填必须逐格取自同一方法的原文数值，绝不跨方法借用。"""

    def setUp(self):
        self.material = extract_tables(SFD_HTML, limit=3, max_cols=14)
        self.item = _item(tables=self.material)

    def test_fill_ours_row(self):
        new = g.repair_block(BROKEN_BLOCK, self.material, self.item)
        self.assertIsNotNone(new)
        rows = {r[0]: r[1:] for r in new["rows"]}
        self.assertIn("RRDB +Ours", rows)
        self.assertEqual(rows["RRDB +Ours"], ["31.63", "0.063", "26.18", "0.109"])

    def test_never_copies_competitor_row(self):
        """回归：早期兜底会拿「指标唯一值」跨方法填，把 RankSR 的数字塞进 Ours。"""
        new = g.repair_block(BROKEN_BLOCK, self.material, self.item)
        ours = next(r for r in new["rows"] if "Ours" in r[0])
        rank = next(r for r in new["rows"] if r[0] == "RankSR")
        self.assertNotEqual(ours[1:], rank[1:], "Ours 行不能与基线行同值")

    def test_competitor_values_come_from_source(self):
        """连基线的错值也一并纠正：SFD 表里 RankSR 的 LPIPS 是 0.070 而非 0.069。"""
        new = g.repair_block(BROKEN_BLOCK, self.material, self.item)
        rank = next(r for r in new["rows"] if r[0] == "RankSR")
        self.assertEqual(rank[1:], ["29.65", "0.070", "24.47", "0.138"])

    def test_broken_material_cannot_fill(self):
        """素材本身就是错位表时，宁可修不好也不能拿错值硬填。"""
        self.assertIsNone(g.repair_block(BROKEN_BLOCK, [BROKEN_MATERIAL], self.item))

    def test_spec_repair_replaces_block(self):
        spec = {"sections": [{"type": "h2", "text": "03 | 关键实验结果解读"},
                             dict(BROKEN_BLOCK)]}
        changed = g.repair_spec_tables(spec, self.item)
        self.assertEqual(changed, 1)
        table = next(b for b in spec["sections"] if b.get("type") == "table")
        self.assertTrue(all(not g.is_blank(c) for c in table["rows"][-1][1:]))

    def test_spec_repair_drops_unfixable(self):
        spec = {"sections": [dict(BROKEN_BLOCK)]}
        item = _item(tables=[BROKEN_MATERIAL])
        self.assertEqual(g.repair_spec_tables(spec, item), 1)
        self.assertFalse(any(b.get("type") == "table" for b in spec["sections"]),
                         "无法回填时必须撤表")
        note = spec["sections"][0]
        self.assertEqual(note["type"], "p")
        self.assertIn("原文", note["text"])

    def test_markdown_repair_keeps_rest_byte_identical(self):
        md = ("# 标题\n\n段落\n\n"
              "| 方法 | Set5 PSNR↑ | Set5 LPIPS↓ | Urban100 PSNR↑ | Urban100 LPIPS↓ |\n"
              "|---|---|---|---|---|\n"
              "| RankSR | 29.65 | 0.069 | 24.47 | 0.130 |\n"
              "| +Ours | — | — | — | — |\n\n结尾\n")
        new, changed = g.repair_markdown_tables(md, self.item)
        self.assertEqual(changed, 1)
        self.assertIn("| RRDB +Ours | 31.63 | 0.063 | 26.18 | 0.109 |", new)
        self.assertTrue(new.startswith("# 标题\n\n段落\n\n"))
        self.assertTrue(new.endswith("\n\n结尾\n"))

    def test_markdown_repair_can_keep_broken_for_gate(self):
        md = ("| 方法 | PSNR |\n|---|---|\n| +Ours | — |\n")
        new, changed = g.repair_markdown_tables(
            md, _item(tables=[BROKEN_MATERIAL]), drop_unfixable=False)
        self.assertEqual(changed, 1)
        self.assertIn("| +Ours | — |", new, "编辑器链路应保留原表交给闸门报错")


class TestSaveGate(unittest.TestCase):
    """保存侧：人工贴进来的残缺表必须拒绝，不能静默入库。"""

    def test_render_edit_rejects_blank_ours(self):
        article = Article(module="paper", title="t", issue_id="x",
                          items=[_item(tables=[BROKEN_MATERIAL])])
        md = ("# 标题\n\n"
              "| 方法 | PSNR |\n|---|---|\n| RankSR | 29.65 |\n| +Ours | — |\n")
        with self.assertRaises(ValueError) as ctx:
            render_edit(article, md)
        self.assertIn("Ours", str(ctx.exception))

    def test_render_edit_repairs_when_material_available(self):
        article = Article(module="paper", title="t", issue_id="x",
                          items=[_item(tables=extract_tables(SFD_HTML, limit=3, max_cols=14))])
        md = ("# 标题\n\n"
              "| 方法 | Set5 PSNR↑ | Set5 LPIPS↓ | Urban100 PSNR↑ | Urban100 LPIPS↓ |\n"
              "|---|---|---|---|---|\n"
              "| RankSR | 29.65 | 0.069 | 24.47 | 0.130 |\n"
              "| +Ours | — | — | — | — |\n")
        result = render_edit(article, md)
        self.assertIn("RRDB +Ours", result.markdown)
        self.assertNotIn("| +Ours | — |", result.markdown)


class TestResultTableFallback(unittest.TestCase):
    """模型漏写表格时的兜底：错位表不许补进正文。"""

    def test_refuses_broken_table(self):
        spec = {"sections": [{"type": "h2", "text": "03 | 关键实验结果解读"},
                             {"type": "p", "text": "结果"}]}
        item = _item(tables=[BROKEN_MATERIAL])
        self.assertEqual(ensure_paper_result_table(spec, item), 0)
        self.assertFalse(any(b.get("type") == "table" for b in spec["sections"]))

    def test_adds_good_table(self):
        spec = {"sections": [{"type": "h2", "text": "03 | 关键实验结果解读"},
                             {"type": "p", "text": "结果"}]}
        item = _item(tables=extract_tables(SFD_HTML, limit=3, max_cols=14))
        self.assertEqual(ensure_paper_result_table(spec, item), 1)
        table = next(b for b in spec["sections"] if b.get("type") == "table")
        self.assertTrue(table["rows"])


class TestPromptRule(unittest.TestCase):
    def test_ours_zero_blank_rule_present(self):
        text = prompts.SYSTEM + prompts.SPEC_GUIDE + prompts.PAPER_PROMPT
        self.assertIn("零留白", text)
        self.assertIn("Ours", text)
        self.assertTrue(any("—" in line and "Ours" in line
                            for line in text.splitlines()), "必须点名破折号占位")


@unittest.skipUnless(DB.exists(), "历史库不在，跳过")
class TestHistory(unittest.TestCase):
    """历史出稿不变量：任何一期论文稿的表格都不许有 Ours 留白。"""

    def test_no_blank_ours_rows(self):
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select issue_id, title, markdown from issues where module='paper'").fetchall()
        conn.close()
        from app.render.md_parse import parse_markdown
        from app.spec_tools import paper_meta_values  # noqa: F401  (确保 spec_tools 可导入)
        for row in rows:
            spec, _ = parse_markdown(row["markdown"] or "\n")
            for table in spec.get("sections") or []:
                if not isinstance(table, dict) or table.get("type") != "table":
                    continue
                gap = g.ours_gap(table.get("header") or [], table.get("rows") or [])
                self.assertIsNone(gap, f"{row['issue_id']} 仍有 Ours 留白：{table.get('rows')}")


if __name__ == "__main__":
    unittest.main()
