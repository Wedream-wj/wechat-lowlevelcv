"""发布属性（原创声明 / 合集）与后台 UI 补勾的回归测试。

背景：微信 draft/add、freepublish/submit 都没有原创声明与合集字段（社区官方回复
「不支持」），所以这里的测试重点不是「微信收不收」，而是：
1. 两项属性有硬默认、别名能归一、错值当场报错；
2. 交给 draft/add 的载荷在推送前被断言过（必填项 + 属性一致性）；
3. 后台 UI 定位失败（尤其缺「每日推送」合集）时给出明确原因，不静默当成功。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.publish import attrs as A
from app.publish import mp_ui


class TestNormalize(unittest.TestCase):
    def test_default_original_is_text(self):
        self.assertEqual(A.normalize_original(""), A.ORIGINAL_TEXT)
        self.assertEqual(A.normalize_original(None), A.ORIGINAL_TEXT)
        self.assertEqual(A.DEFAULT_ORIGINAL, "文字原创")

    def test_aliases(self):
        for raw in ("原创", "图文原创", "文字", "text", " TEXT_ORIGINAL ", "TEXT"):
            with self.subTest(raw=raw):
                self.assertEqual(A.normalize_original(raw), A.ORIGINAL_TEXT)
        self.assertEqual(A.normalize_original("图片原创"), A.ORIGINAL_IMAGE)
        self.assertEqual(A.normalize_original("video"), A.ORIGINAL_VIDEO)

    def test_invalid_original_raises_with_choices(self):
        with self.assertRaises(A.PublishAttrError) as ctx:
            A.normalize_original("随便原创")
        msg = str(ctx.exception)
        self.assertIn("文字原创", msg)
        self.assertIn("WX_ORIGINAL_TYPE", msg)

    def test_collection_defaults_when_blank(self):
        self.assertEqual(A.normalize_collection("  "), "每日推送")
        self.assertEqual(A.normalize_collection("每日推送"), "每日推送")
        self.assertEqual(A.normalize_collection(" 精选 "), "精选")


class TestPublishAttrs(unittest.TestCase):
    def test_from_settings_defaults(self):
        attrs = A.PublishAttrs.from_settings()
        self.assertEqual(attrs.original_type, "文字原创")
        self.assertEqual(attrs.collection, "每日推送")
        self.assertEqual(attrs.payload(),
                         {"original_type": "文字原创", "collection": "每日推送"})

    def test_bad_config_value_fails_fast(self):
        with patch.object(A.settings, "wx_original_type", "乱写"):
            with self.assertRaises(A.PublishAttrError):
                A.PublishAttrs.from_settings()

    def test_todo_mentions_both(self):
        todo = A.PublishAttrs().todo()
        self.assertTrue(any("文字原创" in t for t in todo))
        self.assertTrue(any("每日推送" in t for t in todo))


class TestPayload(unittest.TestCase):
    def _article(self):
        return {"title": "CVPR 2026 | DreamSR", "thumb_media_id": "mid",
                "content": "<p>x</p>", "digest": "d", "author": "a"}

    def test_default_payload_keeps_official_fields_only(self):
        payload, include = A.build_draft_payload(self._article(), A.PublishAttrs(),
                                                 include_attrs=False)
        self.assertFalse(include)
        art = payload["articles"][0]
        self.assertNotIn("original_type", art)   # 微信未支持，默认不下发
        A.assert_draft_payload(payload, A.PublishAttrs(), include_attrs=False)

    def test_extra_fields_injected_when_enabled(self):
        payload, include = A.build_draft_payload(self._article(), A.PublishAttrs(),
                                                 include_attrs=True)
        self.assertTrue(include)
        art = A.assert_draft_payload(payload, A.PublishAttrs(), include_attrs=True)
        self.assertEqual(art["original_type"], "文字原创")
        self.assertEqual(art["collection"], "每日推送")

    def test_assert_catches_tampered_attrs(self):
        payload, _ = A.build_draft_payload(self._article(), A.PublishAttrs(),
                                           include_attrs=True)
        payload["articles"][0]["collection"] = "别的合集"
        with self.assertRaises(A.PublishAttrError) as ctx:
            A.assert_draft_payload(payload, A.PublishAttrs(), include_attrs=True)
        self.assertIn("collection", str(ctx.exception))

    def test_assert_catches_missing_required(self):
        payload, _ = A.build_draft_payload({"title": "", "thumb_media_id": "m",
                                            "content": "c"}, A.PublishAttrs(),
                                           include_attrs=False)
        with self.assertRaises(A.PublishAttrError) as ctx:
            A.assert_draft_payload(payload, A.PublishAttrs(), include_attrs=False)
        self.assertIn("title", str(ctx.exception))

    def test_assert_rejects_empty_articles(self):
        with self.assertRaises(A.PublishAttrError):
            A.assert_draft_payload({"articles": []}, A.PublishAttrs(), include_attrs=False)

    def test_snapshot_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(A.settings, "out_dir", Path(tmp)):
                path = A.write_snapshot(A.DraftRecord(issue_id="i1", attrs=A.PublishAttrs(),
                                                      media_id="m1"))
                self.assertTrue(path.exists())
                text = path.read_text(encoding="utf-8")
                self.assertIn("每日推送", text)
                self.assertIn("文字原创", text)


# ---------------------------------------------------------------- 假浏览器
class FakeLocator:
    def __init__(self, page: "FakePage", selector: str):
        self.page, self.selector = page, selector
        self.first = self

    async def count(self) -> int:
        return 1 if self.selector in self.page.present else 0

    async def click(self, timeout: int = 0) -> None:
        if self.selector not in self.page.present:
            raise RuntimeError("not clickable")
        self.page.clicked.append(self.selector)


class FakePage:
    """只实现 _apply 用到的三个能力：locator / count / click。"""

    def __init__(self, present: set[str]):
        self.present = present
        self.clicked: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class TestMpUi(unittest.TestCase):
    TITLE = "CVPR 2026 | DreamSR：1+16 步扩散"

    def _present(self, *, collection: bool = True, original: bool = True) -> set[str]:
        items = {mp_ui._DRAFT_BOX[0],
                 mp_ui._title_selector(self.TITLE)[0],
                 mp_ui._EDIT_BTN[0],
                 mp_ui._ORIGINAL_PANEL[0],
                 mp_ui._COLLECTION_PANEL[0]}
        if original:
            items.add('text="文字原创"')
        if collection:
            items.add('text="每日推送"')
        return items

    def test_happy_path_marks_both(self):
        page = FakePage(self._present())
        res = asyncio.run(mp_ui._apply(page, self.TITLE, A.PublishAttrs()))
        self.assertTrue(res.ok, res.describe())
        self.assertIn("原创=文字原创", res.applied)
        self.assertIn("合集=每日推送", res.applied)
        self.assertEqual(res.missing, [])
        self.assertFalse(res.skipped)

    def test_missing_collection_reports_clearly(self):
        page = FakePage(self._present(collection=False))
        res = asyncio.run(mp_ui._apply(page, self.TITLE, A.PublishAttrs()))
        self.assertFalse(res.ok)
        self.assertIn("每日推送", res.missing)
        self.assertIn("每日推送", res.reason)
        self.assertIn("合集", res.reason)      # 提示去后台新建合集
        self.assertIn("原创=文字原创", res.applied)   # 另一项不受影响

    def test_draft_not_found(self):
        page = FakePage({mp_ui._DRAFT_BOX[0]})
        res = asyncio.run(mp_ui._apply(page, self.TITLE, A.PublishAttrs()))
        self.assertFalse(res.ok)
        self.assertIn("没找到", res.reason)
        self.assertTrue(any("✗" in s for s in res.steps))

    def test_disabled_is_skipped(self):
        res = asyncio.run(mp_ui.fill_draft_attrs(self.TITLE, A.PublishAttrs(), enabled=False))
        self.assertTrue(res.skipped)
        self.assertIn("WX_UI_FILL", res.reason)

    def test_missing_playwright_is_skipped(self):
        with patch.object(mp_ui, "_load_playwright", return_value=None):
            res = asyncio.run(mp_ui.fill_draft_attrs(self.TITLE, A.PublishAttrs(), enabled=True))
        self.assertTrue(res.skipped)
        self.assertIn("playwright", res.reason)

    def test_describe_readable(self):
        res = asyncio.run(mp_ui._apply(FakePage(self._present()), self.TITLE, A.PublishAttrs()))
        self.assertIn("文字原创", res.describe())
        self.assertIn("每日推送", res.describe())


if __name__ == "__main__":
    unittest.main()
