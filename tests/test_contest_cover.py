"""赛事 / Baseline 精读封面策略测试。

覆盖用户四级铁律：
① 官方视觉直采（og:image / Banner / 主视觉 / 官方 Logo）
② 相关匹配（数据集样例 / baseline 框架图 / 赛事现场 / 社区复盘图）
③ 尽力而为（全无图时退品牌底图，流水线不报错）
④ 不强求（判不出角色的图一张都不放）
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.models import Item
from app.render import cover as cover_mod
from app.render import cover_select as cs
from app.sources import contest_visual

OFFICIAL_HTML = """
<html><head>
  <meta property="og:image" content="https://cdn.example.com/kv/ntire-2026-keyvisual.jpg">
  <meta name="twitter:image" content="https://cdn.example.com/kv/same-keyvisual.jpg">
</head><body>
  <header class="site-header"><a href="/"><img src="/static/ntire-logo-wordmark.png"
      alt="NTIRE logo"></a></header>
  <div class="hero-banner"><img src="/uploads/stage-runtime.jpg" alt=""></div>
  <div class="article-body">
    <p>赛道介绍段落。</p>
    <p><img src="/uploads/dataset-samples-grid.jpg" alt="Dataset samples across scenes"></p>
    <p><img src="/uploads/chart-unknown.jpg"></p>
    <p><img src="https://github.com/u.png?size=40" alt="avatar"></p>
  </div>
  <footer><img src="/static/qrcode-wechat.png" alt="扫码"></footer>
</body></html>
"""

NO_VISUAL_HTML = """
<html><head><title>赛事详情</title></head><body>
  <h1>某低光增强挑战赛</h1><p>本页只有文字说明，没有任何配图。</p>
  <div class="footer"><img src="/static/qrcode.png" alt="二维码"></div>
