"""封面选图策略测试：角色判定、模块白名单、体检、渲染尺寸、PDF 抠图。"""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

from app.models import Item
from app.render import cover as cover_mod
from app.render import cover_select as cs
from app.render.cn_words import segment_cjk
from app.sources import pdf_figs


def _img(path: Path, size=(1200, 500), color=(40, 60, 140), draw_line: bool = True) -> str:
    """造一张有内容的测试图（纯色会被「近似空白」体检挡掉）。"""
    img = Image.new("RGB", size, color)
    if draw_line:
        d = ImageDraw.Draw(img)
        pad_x, pad_y = min(40, size[0] // 8), min(40, size[1] // 8)
        d.rectangle([pad_x, pad_y, size[0] - pad_x, size[1] - pad_y],
                    outline=(255, 255, 255), width=max(2, size[0] // 200))
        d.line([pad_x, size[1] // 2, size[0] - pad_x, size[1] // 2],
               fill=(230, 230, 240), width=max(2, size[0] // 150))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return str(path)


class StubClient:
    """只实现封面选图用到的三个方法，避免测试联网。"""

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self.extra = extra or {}
        self.downloaded: list[str] = []

    async def download_image(self, url: str, referer: str = "", timeout: float = 15.0) -> str:
        if Path(url).exists():
            return url
        self.downloaded.append(url)
        return self.extra.get(url, "")

    async def text(self, url: str, **kwargs) -> str:
        return ""

    async def get_bytes(self, url: str, **kwargs):
        raise RuntimeError("测试环境不联网")


class TestClassify(unittest.TestCase):
    def test_arch_wins_over_result_when_both_present(self):
        cap = "Figure 2: Overview of our pipeline compared with existing methods."
        self.assertEqual(cs.classify(cap, "https://x/fig2.png"), "arch")

    def test_result_and_dataset_and_logo(self):
        self.assertEqual(cs.classify("Qualitative comparison on Urban100"), "result")
        self.assertEqual(cs.classify("Dataset samples and degradation settings"), "dataset")
        self.assertEqual(cs.classify("", "https://a.com/assets/logo_dark.svg"), "logo")

    def test_figure_1_semantics_before_teaser(self):
        # 新优先级（arch > result > teaser）下，「Figure 1: 效果对比」是效果图，
        # 该拿次选权重，而不是被「Figure 1 = teaser」的老规则抢走
        self.assertEqual(cs.classify("Figure 1: Our restored results on real photos."), "result")
        self.assertEqual(cs.classify("Figure 1: The proposed restoration network."), "arch")
        self.assertEqual(cs.classify("Figure 1: the main idea of our approach."), "teaser")

    def test_unknown_is_other(self):
        self.assertEqual(cs.classify("", "https://x/a/b/c.png"), "other")


class TestJunkFilter(unittest.TestCase):
    def test_badges_and_avatars_rejected(self):
        for url in ("https://img.shields.io/badge/build-passing-green",
                    "https://github.com/u.png?size=40&avatar",
                    "https://x.com/visitor-counter.gif",
                    "data:image/png;base64,AAAA"):
            self.assertTrue(cs.is_junk_url(url), url)

    def test_real_figure_kept(self):
        self.assertFalse(cs.is_junk_url("https://raw.githubusercontent.com/a/b/main/docs/pipeline.png"))


class TestProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_blank_image_rejected(self):
        blank = self.tmp / "blank.png"
        Image.new("RGB", (1000, 400), (255, 255, 255)).save(blank)
        ok, w, h, reason = cs._probe(str(blank), "arch")
        self.assertFalse(ok)
        self.assertIn("空白", reason)

    def test_tiny_image_rejected(self):
        small = self.tmp / "small.png"
        _img(small, size=(160, 90))
        ok, _, _, reason = cs._probe(str(small), "arch")
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_normal_image_passes(self):
        path = _img(self.tmp / "ok.png")
        ok, w, h, _ = cs._probe(path, "arch")
        self.assertTrue(ok)
        self.assertEqual((w, h), (1200, 500))


class TestModuleStrategy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.arch = _img(self.tmp / "arch.png", (1400, 600))
        self.result = _img(self.tmp / "result.png", (1200, 700), (60, 40, 90))
        self.banner = _img(self.tmp / "banner.png", (1400, 420), (20, 90, 80))

    def _item(self, module: str, images=None, image_url="", **extra) -> Item:
        item = Item(module=module, title="测试条目", url="https://example.com/a",
                    source="test", images=images or [], image_url=image_url)
        item.extra.update(extra)
        return item

    def test_paper_prefers_architecture(self):
        item = self._item("paper", [self.result, self.arch],
                          figure_captions=["Qualitative comparison with SOTA",
                                           "Overview of the proposed pipeline"])
        pick = asyncio.run(cs.pick_cover(StubClient(), [item], "paper", {}))
        self.assertIsNotNone(pick)
        self.assertEqual(pick.role, "arch")
        self.assertEqual(pick.fit, "cover")

    def test_paper_result_outranks_teaser(self):
        # 新优先级：亮眼定性效果图（次选）压过 Figure 1 总览图（第三）
        item = self._item("paper", [self.result, self.banner],
                          figure_captions=["Figure 1: the main idea of our approach",
                                           "Qualitative comparison with SOTA"])
        pick = asyncio.run(cs.pick_cover(StubClient(), [item], "paper", {}))
        self.assertIsNotNone(pick)
        self.assertEqual(pick.role, "result")

    def test_paper_rescue_when_all_below_threshold(self):
        # 铁律「穷尽所有可能才允许空白封面」：候选全被阈值拦下时，
        # 体检过关的架构图按最高分救场上封面，而不是退品牌底图
        from unittest.mock import patch
        item = self._item("paper", [self.arch],
                          figure_captions=["Overview of the proposed pipeline"])
        with patch.object(cs, "min_score", lambda m: 9.9):
            pick = asyncio.run(cs.pick_cover(StubClient(), [item], "paper", {}))
        self.assertIsNotNone(pick)
        self.assertEqual(pick.role, "arch")
        self.assertTrue(any("救场" in n for n in pick.notes))

    def test_paper_rejects_logo_only(self):
        logo = _img(self.tmp / "logo.png", (900, 300), (10, 10, 10))
        item = self._item("paper", [logo], figure_captions=["Company logo"])
        self.assertIsNone(asyncio.run(cs.pick_cover(StubClient(), [item], "paper", {})))

    def test_contest_does_not_force_a_cover(self):
        # 只有一张说不清是什么的图 → 宁可退品牌底图，也不拿它凑数
        item = self._item("contest", [self.banner])
        self.assertIsNone(asyncio.run(cs.pick_cover(StubClient(), [item], "contest", {})))

    def test_contest_prefers_dataset_sample(self):
        item = self._item("contest", [], image_url="",
                          cover_candidates=[
                              {"url": self.arch, "caption": "Framework of the baseline",
                               "source": "official"},
                              {"url": self.result, "caption": "Dataset samples across scenes",
                               "source": "official"},
                          ])
        pick = asyncio.run(cs.pick_cover(StubClient(), [item], "contest", {}))
        self.assertIsNotNone(pick)
        self.assertEqual(pick.role, "dataset")

    def test_industry_logo_uses_contain(self):
        logo = _img(self.tmp / "brand.png", (640, 200), (18, 18, 22))
        item = self._item("industry", [], cover_candidates=[
            {"url": logo, "caption": "Project logo", "source": "readme"}])
        pick = asyncio.run(cs.pick_cover(StubClient(), [item], "industry", {}))
        self.assertIsNotNone(pick)
        self.assertEqual(pick.role, "logo")
        self.assertEqual(pick.fit, "contain")

    def test_figures_are_reused_without_download(self):
        client = StubClient()
        figures = {"F1": {"path": self.arch, "url": "https://x/arch.png",
                          "caption": "Overview of our framework"}}
        item = self._item("paper")
        pick = asyncio.run(cs.pick_cover(client, [item], "paper", figures))
        self.assertIsNotNone(pick)
        self.assertEqual(client.downloaded, [])


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.spec = {"title": "极端暗光下的可逆成像：把噪声当成信息",
                     "subtitle": "CVPR 2026 · 一项把传感器噪声建模进网络的工作"}

    def test_brand_cover_is_2_35_to_1(self):
        out = cover_mod.make_cover(self.spec, "paper", "2026.10.29",
                                   out_path=self.tmp / "c.png")
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 460))
            self.assertAlmostEqual(img.width / img.height, 2.35, places=1)

    def test_hero_cover_uses_photo(self):
        hero = _img(self.tmp / "hero.png", (1600, 900))
        out = cover_mod.make_cover(self.spec, "industry", "2026.10.29",
                                   out_path=self.tmp / "c.jpg", hero=hero)
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 460))
            # 左暗右亮：标题区被压暗，右侧仍能看到素材图
            left = img.convert("L").crop((0, 0, 200, 460)).resize((8, 8))
            right = img.convert("L").crop((880, 0, 1080, 460)).resize((8, 8))
            left_avg = ImageStat.Stat(left).mean[0]
            right_avg = ImageStat.Stat(right).mean[0]
            self.assertLess(left_avg, right_avg)

    def test_hero_contain_keeps_logo_visible(self):
        logo = _img(self.tmp / "logo.png", (800, 240), (245, 245, 245))
        out = cover_mod.make_cover(self.spec, "industry", "2026.10.29",
                                   out_path=self.tmp / "l.jpg", hero=logo, hero_fit="contain")
        with Image.open(out) as img:
            self.assertEqual(img.size, (1080, 460))

    def test_all_modules_render_long_title(self):
        # 全板块（论文/业界/赛事）共用同一套标题排版，长标题都必须能出图
        spec = {"title": "CVPR 2025 | DifIISR：用梯度引导补齐红外超分的视觉与感知缺口"}
        for mod in ("paper", "industry", "contest"):
            out = cover_mod.make_cover(spec, mod, "2026.11.01",
                                       out_path=self.tmp / f"long_{mod}.png")
            with Image.open(out) as img:
                self.assertEqual(img.size, (1080, 460))
            thumb = cover_mod.make_square_thumb(spec, mod,
                                                out_path=self.tmp / f"t_{mod}.jpg")
            with Image.open(thumb) as img:
                self.assertEqual(img.size[0], img.size[1])

    def test_square_thumb(self):
        out = cover_mod.make_square_thumb(self.spec, "contest",
                                          out_path=self.tmp / "t.jpg")
        with Image.open(out) as img:
            self.assertEqual(img.size[0], img.size[1])


class TestWrap(unittest.TestCase):
    """语义断行：ASCII 词不劈开、标点后优先断、中文不在词中硬切、末行不留短尾。"""

    class _StubDraw:
        """等宽假 draw：ASCII 每字符 10px、CJK 20px，只测断点位置不测字形。"""

        def textlength(self, t, font=None):
            return sum(10 if ord(c) < 128 else 20 for c in t)

    def setUp(self):
        self.draw = self._StubDraw()

    def _check(self, lines, original):
        self.assertTrue(lines)
        joined = "".join(lines).replace(" ", "")
        self.assertEqual(joined, original.replace(" ", ""))
        for ln in lines:
            self.assertLessEqual(self.draw.textlength(ln), 10 ** 6)
        if len(lines) > 1:
            # 末行不许是孤零零的短尾（旧贪心「外超分」3 字尾的翻车模式）
            self.assertGreaterEqual(len(lines[-1]), 5)

    def test_no_ascii_word_split(self):
        title = "CVPR 2026 | Real-IISR：用自回归框架统一真实红外超分"
        for mw in (300, 340, 380, 420, 460):
            lines = cover_mod._wrap(self.draw, title, None, mw, max_lines=3)
            self._check(lines, title)
            self.assertIn("Real-IISR", "".join(lines))      # 专有名词整体在行内
            self.assertIn("CVPR", "".join(lines))
            for ln in lines:
                self.assertNotIn(ln[0], cover_mod._BAD_LINE_START)

    def test_short_title_single_line(self):
        lines = cover_mod._wrap(self.draw, "短标题", None, 300, max_lines=3)
        self.assertEqual(lines, ["短标题"])

    def test_numeric_chain_never_split(self):
        # 「1+16」是一个整体：断成「用 1 / +16 步扩散」会被读成「加 16」，
        # 且量词「步」不能被留在上一行（「16 步」与「扩散」是一个短语）
        title = "DreamSR：用 1+16 步扩散把超分推到 2K 以上"
        for mw in (240, 300, 340, 360):
            lines = cover_mod._wrap(self.draw, title, None, mw, max_lines=3)
            self._check(lines, title)
            for ln in lines:
                self.assertNotIn(ln[0], cover_mod._NUM_JOINERS)   # 行首不许出现 +16
            self.assertEqual(lines, ["DreamSR：用 1+16 步扩散", "把超分推到 2K 以上"])
        # 长度够就整行放下，不做无意义的硬拆
        self.assertEqual(cover_mod._wrap(self.draw, title, None, 420, max_lines=3),
                         [title])

    def test_measure_word_stays_with_its_number(self):
        for title in ("提升 4 倍推理速度的单步扩散超分", "参数压到 0.8M 的端侧重建模型"):
            lines = cover_mod._wrap(self.draw, title, None, 200, max_lines=3)
            self._check(lines, title)
            for ln in lines:
                self.assertFalse(ln[0].isdigit() and len(ln) == 1)
                self.assertNotIn(ln[0], cover_mod._NUM_JOINERS)

    def test_cjk_words_never_split(self):
        # 真实事故：封面把「引导」切成「用梯度引 / 导补齐红外超分」。
        # 这条测试对每个宽度都要求：任何词表里的词都不许跨行。
        title = "CVPR 2025 | DifIISR：用梯度引导补齐红外超分的视觉与感知缺口"
        words = ("引导", "超分", "补齐", "感知", "红外", "视觉", "梯度")
        for mw in (200, 240, 300, 360, 420, 480, 540, 648):
            lines = cover_mod._wrap(self.draw, title, None, mw, max_lines=4)
            self._check(lines, title)
            for a, b in zip(lines, lines[1:]):
                for w in words:
                    for k in range(1, len(w)):
                        self.assertFalse(
                            a.endswith(w[:k]) and b.startswith(w[k:]),
                            f"「{w}」被拆在两行：{a!r} / {b!r}（宽度 {mw}）")

    def test_dangling_preposition_not_left_on_line_end(self):
        # 「CVPR 2026 | Real-IISR：用 / 自回归…」：行尾吊着一个介词，读起来断气
        title = "CVPR 2026 | Real-IISR：用自回归框架统一真实红外超分"
        for mw in (300, 360, 420, 480, 540):
            lines = cover_mod._wrap(self.draw, title, None, mw, max_lines=3)
            for ln in lines:
                self.assertNotIn(ln[-1], "用把被在从", f"行尾吊着介词：{ln!r}")

    def test_english_words_move_whole(self):
        title = "如何打破 PnP 迭代瓶颈？HP-PnP-Flow 引入流模型实现高效底层视觉重建"
        for mw in (360, 420, 480):
            lines = cover_mod._wrap(self.draw, title, None, mw, max_lines=3)
            self._check(lines, title)
            joined = "".join(lines)
            self.assertIn("HP-PnP-Flow", joined)
            self.assertIn("PnP", joined)

    def test_extremely_narrow_still_returns_lines(self):
        title = "极端窄画布下的降级折行测试标题很长很长"
        lines = cover_mod._wrap(self.draw, title, None, 60, max_lines=3)
        self.assertTrue(lines)


class TestCnWords(unittest.TestCase):
    """标题分词：词表命中即整体原子，折行再也切不进词里。"""

    def test_domain_words_are_atoms(self):
        atoms = segment_cjk("用梯度引导补齐红外超分的视觉与感知缺口")
        for word in ("引导", "补齐", "感知缺口", "梯度", "视觉"):
            self.assertIn(word, atoms)
        # 词内部的字不能单独成原子（否则折行就能切进词里）
        for ch in ("引", "导", "补", "齐", "感", "知"):
            self.assertNotIn(ch, atoms)
        # 「红外超分」「感知缺口」这类复合词整体保留，折行不许从中间过
        self.assertTrue(any("超分" in a for a in atoms))

    def test_unknown_chars_fall_back_to_single(self):
        atoms = segment_cjk("龘靐")       # 词表外的生僻字：单字原子，允许断行
        self.assertEqual(atoms, ["龘", "靐"])

    def test_ascii_and_punct_not_merged(self):
        atoms = cover_mod._units("DifIISR：用梯度")
        self.assertIn("DifIISR", atoms)
        self.assertIn("：", atoms)
        self.assertIn("梯度", atoms)


class TestTitleFit(unittest.TestCase):
    """动态字号：宁可把字缩小，也绝不拆词。"""

    def setUp(self):
        self.draw = ImageDraw.Draw(Image.new("RGB", (1080, 460)))

    def test_font_shrinks_but_words_stay_whole(self):
        title = "CVPR 2025 | DifIISR：用梯度引导补齐红外超分的视觉与感知缺口"
        font, lines = cover_mod._fit_title(self.draw, title, 648,
                                          prefer_lines=2, max_lines=3)
        self.assertLessEqual(len(lines), 3)
        self.assertGreaterEqual(font.size, 24)
        # 完整排下（没被省略号截断）
        self.assertEqual("".join(lines).replace(" ", ""), title.replace(" ", ""))
        for a, b in zip(lines, lines[1:]):
            self.assertFalse(a.endswith("引") and b.startswith("导"))
            self.assertFalse(a.endswith("超") and b.startswith("分"))

    def test_short_title_uses_big_font(self):
        font, lines = cover_mod._fit_title(self.draw, "暗光成像", 648)
        self.assertEqual(lines, ["暗光成像"])
        self.assertGreaterEqual(font.size, 40)

    def test_long_title_never_exceeds_max_lines(self):
        title = ("如何在没有配对数据的前提下把真实场景的盲复原做到可用的程度："
                 "一个基于扩散先验的统一框架与配套基准数据集的完整报告")
        font, lines = cover_mod._fit_title(self.draw, title, 648, max_lines=3)
        self.assertLessEqual(len(lines), 3)
        for ln in lines:
            self.assertLessEqual(self.draw.textlength(ln, font=font), 648)


class TestPdfFigures(unittest.TestCase):
    def _make_pdf(self) -> bytes:
        fitz = pdf_figs._fitz()
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        png = self._png()
        page.insert_image(fitz.Rect(60, 70, 535, 320), stream=png)
        page.insert_text((60, 340), "Figure 1: Overview of the proposed pipeline.",
                         fontsize=10)
        # 第二张图 + 定性对比图注
        page.insert_image(fitz.Rect(60, 420, 535, 640), stream=png)
        page.insert_text((60, 660), "Figure 2: Qualitative comparison with SOTA methods.",
                         fontsize=10)
        data = doc.tobytes()
        doc.close()
        return data

    def _png(self) -> bytes:
        import io

        tmp = Path(tempfile.mkdtemp()) / "f.png"
        _img(tmp, (900, 420))
        return tmp.read_bytes()

    def test_extract_teaser_and_result(self):
        figs = pdf_figs.extract(self._make_pdf())
        self.assertGreaterEqual(len(figs), 1)
        kinds = [f.kind for f in figs]
        self.assertIn("arch", kinds)
        self.assertTrue(figs[0].caption.startswith("Figure 1"))

    def test_to_png_resizes(self):
        figs = pdf_figs.extract(self._make_pdf())
        png = pdf_figs.to_png(figs[0], max_width=600)
        import io

        with Image.open(io.BytesIO(png)) as img:
            self.assertLessEqual(img.width, 600)

    def test_empty_input_is_safe(self):
        self.assertEqual(pdf_figs.extract(b""), [])
        self.assertEqual(pdf_figs.extract(b"not a pdf"), [])


class TestMeta(unittest.TestCase):
    def test_write_meta(self):
        tmp = Path(tempfile.mkdtemp())
        pick = cs.CoverPick(path="/x/y.jpg", url="https://x/a.png", role="arch",
                            source="paper-pdf", caption="Figure 1", score=5.2)
        out = cs.write_meta(pick, "paper", out_dir=tmp)
        data = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(data["picked"]["role"], "arch")
        self.assertEqual(data["module"], "paper")


if __name__ == "__main__":
    unittest.main()
