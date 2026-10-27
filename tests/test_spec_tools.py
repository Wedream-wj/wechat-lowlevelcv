"""章节编号连续性与移动端表格压缩的回归测试。

两条都是「看起来只是排版、实际会毁掉阅读体验」的问题，必须由代码兜住：
- 单篇论文素材时模型省略「05 | 总结」却保留「06 | 写在最后」，正文出现 04 → 06 断层；
- 论文原始实验表常有 8–9 列，手机端必须横向滚动，读者看不到右侧的关键指标。
"""
import unittest

from app.llm.prompts import _item_payload
from app.models import Item
from app.render import wechat
from app.render.markdown import spec_to_markdown
from app.spec_tools import (
    MAX_TABLE_COLS,
    compact_table,
    merge_event_rows,
    normalize_meta_cards,
    normalize_section_titles,
    normalized_spec,
    prune_spec_tables,
    renumber_sections,
)


def _spec(*heads: str) -> dict:
    sections = []
    for head in heads:
        sections.append({"type": "h2", "text": head})
        sections.append({"type": "p", "text": "正文"})
    return {"title": "T", "lead": ["导语"], "sections": sections, "sources": []}


class TestSectionNumbering(unittest.TestCase):
    def test_gap_is_closed(self):
        spec = _spec("01 | 背景", "02 | 方法", "03 | 实验", "04 | 对比", "06 | 写在最后")
        self.assertTrue(renumber_sections(spec))
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads, ["01 | 背景", "02 | 方法", "03 | 实验", "04 | 对比", "05 | 写在最后"])

    def test_duplicate_numbers_are_fixed(self):
        spec = _spec("01 | 背景", "01 | 方法", "03 | 实验")
        renumber_sections(spec)
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads, ["01 | 背景", "02 | 方法", "03 | 实验"])

    def test_full_width_bar_and_single_digit(self):
        spec = _spec("1｜背景", "2 | 方法")
        renumber_sections(spec)
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads, ["01 | 背景", "02 | 方法"])

    def test_unnumbered_spec_untouched(self):
        spec = _spec("研究背景", "方法")
        self.assertFalse(renumber_sections(spec))
        self.assertEqual(spec["sections"][0]["text"], "研究背景")

    def test_normalized_spec_does_not_mutate_input(self):
        spec = _spec("01 | 背景", "03 | 写在最后")
        clone = normalized_spec(spec)
        self.assertEqual(spec["sections"][2]["text"], "03 | 写在最后")
        self.assertEqual(clone["sections"][2]["text"], "02 | 写在最后")

    def test_render_html_has_no_gap(self):
        spec = _spec("01 | 背景", "02 | 方法", "03 | 实验", "04 | 对比", "06 | 写在最后")
        html = wechat.render_body(spec, {}, "paper", "2026.10.27")
        for num in ("01", "02", "03", "04", "05"):
            self.assertIn(f"SECTION {num}", html)
        self.assertNotIn("SECTION 06", html)

    def test_markdown_matches_html(self):
        spec = _spec("01 | 背景", "02 | 方法", "04 | 写在最后")
        md = spec_to_markdown(spec, {})
        self.assertIn("## 03 | 写在最后", md)
        self.assertNotIn("## 04", md)


class TestSectionTitles(unittest.TestCase):
    """收尾节统一叫「写在最后」：旧稿/模型偶尔写成「冷静一下」，渲染前必须收敛。"""

    def test_legacy_title_is_renamed_and_number_kept(self):
        spec = _spec("01 | 发生了什么", "02 | 技术变化", "03 | 意味着什么", "04 | 冷静一下")
        self.assertEqual(normalize_section_titles(spec), 1)
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads[-1], "04 | 写在最后")

    def test_subtitle_suffix_is_dropped(self):
        spec = _spec("01 | 发生了什么", "02 | 冷静一下：信息边界与话术打折")
        normalize_section_titles(spec)
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads[-1], "02 | 写在最后")

    def test_other_aliases_and_official_name_untouched(self):
        spec = _spec("01 | 背景", "02 | 结语")
        normalize_section_titles(spec)
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads, ["01 | 背景", "02 | 写在最后"])

        spec2 = _spec("01 | 背景", "02 | 写在最后", "03 | 尾声")
        self.assertEqual(normalize_section_titles(spec2), 0)  # 已有正式名，不再造重复标题
        self.assertEqual(spec2["sections"][4]["text"], "03 | 尾声")

    def test_unnumbered_section_untouched(self):
        spec = _spec("发生了什么", "冷静一下")
        self.assertEqual(normalize_section_titles(spec), 0)
        self.assertEqual(spec["sections"][2]["text"], "冷静一下")

    def test_render_body_shows_official_name(self):
        spec = _spec("01 | 发生了什么", "02 | 冷静一下")
        html = wechat.render_body(spec, {}, "industry", "2026.10.27")
        self.assertIn("写在最后", html)
        self.assertNotIn("冷静一下", html)
        md = spec_to_markdown(spec, {})
        self.assertIn("## 02 | 写在最后", md)
        self.assertNotIn("冷静一下", md)


