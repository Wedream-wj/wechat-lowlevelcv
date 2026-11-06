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
    abbrev_title_venue,
    compact_table,
    ensure_paper_arch_figure,
    ensure_paper_meta_card,
    ensure_paper_result_table,
    merge_event_rows,
    normalize_meta_cards,
    normalize_section_titles,
    normalize_wording,
    normalized_spec,
    prune_spec_tables,
    recast_author_origin,
    renumber_sections,
    scrub_dangling_figures,
    scrub_paper_author_grammar,
    scrub_process_narration,
    scrub_reader_burden,
    strip_countdown_title,
    strip_meta_time,
    venue_abbrev,
    venue_card_label,
    venue_short_label,
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


class TestMetaTime(unittest.TestCase):
    """「当前日期 2026-10-28，距报名关闭只剩 3 天」这类句子必须清掉。

    读者不需要知道编辑是哪天写的稿；而且跨天推送后这句话立刻自相矛盾。
    """

    def test_sentence_removed_others_kept(self):
        spec = {"sections": [{"type": "p", "text":
                              "赛程分两段。比赛时间截止2026-10-31，报名剩余3天。"
                              "当前日期2026-10-28，距报名关闭只剩3天。资格赛提交次数不限。"}]}
        self.assertEqual(strip_meta_time(spec), 1)
        text = spec["sections"][0]["text"]
        self.assertNotIn("当前日期", text)
        self.assertIn("报名剩余3天", text)   # days_left 形态允许保留
        self.assertIn("资格赛提交次数不限", text)

    def test_block_dropped_when_pure_meta(self):
        spec = {"sections": [{"type": "p", "text": "截至今天，本文成稿于 2026-10-28。"},
                              {"type": "p", "text": "正常段落。"}]}
        self.assertEqual(strip_meta_time(spec), 1)
        self.assertEqual(len(spec["sections"]), 1)

    def test_bullets_filtered(self):
        spec = {"sections": [{"type": "bullets",
                              "items": ["距发稿只剩一周", "初赛 100 分制"]}]}
        strip_meta_time(spec)
        self.assertEqual(spec["sections"][0]["items"], ["初赛 100 分制"])

    def test_clean_spec_untouched(self):
        spec = _spec("01 | 背景")
        before = [dict(b) for b in spec["sections"]]
        self.assertEqual(strip_meta_time(spec), 0)
        # 没命中时不做任何改写（包括不重建 sections 列表）
        self.assertEqual(len(spec["sections"]), len(before))


class TestCountdownTitle(unittest.TestCase):
    """报名已关时标题不许带「N 天报名倒计时」：赛程还在，报名口关了。"""

    def test_stripped_when_registration_closed(self):
        spec = {"title": "余震预测国际大赛：3 个时间窗、3 天报名倒计时"}
        self.assertTrue(strip_countdown_title(spec, True))
        self.assertEqual(spec["title"], "余震预测国际大赛：3 个时间窗")

    def test_tail_separator_cleaned(self):
        spec = {"title": "图像超分辨率重建挑战赛，距报名截止只剩 2 天"}
        strip_countdown_title(spec, True)
        self.assertEqual(spec["title"], "图像超分辨率重建挑战赛")

    def test_untouched_when_registration_open(self):
        spec = {"title": "具身智能挑战赛：3 天报名倒计时"}
        self.assertFalse(strip_countdown_title(spec, False))
        self.assertEqual(spec["title"], "具身智能挑战赛：3 天报名倒计时")

    def test_short_result_rejected(self):
        # 删完只剩「短标题」就不动手，宁可留着也不把标题削成半个词
        spec = {"title": "超分赛，3 天倒计时"}
        self.assertFalse(strip_countdown_title(spec, True))

    def test_days_left_hidden_when_closed(self):
        item = Item(module="contest", title="X", url="https://e.com", source="天池",
                    summary="s",
                    extra={"days_left": 3,
                           "contest_detail": {"registration_closed": True}})
        self.assertNotIn("days_left", _item_payload(1, item, []))
        item.extra["contest_detail"]["registration_closed"] = False
        self.assertEqual(_item_payload(1, item, [])["days_left"], 3)


