"""作者署名「零废话」单测。

事故原型（2026-11-15《CVPR 2025 | InvSR》《CVPR 2026 | HDW-SR》等）：
信息卡的【作者团队】写着「由论文作者团队共同完成」——不含任何具体姓名，
读者检索不到人，比缺这一行更伤专业度。根因是榜单类信源（Awesome CVPR 清单）
authors 字段为空，模型照着 Prompt 的句式硬凑了一句通稿。

这里固化四件事：
1. 废话写法一律判空（各种变体），真名写法一律认得出（含 Ø、倒装、「and」）；
2. 信息卡补行只补真名，取不到就删行，绝不写占位；
3. Markdown 与 spec 两条链路都拦得住（编辑保存 / 流水线兜底）；
4. Prompt 与历史库都不再出现这类话术。
"""
import re
import sqlite3
import unittest
from pathlib import Path

from app import edit
from app.llm import prompts
from app.models import Item
from app.spec_tools import (
    ensure_paper_meta_card,
    format_authors_line,
    is_placeholder_authors,
    paper_meta_values,
    parse_author_names,
    strip_placeholder_meta_rows,
)

DB = Path(__file__).resolve().parents[1] / "data" / "llcv.db"

FAKE_LINES = [
    "由论文作者团队共同完成",
    "本论文由多位研究者共同完成",
    "由 TADSR 研究团队共同完成",
    "由 Guo 等研究人员共同完成",
    "作者团队来自西北工业大学",
    "研究团队",
    "以论文首页为准",
    "",
]


class TestFakeAuthorDetection(unittest.TestCase):
    def test_fake_lines_yield_no_names(self):
        for line in FAKE_LINES:
            with self.subTest(line=line):
                self.assertTrue(is_placeholder_authors(line))
                self.assertEqual(parse_author_names(line), [])

    def test_real_names_are_recognized(self):
        cases = {
            "Weihang Kong, Jienan Shen, He Li": ["Weihang Kong", "Jienan Shen", "He Li"],
            "Weihang Kong、Jienan Shen": ["Weihang Kong", "Jienan Shen"],
            "由 Weihang Kong、Jienan Shen 等人组成": ["Weihang Kong", "Jienan Shen"],
            "本文由 August Hoeg 等研究人员共同完成": ["August Hoeg"],
            "Hang Guo, Yong Guo and Yawei Li": ["Hang Guo", "Yong Guo", "Yawei Li"],
            # 人名里的非 ASCII 字母必须留住（VoDaSuRe 首作者）
            "August Leander Høeg, Tim Bjørn Dyrby": ["August Leander Høeg", "Tim Bjørn Dyrby"],
        }
        for line, want in cases.items():
            with self.subTest(line=line):
                self.assertEqual(parse_author_names(line), want)

    def test_surname_first_is_flipped(self):
        """「Chen, Junyang」是 OpenAlex 的姓氏前置写法，必须还原成「Junyang Chen」。"""
        names = parse_author_names("Hao Chen、Chen, Junyang、Pan, Jinshan、Jiangxin Dong")
        self.assertEqual(names, ["Hao Chen", "Junyang Chen", "Jinshan Pan", "Jiangxin Dong"])

    def test_format_caps_at_six(self):
        many = [f"Author{i} Name" for i in range(1, 9)]
        self.assertEqual(format_authors_line(many),
                         "Author1 Name, Author2 Name, Author3 Name, "
                         "Author4 Name, Author5 Name, Author6 Name 等")
        self.assertEqual(format_authors_line([]), "")
        self.assertEqual(format_authors_line(["研究团队"]), "")