class TestIndustryCardRows(unittest.TestCase):
    """产业事件卡只留「谁干了什么」：主体+事件并成一行，规模/金额不再单列。"""

    ROWS = [["主体", "Runway"], ["事件", "发布开放权重世界动作模型 Praxis-1"],
            ["规模或金额", "0.95 相关性"], ["时间", "2026 年 10 月"],
            ["影响面", "机器人策略训练与评测"]]

    def test_subject_and_event_merged_into_one_row(self):
        rows, changed = merge_event_rows([list(r) for r in self.ROWS])
        self.assertTrue(changed)
        self.assertEqual(rows[0], ["事件", "Runway 发布开放权重世界动作模型 Praxis-1（0.95 相关性）"])
        labels = [r[0] for r in rows]
        self.assertNotIn("主体", labels)
        self.assertNotIn("规模或金额", labels)
        self.assertEqual(labels, ["事件", "时间", "影响面"])

    def test_amount_folded_into_event_instead_of_dropped(self):
        text = merge_event_rows([["主体", "Runway"], ["事件", "开源 Praxis-1"],
                                 ["金额", "未公布"]])[0][0][1]
        self.assertIn("未公布", text)

    def test_ascii_subject_gets_space(self):
        rows, _ = merge_event_rows([["主体", "MiniMax"], ["事件", "绑定中国电信"]])
        self.assertEqual(rows[0][1], "MiniMax 绑定中国电信")

    def test_already_merged_rows_are_untouched(self):
        rows = [["事件", "Runway 开源 Praxis-1"], ["时间", "2026-10"]]
        self.assertEqual(merge_event_rows([list(r) for r in rows]), (rows, False))

    def test_project_card_not_merged(self):
        spec = {"title": "T", "lead": ["x"], "sections": [
            {"type": "meta_card", "label": "PROJECT INFO",
             "rows": [["项目", "RP1"], ["仓库", "github.com/a/b"], ["规模", "—"]]},
        ], "sources": []}
        self.assertEqual(normalize_meta_cards(spec, "industry"), 0)
        self.assertEqual(len(spec["sections"][0]["rows"]), 3)

    def test_render_body_merges_industry_card_only(self):
        spec = {"title": "T", "lead": ["x"], "sections": [
            {"type": "meta_card", "rows": [list(r) for r in self.ROWS]},
        ], "sources": []}
        html = wechat.render_body(spec, {}, "industry", "2026.10.27")
        self.assertIn("Runway 发布开放权重世界动作模型 Praxis-1", html)
        self.assertNotIn("规模或金额", html)
        self.assertNotIn(">主体<", html)
        # 论文模块的卡片不合并不裁剪
        paper_spec = {"title": "T", "lead": ["x"], "sections": [
            {"type": "meta_card", "rows": [["论文标题", "X"], ["期刊/会议", "CVPR 2026"]]},
        ], "sources": []}
        paper_html = wechat.render_body(paper_spec, {}, "paper", "2026.10.27")
        self.assertIn("论文标题", paper_html)


