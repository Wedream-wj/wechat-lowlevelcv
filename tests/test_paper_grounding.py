"""论文元信息 ↔ 正文主体「原子绑定」单测。

事故原型（2026-11-14《NEURIPS 2026 | DeLang-SR》）：一期 3 篇候选，正文精读
DeLang-SR，信息卡却填了同批第一篇 MUTE 的标题与作者。这里把当时的数据原样
固化成夹具，验证四件事：
1. 判据能认出「正文讲的是谁」，并指出信息卡错位；
2. 重绑后三行元数据逐字对齐目标论文，一位作者都不串；
3. 取不到权威字段时删行而不编造（宁可少一行，不许编人名）；
4. Prompt 侧真的带上了 target_paper 权威块与锁定铁律。
"""
import unittest

from app.filters import grounding
from app.llm.prompts import PAPER_FOCUS_RULES, build_user_prompt
from app.models import Item

MUTE_TITLE = ("MUTE: Multi-Level Alignment Uncoupling Against Talking-Head "
              "Exploitation for Voice Protection")
DELANG_TITLE = ("Decoupling Direction and Magnitude: Language-Steered Flow Matching "
                "for Super-Resolution in the Dark")
COLOR_TITLE = "超越固定亮度: 迈向全色与正色图像着色"


def _items() -> list[Item]:
    mute = Item(module="paper", title=MUTE_TITLE, url="https://arxiv.org/abs/2611.11989",
                source="arXiv", venue="NeurIPS", year="2026",
                authors=["Donghyun Kim", "Jin Hong", "Seungmin Kim"], score=9.0,
                keywords=["super-resolution", "image restoration"],
                summary="Voice protection against talking-head exploitation, image restoration.")
    delang = Item(module="paper", title=DELANG_TITLE, url="https://arxiv.org/abs/2611.10485",
                  source="arXiv", venue="NeurIPS", year="2026",
                  authors=["Jiaxin Gao", "Ziyu Yue", "Yaohua Liu"], score=8.6,
                  code_url="https://github.com/moriyaya/DeLang-SR",
                  keywords=["super-resolution", "image restoration"],
                  summary="We propose DeLang-SR (Degradation-Language steered Super-Resolution), "
                          "a one-step flow-matching framework for image restoration.",
                  images=["out/assets/delang-arch.png"])
    color = Item(module="paper", title=COLOR_TITLE, url="https://arxiv.org/abs/2608.10798",
                 source="熵视", venue="European Conference on Computer Vision (ECCV)",
                 year="2026", authors=["Swarnim Maheshwari"], score=7.9,
                 keywords=["super-resolution", "image restoration"],
                 summary="Colorization and image restoration benchmark.")
    return [mute, delang, color]


def _incident_spec() -> dict:
    """事故稿原样：正文讲 DeLang-SR、配图来自 DeLang，信息卡填的却是 MUTE。"""
    return {
        "title": "NEURIPS 2026 | DeLang-SR：解耦方向与幅度，单步流匹配攻克暗光超分",
        "subtitle": "把低光退化翻译成语言先验",
        "lead": ["暗光超分的难点在于空间异质性。DeLang-SR 用退化语言引导速度场方向。"],
        "sections": [
            {"type": "meta_card", "label": "PAPER INFO", "rows": [
                ["论文名称", MUTE_TITLE],
                ["发表期刊/会议", "NeurIPS 2026"],
                ["作者团队", "由 Donghyun Kim、Jin Hong 等研究人员共同完成"],
                ["开源代码", "https://github.com/moriyaya/DeLang-SR"],
            ]},
            {"type": "h2", "text": "01 | 研究背景与痛点"},
            {"type": "p", "text": "标准扩散与流匹配模型采用全局均匀的积分轨迹，DeLang-SR 把方向与幅度拆开。"},
            {"type": "figure", "ref": "F1", "src": "assets/delang-arch.png",
             "caption": "图 1：整体架构。"},
            {"type": "p", "text": "消融实验里去掉退化语言后，PSNR 从 22.986 降到 22.433。"},
        ],
    }


class TestSignatures(unittest.TestCase):
    def test_method_codes_extracted(self):
        self.assertIn("DeLang-SR", grounding.title_signatures(DELANG_TITLE + " DeLang-SR"))
        self.assertIn("MUTE", grounding.title_signatures(MUTE_TITLE))
        # 通用写法不是代号
        self.assertNotIn("R-CNN", grounding.title_signatures("PigMS R-CNN for pigs"))
        self.assertNotIn("Multi-Level", grounding.title_signatures(MUTE_TITLE))

    def test_repo_name_counts_as_signature(self):
        item = _items()[1]
        sigs = grounding.item_signatures(item)
        self.assertIn("DeLang-SR", sigs)
        self.assertEqual(sigs["DeLang-SR"], 1.0)   # 仓库名与标题同级可信


