import unittest

from app.llm.client import extract_json, validate_spec
from app.llm.mock import DEMO_SPECS
from app.render import wechat
from app.render.markdown import spec_to_markdown


class TestJsonExtraction(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json("```json\n{\"a\": [1,2]}\n```"), {"a": [1, 2]})

    def test_with_noise(self):
        text = '好的，以下是结果：\n{"title": "含\\"引号\\"的标题", "n": 2}\n希望有帮助'
        data = extract_json(text)
        self.assertEqual(data["n"], 2)

    def test_invalid(self):
        with self.assertRaises(Exception):
            extract_json("完全没有 JSON")


class TestSpecValidation(unittest.TestCase):
    def test_validate_ok(self):
        spec = validate_spec({"title": "T", "sections": [{"type": "p", "text": "x"},
                                                          {"type": "unknown"}, {"type": "p", "text": ""}]})
        self.assertEqual(len(spec["sections"]), 1)

    def test_validate_rejects_empty(self):
        with self.assertRaises(Exception):
            validate_spec({"title": "", "sections": []})


class TestWeChatRender(unittest.TestCase):
    def setUp(self):
        self.spec = DEMO_SPECS["paper"]
        self.figures = {"F1": {"path": "assets/demo_arch.png", "caption": "架构图"}}

    def test_body_has_inline_styles(self):
        html = wechat.render_body(self.spec, self.figures, "paper", "2026.10.18")
        self.assertIn('style="font-family:', html)
        self.assertIn("assets/demo_arch.png", html)
        self.assertIn("SECTION 01", html)
        self.assertIn("PAPER INFO", html)
        self.assertIn("写在最后", html)

    def test_escape(self):
        html = wechat.render_body({"title": "T", "lead": ["<script>alert(1)</script>"],
                                   "sections": [], "sources": []}, {}, "paper")
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_inline_formatting(self):
        out = wechat.inline("看 **加粗** 与 ==高亮== 和 `code` 还有 [链接](https://a.com)")
        self.assertIn("<strong", out)
        self.assertIn("#FFEFC2", out)
        self.assertIn("<code", out)
        # 微信会剥离正文 <a>，链接只保留带下划线的文字样式
        self.assertNotIn("<a ", out)
        self.assertIn("链接", out)

    def test_contest_render(self):
        html = wechat.render_body(DEMO_SPECS["contest"], {}, "contest", "2026.10.18")
        self.assertIn("CHALLENGE", html)
        self.assertIn("赛题方向", html)
        self.assertIn("奖金池", html)

    def test_markdown_roundtrip(self):
        md = spec_to_markdown(self.spec, self.figures)
        self.assertTrue(md.startswith("# "))
        self.assertIn("![", md)
        self.assertIn("## 01", md)


if __name__ == "__main__":
    unittest.main()
