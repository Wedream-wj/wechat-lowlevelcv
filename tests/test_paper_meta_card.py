"""信息卡「行名 + venue」两类历史不一致的单测。

事故原型（2026-11-16 整理）：《CVPR 2025 | InvSR》信息卡只写「CVPR」缺年份；
《ECCV 2026 | DORNet》标题写 ECCV 2026、卡片写 CVPR 2025；另有 16 期旧稿仍用
`论文标题` / `期刊/会议` / `发表状态` 这类历史行名，检索与字段抽取会漏。

这里固化四件事：
1. venue 上卡一律「全称 + 年份 + 括注简称」，只有简称也要补齐年份；
2. 标题里的 venue 简称大小写按学术圈通用写法（NeurIPS 不是 NEURIPS）；
3. Markdown 信息卡行名收敛到三行正式行名，值一个字不动，正文零改动；
4. 历史库里不再存在旧行名 / 缺年份 venue / 标题与卡片打架。
"""
import re
import sqlite3
import unittest
from datetime import datetime
from pathlib import Path

from app import edit
from app.models import Article, Item
from app.spec_tools import (
    _meta_kind,
    abbrev_title_venue,
    item_venue_label,
    item_venue_short,
    venue_abbrev,
    venue_card_label,
    venue_short_label,
)

DB = Path(__file__).resolve().parents[1] / "data" / "llcv.db"
CANON = ["论文名称", "发表期刊/会议", "作者团队"]


class TestVenueLabel(unittest.TestCase):
    def test_short_venue_gets_the_year_back(self):
        """《InvSR》卡片只写「CVPR」：简称不是省略年份的理由。"""
        self.assertEqual(venue_card_label("CVPR", "2025"), "CVPR 2025")
        self.assertEqual(venue_card_label("CVPR 2025", "2025"), "CVPR 2025")
        self.assertEqual(venue_card_label("Sensors", "2021"), "Sensors 2021")

    def test_journal_keeps_full_name_and_gets_year(self):
        self.assertEqual(venue_card_label("Pattern Recognition", "2026"),
                         "Pattern Recognition 2026（PR）")
        # Crossref 的卷期/DOI 尾巴不许上卡
        self.assertEqual(
            venue_card_label(
                "Pattern Recognition, 177 (2026) 113339. doi:10.1016/j.patcog.2026.113339", "2026"),
            "Pattern Recognition 2026（PR）")

    def test_paren_abbrev_is_normalized(self):
        self.assertEqual(venue_card_label("European Conference on Computer Vision (ECCV)", "2026"),
                         "European Conference on Computer Vision 2026（ECCV）")
        self.assertEqual(
            venue_card_label("2024 IEEE International Conference on Multimedia and Expo (ICME)", "2024"),
            "IEEE International Conference on Multimedia and Expo 2024（ICME）")

    def test_conference_case_is_not_shouted(self):
        self.assertEqual(venue_abbrev("NEURIPS"), "NeurIPS")
        self.assertEqual(venue_abbrev("NeurIPS 2026"), "NeurIPS")
        self.assertEqual(venue_short_label("NeurIPS", "2026"), "NeurIPS 2026")
        self.assertEqual(abbrev_title_venue("NEURIPS 2026｜DeLang-SR：解耦"),
                         "NeurIPS 2026 | DeLang-SR：解耦")

    def test_short_label_from_full_name(self):
        self.assertEqual(venue_short_label("European Conference on Computer Vision (ECCV)", "2026"),
                         "ECCV 2026")
        self.assertEqual(venue_short_label("CVPR", "2025"), "CVPR 2025")


class TestItemVenue(unittest.TestCase):
    def _item(self, venue="", year="", url="https://arxiv.org/abs/2601.00001", pub=None):
        return Item(module="paper", title="Some Paper", url=url, source="Awesome CVPR 2026",
                    venue=venue, year=year, extra={"publication": pub} if pub else {})

    def test_publication_venue_wins_over_list_label(self):
        """《LSGQuant》榜单标 CVPR、查证是 ICML：以查证记录为准。"""
        item = self._item(venue="CVPR 2026", year="2026",
                          pub={"accepted": True, "venue": "ICML 2026", "year": "2026"})
        self.assertEqual(item_venue_label(item), "ICML 2026")
        self.assertEqual(item_venue_short(item), "ICML 2026")

    def test_missing_year_is_filled_from_item(self):
        item = self._item(venue="CVPR 2025", year="2025",
                          pub={"accepted": True, "venue": "CVPR", "year": ""})
        self.assertEqual(item_venue_label(item), "CVPR 2025")

    def test_preprint_venue_is_not_dressed_as_a_conference(self):
        item = self._item(venue="arXiv (Cornell University)", year="2026")
        self.assertEqual(item_venue_label(item), "arXiv 预印本")
        self.assertEqual(item_venue_short(item), "")

    def test_unverifiable_venue_does_not_downgrade_accepted_paper(self):
        """《UPSR》被查证链路误标 preprint_only：没有会议名的反证不足以降级 CVPR。"""
        item = self._item(venue="CVPR 2025", year="2025",
                          pub={"accepted": False, "preprint_only": True})
        self.assertEqual(item_venue_label(item), "CVPR 2025")