class TestWording(unittest.TestCase):
    """「抄作业 / 套用 / 搬运」这类词 Prompt 明令禁止，但模型会换说法回来。"""

    def test_banned_words_replaced(self):
        spec = {"lead": ["先说结论：这段代码可以直接抄走。"],
                "sections": [
                    {"type": "p", "text": "这是整篇报告里最值得抄作业的一处。"},
                    {"type": "bullets", "items": ["先套用它的数据管道", "搬运过来改两行"]},
                ],
                "takeaway": "照着抄一遍不如理解一遍。"}
        self.assertGreaterEqual(normalize_wording(spec), 4)
        blob = " ".join([*spec["lead"], spec["takeaway"],
                         *[b.get("text", "") for b in spec["sections"]],
                         *[i for b in spec["sections"] for i in (b.get("items") or [])]])
        for bad in ("抄走", "抄作业", "套用", "搬运", "抄一"):
            self.assertNotIn(bad, blob)
        self.assertIn("借鉴", blob)

    def test_clean_spec_untouched(self):
        spec = {"sections": [{"type": "p", "text": "可直接复用其数据处理 Pipeline。"}]}
        self.assertEqual(normalize_wording(spec), 0)


class TestReaderBurdenScrub(unittest.TestCase):
    """本号「只挑值得读的那几篇」，推送即已筛选——不能把选择题丢回给读者。"""

    def test_tradeoff_sentence_is_recast(self):
        spec = {"lead": ["DreamSR 给出了一条折中路线。"
                         "它值不值得读，取决于你是否接受以 PSNR 换取感知质量的"
                         "定量权衡（Trade-off）。"],
                "sections": [], "takeaway": ""}
        self.assertGreaterEqual(scrub_reader_burden(spec), 1)
        para = spec["lead"][0]
        self.assertNotIn("值不值得读", para)
        self.assertIn("它的应用价值，依赖于人们是否接受以 PSNR 换取感知质量的", para)

    def test_bare_question_dropped_keeps_rest(self):
        spec = {"sections": [{"type": "p",
                              "text": "值不值得读？如果做全景方向，这是最干净的设计。"}]}
        self.assertEqual(scrub_reader_burden(spec), 1)
        self.assertEqual(spec["sections"][0]["text"], "如果做全景方向，这是最干净的设计。")

    def test_block_removed_when_all_burden(self):
        spec = {"sections": [{"type": "p", "text": "值不值得读？读者自行判断。"}]}
        self.assertEqual(scrub_reader_burden(spec), 2)   # 两句各算一处
        self.assertEqual(spec["sections"], [])

    def test_takeaway_cleaned(self):
        spec = {"sections": [], "takeaway": "值不值得读？这是一次干净的重构。"}
        scrub_reader_burden(spec)
        self.assertNotIn("值不值得读", spec["takeaway"])

    def test_clean_text_untouched(self):
        spec = {"sections": [{"type": "p", "text": "它的应用价值，依赖于人们是否接受取舍。"}]}
        self.assertEqual(scrub_reader_burden(spec), 0)


class TestProcessNarrationScrub(unittest.TestCase):
    """素材表格错位、抓取受限属于内部工序，一个字都不该出现在正文里。"""

    def test_alignment_complaint_dropped(self):
        spec = {"sections": [{"type": "p",
                              "text": "下表摘自论文在 RealSR 与 DRealSR 两个基准上的对比。"
                                      "需要说明的是，素材表格的行列对齐存在错位，"
                                      "以下按单元格原值呈现，不做重排。"}]}
        self.assertEqual(scrub_process_narration(spec), 1)
        self.assertEqual(spec["sections"][0]["text"],
                         "下表摘自论文在 RealSR 与 DRealSR 两个基准上的对比。")

    def test_crawl_limit_complaint_dropped(self):
        spec = {"sections": [{"type": "p", "text": "受抓取限制影响，部分行缺失。"
                                                   "作者在 4 个基准上做了验证。"}]}
        scrub_process_narration(spec)
        self.assertNotIn("抓取限制", spec["sections"][0]["text"])
        self.assertIn("4 个基准", spec["sections"][0]["text"])

    def test_table_block_kept(self):
        spec = {"sections": [{"type": "table", "header": ["Method", "PSNR"],
                              "rows": [["Ours", "38.68"]]}]}
        self.assertEqual(scrub_process_narration(spec), 0)
        self.assertEqual(spec["sections"][0]["rows"], [["Ours", "38.68"]])


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


def _paper_item(**kw) -> Item:
    base = dict(
        module="paper",
        title="WeaCount: Spatial-Aware Decoupling for Crowd Counting in Adverse Weather",
        url="https://doi.org/10.1016/j.patcog.2026.115188",
        source="Crossref",
        venue="Pattern Recognition",
        year="2026",
        authors=["Weihang Kong", "Jienan Shen", "He Li", "Yang Li"],
    )
    base.update(kw)
    return Item(**base)


