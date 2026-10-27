"""论文发表状态查证与标题信誉度规则测试（纯离线，不依赖网络）。"""
from __future__ import annotations

import unittest

from app.llm.prompts import enforce_title_rules
from app.sources.publication import (
    _from_author_declared,
    _parse_openreview,
    _parse_s2,
    normalize_venue,
)


class TestNormalizeVenue(unittest.TestCase):
    def test_alias_full_name_to_short(self):
        venue, _ = normalize_venue(
            "IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)")
        self.assertEqual(venue.split()[0], "CVPR")

    def test_full_name_journal(self):
        venue, _ = normalize_venue("IEEE Transactions on Image Processing")
        self.assertEqual(venue.split()[0], "TIP")

    def test_short_token_with_year(self):
        venue, _ = normalize_venue("Accepted to NeurIPS 2025")
        self.assertEqual(venue, "NeurIPS 2025")

    def test_non_venue_text(self):
        venue, _ = normalize_venue("We propose a new diffusion model for deraining")
        self.assertEqual(venue, "")


class TestAuthorDeclared(unittest.TestCase):
    def test_accepted_comment(self):
        out = _from_author_declared("Accepted by CVPR 2026. Code at github.com/x/y", "")
        self.assertIsNotNone(out)
        self.assertTrue(out["accepted"])
        self.assertEqual(out["venue"].split()[0], "CVPR")

    def test_journal_ref_only(self):
        out = _from_author_declared("", "IEEE Transactions on Image Processing, 2025")
        self.assertIsNotNone(out)
        self.assertEqual(out["venue"].split()[0], "TIP")

    def test_preprint_comment(self):
        out = _from_author_declared("12 pages, 5 figures", "")
        self.assertIsNone(out)


class TestChannelParsers(unittest.TestCase):
    def test_openreview_hit(self):
        data = {"notes": [{"content": {
            "title": "Freqformer: Efficient Frequency Routing for Image Super-Resolution",
            "venue": "CVPR 2025",
        }}]}
        out = _parse_openreview(data, "Freqformer: Efficient Frequency Routing "
                                      "for Image Super-Resolution")
        self.assertIsNotNone(out)
        self.assertTrue(out["accepted"])

    def test_openreview_unrelated_note_ignored(self):
        data = {"notes": [{"content": {
            "title": "Some Totally Different Paper About Radar",
            "venue": "CVPR 2025",
        }}]}
        out = _parse_openreview(data, "Freqformer: Efficient Frequency Routing "
                                      "for Image Super-Resolution")
        self.assertIsNone(out)

    def test_s2_hit_via_publication_venue(self):
        data = {"title": "X", "venue": "", "year": 2025,
                "publicationVenue": {"name": "IEEE Transactions on Image Processing"}}
        out = _parse_s2(data)
        self.assertIsNotNone(out)
        self.assertEqual(out["venue"].split()[0], "TIP")

    def test_s2_arxiv_only_venue(self):
        data = {"title": "X", "venue": "arXiv.org", "year": 2026,
                "publicationVenue": {"name": "arXiv.org"}}
        self.assertIsNone(_parse_s2(data))


class TestEnforceTitleRules(unittest.TestCase):
    def _spec(self, title: str) -> dict:
        return {"title": title, "sections": []}

    def test_strips_arxiv_prefix(self):
        spec = self._spec("arXiv 2026 | HP-PnP-Flow：流模型打破 PnP 迭代瓶颈")
        self.assertTrue(enforce_title_rules(spec, "paper"))
        self.assertNotIn("arxiv", spec["title"].lower())
        self.assertIn("HP-PnP-Flow", spec["title"])

    def test_strips_bare_arxiv_token(self):
        spec = self._spec("HP-PnP-Flow：arXiv 高效底层视觉重建")
        self.assertTrue(enforce_title_rules(spec, "paper"))
        self.assertNotIn("arxiv", spec["title"].lower())

    def test_other_modules_untouched(self):
        spec = self._spec("arXiv 2026 | 某业界动态")
        self.assertFalse(enforce_title_rules(spec, "industry"))
        self.assertIn("arXiv", spec["title"])

    def test_clean_title_untouched(self):
        spec = self._spec("CVPR 2026 | HP-PnP-Flow：流模型打破 PnP 迭代瓶颈")
        self.assertFalse(enforce_title_rules(spec, "paper"))

    def test_venue_mismatch_corrected(self):
        """实测 bug：OpenReview 查到 ICML 2026，模型写成 CVPR 2026——代码层强制对齐。"""
        from tests.test_sources_and_filters import make_item

        item = make_item(title="LSGQuant: Layer-Sensitive Quantization")
        item.extra["publication"] = {"accepted": True, "venue": "ICML 2026",
                                     "source": "OpenReview"}
        spec = self._spec("CVPR 2026 | LSGQuant：一步扩散视频超分的分层量化")
        self.assertTrue(enforce_title_rules(spec, "paper", [item]))
        self.assertTrue(spec["title"].startswith("ICML 2026"))
        self.assertNotIn("CVPR", spec["title"])

    def test_venue_correct_untouched(self):
        from tests.test_sources_and_filters import make_item

        item = make_item(title="LSGQuant")
        item.extra["publication"] = {"accepted": True, "venue": "ICML 2026",
                                     "source": "OpenReview"}
        spec = self._spec("ICML 2026 | LSGQuant：一步扩散视频超分")
        self.assertFalse(enforce_title_rules(spec, "paper", [item]))

    def test_venue_featured_not_first_item(self):
        """头条未必是 selected[0]：标题写的是第 2 篇（已收录），校正仍要对齐。"""
        from tests.test_sources_and_filters import make_item

        preprint = make_item(title="HP-PnP-Flow: Hybrid Time Scheduling")
        preprint.extra["publication"] = {"accepted": False, "preprint_only": True}
        accepted = make_item(title="LSGQuant: Layer-Sensitive Quantization")
        accepted.extra["publication"] = {"accepted": True, "venue": "ICML 2026",
                                         "source": "OpenReview"}
        spec = self._spec("CVPR 2026 | LSGQuant：一步扩散视频超分的分层量化")
        self.assertTrue(enforce_title_rules(spec, "paper", [preprint, accepted]))
        self.assertTrue(spec["title"].startswith("ICML 2026"))

    def test_preprint_fabricated_venue_stripped(self):
        """实测 bug：检索确认纯预印本的论文，模型却给它贴了 CVPR 2026 前缀。"""
        from tests.test_sources_and_filters import make_item

        item = make_item(title="DiffHDR: 基于视频扩散模型的 HDR 重建")
        item.extra["publication"] = {"accepted": False, "preprint_only": True}
        spec = self._spec("CVPR 2026 | 过曝欠曝细节丢失？DiffHDR 用扩散先验修复辐射率")
        self.assertTrue(enforce_title_rules(spec, "paper", [item]))
        self.assertNotIn("CVPR", spec["title"])
        self.assertIn("DiffHDR", spec["title"])


if __name__ == "__main__":
    unittest.main()