class TestMetaCardGuard(unittest.TestCase):
    def _card(self, *rows):
        return {"type": "meta_card", "label": "PAPER INFO", "rows": [list(r) for r in rows]}

    def test_fake_row_is_deleted(self):
        spec = {"sections": [self._card(["论文名称", "X"], ["作者团队", "由论文作者团队共同完成"])]}
        self.assertEqual(strip_placeholder_meta_rows(spec), 1)
        self.assertEqual([r[0] for r in spec["sections"][0]["rows"]], ["论文名称"])

    def test_placeholder_venue_is_deleted(self):
        spec = {"sections": [self._card(["论文名称", "X"], ["发表期刊/会议", "以原文标注为准"])]}
        self.assertEqual(strip_placeholder_meta_rows(spec), 1)

    def test_ensure_does_not_invent_authors(self):
        item = Item(module="paper", title="Some Paper", url="https://arxiv.org/abs/2601.00001",
                    source="Awesome CVPR 2026", venue="CVPR 2026", authors=[])
        spec = {"sections": [self._card(["论文名称", "Some Paper"],
                                        ["作者团队", "由论文作者团队共同完成"])]}
        ensure_paper_meta_card(spec, item)
        labels = [r[0] for r in spec["sections"][0]["rows"]]
        self.assertNotIn("作者团队", labels)          # 取不到真名 → 整行删掉

    def test_ensure_rewrites_with_real_names(self):
        item = Item(module="paper", title="Some Paper", url="https://arxiv.org/abs/2601.00001",
                    source="arXiv", venue="CVPR 2026",
                    authors=["Zongsheng Yue", "Kang Liao", "Chen Change Loy"])
        spec = {"sections": [self._card(["论文名称", "Some Paper"],
                                        ["作者团队", "由论文作者团队共同完成"])]}
        ensure_paper_meta_card(spec, item)
        row = next(r for r in spec["sections"][0]["rows"] if r[0] == "作者团队")
        self.assertEqual(row[1], "Zongsheng Yue, Kang Liao, Chen Change Loy")

    def test_venue_fallback_is_empty_not_placeholder(self):
        item = Item(module="paper", title="Some Paper", url="https://example.org/x",
                    source="Awesome CVPR 2026", venue="")
        self.assertNotIn("以原文标注为准", paper_meta_values(item)["发表期刊/会议"])


class TestMarkdownGuard(unittest.TestCase):
    def test_editor_strips_fake_author_line(self):
        md = ("# T\n\n- **论文名称**：X\n- **作者团队**：由论文作者团队共同完成\n"
              "- **开源代码**：https://x\n\n正文一段。\n")
        text, dropped = edit.drop_fake_author_rows(md)
        self.assertEqual(dropped, 1)
        self.assertNotIn("共同完成", text)
        self.assertIn("- **论文名称**：X", text)

    def test_editor_keeps_real_author_line(self):
        md = "- **作者团队**：Zongsheng Yue, Kang Liao, Chen Change Loy\n"
        text, dropped = edit.drop_fake_author_rows(md)
        self.assertEqual(dropped, 0)
        self.assertEqual(text, md.rstrip("\n"))

    def test_render_edit_rejects_nothing_but_cleans(self):
        article = _article("- **作者团队**：由论文作者团队共同完成")
        result = edit.render_edit(article, article.markdown)
        self.assertNotIn("共同完成", result.markdown)
        self.assertNotIn("共同完成", result.html)


class TestPromptRules(unittest.TestCase):
    def test_focus_rules_forbid_empty_author_talk(self):
        rules = prompts.PAPER_FOCUS_RULES
        self.assertIn("作者团队", rules)
        self.assertIn("严禁", rules)
        self.assertIn("整行省略", rules)

    def test_schema_example_uses_real_names(self):
        source = Path(prompts.__file__).read_text(encoding="utf-8")
        self.assertIn("Weihang Kong, Jienan Shen, He Li", source)
        self.assertNotIn("A、B、C 等人", source)
        # 规则段允许出现「由论文作者团队共同完成」，但只能作为被明令禁止的反例：
        # 命中位置前后 150 字里必须能找到禁令词
        banned = ("禁止", "严禁", "不得", "绝不", "零废话", "不含具体姓名")
        for m in re.finditer("由论文作者团队共同完成", source):
            window = source[max(0, m.start() - 150):m.end() + 150]
            self.assertTrue(any(w in window for w in banned),
                            f"这句话出现在非禁令语境：{window[:80]}")


class TestHistoryArchive(unittest.TestCase):
    """历史出稿库里的作者行必须全部是具体人名（含 2026-11-15 清洗后的状态）。"""

    @unittest.skipUnless(DB.exists(), "本地库不存在")
    def test_no_fake_author_rows_in_db(self):
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT issue_id, markdown FROM issues WHERE module='paper'").fetchall()
        conn.close()
        self.assertTrue(rows)
        for issue_id, markdown in rows:
            with self.subTest(issue_id=issue_id):
                for line in (markdown or "").splitlines():
                    m = edit._META_AUTHOR_LINE_RE.match(line)
                    if not m:
                        continue
                    self.assertFalse(is_placeholder_authors(m.group(1)),
                                     f"{issue_id} 仍有废话作者行：{line.strip()}")


def _article(author_line: str):
    from app.models import Article

    md = ("# 测试标题\n\n**导读**：一句话。\n\n- **论文名称**：Some Paper\n"
          f"{author_line}\n- **开源代码**：https://github.com/x/y\n\n"
          "## 01 | 研究背景与痛点\n\n正文内容。\n")
    return Article(module="paper", title="测试标题", issue_id="test-author-guard",
                   markdown=md, created_at=__import__("datetime").datetime(2026, 11, 16))


if __name__ == "__main__":
    unittest.main()