class TestVenueAbbreviation(unittest.TestCase):
    def test_full_names_collapse_to_standard_abbrev(self):
        self.assertEqual(venue_abbrev("Pattern Recognition"), "PR")
        self.assertEqual(venue_abbrev("Knowledge-Based Systems"), "KBS")
        self.assertEqual(
            venue_abbrev("IEEE Transactions on Pattern Analysis and Machine Intelligence"),
            "TPAMI")

    def test_longer_journal_wins_over_its_prefix(self):
        # PRL 必须先于 PR 命中，否则会产出 "PRL" 被截成 "PR" 的错误简称
        self.assertEqual(venue_abbrev("Pattern Recognition Letters"), "PRL")

    def test_already_short_and_unknown_venues(self):
        self.assertEqual(venue_abbrev("CVPR"), "CVPR")
        self.assertEqual(venue_abbrev("arXiv"), "")

    def test_short_label_and_card_label(self):
        self.assertEqual(venue_short_label("Pattern Recognition", "2026"), "PR 2026")
        self.assertEqual(venue_short_label("Pattern Recognition 2026"), "PR 2026")
        self.assertEqual(venue_card_label("Pattern Recognition 2026"),
                         "Pattern Recognition 2026（PR）")

    def test_title_uses_abbrev_and_normalizes_separator(self):
        self.assertEqual(
            abbrev_title_venue("Pattern Recognition 2026｜WeaCount：空间感知解耦，攻克恶劣天气人群计数"),
            "PR 2026 | WeaCount：空间感知解耦，攻克恶劣天气人群计数")
        self.assertEqual(abbrev_title_venue("Pattern Recognition (PR) 2026｜WeaCount：空间感知解耦"),
                         "PR 2026 | WeaCount：空间感知解耦")
        self.assertEqual(abbrev_title_venue("Knowledge-Based Systems 2025 | XXX"),
                         "KBS 2025 | XXX")
        self.assertEqual(
            abbrev_title_venue(
                "IEEE Transactions on Pattern Analysis and Machine Intelligence 2024｜YYY"),
            "TPAMI 2024 | YYY")

    def test_title_without_venue_untouched(self):
        title = "无惧复杂失真！XXX 让低光增强一次到位"
        self.assertEqual(abbrev_title_venue(title), title)


class TestAuthorGrammar(unittest.TestCase):
    def test_name_list_after_origin_is_recast(self):
        sent = ("论文发表在 Pattern Recognition 2026，作者团队来自 "
                "Weihang Kong、Jienan Shen、He Li、Yang Li。")
        out = recast_author_origin(sent)
        self.assertIn("作者团队由 Weihang Kong、Jienan Shen、He Li、Yang Li 等人组成", out)
        self.assertNotIn("来自 Weihang", out)

    def test_affiliation_after_origin_is_kept(self):
        sent = "研究团队来自清华大学电子工程系。"
        self.assertEqual(recast_author_origin(sent), sent)

    def test_scrub_covers_lead_and_sections(self):
        spec = {"lead": ["作者团队来自 Weihang Kong、Jienan Shen。"],
                "sections": [{"type": "p", "text": "本文作者来自 He Li、Yang Li。"}]}
        self.assertGreaterEqual(scrub_paper_author_grammar(spec), 2)
        self.assertIn("由 Weihang Kong、Jienan Shen 等人组成", spec["lead"][0])
        self.assertIn("由 He Li、Yang Li 等人组成", spec["sections"][0]["text"])