SVI_TABLE = {
    "header": ["Method", "PSNR", "WS-PSNR", "WS-SSIM", "EPE", "SEPE", "AE",
               "Params (M)", "Latency (ms/f)"],
    "rows": [
        ["SuperSloMo", "33.11", "32.48", "0.9045", "15.79", "23.74", "29.76", "19.8", "62"],
        ["FILM", "35.98", "35.84", "0.9282", "14.65", "20.26", "23.19", "34.4", "393"],
        ["SVI360 (Ours)", "38.68", "37.73", "0.9647", "12.36", "15.22", "19.74", "50.1", "365"],
    ],
}


class TestTableCompaction(unittest.TestCase):
    def test_wide_metric_table_is_trimmed_to_core_columns(self):
        out = compact_table(SVI_TABLE)
        self.assertEqual(out["header"],
                         ["Method", "PSNR", "WS-PSNR", "SEPE", "Latency (ms/f)"])
        self.assertLessEqual(len(out["header"]), MAX_TABLE_COLS)
        # 数字原样搬移，不能出现错位
        self.assertEqual(out["rows"][2], ["SVI360 (Ours)", "38.68", "37.73", "15.22", "365"])
        self.assertEqual(out["rows"][0][1], "33.11")

    def test_source_table_not_mutated(self):
        before = [list(r) for r in SVI_TABLE["rows"]]
        compact_table(SVI_TABLE)
        self.assertEqual(SVI_TABLE["rows"], before)
        self.assertEqual(len(SVI_TABLE["header"]), 9)

    def test_narrow_table_untouched(self):
        table = {"header": ["方法", "PSNR", "LPIPS"], "rows": [["Ours", "32.1", "0.21"]]}
        self.assertIs(compact_table(table), table)

    def test_transposed_table_keeps_ours_column(self):
        table = {
            "header": ["Dataset", "Metrics", "Input", "DMCNN", "MBCNN",
                       "FHDe2Net", "ESDNet", "Freqformer"],
            "rows": [
                ["FHDMi", "PSNR", "20.30", "23.52", "25.01", "25.10", "32.10", "32.92"],
                ["FHDMi", "SSIM", "0.80", "0.86", "0.895", "0.90", "0.921", "0.925"],
            ],
        }
        out = compact_table(table)
        self.assertIn("Freqformer", out["header"])
        self.assertLessEqual(len(out["header"]), MAX_TABLE_COLS)

    def test_non_metric_wide_table_left_alone(self):
        table = {
            "header": ["赛事", "方向", "截止", "奖金", "难度", "平台", "组队"],
            "rows": [["A 赛", "超分", "10.30", "10 万", "中", "天池", "允许"]],
        }
        self.assertIs(compact_table(table), table)

    def test_prune_spec_tables_counts_and_keeps_other_blocks(self):
        spec = {
            "title": "T",
            "sections": [
                {"type": "h2", "text": "01 | 实验"},
                {"type": "table", **SVI_TABLE},
                {"type": "p", "text": "解读"},
            ],
        }
        self.assertEqual(prune_spec_tables(spec), 1)
        self.assertEqual(len(spec["sections"][1]["header"]), MAX_TABLE_COLS)
        self.assertEqual(spec["sections"][2]["text"], "解读")

    def test_material_payload_is_compact_but_source_pool_kept(self):
        """喂给模型的是压缩表，但原始表仍留在 item.extra 里供数字校验使用。"""
        item = Item(module="paper", title="T", url="u", source="s",
                    extra={"tables": [SVI_TABLE]})
        payload = _item_payload(1, item, [])
        self.assertEqual(len(payload["tables"][0]["header"]), MAX_TABLE_COLS)
        self.assertEqual(len(item.extra["tables"][0]["header"]), 9)

    def test_render_table_five_columns_full_width(self):
        table = compact_table(SVI_TABLE)
        out = wechat.table(table["header"], table["rows"])
        self.assertEqual(out.count("<th "), 5)
        self.assertIn("width:100%", out)
        # 列数与表头对齐：3 行 × 5 列
        self.assertEqual(out.count("<td "), 15)

    def test_render_drops_cells_beyond_header(self):
        out = wechat.table(["方法", "PSNR", "SSIM"], [["Ours", "32.1", "0.95", "多余列"]])
        self.assertEqual(out.count("<td "), 3)
        self.assertNotIn("多余列", out)


if __name__ == "__main__":
    unittest.main()