</body></html>
"""


def _img(path: Path, size=(1200, 500), color=(40, 60, 140)) -> str:
    """造一张有内容的测试图（纯色会被「近似空白」体检挡掉）。"""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, size[0] - 40, size[1] - 40], outline=(255, 255, 255), width=4)
    d.line([40, size[1] // 2, size[0] - 40, size[1] // 2], fill=(230, 230, 240), width=3)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return str(path)


class StubClient:
    """只实现封面选图用到的方法，测试全程不联网。"""

    def __init__(self, mapping: dict[str, str] | None = None, html: str = "") -> None:
        self.mapping = mapping or {}
        self.html = html
        self.downloaded: list[str] = []

    async def download_image(self, url: str, referer: str = "", timeout: float = 15.0) -> str:
        if Path(url).exists():
            return url
        self.downloaded.append(url)
        return self.mapping.get(url, "")

    async def text(self, url: str, **kwargs) -> str:
        return self.html

    async def get_bytes(self, url: str, **kwargs):
        raise RuntimeError("测试环境不联网")


def _item(**extra) -> Item:
    item = Item(module="contest", title="NTIRE 2026 低光增强挑战赛精读",
                url="https://www.cvlai.net/ntire/2026/", source="官方赛事页",
                summary="官方挑战赛报告", category="底层视觉")
    item.extra.update(extra)
    return item


# ---------------------------------------------------------------- 官方视觉抽取
class TestExtractVisuals:
    def test_og_image_is_official(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://www.cvlai.net/ntire/2026/")
        assert visuals, "官方页必须能抽出视觉候选"
        assert visuals[0]["role"] == "official"
        assert "keyvisual" in visuals[0]["url"]

    def test_banner_container_is_official(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://x.org/")
        roles = {v["url"].rsplit("/", 1)[-1]: v["role"] for v in visuals}
        assert roles["stage-runtime.jpg"] == "official", "hero/banner 容器里的图是主视觉"

    def test_logo_detected_and_wordmark_kept(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://x.org/")
        logos = [v for v in visuals if v["role"] == "logo"]
        assert any("wordmark" in v["url"] for v in logos)

    def test_caption_gives_dataset_role(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://x.org/")
        capd = [v for v in visuals if "dataset-samples" in v["url"]]
        assert capd and capd[0]["caption"] == "Dataset samples across scenes"
        assert capd[0]["role"] == ""      # 交给 cover_select.classify 判 dataset

    def test_junk_and_avatars_dropped(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://x.org/")
        urls = " ".join(v["url"] for v in visuals)
        assert "qrcode" not in urls
        assert "size=40" not in urls

    def test_relative_urls_resolved(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://www.cvlai.net/ntire/2026/")
        assert all(v["url"].startswith("http") for v in visuals)

    def test_github_social_card_rejected(self):
        html = ('<html><head><meta property="og:image" '
                'content="https://opengraph.githubassets.com/abc/repo"></head></html>')
        assert contest_visual.extract_visuals(html, "https://github.com/a/b") == []

    def test_no_image_page_returns_empty(self):
        assert contest_visual.extract_visuals(NO_VISUAL_HTML, "https://x.org/") == []


# ---------------------------------------------------------------- 角色权重
class TestContestRoles:
    def _score(self, role: str, source: str = "official") -> float:
        return cs.score(cs.Candidate(url="https://x/a.jpg", role=role, source=source),
                        "contest")

    def test_priority_order(self):
        # 用户铁律：官方直采 > 相关匹配 > 兜底 related
        assert (self._score("official") > self._score("logo") > self._score("dataset")
                > self._score("arch") > self._score("venue") > self._score("related"))

    def test_accept_roles(self):
        allowed = cs._ACCEPT_ROLES["contest"]
        assert {"official", "logo", "dataset", "arch", "venue", "related"} <= allowed
        assert "other" not in allowed      # 判不出角色的一律不进候选

    def test_rescue_only_thematic_roles(self):
        assert cs._rescue_ok("contest", cs.Candidate(role="official"))
        assert cs._rescue_ok("contest", cs.Candidate(role="dataset"))
        # 「不强求」：related / other 分数不够就不救，宁可品牌底图
        assert not cs._rescue_ok("contest", cs.Candidate(role="related"))
        assert not cs._rescue_ok("contest", cs.Candidate(role="other"))

    def test_from_visuals_maps_first_body_image_to_related(self):
        visuals = contest_visual.extract_visuals(OFFICIAL_HTML, "https://x.org/")
        cands = cs._from_visuals(visuals, "contest")
        by_name = {c.url.rsplit("/", 1)[-1]: c for c in cands}
        # 官方页正文第一张（有图注）按图注判 dataset，而不是粗暴 related
        assert by_name["dataset-samples-grid.jpg"].role == "dataset"
        # 判不出角色的第二张正文图不会被打上 related
        assert by_name["chart-unknown.jpg"].role == "other"


# ---------------------------------------------------------------- 三种场景
class TestContestScenarios:
    def test_a_official_visual_wins(self, tmp_path):
        hero = _img(tmp_path / "kv.jpg")
        client = StubClient({"https://cdn.example.com/kv/ntire-2026-keyvisual.jpg": hero})
        item = _item(contest_detail={"visuals": [
            {"url": "https://cdn.example.com/kv/ntire-2026-keyvisual.jpg",
             "role": "official", "source": "official", "referer": item_url()},
        ]})
        pick = asyncio.run(cs.pick_cover(client, [item], "contest", {}))
        assert pick is not None
        assert pick.role == "official"
        assert pick.source == "official"
        assert pick.fit == "cover"

    def test_official_logo_uses_contain(self, tmp_path):
        logo = _img(tmp_path / "logo.png", (640, 200), (16, 18, 26))
        client = StubClient({"https://cdn.example.com/logo.png": logo})
        item = _item(contest_detail={"visuals": [
            {"url": "https://cdn.example.com/logo.png", "role": "logo",
             "source": "official", "referer": item_url()}]})
        pick = asyncio.run(cs.pick_cover(client, [item], "contest", {}))
        assert pick is not None and pick.role == "logo"
        assert pick.fit == "contain", "官方 logo 必须 contain，裁切会切掉 wordmark"

    def test_b_related_dataset_sample(self, tmp_path):
        sample = _img(tmp_path / "sample.jpg")
        client = StubClient({"https://cdn.example.com/samples.jpg": sample})
        item = _item(contest_detail={"visuals": [
            {"url": "https://cdn.example.com/samples.jpg", "role": "",
             "caption": "Dataset samples across scenes", "source": "official",
             "body_no": 0, "referer": item_url()}]})
        pick = asyncio.run(cs.pick_cover(client, [item], "contest", {}))
        assert pick is not None and pick.role == "dataset"

    def test_c_no_visual_degrades_quietly(self, tmp_path, monkeypatch):
        """穷尽检索也拿不到图：返回 None（走品牌底图），不抛异常、不硬凑。"""
        async def no_visuals(client, url, **kwargs):
            return []

        monkeypatch.setattr(contest_visual, "page_visuals", no_visuals)
        item = _item(contest_detail={"visuals": [], "fetched": True})
        pick = asyncio.run(cs.pick_cover(StubClient(html=NO_VISUAL_HTML), [item],
                                         "contest", {}))
        assert pick is None
        # 品牌底图必须能正常渲染（降级不是失败）
        out = cover_mod.make_cover({"title": "NTIRE 2026 低光增强挑战赛精读",
                                    "subtitle": "官方报告 + 冠军方案拆解"},
                                   "contest", "2026.11.01",
                                   out_path=tmp_path / "fallback.png")
        with Image.open(out) as img:
            assert img.size == (1080, 460)

    def test_garbage_image_rejected(self, tmp_path, monkeypatch):
        """抓到的是空白图/小图 → 体检挡下 → 退底图，绝不把乱码图放上封面。"""
        async def no_visuals(client, url, **kwargs):
            return []

        monkeypatch.setattr(contest_visual, "page_visuals", no_visuals)
        blank = tmp_path / "blank.png"
        Image.new("RGB", (1000, 400), (255, 255, 255)).save(blank)
        client = StubClient({"https://cdn.example.com/kv.jpg": str(blank)})
        item = _item(contest_detail={"visuals": [
            {"url": "https://cdn.example.com/kv.jpg", "role": "official",
             "source": "official"}]})
        pick = asyncio.run(cs.pick_cover(client, [item], "contest", {}))
        assert pick is None

    def test_platform_banner_counts_as_official(self, tmp_path):
        banner = _img(tmp_path / "banner.jpg")
        client = StubClient({"https://cdn.example.com/tianchi-cover.jpg": banner})
        item = _item(platform="阿里天池")
        item.image_url = "https://cdn.example.com/tianchi-cover.jpg"
        pick = asyncio.run(cs.pick_cover(client, [item], "contest", {}))
        assert pick is not None and pick.role == "official"


class TestContestTitleWrap:
    """铁律回归：赛事封面标题同样不许劈开中文词。"""

    def test_words_are_atomic(self):
        from app.render.cn_words import is_word

        units = cover_mod._units("低光增强挑战赛：把噪声当成信息的可逆成像")
        multi = [u for u in units if len(u) > 1 and cover_mod._is_cjk(u[0])]
        assert multi and all(is_word(u) for u in multi), f"出现劈开的词：{multi}"

    def test_contest_cover_fits_two_lines(self, tmp_path):
        spec = {"title": "NTIRE 2026 低光增强挑战赛：冠军方案把噪声当信息",
                "subtitle": "官方报告 + 冠军方案拆解"}
        out = cover_mod.make_cover(spec, "contest", "2026.11.01",
                                   out_path=tmp_path / "c.jpg")
        with Image.open(out) as img:
            assert img.size == (1080, 460)


def item_url() -> str:
    return "https://www.cvlai.net/ntire/2026/"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
