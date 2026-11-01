"""CVF Open Access / NTIRE 报告源的回归测试。

这条链路的每一步都可能静默出错，而且错了很难在成稿里发现：
- 菜单页链接两种写法（`CVPR2026_workshops` 无 `/menu` 后缀），漏一种最新一届就抓不到；
- NTIRE 页里有 68 篇 workshop 论文，只有标题含「NTIRE 20XX Challenge」的才是官方报告；
- 报告正文里 2.x 是数据集小节，按 `3.x` 硬切会把「训练集划分」写成参赛方案；
- 「2,500 submissions」不是参赛人数，混用会让正文写出错误的规模；
- 同届其他赛道的复盘（Track 2）不能当成这条赛道（Track 1）的冠军方案。
"""
import unittest

from app.llm.prompts import CONTEST_NTIRE_PROMPT, build_user_prompt
from app.models import Item
from app.sources import cvf

MENU = """
<html><body>
  <a href="CVPR2026_workshops">Workshops</a>
  <a href="/ICCV2025_workshops/menu">Workshops</a>
  <a href="/CVPR2025_workshops/menu">Workshops</a>
  <a href="CVPR2026">Main Conference</a>
</body></html>
"""

WORKSHOP_MENU = """
<html><body>
  <a href="/CVPR2026_workshops/NTIRE">NTIRE: New Trends in Image Restoration and Enhancement</a>
  <a href="/CVPR2026_workshops/MIPI">MIPI</a>
</body></html>
"""

NTIRE_PAGE = """
<html><body>
<dl>
  <dt class="ptitle"><a href="/content/CVPR2026W/NTIRE/html/A_NTIRE_2026_Nighttime_Dehazing_CVPRW_2026_paper.html">
      NTIRE 2026 Nighttime Image Dehazing Challenge Report</a></dt>
  <dd>Author One, Author Two</dd>
  <dt class="ptitle"><a href="/content/CVPR2026W/NTIRE/html/B_A_Simple_Baseline_CVPRW_2026_paper.html">
      A Simple Baseline for Low-Light Enhancement</a></dt>
  <dd>Author Three</dd>
  <dt class="ptitle"><a href="/content/CVPR2026W/NTIRE/html/C_NTIRE_2026_RAIM_Challenge_CVPRW_2026_paper.html">
      NTIRE 2026 The 3rd Restore Any Image Model (RAIM) Challenge: Professional Image Quality
      Assessment (Track 1)</a></dt>
  <dd>Author Four</dd>
</dl>
</body></html>
"""

# 报告正文切片：2.x 是数据小节（必须被跳过），3.x 才是各队方法
REPORT = """
2. NTIRE 2026 RAW Reconstruction Track 1
2.1. Training Data
We curated a novel reasoning-oriented dataset with 1200 pairs.
2.2. Validation and Test Data
Following the pipeline described previously, we split 20% for validation.
2.3. Evaluation Measures
We report PSNR and SSIM in the RAW domain.
3. Challenge Methods
In the following sections we describe the top challenge submissions.
3.1. DualRAW - Dual Intensity sRGB to RAW Reconstruction
Sony AI
Beril Besbinar, Daisuke Iso
DualRAW draws inspiration from RawHDR and uses dual intensity guidance.
3.2. DBNet: A Dynamical Bias Convolution Network
Team TongJi-IPOE
Pengzhou Ji, Xiong Dun
DBNet is a lightweight method for RAW reconstruction.
Table 1. Award Ranking for the Grand Prize after organizer-side verification.
Rank Team Final Award Score Phase 2 Score Phase 3 Score
1 DualRAW 0.7305 0.6943 0.7667 2 DBNet 0.7253 0.6826 0.7679 3 ULite 0.7040 0.7010 0.7100
4. Teams & Affiliations
The challenge attracted 192 registered participants and over 2,530 submissions.
"""


class TestDiscovery(unittest.TestCase):
    def test_menu_editions_sorted_and_menu_suffix(self):
        eds = cvf._workshop_editions(MENU)
        self.assertEqual(eds[0][0], "CVPR")
        self.assertEqual(eds[0][1], 2026)
        # CVPR2026 的 href 没有 /menu 后缀，要补齐，否则请求 404
        self.assertTrue(eds[0][2].endswith("/menu"))
        self.assertIn("ICCV2025", eds[1][2])

    def test_ntire_link_found(self):
        link = cvf._ntire_link(WORKSHOP_MENU)
        self.assertTrue(link.endswith("/CVPR2026_workshops/NTIRE"))

    def test_ntire_link_absent(self):
        self.assertEqual(cvf._ntire_link("<a href='/CVPR2026_workshops/MIPI'>MIPI</a>"), "")


