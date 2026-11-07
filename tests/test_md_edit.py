"""人工润色链路单测：Markdown 解析 / 重排 / 落库 / 推送取最新版。

这条链路的核心契约只有一句：**Markdown 是唯一事实源，推送读库里的 HTML**。
因此测试盯三件事：
1. `spec_to_markdown` 的产出能被 `parse_markdown` 原样还原（含图表标签，不许丢）；
2. 保存后库里的 markdown/html/标题/版本号同步更新，磁盘三件套跟着重写；
3. 已推过的稿子被润色后，`push_issue` 发给微信的 content 必须是新版，且
   发布属性仍是「文字原创 / 每日推送」（这里用假客户端拦下 payload）。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import db, edit
from app.config import settings
from app.models import Article, Item
from app.render import wechat
from app.render.markdown import spec_to_markdown
from app.render.md_parse import parse_markdown, md_to_body

SPEC = {
    "title": "PR 2026 | MDKD：多维蒸馏让轻量人群定位穿透低能见度",
    "subtitle": "四分之一参数的学生，逼近带复原分支的教师",
    "digest": "雾雨雪夜间场景下，MDKD 把复原能力留在训练期。",
    "lead": ["雾天路口，人头只剩几团模糊轮廓。", "第二段导语。"],
    "sections": [
        {"type": "meta_card", "label": "PAPER INFO", "rows": [
            ["论文名称", "Lightweight and effective crowd counting"],
            ["发表期刊/会议", "Pattern Recognition（PR）"],
            ["作者团队", "由 Boyang Yao 等研究人员共同完成"],
        ]},
        {"type": "h2", "text": "01 | 研究背景与痛点"},
        {"type": "p", "text": "这是**加粗**的正文，还有 `code` 与 ==高亮==。"},
        {"type": "h3", "text": "三维蒸馏"},
        {"type": "bullets", "items": ["特征对齐", "样本关系蒸馏"]},
        {"type": "numbered", "items": ["第一步", "第二步"]},
        {"type": "quote", "text": "引用一句话。"},
        {"type": "highlight", "title": "核心结论", "text": "蒸馏把成本拆到两个阶段。"},
        {"type": "table", "header": ["Method", "GAME(1)↓"],
         "rows": [["P2PNet", "117.02"], ["Ours", "105.16"]]},
        {"type": "figure", "ref": "F1", "caption": "MDKD 整体架构。"},
        {"type": "code", "lang": "python", "caption": "", "code": "print('hi')"},
        {"type": "divider"},
    ],
    "takeaway": "把复原分支留在教师，蒸馏给四分之一参数的学生。",
    "sources": [{"name": "Pattern Recognition 2026", "url": "https://doi.org/10.1016/j.patcog.2026.113339"}],
}
FIGURES = {"F1": {"path": "assets/mdkd_fig3.png", "caption": "MDKD 整体架构。"}}


def _md() -> str:
    return spec_to_markdown(SPEC, FIGURES, "paper")


class TestParseMarkdown(unittest.TestCase):
    def test_round_trip_structural(self):
        spec, figures = parse_markdown(_md())
        self.assertIn("MDKD", spec["title"])
        self.assertIn("四分之一参数", spec["subtitle"])
        self.assertIn("雾雨雪", spec["digest"])
        self.assertEqual(len(spec["lead"]), 2)
        types = [b["type"] for b in spec["sections"]]
        for want in ("meta_card", "h2", "p", "h3", "bullets", "numbered", "quote",
                     "highlight", "table", "figure", "code", "divider"):
            self.assertIn(want, types, f"块类型 {want} 丢了：{types}")

    def test_meta_card_rows_preserved(self):
        spec, _ = parse_markdown(_md())
        card = next(b for b in spec["sections"] if b["type"] == "meta_card")
        self.assertEqual(card["rows"][0][0], "论文名称")
        self.assertIn("Pattern Recognition", card["rows"][1][1])

    def test_table_cells_exact(self):
        spec, _ = parse_markdown(_md())
        table = next(b for b in spec["sections"] if b["type"] == "table")
        self.assertEqual(table["header"], ["Method", "GAME(1)↓"])
        self.assertEqual(table["rows"], [["P2PNet", "117.02"], ["Ours", "105.16"]])

    def test_figure_path_and_caption(self):
        spec, figures = parse_markdown(_md())
        fig = next(b for b in spec["sections"] if b["type"] == "figure")
        self.assertEqual(fig["caption"], "MDKD 整体架构。")
        self.assertEqual(figures[fig["ref"]]["path"], "assets/mdkd_fig3.png")

    def test_takeaway_and_sources(self):
        spec, _ = parse_markdown(_md())
        self.assertIn("复原分支留在教师", spec["takeaway"])
        self.assertEqual(spec["sources"][0]["url"], "https://doi.org/10.1016/j.patcog.2026.113339")

    def test_raw_html_is_escaped(self):
        spec, _ = parse_markdown("# T\n\n<script>alert(1)</script>\n")
        html, _, _ = md_to_body("# T\n\n<script>alert(1)</script>\n", "paper")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_body_keeps_figures_and_tables(self):
        html, spec, _ = md_to_body(_md(), "paper")
        self.assertEqual(html.count("<img"), 1)
        self.assertIn("<table", html)
        self.assertIn("SECTION 01", html)
        self.assertIn("<strong", html)          # **加粗** 不能退化
        self.assertNotIn("<script", html)

    def test_empty_markdown_rejected(self):
        article = Article(module="paper", title="t", markdown="x", html="<p>x</p>",
                          issue_id="paper-x")
        with self.assertRaises(ValueError):
            edit.render_edit(article, "   \n  ")


class TestEditPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._old_db = settings.db_path
        self._old_out = settings.out_dir
        settings.db_path = root / "test.db"
        settings.out_dir = root / "out"
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        db.init_db()
        self.article = Article(
            module="paper", title="原标题", subtitle="", digest="",
            markdown=_md(), html=wechat.render_body(SPEC, FIGURES, "paper"),
            cover_path=str(root / "out" / "assets" / "cover.png"),
            items=[Item(module="paper", title="t", url="https://doi.org/x",
                        source="crossref", summary="s")],
            issue_id="paper-test-1", status="rendered",
        )
        db.save_article(self.article)
        Path(settings.out_dir / "assets").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        settings.db_path = self._old_db
        settings.out_dir = self._old_out
        self.tmp.cleanup()

    def test_save_updates_db_files_and_rev(self):
        new_md = _md().replace("雾天路口，人头只剩几团模糊轮廓。",
                               "雾天路口，人头只剩几团模糊轮廓——这是人工润色后的句子。")
        payload = edit.save_edit(self.article, new_md)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["rev"], 1)

        stored = db.get_article("paper-test-1")
        self.assertIn("人工润色后的句子", stored.markdown)
        self.assertIn("人工润色后的句子", stored.html)
        self.assertEqual(stored.title, stored.title.strip())

        for name in ("paper-test-1.md", "paper-test-1.html", "preview_paper-test-1.html"):
            self.assertTrue((settings.out_dir / name).exists(), f"{name} 没落盘")
        self.assertIn("人工润色后的句子",
                      (settings.out_dir / "preview_paper-test-1.html").read_text(encoding="utf-8"))

        meta = db.edit_meta("paper-test-1")
        self.assertEqual(meta["rev"], 1)
        self.assertTrue(meta["edited_at"])

    def test_edit_after_push_marks_needs_repush(self):
        db.update_article_status("paper-test-1", "pushed", media_id="m1")
        edit.save_edit(self.article, _md() + "\n人工追加的一段。\n")
        self.assertEqual(db.edit_meta("paper-test-1")["status"], "edited")

    def test_title_follows_markdown(self):
        edit.save_edit(self.article, _md().replace(
            "# PR 2026 | MDKD", "# PR 2026 | MDKD（人工改题）"))
        self.assertIn("人工改题", db.get_article("paper-test-1").title)


class _FakeWx:
    """假微信客户端：记下 draft/add 的 payload，不发任何网络请求。"""
    last: dict = {}

    def __init__(self):
        self.ready = True
        self.last_include_attrs = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rewrite_images(self, html):
        return html

    async def upload_cover_material(self, path):
        return "thumb-1"

    async def add_draft(self, **kwargs):
        _FakeWx.last = dict(kwargs)
        return "media-1"


class TestPushUsesLatestEdit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._old_db = settings.db_path
        self._old_out = settings.out_dir
        settings.db_path = root / "test.db"
        settings.out_dir = root / "out"
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        db.init_db()
        self.article = Article(
            module="paper", title="原标题", markdown=_md(),
            html=wechat.render_body(SPEC, FIGURES, "paper"),
            cover_path="cover.png",
            items=[Item(module="paper", title="t", url="https://doi.org/x", source="crossref")],
            issue_id="paper-test-2", status="rendered",
        )
        db.save_article(self.article)

    def tearDown(self):
        settings.db_path = self._old_db
        settings.out_dir = self._old_out
        self.tmp.cleanup()

    def test_payload_carries_polished_content_and_attrs(self):
        from app.pipeline import push_issue

        edit.save_edit(self.article, _md().replace(
            "引用一句话。", "这是润色后才有的句子。"))

        with patch("app.publish.wechat_api.WeChatClient", _FakeWx), \
             patch("app.publish.mp_ui.fill_draft_attrs",
                   new=_async_none()), \
             patch("app.publish.attrs.write_snapshot", return_value=Path("snap.json")):
            article = asyncio.run(push_issue("paper-test-2"))

        payload = _FakeWx.last
        self.assertIn("这是润色后才有的句子", payload["content"])
        self.assertEqual(payload["thumb_media_id"], "thumb-1")
        attrs = payload.get("attrs")
        self.assertIsNotNone(attrs)
        self.assertEqual(attrs.original_type, "文字原创")
        self.assertEqual(attrs.collection, "每日推送")
        self.assertEqual(article.status, "pushed")


def _async_none():
    async def _fn(*args, **kwargs):
        from app.publish import mp_ui
        return mp_ui.FillResult(reason="测试跳过后台补勾")
    return _fn


if __name__ == "__main__":
    unittest.main()