class TestResolve(unittest.TestCase):
    def test_body_subject_wins_over_meta_card(self):
        target, info = grounding.resolve_meta_subject(_incident_spec(), _items())
        self.assertIsNotNone(target)
        self.assertEqual(target.title, DELANG_TITLE)
        self.assertTrue(info["mismatch"])
        self.assertIn("MUTE", info["reason"])
        self.assertIn("Decoupling Direction", info["reason"])

    def test_bind_rewrites_three_rows(self):
        spec = _incident_spec()
        target, _ = grounding.resolve_meta_subject(spec, _items())
        grounding.bind_paper_meta(spec, target)
        rows = {r[0]: r[1] for r in spec["sections"][0]["rows"]}
        self.assertEqual(rows["论文名称"], DELANG_TITLE)
        self.assertIn("Jiaxin Gao", rows["作者团队"])
        self.assertIn("Ziyu Yue", rows["作者团队"])
        self.assertNotIn("Donghyun", rows["作者团队"])     # 绝不串作者
        self.assertEqual(grounding.assert_paper_meta(spec, target), [])

    def test_incident_spec_would_fail_before_binding(self):
        """绑定前的断言必须报错——这正是「熔断」的触发条件。"""
        spec = _incident_spec()
        target = _items()[1]
        problems = grounding.assert_paper_meta(spec, target)
        self.assertTrue(problems)
        self.assertIn("论文名称", problems[0])

    def test_hallucinated_meta_falls_back_to_focus(self):
        spec = _incident_spec()
        spec["sections"][0]["rows"][0] = ["论文名称", "A Completely Fabricated Paper Title"]
        items = _items()
        target, info = grounding.resolve_meta_subject(spec, items)
        self.assertEqual(target.title, DELANG_TITLE)      # 仍以正文为准
        grounding.bind_paper_meta(spec, target)
        self.assertEqual(grounding.meta_title(spec), DELANG_TITLE)

    def test_missing_authors_drops_row_instead_of_fabricating(self):
        item = Item(module="paper", title=DELANG_TITLE, url="u", source="arXiv",
                    venue="NeurIPS", year="2026", authors=[])
        spec = {"title": "x", "sections": [
            {"type": "meta_card", "rows": [
                ["论文名称", DELANG_TITLE],
                ["作者团队", "由 Fake Person、Another Ghost 等研究人员共同完成"],
            ]},
        ]}
        res = grounding.bind_paper_meta(spec, item)
        self.assertIn("作者团队", res["removed"])
        labels = [r[0] for r in spec["sections"][0]["rows"]]
        self.assertNotIn("作者团队", labels)
        self.assertNotIn("Fake Person", str(spec))

    def test_single_candidate_is_always_target(self):
        items = [_items()[1]]
        spec = {"title": "t", "sections": [
            {"type": "meta_card", "rows": [["论文名称", "别的论文"]]}]}
        target, info = grounding.resolve_meta_subject(spec, items)
        self.assertEqual(target.title, DELANG_TITLE)
        self.assertTrue(info["mismatch"])          # 「别的论文」对不上任何候选 → 疑似编造


class TestPromptBinding(unittest.TestCase):
    def test_target_paper_block_and_rules_injected(self):
        prompt = build_user_prompt("paper", _items(), {}, "2026-11-14", focus=_items()[1])
        self.assertIn('"target_paper"', prompt)
        self.assertIn(DELANG_TITLE, prompt)
        self.assertIn(PAPER_FOCUS_RULES.strip().splitlines()[0], prompt)
        self.assertNotIn(MUTE_TITLE, prompt.split('"target_paper"')[1].split("}")[0])

    def test_no_focus_no_block(self):
        prompt = build_user_prompt("paper", _items(), {}, "2026-11-14")
        self.assertNotIn('"target_paper"', prompt)
        self.assertNotIn(PAPER_FOCUS_RULES.strip().splitlines()[0], prompt)


if __name__ == "__main__":
    unittest.main()


