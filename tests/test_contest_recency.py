"""赛事时效闸门（Time-Validity Gatekeeper）测试。

核心回归：实测把 2019-11-18 截止的「FlyAI 超分挑战赛」写成了推文。
根因有两条，这里逐条钉住：
1. `parse_deadline` 的年份下限卡在 2020，2019 的日期被解析成 None，
   于是「过期」退化成了「没有截止线索」；
2. FlyAI 源给所有条目打 `evergreen + confirmed_active`，`filter_active` 直接放行。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.filters import recency
from app.models import Item
from app.sources import contest_feeds
from app.sources import competitions as comp

NOW = datetime(2026, 11, 11, 12, 0, 0)


def _contest(title="X 挑战赛", **extra) -> Item:
    base = {"platform": "FlyAI", "deadline": "", "entry_url": "https://e.com"}
    base.update(extra)
    return Item(module="contest", title=title, url="https://e.com", source="FlyAI",
                summary=str(base.pop("summary", "")), extra=base)


class TestDateParsing(unittest.TestCase):
    def test_old_deadline_no_longer_silently_dropped(self):
        # 回归：2019 的日期必须能解析出来，否则它会被当成「无截止线索」而放行
        dt = comp.parse_deadline("2019-11-18")
        self.assertIsNotNone(dt)
        self.assertEqual((dt.year, dt.month, dt.day), (2019, 11, 18))

    def test_free_text_formats(self):
        self.assertEqual(recency.parse_date("报名截止：2026-11-18 23:59").day, 18)
        self.assertEqual(recency.parse_date("2026年12月5日").month, 12)
        self.assertEqual(recency.parse_date("March 22, 2026").month, 3)
        self.assertEqual(recency.parse_date("22 December 2026").day, 22)
        self.assertIsNone(recency.parse_date("没有日期"))

    def test_aware_iso_becomes_naive_local(self):
        # 各源混着给 naive / aware ISO，不归一化会在比较时抛 TypeError
        dt = recency.parse_date("2026-12-01T08:00:00+00:00")
        self.assertIsNotNone(dt)
        self.assertIsNone(dt.tzinfo)


class TestExpiryVeto(unittest.TestCase):
    def test_flyai_2019_vetoed_even_with_active_flags(self):
        item = _contest("FlyAI 超分挑战赛", deadline="2019-11-18 23:59:59",
                        evergreen=True, confirmed_active=True)
        v = recency.evaluate(item, NOW)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, "expired")
        self.assertIn("2019-11-18", v.reason)

    def test_expired_by_end_field(self):
        item = _contest("老比赛", end="2024-06-01")
        self.assertEqual(recency.evaluate(item, NOW).status, "expired")

    def test_title_year_2019_is_historical(self):
        # 只有标题年份可查：2019 年的场次不该被写成「正在报名」
        item = _contest("FlyAI 2019 超分挑战赛", evergreen=True, confirmed_active=True)
        v = recency.evaluate(item, NOW)
        self.assertFalse(v.ok)
        self.assertIn(v.status, ("historical", "expired"))

    def test_past_page_date_vetoes_even_with_active_flag(self):
        # 抓取阶段没有日期，官方页核验时才发现「报名截止 2019-11-18」
        item = _contest("老比赛", confirmed_active=True)
        detail = {"page_excerpt": "赛事时间 2019-11-01 至 2019-11-18，报名已截止",
                  "timeline": []}
        v = recency.evaluate(item, NOW, detail=detail)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, "expired")

    def test_evergreen_without_evidence_vetoed(self):
        # 「长期开放」不再等于「此刻在办」——这正是 2019 赛事混进来的那条缝
        v = recency.evaluate(_contest("图像纹理分类", evergreen=True), NOW)
        self.assertFalse(v.ok)
        self.assertEqual(v.status, "undated")


class TestActiveKept(unittest.TestCase):
    def test_future_deadline_active_with_days_left(self):
        deadline = (NOW + timedelta(days=18)).strftime("%Y-%m-%d")
        item = _contest("NTIRE 2026", deadline=deadline)
        v = recency.evaluate(item, NOW)
        self.assertTrue(v.ok)
        self.assertEqual(v.status, "active")
        self.assertEqual(v.days_left, 18)

    def test_deadline_too_far_is_windowed(self):
        deadline = (NOW + timedelta(days=400)).strftime("%Y-%m-%d")
        self.assertEqual(recency.evaluate(_contest(deadline=deadline), NOW).status, "windowed")

    def test_api_active_flag_without_dates_kept_unverified(self):
        item = _contest("Codabench 新场次", confirmed_active=True, origin="codabench")
        v = recency.evaluate(item, NOW)
        self.assertTrue(v.ok)
        self.assertFalse(v.verified)

    def test_title_future_year_counts_as_active(self):
        v = recency.evaluate(_contest("NTIRE 2026 图像复原挑战赛"), NOW)
        self.assertTrue(v.ok)

    def test_deadline_today_still_open(self):
        # 「今天 23:59 截止」不能中午就判过期：只有日期没时刻的，补到当天 23:59:59
        item = _contest("今天截止的比赛", deadline="2026-11-11 23:59:59")
        v = recency.evaluate(item, NOW)
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.days_left, 0)

    def test_learning_resource_skips_gate(self):
        item = _contest("往届 baseline：超分", is_learning_resource=True)
        self.assertEqual(recency.evaluate(item, NOW).status, "learning")


class TestGate(unittest.TestCase):
    def test_gate_keeps_live_and_reports_expired(self):
        live = _contest("活的比赛", deadline=(NOW + timedelta(days=20)).strftime("%Y-%m-%d"))
        dead = _contest("2019 老年赛", deadline="2019-11-18")
        stale = _contest("FlyAI 图像纹理分类", evergreen=True)
        kept, rejected = recency.gate([live, dead, stale], NOW)
        self.assertEqual([i.title for i in kept], ["活的比赛"])
        self.assertEqual({r["status"] for r in rejected}, {"expired", "undated"})
        self.assertEqual(kept[0].extra["days_left"], 20)
        self.assertEqual(kept[0].extra["recency"]["status"], "active")
        self.assertGreater(kept[0].score, 0)

    def test_gate_reject_line_is_readable(self):
        _, rejected = recency.gate([_contest("FlyAI 超分挑战赛", deadline="2019-11-18")], NOW)
        line = recency.describe_rejects(rejected)
        self.assertIn("FlyAI 超分挑战赛", line)
        self.assertIn("2019-11-18", line)

    def test_filter_active_vetoes_expired_active_flag(self):
        # 回归用例：这条以前必然通过（confirmed_active 直接放行）
        dead = _contest("FlyAI 超分挑战赛", deadline="2019-11-18 23:59:59",
                        evergreen=True, confirmed_active=True)
        self.assertEqual(comp.filter_active([dead]), [])


class TestSourcesNoLongerAssertActive(unittest.TestCase):
    HTML = """
    <html><body>
      <a href="https://www.flyai.com/d/Super-Resolution">
        <div class="compatibleIE">
          <div class="pro_title"><div class="name"><h2>超分挑战赛</h2><p class="noReg">可报名</p></div>
          <div class="bonus"><p>奖金池 ¥ 50,000</p></div></div>
          <div class="describe">报名截止 2019-11-18，图像四倍超分。</div>
        </div>
      </a>
    </body></html>
    """

    def test_flyai_does_not_claim_confirmed_active(self):
        items = contest_feeds.parse_flyai(self.HTML)
        self.assertEqual(len(items), 1)
        self.assertNotIn("confirmed_active", items[0].extra)
        self.assertTrue(items[0].extra["evergreen"])

    def test_flyai_2019_card_is_vetoed_by_gate(self):
        items = contest_feeds.parse_flyai(self.HTML)
        kept, rejected = recency.gate(items, NOW)
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0]["status"], "expired")


class TestPromptAndSpecGuards(unittest.TestCase):
    def test_prompt_has_time_validity_rule(self):
        from app.llm.prompts import CONTEST_PROMPT

        self.assertIn("时效铁律", CONTEST_PROMPT)
        self.assertIn("逐字照抄", CONTEST_PROMPT)
        self.assertIn("2023 年及更早的往届赛事一律不写", CONTEST_PROMPT)

    def test_user_prompt_carries_today(self):
        from app.llm.prompts import build_user_prompt

        text = build_user_prompt("contest", [_contest("X 挑战赛")], {}, "2026.11.11")
        self.assertIn("2026.11.11", text)

    def test_stale_claims_finds_past_deadline_in_spec(self):
        spec = {"title": "某赛事", "digest": "", "lead": ["报名截止 2024-03-01，提交 2024-04-01。"],
                "sections": [{"type": "paragraph", "text": "赛程紧凑。"}]}
        hits = recency.stale_claims(spec, NOW)
        self.assertTrue(hits)
        self.assertIn("2024-03-01", hits[0])

    def test_stale_claims_ignores_future_deadline(self):
        spec = {"lead": ["报名 2026-12-05 截止。"], "sections": []}
        self.assertEqual(recency.stale_claims(spec, NOW), [])


class TestPipelineVeto(unittest.IsolatedAsyncioTestCase):
    """接线测试：候选全过期时，流水线必须**不出稿**并说清原因。

    这条防的是「闸门写了但没接上」——单测全绿、线上照样发旧赛事，是最难查的一类事故。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from app import db
        from app.config import settings

        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.db_path
        settings.db_path = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self):
        from app.config import settings

        settings.db_path = self._old_db
        self._tmp.cleanup()

    async def _run(self, pool):
        from unittest.mock import patch

        from app import pipeline

        async def fake_collect(module, progress=None):
            return list(pool), []

        async def fake_detail(client, item):
            return {"fetched": True, "page_excerpt": "", "timeline": [],
                    "registration_closed": False}

        with patch.object(pipeline, "collect", fake_collect), \
                patch.object(pipeline._competitions, "fetch_contest_detail", fake_detail):
            return await pipeline.run_module("contest", demo=True)

    async def test_expired_candidate_blocks_issue(self):
        # 抓取阶段就被打了 confirmed_active，但截止时间是 2019（历史原件）
        dead = Item(module="contest", title="FlyAI 超分挑战赛", url="https://e.com",
                    source="FlyAI", summary="图像四倍超分。", score=20.0,
                    keywords=["图像超分"],
                    extra={"platform": "FlyAI", "deadline": "2019-11-18 23:59:59",
                           "evergreen": True, "confirmed_active": True})
        result = await self._run([dead])
        self.assertIsNone(result.article)
        blob = str(result.errors)
        self.assertIn("时效闸门", blob)
        self.assertIn("2019-11-18", blob)


if __name__ == "__main__":
    unittest.main()