class TestPaperMetaCard(unittest.TestCase):
    def test_card_inserted_with_three_required_rows(self):
        item = _paper_item()
        spec = {"title": "PR 2026 | WeaCount", "sections": [
            {"type": "h2", "text": "01 | 研究背景与痛点"},
            {"type": "p", "text": "正文"},
        ]}
        self.assertEqual(ensure_paper_meta_card(spec, item), 1)
        card = spec["sections"][0]
        self.assertEqual(card["type"], "meta_card")
        self.assertEqual(card["label"], "PAPER INFO")
        labels = [r[0] for r in card["rows"]]
        self.assertEqual(labels[:3], ["论文名称", "发表期刊/会议", "作者团队"])
        self.assertEqual(card["rows"][0][1], item.title)
        self.assertIn("PR", card["rows"][1][1])
        authors = card["rows"][2][1]
        self.assertIn("Weihang Kong", authors)
        self.assertIn("、", authors)
        self.assertNotIn("来自", authors)

    def test_existing_card_rows_are_renamed_and_promoted(self):
        item = _paper_item()
        spec = {"title": "T", "sections": [
            {"type": "meta_card", "rows": [
                ["开源代码", "https://github.com/x/y"],
                ["论文标题", "旧标题"],
                ["期刊/会议", "Pattern Recognition 2026"],
            ]},
            {"type": "h2", "text": "01 | 背景"},
        ]}
        self.assertGreaterEqual(ensure_paper_meta_card(spec, item), 1)
        rows = spec["sections"][0]["rows"]
        self.assertEqual([r[0] for r in rows][:3],
                         ["论文名称", "发表期刊/会议", "作者团队"])
        self.assertEqual(rows[0][1], item.title)

    def test_no_item_is_noop(self):
        spec = {"title": "T", "sections": [{"type": "h2", "text": "01 | 背景"}]}
        self.assertEqual(ensure_paper_meta_card(spec, None), 0)

    def test_placeholder_rows_are_dropped(self):
        item = _paper_item()
        spec = {"title": "T", "sections": [
            {"type": "meta_card", "label": "PAPER INFO", "rows": [
                ["论文名称", item.title],
                ["研究机构", "官方未公布"],
                ["开源代码", "未披露"],
            ]},
            {"type": "h2", "text": "01 | 背景"},
        ]}
        ensure_paper_meta_card(spec, item)
        labels = [r[0] for r in spec["sections"][0]["rows"]]
        self.assertNotIn("研究机构", labels)
        self.assertNotIn("开源代码", labels)


class TestPaperFigureAndTable(unittest.TestCase):
    def _spec(self) -> dict:
        return {"title": "PR 2026 | WeaCount", "sections": [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "p", "text": "方法正文"},
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "p", "text": "结果正文"},
        ]}

    def test_arch_figure_inserted_after_first_paragraph(self):
        item = _paper_item()
        figures = {"F1": {"item_id": f"paper:{item.fingerprint}",
                          "caption": "Overall architecture of WeaCount"}}
        spec = self._spec()
        self.assertEqual(ensure_paper_arch_figure(spec, item, figures), 1)
        fig = spec["sections"][2]
        self.assertEqual((fig["type"], fig["ref"]), ("figure", "F1"))
        self.assertTrue(fig["caption"].startswith("图 1："))

    def test_arch_figure_skipped_when_present_or_missing(self):
        item = _paper_item()
        figures = {"F1": {"item_id": f"paper:{item.fingerprint}", "caption": "Overall architecture"}}
        spec = {"sections": [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "figure", "ref": "F1", "caption": "图 1：整体架构。"},
        ]}
        self.assertEqual(ensure_paper_arch_figure(spec, item, figures), 0)
        self.assertEqual(ensure_paper_arch_figure(self._spec(), item, {}), 0)

    def test_result_table_inserted_from_material(self):
        item = _paper_item(extra={"tables": [{
            "caption": "Table 1: Comparison with SOTA",
            "header": ["Method", "MAE", "MSE"],
            "rows": [["Baseline", "9.4", "15.6"], ["Ours", "8.1", "12.3"]],
        }]})
        spec = self._spec()
        self.assertEqual(ensure_paper_result_table(spec, item), 1)
        table = spec["sections"][-1]
        self.assertEqual(table["type"], "table")
        self.assertEqual(table["header"], ["Method", "MAE", "MSE"])
        self.assertEqual(table["rows"][-1][0], "Ours")

    def test_result_table_skipped_without_material(self):
        item = _paper_item()
        self.assertEqual(ensure_paper_result_table(self._spec(), item), 0)

    def test_dangling_figure_dropped_and_replaced(self):
        item = _paper_item()
        spec = {"sections": [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "figure", "ref": "Fig.1", "caption": "图 1：模型架构。"},
            {"type": "p", "text": "方法正文"},
        ]}
        self.assertEqual(scrub_dangling_figures(spec, {}), 1)
        self.assertFalse([b for b in spec["sections"] if b.get("type") == "figure"])

        figures = {"F1": {"item_id": f"paper:{item.fingerprint}",
                          "caption": "Overall architecture of WeaCount"}}
        spec = {"sections": [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "figure", "ref": "Fig.1", "caption": "图 1：模型架构。"},
            {"type": "p", "text": "方法正文"},
        ]}
        self.assertEqual(scrub_dangling_figures(spec, figures), 1)
        self.assertEqual(ensure_paper_arch_figure(spec, item, figures), 1)
        refs = [b["ref"] for b in spec["sections"] if b.get("type") == "figure"]
        self.assertEqual(refs, ["F1"])


if __name__ == "__main__":
    unittest.main()
