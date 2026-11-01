"""业界进展「选题查重 + 主题强收敛 + 实事求是」的回归测试。

三组事故驱动：
1. 同一天出了两篇 OpenAI 融资稿（「砍掉三分之一：300 亿变 200 亿」与
   「砍掉 100 亿美元，估值降至 1.25 万亿」）——标题相似度拦不住，必须按事件判重；
2. 商业快讯正文里夹了「ECCV 2026 论文 SparkVSR 开源，仓库 745 stars」这种无关学术信息；
3. 编造素材里没有的金额/比例。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db
from app.config import settings
from app.filters import events as ev
from app.llm.prompts import (
    INDUSTRY_FACT_RETRY,
    INDUSTRY_PROMPT,
    unsupported_industry_numbers,
)
from app.models import Article, Item
from app.spec_tools import normalize_wording, prune_industry_offtopic

OPENAI_A = "OpenAI 融资目标砍掉三分之一：300 亿变 200 亿"
OPENAI_B = "OpenAI 融资目标砍掉 100 亿美元，估值降至 1.25 万亿"


def industry(title: str, summary: str = "", source: str = "量子位", score: float = 10.0,
             **extra) -> Item:
    return Item(module="industry", title=title, url=f"https://example.com/{abs(hash(title))}",
                source=source, summary=summary, score=score, extra=dict(extra))


class TestEventSignature(unittest.TestCase):
    def test_same_financing_event_different_numbers(self):
        a, b = ev.event_signature(OPENAI_A), ev.event_signature(OPENAI_B)
        self.assertEqual(a.etype, "融资")
        self.assertTrue(ev.same_event(a, b))

    def test_same_company_different_event_type(self):
        a = ev.event_signature(OPENAI_A)
        b = ev.event_signature("OpenAI 发布 GPT-6，上下文窗口翻倍")
        self.assertFalse(ev.same_event(a, b))

    def test_different_company_same_event_type(self):
        a = ev.event_signature(OPENAI_A)
        b = ev.event_signature("Anthropic 完成 60 亿美元新一轮融资，估值 900 亿")
        self.assertFalse(ev.same_event(a, b))

    def test_different_product_launches_are_different_events(self):
        a = ev.event_signature("OpenAI 发布 GPT-6")
        b = ev.event_signature("OpenAI 发布 Sora 3")
        self.assertFalse(ev.same_event(a, b))

    def test_same_product_launch_reworded(self):
        a = ev.event_signature("OpenAI 发布 GPT-6")
        b = ev.event_signature("OpenAI 上线 GPT-6 预览版，推理成本降低 40%")
        self.assertTrue(ev.same_event(a, b))

    def test_chip_event_same_entity(self):
        a = ev.event_signature("Momenta 姐妹公司首秀：700TOPS 芯片 Day 0 跑通世界模型")
        b = ev.event_signature("Momenta 新芯片 X7 流片成功")
        self.assertEqual(a.etype, "芯片算力")
        self.assertTrue(ev.same_event(a, b))


class TestDedupeEvents(unittest.TestCase):
    def _entry(self, title: str, days_ago: float = 1.0) -> dict:
        when = datetime.now() - timedelta(days=days_ago)
        return {"title": title, "summary": "", "url": "", "source": "量子位",
                "created_at": when.isoformat()}

    def test_blocks_same_event_from_history(self):
        items = [industry(OPENAI_B), industry("Momenta 新芯片 X7 流片成功")]
        kept, dropped = ev.dedupe_events(items, [self._entry(OPENAI_A)], days=14)
        self.assertEqual([i.title for i in kept], ["Momenta 新芯片 X7 流片成功"])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["event"], "openai / 融资")
        self.assertEqual(dropped[0]["matched_title"], OPENAI_A)

    def test_window_expiry(self):
        items = [industry(OPENAI_B)]
        kept, dropped = ev.dedupe_events(items, [self._entry(OPENAI_A, days_ago=30)], days=14)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_no_history_is_noop(self):
        items = [industry(OPENAI_B)]
        kept, dropped = ev.dedupe_events(items, [])
        self.assertEqual(kept, items)
        self.assertEqual(dropped, [])


class TestCollapseSameEvent(unittest.TestCase):
    def test_merges_duplicates_and_keeps_sources(self):
        main = industry(OPENAI_B, summary="融资目标下调至 200 亿美元", score=12)
        dup = industry(OPENAI_A, summary="砍掉三分之一", score=9)
        other = industry("Momenta 新芯片 X7 流片成功", score=11)
        kept, merged = ev.collapse_same_event([main, dup, other])
        self.assertEqual(len(kept), 2)
        self.assertIn(main.title, [i.title for i in kept])
        self.assertNotIn(dup.title, [i.title for i in kept])
        sources = main.extra["same_event_sources"]
        self.assertEqual(sources[0]["title"], OPENAI_A)
        self.assertIn(main.title, merged)

    def test_no_merge_for_unrelated(self):
        a = industry(OPENAI_A)
        b = industry("Runway 开源世界动作模型 Praxis-1")
        kept, merged = ev.collapse_same_event([a, b])
        self.assertEqual(len(kept), 2)
        self.assertEqual(merged, {})

    def test_pick_subject_prefers_full_text(self):
        brief = industry("OpenAI 融资目标再调整", summary="短摘要", score=15)
        full = industry("OpenAI 融资目标下调至 200 亿美元", summary="短", score=12,
                        full_text="据披露，本轮融资目标从 300 亿美元下调至约 200 亿美元……")
        self.assertIs(ev.pick_subject([brief, full]), full)
        # 都没有正文时退回分数第一名
        self.assertIs(ev.pick_subject([brief, industry("另一条", score=3)]), brief)


class TestPruneIndustryOfftopic(unittest.TestCase):
    def _spec(self) -> dict:
        return {
            "title": "OpenAI 融资目标砍掉 100 亿美元，估值降至 1.25 万亿",
            "lead": ["OpenAI 把融资目标从 300 亿美元下调至约 200 亿美元，估值降至 1.25 万亿。",
                     "同期还有一家公司开源了世界模型。"],
            "sections": [
                {"type": "meta_card", "label": "INDUSTRY SNAPSHOT", "rows": [
                    ["事件", "OpenAI 下调融资目标至约 200 亿美元"],
                    ["时间", "2026-10-30"],
                    ["参考", "ECCV 2026 论文 SparkVSR 开源"],
                ]},
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text": "OpenAI 将融资目标从 300 亿美元下调至约 200 亿美元。"},
                {"type": "bullets", "items": [
                    "ECCV 2026 论文 SparkVSR 开源，做交互式视频超分，仓库 745 stars，最近更新 2026-08-03。",
                    "上述两条 OpenAI 消息均为官方口径，最终条款以交割为准。",
                ]},
                {"type": "h2", "text": "02 | 技术上看，真正的变化在哪"},
                {"type": "p", "text": "资本对通用大模型的定价正在回撤。"},
                {"type": "h2", "text": "03 | 对读者意味着什么"},
                {"type": "p", "text": "西湖数智 Awomo 拿下超亿元融资，说明物理世界模型在加速。"},
                {"type": "h2", "text": "04 | 写在最后"},
                {"type": "p", "text": "算力预算的分配方式变了。"},
            ],
            "takeaway": "融资条件收紧，交付节奏被迫调整。",
            "sources": [
                {"name": "OpenAI 融资目标下调", "url": "https://example.com/a"},
                {"name": "taco-group/SparkVSR", "url": "https://github.com/taco-group/SparkVSR"},
            ],
        }

    def setUp(self):
        self.main = industry(OPENAI_B, summary="融资目标下调至 200 亿美元")
        self.rivals = [industry("西湖世界模型研究中心成立，Awomo 融资超亿元")]

    def test_removes_paper_and_repo_noise(self):
        spec = self._spec()
        stats = prune_industry_offtopic(spec, self.main, self.rivals)
        body = str(spec)
        self.assertNotIn("SparkVSR", body)
        self.assertNotIn("745 stars", body)
        self.assertNotIn("github.com/taco-group", body)
        self.assertGreaterEqual(stats["noise"], 1)
        self.assertGreaterEqual(stats["sources"], 1)
        self.assertGreaterEqual(stats["cards"], 1)

    def test_removes_second_company_news(self):
        spec = self._spec()
        prune_industry_offtopic(spec, self.main, self.rivals)
        self.assertNotIn("Awomo", str(spec))
        self.assertNotIn("同期还有", str(spec["lead"][1]) if len(spec["lead"]) > 1 else "")

    def test_clean_spec_untouched(self):
        spec = {
            "title": "OpenAI 融资目标下调至约 200 亿美元",
            "lead": ["融资目标从 300 亿美元下调至约 200 亿美元，估值降至 1.25 万亿。"],
            "sections": [
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text": "OpenAI 将融资目标从 300 亿美元下调至约 200 亿美元。"},
            ],
            "sources": [{"name": "OpenAI 融资", "url": "https://example.com/a"}],
        }
        before = str(spec)
        stats = prune_industry_offtopic(spec, self.main, self.rivals)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(str(spec), before)

    def test_rival_from_summary_without_marker(self):
        """串题句不一定带「同期/此外」，也不一定在标题里点名第二家公司。"""
        rival = industry("西湖世界模型研究中心成立",
                         summary="该中心依托 AutoLab，孵化公司 Awomo 完成超亿元融资")
        spec = {
            "title": "t", "lead": [],
            "sections": [
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text": "资本对通用大模型的定价正在回撤。"
                                        "AutoLab 早期用像素生成证明理解，2024 年拿下自动驾驶赛道冠军。"},
            ],
            "sources": [],
        }
        prune_industry_offtopic(spec, self.main, [rival])
        body = str(spec)
        self.assertNotIn("AutoLab", body)
        self.assertIn("资本对通用大模型的定价正在回撤", body)

    def test_empty_section_removed(self):
        spec = {
            "title": "t",
            "lead": [],
            "sections": [
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text": "OpenAI 将融资目标下调。"},
                {"type": "h2", "text": "02 | 顺带看看"},
                {"type": "bullets", "items": ["ECCV 2026 论文 SparkVSR 开源，仓库 745 stars。"]},
            ],
            "sources": [],
        }
        stats = prune_industry_offtopic(spec, self.main, self.rivals)
        heads = [b["text"] for b in spec["sections"] if b.get("type") == "h2"]
        self.assertEqual(heads, ["01 | 发生了什么"])
        self.assertEqual(stats["sections"], 1)


class TestIndustryNumbers(unittest.TestCase):
    def setUp(self):
        self.item = industry(
            OPENAI_B,
            summary="OpenAI 把融资目标从 300 亿美元下调至约 200 亿美元，估值降至 1.25 万亿",
            full_text="OpenAI 将本轮融资目标从 300 亿美元下调至约 200 亿美元，"
                      "目前已取得约 110 亿美元投资承诺，占目标的 55%。",
        )

    def _spec(self, text: str) -> dict:
        return {"title": "t", "lead": [text], "sections": [], "takeaway": ""}

    def test_material_amounts_pass(self):
        spec = self._spec("融资目标从 300 亿美元下调至约 200 亿美元，已获 110 亿美元承诺，占 55%。")
        self.assertEqual(unsupported_industry_numbers(spec, [self.item]), [])

    def test_derived_difference_is_allowed(self):
        # 100 亿美元 = 300 亿 - 200 亿：同一句素材里的合法换算，不算编造
        spec = self._spec("融资目标砍掉 100 亿美元。")
        self.assertEqual(unsupported_industry_numbers(spec, [self.item]), [])

    def test_fabricated_amount_flagged(self):
        spec = self._spec("融资目标砍掉 175 亿美元，估值降至 1.4 万亿。")
        bad = unsupported_industry_numbers(spec, [self.item])
        self.assertIn("175", bad)

    def test_year_and_range_not_flagged(self):
        spec = self._spec("到 2026 年为止，行业还会经历 6–18 个月的调整期。")
        self.assertEqual(unsupported_industry_numbers(spec, [self.item]), [])


class TestPromptRules(unittest.TestCase):
    def test_single_event_ironclad(self):
        for kw in ("单一事件铁律", "全篇只写一个核心事件", "其它公司的动态",
                   "严禁跨界夹杂学术与代码库信息", "745 stars", "同期还有"):
            self.assertIn(kw, INDUSTRY_PROMPT)

    def test_truthfulness_rules(self):
        for kw in ("逐字找到", "据传", "知情人士透露", "禁止为凑字数注水"):
            self.assertIn(kw, INDUSTRY_PROMPT)
        self.assertIn("找不到", INDUSTRY_FACT_RETRY)

    def test_multitopic_instructions_removed(self):
        # 旧的「一期多条动态，其余用 bullets 带过」是串题的根源，必须删干净
        self.assertNotIn("其余用 bullets 各一句带过", INDUSTRY_PROMPT)
        self.assertNotIn("多赛道混合的一期", INDUSTRY_PROMPT)

    def test_title_self_audit(self):
        for kw in ("先自审，再动笔", "可读闸", "黑话闸", "空话闸"):
            self.assertIn(kw, INDUSTRY_PROMPT)


class TestHypeScrub(unittest.TestCase):
    def test_industry_hype_words_replaced(self):
        spec = {"lead": ["这条消息炸裂了整个行业，OpenAI 彻底改变世界。"],
                "sections": [{"type": "p", "text": "它碾压了所有对手。"}], "takeaway": ""}
        hits = normalize_wording(spec, "industry")
        self.assertGreater(hits, 0)
        blob = str(spec)
        for bad in ("炸裂", "彻底改变世界", "碾压"):
            self.assertNotIn(bad, blob)


class TestHistoryEntries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.db_path
        settings.db_path = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self):
        settings.db_path = self._old_db
        self._tmp.cleanup()

    def test_entries_carry_summary_and_time(self):
        item = industry(OPENAI_A, summary="融资目标从 300 亿美元砍到 200 亿美元")
        article = Article(module="industry", title="一期", items=[item],
                          created_at=datetime.now(), issue_id="probe-ind")
        db.save_article(article)
        entries = db.recent_history_entries("industry", days=30)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], OPENAI_A)
        self.assertIn("300 亿美元", entries[0]["summary"])
        self.assertTrue(entries[0]["created_at"])
        # 端到端：读出来的历史可以直接喂给事件查重
        kept, dropped = ev.dedupe_events([industry(OPENAI_B)], entries, days=14)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)


if __name__ == "__main__":
    unittest.main()


class TestIndustryTitleAudit(unittest.TestCase):
    """标题自审：产业稿的问题不是黑话，是「绕」。"""

    def setUp(self):
        from app.llm.title_audit import industry_title_issues, industry_title_fallback

        self.issues = industry_title_issues
        self.fallback = industry_title_fallback
        self.item = industry("刚刚，DeepSeek重磅开源：Agent底座架构！",
                             summary="Super-agent 采用 Router/Worker 双层架构")

    def test_twisted_sentence_flagged(self):
        bad = "DeepSeek 开源 Agent 底座：把人类注意力从 3 到 5 个会话里解出来"
        found = " ".join(self.issues(bad, self.item))
        self.assertIn("绕弯", found)

    def test_marketing_words_flagged(self):
        found = " ".join(self.issues("某公司重磅官宣，行业迎来新变局", self.item))
        self.assertIn("空话", found)

    def test_missing_subject_flagged(self):
        found = " ".join(self.issues("开源一个 Agent 底座，任务跨平台连续推进", self.item))
        self.assertIn("主体", found)

    def test_good_title_passes(self):
        good = "DeepSeek 开源 Agent 底座：一个任务跨 IM 与 GitHub 连续推进"
        self.assertEqual(self.issues(good, self.item), [])

    def test_fallback_strips_media_hype(self):
        title = self.fallback(self.item)
        self.assertNotIn("重磅", title)
        self.assertFalse(title.startswith("刚刚"))
        self.assertEqual(self.issues(title, self.item), [])

    def test_width_counts_latin_as_one_slot(self):
        from app.llm.title_audit import title_width

        self.assertLess(title_width("DeepSeek 开源 Agent 底座"),
                        len("DeepSeek 开源 Agent 底座"))