class TestReportList(unittest.TestCase):
    def test_only_challenge_reports_kept(self):
        items = cvf.parse_ntire_page(NTIRE_PAGE, conference="CVPR 2026 Workshops", year=2026)
        titles = [i.title for i in items]
        self.assertEqual(len(items), 2)  # 「A Simple Baseline」不是官方报告
        self.assertTrue(all("challenge" in t.lower() for t in titles))
        self.assertTrue(all(i.extra["origin"] == "ntire" for i in items))
        self.assertTrue(all(i.extra["is_learning_resource"] for i in items))

    def test_pdf_url_derived(self):
        items = cvf.parse_ntire_page(NTIRE_PAGE, conference="CVPR 2026 Workshops", year=2026)
        for item in items:
            self.assertTrue(item.extra["pdf_url"].endswith(".pdf"))

    def test_track_cleaned_and_track_no_kept(self):
        items = cvf.parse_ntire_page(NTIRE_PAGE, conference="CVPR 2026 Workshops", year=2026)
        dehaze = next(i for i in items if "Dehazing" in i.title)
        self.assertEqual(dehaze.extra["track"], "Nighttime Image Dehazing")
        raim = next(i for i in items if "RAIM" in i.title)
        # 序数「The 3rd」要去掉，但赛道号必须留着（社区检索靠它区分 Track 1/2）
        self.assertNotIn("3rd", raim.extra["track"])
        self.assertIn("Track 1", raim.extra["track"])

    def test_lowlevel_track_scores_higher(self):
        items = cvf.parse_ntire_page(NTIRE_PAGE, conference="CVPR 2026 Workshops", year=2026)
        dehaze = next(i for i in items if "Dehazing" in i.title)
        raim = next(i for i in items if "RAIM" in i.title)
        self.assertGreater(dehaze.score, raim.score)


class TestReportParsing(unittest.TestCase):
    def test_sections_skip_data_chapters(self):
        secs = cvf.parse_sections(REPORT)
        nums = [s["num"] for s in secs]
        self.assertEqual(nums, ["3.1", "3.2"])  # 2.1/2.2/2.3 不能被当成队伍方案
        self.assertEqual(secs[0]["team"], "Sony AI")
        self.assertIn("DualRAW", secs[0]["method"])

    def test_no_anchor_returns_empty(self):
        self.assertEqual(cvf.parse_sections("1. Introduction\n2. Conclusion\n"), [])

    def test_leaderboard(self):
        board = cvf.parse_leaderboard(REPORT)
        self.assertEqual([b["team"] for b in board][:3], ["DualRAW", "DBNet", "ULite"])
        self.assertEqual(board[0]["score"], "0.7305")

    def test_champion_merges_team_and_method(self):
        champ = cvf._pick_champion(cvf.parse_sections(REPORT), cvf.parse_leaderboard(REPORT))
        self.assertEqual(champ["team"], "DualRAW")
        self.assertIn("DualRAW", champ["method"])
        self.assertEqual(champ["score"], "0.7305")

    def test_stats_do_not_confuse_submissions_with_people(self):
        stats = cvf.report_stats(REPORT)
        self.assertEqual(stats["participants"], "192")
        self.assertEqual(stats["submissions"], "2,530")
        self.assertIn("192 registered participants", stats["participants_hint"])


class TestCommunityFilter(unittest.TestCase):
    def test_relevance_requires_ntire_or_year_plus_track(self):
        self.assertTrue(cvf.is_relevant_hit("NTIRE 2026 去噪赛道冠军方案解析", "2026", "Denoising"))
        self.assertTrue(cvf.is_relevant_hit("2026 图像去噪实战", "2026", "Image Denoising"))
        self.assertFalse(cvf.is_relevant_hit("低照度图像处理算法综述", "2026", "Denoising"))

    def test_track_conflict(self):
        self.assertTrue(cvf.track_conflict("NTIRE 2026 RAIM Track 2 冠军方案", "RAIM Track 1"))
        self.assertFalse(cvf.track_conflict("NTIRE 2026 RAIM Track 1 冠军方案", "RAIM Track 1"))
        # 赛道本身没有编号时不判冲突
        self.assertFalse(cvf.track_conflict("NTIRE 2026 Track 2", "Nighttime Dehazing"))

    def test_rank_prefers_champion_and_penalises_other_track(self):
        rows = [
            {"title": "NTIRE 2026 Track 2 冠军方案", "snippet": "冠军 方案", "url": "u1"},
            {"title": "NTIRE 2026 DualRAW 冠军方案复盘", "snippet": "DualRAW 冠军 方案", "url": "u2"},
        ]
        ranked = cvf.rank_hits(rows, {"team": "DualRAW", "method": "DualRAW"}, "RAIM Track 1")
        self.assertEqual(ranked[0]["url"], "u2")
        self.assertFalse(next(r for r in ranked if r["url"] == "u1")["same_track"])


class TestPromptRouting(unittest.TestCase):
    def _item(self, **extra) -> Item:
        return Item(module="contest", title="NTIRE 2026 夜间去雾挑战赛报告",
                    url="https://example.com/r", source="CVF NTIRE 报告",
                    summary="s", category="学习资源",
                    extra={"is_learning_resource": True, **extra})

    def test_ntire_material_picks_report_prompt(self):
        item = self._item(origin="ntire", year="2026", track="Nighttime Dehazing",
                          ntire_report={"champion": {"team": "X"}, "sections": [],
                                        "leaderboard": [], "community": []})
        prompt = build_user_prompt("contest", [item], {}, "2026.10.29", variant="baseline")
        self.assertTrue(prompt.startswith(CONTEST_NTIRE_PROMPT[:60]))
        self.assertIn('"ntire_report"', prompt)

    def test_datashale_material_keeps_baseline_prompt(self):
        item = self._item(folder="某赛题", baseline_detail={"files": ["a.py"]})
        prompt = build_user_prompt("contest", [item], {}, "2026.10.29", variant="baseline")
        self.assertIn('"baseline_detail"', prompt)
        self.assertNotIn("NTIRE（CVPR/ICCV/WACV", prompt)


if __name__ == "__main__":
    unittest.main()
