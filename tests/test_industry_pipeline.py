"""业界流水线的端到端闸门测试：重复事件必须死在选题阶段，不许进 Prompt。

单元测试覆盖了判重函数本身，这里验证的是**接线**：如果哪天有人把 pipeline 里的
industry 分支删了，或者把 selected 又放回多条，这条用例会直接红。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import db, pipeline
from app.config import settings
from app.llm.mock import mock_spec
from app.models import Article, Item

OPENAI_A = "OpenAI 融资目标砍掉三分之一：300 亿变 200 亿"
OPENAI_B = "OpenAI 融资目标砍掉 100 亿美元，估值降至 1.25 万亿"


def _item(title: str, summary: str = "", score: float = 12.0) -> Item:
    return Item(module="industry", title=title, url=f"https://example.com/{abs(hash(title))}",
                source="量子位", summary=summary, score=score, keywords=["大模型", "融资"])


class _FakeLLM:
    """不走网络：直接把素材拼成 mock 稿，并记录「模型实际收到了几条素材」。"""

    enabled = False
    mode = "mock"

    seen: list[list[str]] = []

    def __init__(self, *args, **kwargs) -> None:  # 与 LLMClient() 的调用方式保持一致
        pass

    async def write(self, module, items, figures, date_str, on_progress=None,
                    extra_instruction="", variant=None):
        _FakeLLM.seen.append([i.title for i in items])
        return mock_spec(module, items, figures)

    async def rewrite_industry_title(self, spec, item, issues):
        return None


class TestIndustryPipelineGates(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.db_path
        settings.db_path = Path(self._tmp.name) / "test.db"
        db.init_db()
        _FakeLLM.seen = []
        # 历史出稿：今天已经写过一次 OpenAI 融资（A 口径）
        db.save_article(Article(module="industry", title=OPENAI_A, items=[_item(OPENAI_A)],
                                created_at=datetime.now(), issue_id="hist-openai"))

    def tearDown(self):
        settings.db_path = self._old_db
        self._tmp.cleanup()

    async def _run(self, pool: list[Item]):
        async def fake_collect(module, progress=None):
            return list(pool), []

        async def fake_pick(*args, **kwargs):
            return None

        with patch.object(pipeline, "collect", fake_collect), \
                patch.object(pipeline, "LLMClient", _FakeLLM), \
                patch.object(pipeline.cover_select, "pick_cover", fake_pick):
            return await pipeline.run_module("industry", demo=True)

    async def test_duplicate_event_blocked_before_prompt(self):
        result = await self._run([
            _item(OPENAI_B, "融资目标从 300 亿美元下调至约 200 亿美元"),
            _item("Momenta 芯片公司发布 X7，700TOPS 车规算力"),
        ])
        self.assertIsNotNone(result.article)
        # 模型只收到 1 条素材，而且是没写过的那件事
        self.assertTrue(_FakeLLM.seen)
        self.assertEqual(len(_FakeLLM.seen[0]), 1)
        self.assertIn("Momenta", _FakeLLM.seen[0][0])
        self.assertNotEqual(result.article.title, OPENAI_A)
        self.assertNotIn("OpenAI", result.article.title)

    async def test_same_batch_duplicates_merged_into_one(self):
        result = await self._run([
            _item("Momenta 芯片公司发布 X7，700TOPS 车规算力", score=12.0),
            _item("Momenta 新芯片 X7 流片成功，NPU 利用率 98.7%", score=11.0),
        ])
        self.assertIsNotNone(result.article)
        self.assertEqual(len(_FakeLLM.seen[0]), 1)

    async def test_all_candidates_blocked_reports_reason(self):
        result = await self._run([_item(OPENAI_B, "融资目标下调至约 200 亿美元")])
        self.assertIsNone(result.article)
        blob = str(result.errors)
        self.assertIn("筛选后为 0", blob)
        self.assertIn("历史出稿相似挡 1", blob)


if __name__ == "__main__":
    unittest.main()
