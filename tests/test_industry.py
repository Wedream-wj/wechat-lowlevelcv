"""业界模块升级的回归测试：中文媒体解析、泛 AI 词表、信息卡标签。"""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import topics
from app.filters.scoring import classify_category, score_item
from app.llm.prompts import INDUSTRY_PROMPT, industry_lead_is_flat, _item_payload
from app.llm.mock import mock_spec
from app.models import Item
from app.render import wechat
from app.render.markdown import spec_to_markdown
from app.sources import chinese_media as cm

FIXTURES = Path(__file__).parent / "fixtures"


class TestChineseTime(unittest.TestCase):
    NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

    def test_minutes(self):
        self.assertEqual(cm.parse_cn_time("5分钟前", self.NOW), self.NOW - timedelta(minutes=5))

    def test_hours(self):
        self.assertEqual(cm.parse_cn_time("3小时前", self.NOW), self.NOW - timedelta(hours=3))

    def test_just_now(self):
        self.assertEqual(cm.parse_cn_time("刚刚", self.NOW), self.NOW)

    def test_yesterday(self):
        self.assertEqual(cm.parse_cn_time("昨天", self.NOW), self.NOW - timedelta(days=1))

    def test_short_and_full_date(self):
        self.assertEqual(cm.parse_cn_time("10-25", self.NOW).day, 25)
        self.assertEqual(cm.parse_cn_time("2026-10-25 20:57:51", self.NOW).day, 25)

    def test_unknown(self):
        self.assertIsNone(cm.parse_cn_time("", self.NOW))
        self.assertIsNone(cm.parse_cn_time("前天晚间", self.NOW))


class TestQbitaiParse(unittest.TestCase):
    def setUp(self):
        self.items = cm.parse_qbitai((FIXTURES / "qbitai_home.html").read_text(encoding="utf-8"))

    def test_fields(self):
        self.assertEqual(len(self.items), 2)
        first = self.items[0]
        self.assertIn("人形机器人", first.title)
        self.assertTrue(first.url.startswith("https://www.qbitai.com/2026/10/"))
        self.assertIn("全套图纸开源", first.summary)
        self.assertTrue(first.image_url.startswith("https://i.qbitai.com/"))
        self.assertEqual(first.source, "量子位")
        self.assertIsNotNone(first.published_at)

    def test_industry_category_and_score(self):
        # 产业语境下「人形机器人」应落到具身分类，而不是返回空串
        self.assertEqual(self.items[0].category, "具身智能与机器人")
        self.assertGreater(self.items[0].score, 10)
        self.assertTrue(self.items[0].keywords)


class TestHackcvParse(unittest.TestCase):
    BRIEF = "https://hackcv.com/posts/research-brief-2026-10-27/"

    def setUp(self):
        html = (FIXTURES / "hackcv_brief.html").read_text(encoding="utf-8")
        self.items = cm.parse_hackcv_brief(html, self.BRIEF)

    def test_breaks_digest_into_items(self):
        self.assertEqual(len(self.items), 4)
        titles = [i.title for i in self.items]
        self.assertIn("OpenAI 发布 GPT-6.1 Terra", titles)
        # 简报里的编号前缀要清掉，否则标题会被写成「1. xxx」
        self.assertFalse(any(t[:1].isdigit() for t in titles))

    def test_skips_arxiv_section(self):
        self.assertFalse(any("MTA-Zero" in i.title for i in self.items))

    def test_summary_markers_cleaned(self):
        # 「简介 ：」这类字段标签会被模型复述，解析阶段就清掉
        self.assertFalse(any(i.summary.startswith("简介") for i in self.items))
        self.assertTrue(any("｜热度：" in i.summary or "｜推荐理由：" in i.summary
                            for i in self.items))

    def test_external_link_preferred(self):
        repo = next(i for i in self.items if i.title.startswith("cheaper-inference"))
        self.assertTrue(repo.url.startswith("https://github.com/"))
        news = next(i for i in self.items if "GPT-6.1" in i.title)
        self.assertTrue(news.url.startswith(self.BRIEF + "#"))
        self.assertIsNotNone(news.published_at)

    def test_brief_urls_sorted(self):
        html = """<html><body>
        <a href="/posts/research-brief-week43-2026-10-25/">周报</a>
        <a href="/posts/research-brief-2026-10-25/">25</a>
        <a href="/posts/research-brief-2026-10-27/">27</a>
        <a href="/posts/research-brief-2026-10-26/">26</a>
        </body></html>"""
        urls = cm.hackcv_brief_urls(html, limit=2)
        self.assertEqual(len(urls), 2)
        self.assertIn("2026-10-27", urls[0])
        self.assertIn("2026-10-26", urls[1])


