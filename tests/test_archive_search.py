"""历史出稿（分页 + 模糊检索）单测。

盯三件事：
1. 全量扫描：超过一页的存档能被完整翻到，页码越界自动收敛；
2. 模糊检索：标题 / 正文 / 板块中文名 / 大小写不敏感都能命中，命中带正文片段；
3. 兼容：旧的 list_articles 接口不变，事件查重等内部调用不受影响。
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import db
from app.config import settings
from app.models import Article

MODULES = ["paper", "industry", "contest"]


class TestArchiveSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._old_db = settings.db_path
        settings.db_path = root / "test.db"
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        db.init_db()
        base = datetime(2026, 11, 14, 12, 0, 0)
        for i in range(27):
            module = MODULES[i % 3]
            title = f"样本稿 第 {i + 1} 期"
            body = f"# {title}\n\n正文 {i + 1}。\n"
            if i == 5:
                title = "Sensors 2021 | PigMS R-CNN"
                body = "# PigMS R-CNN\n\n软非极大值抑制解决群养猪粘连分割。\n"
            db.save_article(Article(
                module=module, title=title, digest=f"摘要 {i + 1}",
                markdown=body, html=f"<p>{title}</p>", status="rendered",
                issue_id=f"{module}-20261114-{i:03d}",
                created_at=base - timedelta(days=i),
            ))

    def tearDown(self):
        settings.db_path = self._old_db
        self.tmp.cleanup()

    def test_full_scan_and_pagination(self):
        r = db.search_articles(page=1, page_size=20)
        self.assertEqual(r["total"], 27)
        self.assertEqual(r["pages"], 2)
        self.assertEqual(len(r["items"]), 20)
        r2 = db.search_articles(page=2, page_size=20)
        self.assertEqual(len(r2["items"]), 7)
        # 时间倒序：第 1 页开头是最新一期，末页收着最早一期
        self.assertIn("第 1 期", r["items"][0]["title"])
        self.assertIn("第 27 期", r2["items"][-1]["title"])
        # 页码越界收敛到最后一页；page_size 上限 200
        self.assertEqual(db.search_articles(page=99, page_size=20)["page"], 2)
        self.assertEqual(db.search_articles(page_size=500)["page_size"], 200)
        # 非法参数不炸
        self.assertEqual(db.search_articles(page="x", page_size="y")["page"], 1)

    def test_fuzzy_match_fields(self):
        # 标题
        r = db.search_articles(query="PigMS")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["items"][0]["title"], "Sensors 2021 | PigMS R-CNN")
        # 正文
        r = db.search_articles(query="软非极大值抑制")
        self.assertEqual(r["total"], 1)
        self.assertIn("软非极大值抑制", r["items"][0]["snippet"])
        # 板块中文名 → 命中该模块全部稿件
        r = db.search_articles(query="学术论文")
        self.assertEqual(r["total"], 9)
        self.assertTrue(all(i["module"] == "paper" for i in r["items"]))
        r = db.search_articles(query="赛事竞赛")
        self.assertEqual(r["total"], 9)
        # 英文关键词不区分大小写（标题里是大写，搜小写也要命中）
        self.assertEqual(db.search_articles(query="pigms")["total"], 1)
        # 期号也能搜
        self.assertEqual(db.search_articles(query="20261114-005")["total"], 1)

    def test_query_resets_within_window(self):
        # 搜索结果不足一页时，page 会被收敛回 1
        r = db.search_articles(query="PigMS", page=5, page_size=20)
        self.assertEqual(r["page"], 1)
        self.assertEqual(r["pages"], 1)

    def test_module_filter_combined_with_query(self):
        r = db.search_articles(module="paper", query="样本稿", page=1, page_size=50)
        self.assertEqual(r["total"], 9)
        self.assertTrue(all(i["module"] == "paper" for i in r["items"]))

    def test_legacy_list_articles_untouched(self):
        rows = db.list_articles(limit=30)
        self.assertEqual(len(rows), 27)
        self.assertEqual(rows[0]["created_at"], "2026-11-14T12:00:00")


if __name__ == "__main__":
    unittest.main()