class TestMetaCardLines(unittest.TestCase):
    def _md(self, rows, title="# 测试标题", tail="## 01 | 研究背景与痛点\n\n正文。"):
        block = "\n".join(rows)
        return f"{title}\n\n**导读**：一句话。\n\n{block}\n\n{tail}\n"

    def test_old_labels_are_renamed_and_ordered(self):
        md = self._md([
            "- **论文标题**：Some Paper",
            "- **作者团队**：Zongsheng Yue, Kang Liao",
            "- **期刊/会议**：CVPR",
            "- **开源代码**：https://github.com/x/y",
        ])
        new, changes = edit.normalize_meta_card_lines(md, "paper")
        self.assertTrue(any("论文标题" in c for c in changes))
        self.assertIn("- **论文名称**：Some Paper", new)
        self.assertIn("- **发表期刊/会议**：CVPR", new)     # 值原样，不在这里改 venue
        labels = re.findall(r"- \*\*(.+?)\*\*：", new)
        self.assertEqual(labels, CANON + ["开源代码"])
        self.assertIn("## 01 | 研究背景与痛点", new)

    def test_status_row_is_merged_into_venue(self):
        md = self._md(["- **论文标题**：SONIC", "- **作者团队**：A B", "- **发表状态**：arXiv 预印本"])
        new, _ = edit.normalize_meta_card_lines(md, "paper")
        self.assertIn("- **发表期刊/会议**：arXiv 预印本", new)
        self.assertNotIn("发表状态", new)

    def test_duplicate_semantic_rows_collapse(self):
        md = self._md(["- **论文名称**：X", "- **期刊/会议**：CVPR 2025",
                       "- **发表期刊/会议**：CVPR 2025", "- **作者团队**：A B"])
        new, changes = edit.normalize_meta_card_lines(md, "paper")
        self.assertEqual(new.count("**发表期刊/会议**"), 1)
        self.assertTrue(any("重复行" in c for c in changes))

    def test_idempotent_and_module_scoped(self):
        md = self._md(["- **论文标题**：X", "- **期刊/会议**：CVPR 2025"])
        once, _ = edit.normalize_meta_card_lines(md, "paper")
        twice, changes = edit.normalize_meta_card_lines(once, "paper")
        self.assertEqual(once, twice)
        self.assertEqual(changes, [])
        untouched, _ = edit.normalize_meta_card_lines(md, "industry")
        self.assertEqual(untouched, md)

    def test_body_bullets_are_not_touched(self):
        md = self._md(["- **论文名称**：X"], tail="## 01 | 背景\n\n- **期刊/会议**：正文里的普通列表")
        new, _ = edit.normalize_meta_card_lines(md, "paper")
        self.assertIn("- **期刊/会议**：正文里的普通列表", new)

    def test_editor_save_path_normalizes_labels(self):
        md = self._md(["- **论文标题**：X", "- **期刊/会议**：CVPR 2025",
                       "- **作者团队**：A B"], title="# CVPR 2025 | X：一句话")
        article = Article(module="paper", title="CVPR 2025 | X：一句话", issue_id="test-meta-labels",
                          markdown=md, created_at=datetime(2026, 11, 16))
        result = edit.render_edit(article, md)
        self.assertIn("- **论文名称**：X", result.markdown)
        self.assertIn("- **发表期刊/会议**：CVPR 2025", result.markdown)


class TestHistoryArchive(unittest.TestCase):
    """整理后的历史库必须满足的行名 / venue 不变量（复跑脚本仍为 0 期问题）。"""

    @unittest.skipUnless(DB.exists(), "本地库不存在")
    def test_cards_use_canonical_labels_and_venues(self):
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT issue_id, title, markdown FROM issues WHERE module='paper'").fetchall()
        conn.close()
        self.assertTrue(rows)
        for issue_id, title, markdown in rows:
            with self.subTest(issue_id=issue_id):
                lines = (markdown or "").splitlines()
                parsed = [m for m in (edit._META_LINE_RE.match(line) for line in lines) if m]
                labels = [m.group(1) for m in parsed]
                kinds = [_meta_kind(label) for label in labels]
                canonical = [k for k in kinds if k]
                # 1) 不再有旧行名
                for label, kind in zip(labels, kinds):
                    if kind:
                        self.assertEqual(label, kind, f"{issue_id} 仍是旧行名「{label}」")
                # 2) 三行按固定顺序置顶
                self.assertEqual(canonical, [k for k in CANON if k in canonical],
                                 f"{issue_id} 信息卡三行顺序不对：{labels}")
                # 3) venue 必须有年份（预印本除外），且不许带卷期/DOI 尾巴
                for m in parsed:
                    if _meta_kind(m.group(1)) != "发表期刊/会议":
                        continue
                    value = m.group(2).strip()
                    if "预印本" in value:
                        continue
                    self.assertRegex(value, r"(?:19|20)\d{2}",
                                     f"{issue_id} 卡片 venue 缺年份：{value}")
                    self.assertNotRegex(value, r"doi|\(\s*20\d{2}\s*\)\s*\d+",
                                        f"{issue_id} 卡片 venue 仍带卷期/DOI：{value}")
                # 4) 标题不许把简称喊成全大写
                self.assertNotIn("NEURIPS", (title or ""), f"{issue_id} 标题仍写 NEURIPS")


if __name__ == "__main__":
    unittest.main()
