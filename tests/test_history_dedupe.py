"""历史去重链路测试：相似度比对逻辑 + issues.items_json 归档/读取。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app import db
from app.config import settings
from app.filters.scoring import dedupe_history
from app.models import Article, Item
from tests.test_sources_and_filters import make_item


class TestDedupeHistory(unittest.TestCase):
    def test_blocks_similar_title(self):
        history = ["Freqformer: Efficient Frequency Routing for Image Super-Resolution"]
        same_paper_other_source = make_item(
            title="Freqformer: Efficient Frequency Routing for Image Super-Resolution (CVPR 2025)")
        unrelated = make_item(title="DiffUIR: Rethinking Diffusion for Real-World Restoration")
        kept, dropped = dedupe_history([same_paper_other_source, unrelated], history)
        self.assertEqual([i.title for i in kept], [unrelated.title])
        self.assertEqual(len(dropped), 1)

    def test_blocks_same_baseline_renamed(self):
        history = ["2021阿里云供应链大赛——需求预测及单级库存优化 baseline"]
        renamed = make_item(title="2021阿里云供应链大赛-需求预测及单级库存优化")
        kept, dropped = dedupe_history([renamed], history)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(dropped), 1)

    def test_empty_history_is_noop(self):
        items = [make_item(title="Some Paper")]
        kept, dropped = dedupe_history(items, [])
        self.assertEqual(kept, items)
        self.assertEqual(dropped, [])


class TestHistoryArchive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.db_path
        settings.db_path = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self):
        settings.db_path = self._old_db
        self._tmp.cleanup()

    def test_save_then_read_titles(self):
        item = make_item(title="History Archive Probe Paper")
        article = Article(module="paper", title="一期", subtitle="", digest="",
                          markdown="m", html="h", cover_path="", items=[item],
                          created_at=datetime.now(), issue_id="probe-1")
        db.save_article(article)
        titles = db.recent_history_titles("paper", days=90)
        self.assertIn(item.title, titles)
        # 其它模块读不到这期
        self.assertNotIn(item.title, db.recent_history_titles("contest", days=90))

    def test_old_issues_out_of_window(self):
        item = make_item(title="Ancient Issue Paper")
        article = Article(module="paper", title="旧期", subtitle="", digest="",
                          markdown="m", html="h", cover_path="", items=[item],
                          created_at=datetime(2020, 1, 1), issue_id="probe-old")
        db.save_article(article)
        self.assertNotIn(item.title, db.recent_history_titles("paper", days=90))


if __name__ == "__main__":
    unittest.main()