class TestPipelineReplay(unittest.IsolatedAsyncioTestCase):
    """事故全链路回放：真管线 + 假模型，验证串台会在出稿前被纠正。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from app import db
        from app.config import settings

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._old_db, self._old_out = settings.db_path, settings.out_dir
        settings.db_path = root / "test.db"
        settings.out_dir = root / "out"
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        settings.out_dir.mkdir(parents=True, exist_ok=True)
        db.init_db()

    def tearDown(self):
        from app.config import settings

        settings.db_path, settings.out_dir = self._old_db, self._old_out
        self._tmp.cleanup()

    async def test_meta_is_rebound_to_body_subject(self):
        from unittest.mock import patch
        from app import pipeline

        items = _items()
        delang = items[1]
        delang.extra["tables"] = [{"header": ["方法", "PSNR", "LPIPS"],
                                   "rows": [["Ours", "22.986", "0.2482"],
                                            ["Baseline", "22.433", "0.2601"]],
                                   "caption": "表 1：暗光超分对比。"}]
        spec = _incident_spec()
        spec["digest"] = "一句话"
        spec["sections"] = spec["sections"][:2] + [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "p", "text": "DeLang-SR 用退化语言引导 SD3 速度场方向，用空间场控制幅度。"},
            {"type": "figure", "ref": "F1", "caption": "图 1：整体架构。",
             "src": "assets/delang-arch.png"},
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "table", "header": ["方法", "PSNR", "LPIPS"],
             "rows": [["Ours", "22.986", "0.2482"], ["Baseline", "22.433", "0.2601"]],
             "caption": "表 1：暗光超分对比。"},
            {"type": "p", "text": "PSNR 从 22.433 升到 22.986，高 0.553 dB。"},
            {"type": "h2", "text": "04 | 与 SOTA 的对比与分析"},
            {"type": "p", "text": "对比 Restormer 等基线，DeLang-SR 在暗区更稳。"},
            {"type": "h2", "text": "05 | 写在最后"},
            {"type": "p", "text": "把退化程度变成可控制的条件信号，是这条路线的关键一步。"},
        ]
        spec["takeaway"] = "DeLang-SR 把流匹配的单步更新拆成方向与幅度两个控制量。"

        class _FakeLLM:
            enabled = True
            mode = "mock"

            def __init__(self, *a, **kw):
                pass

            async def write(self, module, items, figures, date_str, on_progress=None,
                            extra_instruction="", variant=None, focus=None):
                return {k: (list(v) if isinstance(v, list) else v) for k, v in spec.items()}

            async def rewrite_industry_title(self, *a, **kw):
                return None

        async def fake_collect(module, progress=None):
            return list(items), []

        async def fake_enrich(client, selected, limit=0):
            return None

        async def fake_publication(client, *a, **kw):
            return {"checked": True, "accepted": True, "venue": "NeurIPS", "source": "test"}

        async def fake_figures(client, selected, module):
            return {"F1": {"item_id": f"paper:{delang.fingerprint}", "caption": "图 1：整体架构。",
                           "path": "assets/delang-arch.png", "title": delang.title}}

        async def fake_cover(*a, **kw):
            return None

        with patch.object(pipeline, "collect", fake_collect), \
                patch.object(pipeline, "LLMClient", _FakeLLM), \
                patch.object(pipeline.arxiv, "enrich_figures", fake_enrich), \
                patch.object(pipeline._publication, "resolve_publication", fake_publication), \
                patch.object(pipeline, "build_figures", fake_figures), \
                patch.object(pipeline.cover_select, "pick_cover", fake_cover):
            result = await pipeline.run_module("paper", demo=True)

        self.assertIsNotNone(result.article, f"管线未出稿：{result.errors}")
        cards = [b for b in result.article.markdown.split("|---")
                 if "论文名称" in b or "Jiaxin Gao" in b]
        md = result.article.markdown
        self.assertIn(DELANG_TITLE, md)            # 论文名称 = 正文那篇
        self.assertIn("Jiaxin Gao", md)            # 作者 = 正文那篇
        self.assertNotIn("Donghyun Kim", md)       # 绝不残留 MUTE 的作者
        self.assertTrue(any("元信息一致性" in str(e.get("source"))
                            for e in result.errors if isinstance(e, dict)))


class TestPreprintVenue(unittest.TestCase):
    """OpenAlex 会把预印本托管方写成「arXiv (Cornell University)」——不能原样上信息卡。"""

    def test_venue_label_normalized(self):
        from app.spec_tools import venue_card_label

        self.assertEqual(venue_card_label("arXiv (Cornell University)", "2026"), "arXiv 预印本")
        self.assertEqual(venue_card_label("arXiv.org", "2026"), "arXiv 预印本")
        self.assertEqual(venue_card_label("SSRN Electronic Journal"), "预印本（未经同行评议）")
        self.assertEqual(venue_card_label("Pattern Recognition", "2026"),
                         "Pattern Recognition 2026（PR）")

    def test_title_prefix_stripped_for_preprint(self):
        from app.llm.prompts import enforce_title_rules

        item = Item(module="paper", title=DELANG_TITLE, url="u", source="arXiv",
                    venue="arXiv (Cornell University)", year="2026",
                    extra={"publication": {"checked": True, "accepted": None,
                                           "venue": "arXiv (Cornell University)"}})
        spec = {"title": "arXiv (Cornell University) 2026 | 解耦方向与幅度：语言引导流匹配实现暗光超分"}
        self.assertTrue(enforce_title_rules(spec, "paper", [item]))
        self.assertEqual(spec["title"], "解耦方向与幅度：语言引导流匹配实现暗光超分")


class TestCrossLanguageTitle(unittest.TestCase):
    """英文原名 vs 中文标题：同一篇论文必须认成同一篇（代号是关键，不能先小写化）。"""

    def test_same_code_different_language(self):
        self.assertGreaterEqual(grounding._similarity(
            "MixCompress: Mixture-of-Experts for Variable-Rate Neural Image Compression",
            "MixCompress: 用于可变码率神经图像压缩的专家混合模型"), 0.72)
        self.assertGreaterEqual(grounding._similarity(
            "SVI360: Spherical Video Frame Interpolation", "SVI360: 球面视频插帧"), 0.72)

    def test_different_papers_not_matched(self):
        self.assertLess(grounding._similarity(MUTE_TITLE, DELANG_TITLE), 0.72)