class TestIndustryScope(unittest.TestCase):
    def _item(self, title: str, module: str = "industry") -> Item:
        return Item(module=module, title=title, url="u", source="s",
                    published_at=datetime.now(timezone.utc), summary="")

    def test_generic_ai_news_scores(self):
        item = self._item("英伟达发布新一代 GPU，单卡算力翻倍")
        score, hits = score_item(item, topics)
        self.assertGreaterEqual(score, 5)
        self.assertIn("算力", hits)

    def test_industry_beats_paper_on_same_news(self):
        title = "MiniMax 深度绑定中国电信，大模型政企赛道开打"
        industry_score, _ = score_item(self._item(title), topics)
        paper_score, _ = score_item(self._item(title, "paper"), topics)
        self.assertGreater(industry_score, paper_score)

    def test_industry_category_not_low_level(self):
        text = "某公司发布大模型与智能体"
        self.assertEqual(classify_category(text, topics, "industry"), "大模型与 Agent")
        # 默认词表是底层视觉任务分类，不该把产业新闻标成「生成式复原」
        self.assertNotEqual(classify_category(text, topics), "大模型与 Agent")

    def test_payload_carries_published_and_full_text(self):
        item = self._item("某具身智能公司完成 5 亿融资")
        item.extra["full_text"] = "正文事实一。正文事实二。"
        payload = _item_payload(1, item, [])
        self.assertEqual(payload["full_text"], "正文事实一。正文事实二。")
        self.assertEqual(payload["published"], datetime.now(timezone.utc).strftime("%Y-%m-%d"))


class TestIndustryHook(unittest.TestCase):
    def test_flat_openers_detected(self):
        for opener in ("近日，OpenAI 发布了新模型", "随着大模型价格战继续",
                       "据悉，某公司完成新一轮融资", "OpenAI 宣布推出新工具"):
            self.assertTrue(industry_lead_is_flat({"lead": [opener]}), opener)

    def test_conflict_lead_passes(self):
        hook = ("当学术界还在卷 0.1 dB 的 PSNR 时，真正被几万用户每天使用的，"
                "是一个把十几个模型缝起来的桌面端壳子。")
        self.assertFalse(industry_lead_is_flat({"lead": [hook]}))

    def test_missing_lead_is_flat(self):
        self.assertTrue(industry_lead_is_flat({"lead": []}))


class TestCardLabel(unittest.TestCase):
    def test_project_card_in_industry(self):
        block = {"rows": [["项目", "RP1"], ["仓库", "github.com/Roboparty/RP1"],
                          ["Stars", "2.6k"]]}
        self.assertEqual(wechat.resolve_card_label(block, "industry"), "PROJECT INFO")

    def test_event_card_in_industry(self):
        block = {"rows": [["主体", "康佳"], ["事件", "正式退市"], ["规模", "4 年亏损 203 亿"]]}
        self.assertEqual(wechat.resolve_card_label(block, "industry"), "INDUSTRY SNAPSHOT")

    def test_paper_module_never_becomes_project(self):
        block = {"rows": [["论文标题", "X"], ["开源代码", "github.com/a/b"]]}
        self.assertEqual(wechat.resolve_card_label(block, "paper"), "PAPER INFO")

    def test_explicit_label_wins_and_invalid_falls_back(self):
        self.assertEqual(wechat.resolve_card_label({"label": "INDUSTRY SNAPSHOT", "rows": []},
                                                   "industry"), "INDUSTRY SNAPSHOT")
        self.assertEqual(wechat.resolve_card_label({"label": "WHATEVER", "rows": []},
                                                   "industry"), "INDUSTRY SNAPSHOT")

    def test_contest_card_uses_contest_label(self):
        block = {"rows": [["赛题", "图像复原"], ["平台", "天池"]]}
        self.assertEqual(wechat.resolve_card_label(block, "contest"), "CONTEST INFO")

    def test_render_industry_never_shows_paper_info(self):
        spec = {
            "title": "T", "lead": ["x"],
            "sections": [
                {"type": "meta_card", "rows": [["主体", "某公司"], ["事件", "发布模型"]]},
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text": "正文"},
            ],
            "sources": [],
        }
        html = wechat.render_body(spec, {}, "industry", "2026.10.27")
        self.assertIn("INDUSTRY SNAPSHOT", html)
        self.assertNotIn("PAPER INFO", html)

    def test_render_project_card_has_project_title(self):
        spec = {
            "title": "T", "lead": ["x"],
            "sections": [
                {"type": "meta_card", "rows": [["项目", "RP1"], ["仓库", "github.com/a/b"]]},
            ],
            "sources": [],
        }
        html = wechat.render_body(spec, {}, "industry", "2026.10.27")
        self.assertIn("PROJECT INFO", html)


class TestClosingSection(unittest.TestCase):
    """收尾节统一叫「写在最后」，且承载终局判断（成本结构 / 演进路径 / 可检验结论）。"""

    def test_prompt_uses_official_name_only(self):
        self.assertIn('h2 "04 | 写在最后"', INDUSTRY_PROMPT)
        # 旧叫法只能作为「禁用示例」出现，不能还挂在章节定义上
        self.assertNotIn('h2 "04 | 冷静一下"', INDUSTRY_PROMPT)
        # 内容要求不能被弱化成「边界说明」
        for kw in ("成本结构", "演进路径", "终极判断"):
            self.assertIn(kw, INDUSTRY_PROMPT)

    def test_mock_spec_closing_section(self):
        items = [Item(module="industry", title="某公司发布模型", url="https://a.com", source="量子位")]
        spec = mock_spec("industry", items, {})
        heads = [b["text"] for b in spec["sections"] if b["type"] == "h2"]
        self.assertEqual(heads[-1], "03 | 写在最后")
        self.assertNotIn("冷静一下", str(spec))
        html = wechat.render_body(spec, {}, "industry", "2026.10.27")
        md = spec_to_markdown(spec, {})
        for text in (html, md):
            self.assertIn("写在最后", text)
            self.assertNotIn("冷静一下", text)


if __name__ == "__main__":
    unittest.main()
