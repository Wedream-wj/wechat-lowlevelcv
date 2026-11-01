"""业界进展封面策略单测：正文配图抽取、角色体系、救场与科技感底图。"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageStat

from app.render import cover as cover_mod
from app.render import cover_select
from app.sources.chinese_media import extract_article_figures
from app.models import Item

FIXTURE_HTML = """
<html><body>
<div class="sidebar"><img src="/wp-content/themes/liangziwei/imagesnew/head.jpg"></div>
<div class="content"><div class="article">
  <div class="article_info"><img src="/avatar/author.png" width="40"></div>
  <p>正文第一段。</p>
  <p><img src="https://i.example.com/uploads/chip-main.webp" alt="700TOPS 芯片发布现场"></p>
  <p>正文第二段。</p>
  <p><img src="https://i.example.com/uploads/board-closeup.webp"></p>
  <figure><img src="/static/qrcode.png" width="120">
    <figcaption>扫码加入社群</figcaption></figure>
  <p><img src="https://i.example.com/uploads/car-road.jpg"></p>
</div></div>
<div class="footer"><img src="/wp-content/uploads/imgs/qbitai-logo-1.png"></div>
</body></html>
"""


def _item(tmp_path: Path, **extra) -> Item:
    item = Item(module="industry", title="Momenta 姐妹公司首秀：700TOPS 芯片 Day 0 跑通世界模型",
                url="https://www.qbitai.com/2026/10/506866.html", source="量子位",
                summary="芯片 Day 0 跑通世界模型", category="industry")
    item.extra.update(extra)
    return item


class TestExtractArticleFigures:
    def test_keeps_body_images_only(self):
        figs = extract_article_figures(FIXTURE_HTML, "https://www.qbitai.com/2026/10/x.html")
        urls = [f["url"] for f in figs]
        # 头像 / 作者栏 / 二维码 / logo 全部剔除
        assert all("head.jpg" not in u for u in urls)
        assert all("author" not in u for u in urls)
        assert all("qrcode" not in u for u in urls)
        assert all("logo" not in u for u in urls)
        # 正文三张图都在
        assert any("chip-main" in u for u in urls)
        assert any("board-closeup" in u for u in urls)
        assert any("car-road" in u for u in urls)

    def test_first_figure_is_hero(self):
        figs = extract_article_figures(FIXTURE_HTML, "https://www.qbitai.com/2026/10/x.html")
        assert figs[0]["order"] == 0
        assert "chip-main" in figs[0]["url"]

    def test_captions(self):
        figs = extract_article_figures(FIXTURE_HTML, "https://www.qbitai.com/2026/10/x.html")
        assert figs[0]["caption"] == "700TOPS 芯片发布现场"   # alt
        assert figs[2]["caption"] == ""                        # qrcode 被滤，car-road 无说明

    def test_empty_html(self):
        assert extract_article_figures("", "https://x.com/") == []


class TestRolesAndWeights:
    def test_product_words(self):
        assert cover_select.classify("超大核 NPU 芯片特写") == "product"
        assert cover_select.classify(url="board/soc-chip.png") == "product"

    def test_venue_words(self):
        assert cover_select.classify("发布会现场") == "venue"
        assert cover_select.classify("keynote stage") == "venue"

    def test_industry_priority_order(self):
        def cand(role, source="media", caption=""):
            c = cover_select.Candidate(url="https://x/a.png", role=role,
                                       source=source, caption=caption)
            return cover_select.score(c, "industry")
        # 用户铁律：核心产品图/芯片架构 > 报道主视觉 > logo > other
        assert cand("arch") > cand("body-hero") > cand("logo") > cand("other")

    def test_body_hero_only_for_industry(self):
        cands = [{"url": "https://i.example.com/a.webp", "caption": "", "order": 0}]
        item = _item(Path("/tmp"), cover_candidates=cands)
        out = cover_select._from_items([item], "industry")
        assert out[0].role == "body-hero"
        # paper 不认 body-hero：同一条素材在论文模块按 other 处理（并被白名单拦下）
        out_paper = cover_select._from_items([item], "paper")
        assert out_paper[0].role == "other"


class TestRescue:
    def _big_image(self, tmp_path: Path) -> str:
        img = Image.new("RGB", (960, 420))
        for y in range(420):
            for x in range(0, 960, 8):
                img.putpixel((x, y), (x % 255, y % 255, (x + y) % 255))
        p = tmp_path / "hero.jpg"
        p.write_bytes(b"")
        img.save(p, "JPEG")
        return str(p)

    def test_industry_rescues_real_photos(self, tmp_path):
        hero = self._big_image(tmp_path)
        ok, w, h, _ = cover_select._probe(hero, "body-hero")
        assert ok
        cand = cover_select.Candidate(url="https://x/a.webp", path=hero,
                                      role="body-hero", source="media")
        assert cover_select._rescue_ok("industry", cand)
        # other（完全判不出是什么）的小竖图不配救场——图文不搭比没图更伤
        other = cover_select.Candidate(url="https://x/b.png", path=hero,
                                       role="other", source="media")
        other.width, other.height = 300, 500
        assert not cover_select._rescue_ok("industry", other)
        # other 的大横幅（媒体头图常见比例）可以救场
        other.width, other.height = 960, 420
        assert cover_select._rescue_ok("industry", other)
        # paper 依旧只救三大角色
        assert not cover_select._rescue_ok("paper", cand)

    def test_pick_cover_never_returns_none_for_industry(self, tmp_path, monkeypatch):
        """体检过关的实景图存在时，industry 不许返回 None（严禁封面空洞）。"""
        hero = self._big_image(tmp_path)

        class FakeClient:
            async def download_image(self, url, referer="", timeout=15.0):
                return hero

            async def text(self, url, **kw):
                return ""

        item = _item(tmp_path, cover_candidates=[
            {"url": "https://i.example.com/a.webp", "caption": "", "order": 0}])
        monkeypatch.setattr(cover_select, "min_score", lambda m: 99.0)  # 阈值拉爆
        import asyncio

        async def run():
            return await cover_select.pick_cover(FakeClient(), [item], "industry")

        pick = asyncio.run(run())
        assert pick is not None, "industry 有体检过关的实景图却不许退 None"
        assert pick.role == "body-hero"


class TestTechFallback:
    def test_fallback_cover_has_depth(self, tmp_path):
        """品牌底图必须有「高阶科技感」的层次：光晕/点阵让右上明显亮于左下。"""
        # 直接对纯底调用 _decorate：绕开标题文字亮度干扰，只量装饰层本身
        img = Image.new("RGB", (1080, 460), (9, 34, 33))
        cover_mod._decorate(img, "#5FE0C8")
        gray = img.convert("L")
        w, h = img.size
        glow = gray.crop((int(w * 0.78), 0, w, int(h * 0.30)))
        corner = gray.crop((0, int(h * 0.72), int(w * 0.16), h))
        diff = ImageStat.Stat(glow).mean[0] - ImageStat.Stat(corner).mean[0]
        assert diff > 8, f"底图层次感不足（光晕区仅比角落亮 {diff:.1f}）"
        assert ImageStat.Stat(gray).stddev[0] > 6, "底图过于平坦（疑似退化成纯渐变）"

    def test_word_wrap_unchanged(self):
        """换行铁律回归：底图升级不得影响语义断行（词是原子，不许劈开）。"""
        text = "用梯度引导补齐红外超分的视觉与感知缺口"
        units = cover_mod._units(text)
        assert "引导" in units and "红外超分" in units
        # 更强的性质：任何长度 >1 的中文原子都必须是词表里的真词（单字未登录才允许落单）
        from app.render.cn_words import is_word
        multi = [u for u in units if len(u) > 1 and cover_mod._is_cjk(u[0])]
        assert multi and all(is_word(u) for u in multi), f"出现劈开的词：{multi}"
