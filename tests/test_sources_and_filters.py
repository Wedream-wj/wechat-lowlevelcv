import unittest
from pathlib import Path

from app.config import topics
from app.models import Item
from app.filters.scoring import classify_category, score_item, select


FIXTURE = Path(__file__).parent / "fixtures" / "lowlevelcv_page1.html"


def make_item(**kw) -> Item:
    base = dict(module="paper", title="A super-resolution method", url="https://arxiv.org/abs/1",
                source="test", summary="image restoration for low-level vision", venue="CVPR 2026",
                year="2026")
    base.update(kw)
    return Item(**base)


class TestScoring(unittest.TestCase):
    def test_keyword_and_venue_score(self):
        item = make_item()
        score, hits = score_item(item, topics)
        self.assertGreater(score, 5.0)
        self.assertTrue(any("super-resolution" in h or "restoration" in h for h in hits))

    def test_negative_keyword_penalized(self):
        good = make_item()
        bad = make_item(title="招聘 · 算法实习生（图像方向）")
        s_good, _ = score_item(good, topics)
        s_bad, _ = score_item(bad, topics)
        self.assertLess(s_bad, s_good)

    def test_select_dedupes_similar_titles(self):
        items = [
            make_item(title="Freqformer for demoireing", category="去摩尔纹", score=9, url="u1"),
            make_item(title="Freqformer for demoireing!", category="去摩尔纹", score=8, url="u2"),
            make_item(title="RawJPEG Adapter for compression", category="压缩与编码", score=7, url="u3"),
        ]
        for i in items:
            i.score, i.keywords = score_item(i, topics)
        picked = select(items, topics, "paper", min_score=0, top_n=3, freshness_days=10000,
                        require_keyword_hit=0)
        titles = {p.title for p in picked}
        self.assertEqual(len(titles), 2)  # 相似标题被去重

    def test_classify_category(self):
        self.assertEqual(classify_category("图像超分辨率的新方法", topics), "超分辨率")
        self.assertEqual(classify_category("无关内容 xyz", topics), "")


class TestLowlevelcvParser(unittest.TestCase):
    def test_parse_fixture(self):
        from app.sources.lowlevelcv import parse_papers_html

        items = parse_papers_html(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(items), 9)
        first = items[0]
        self.assertIn("摩尔纹", first.title)
        self.assertTrue(first.image_url.startswith("https://lowlevelcv.com/local_upload"))
        self.assertEqual(first.venue, "European Conference on Computer Vision (ECCV)")
        self.assertIn("arxiv.org", first.url)
        self.assertGreaterEqual(len(first.authors), 2)
        self.assertTrue(first.summary)
        # 至少一条带代码链接
        self.assertTrue(any(i.code_url for i in items))


if __name__ == "__main__":
    unittest.main()
