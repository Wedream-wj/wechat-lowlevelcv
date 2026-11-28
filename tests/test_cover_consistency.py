"""封面图「图文 100% 强一致」回归测试。

固化 2026-11-24 事故：《CVPR 2025 | VideoGigaGAN》历史稿首图被同日的
「CVPR 2024 | SR4IR」封面顶掉（共用 `cover_paper_20261117.jpg`）。
覆盖：唯一命名 / 生成前一致性 / 归属标签复核 / 中文断行 / Prompt 条款 / 存档终态。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.llm import prompts
from app.render import cover as cover_mod
from app.render import cover_guard as guard

VGG = "CVPR 2025 | VideoGigaGAN：流引导传播重塑视频超分细节"
SR4IR = "CVPR 2024 | 超分只为识别服务？SR4IR 用任务驱动感知损失重塑超分目标"
VGG_SPEC = {"title": VGG, "subtitle": "用 GAN 先验补高频，以 LPIPS 换 PSNR 的定量权衡"}
SR4IR_SPEC = {"title": SR4IR}


def _pick(caption="", url="", source="paper-pdf", path="", owner_title=""):
    return SimpleNamespace(caption=caption, url=url, source=source,
                           path=path, owner_title=owner_title)


def _item(title):
    return SimpleNamespace(title=title)


class TestUniqueCoverName(unittest.TestCase):
    """同日多篇不同标题 → 封面文件名必不同（事故根因）。"""

    def test_same_day_different_titles_never_share_path(self):
        a = guard.cover_filename("paper", "2026.11.17", VGG_SPEC, ".jpg")
        b = guard.cover_filename("paper", "2026.11.17", SR4IR_SPEC, ".jpg")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("cover_paper_20261117_"))
        self.assertNotIn("cover_paper_20261117.jpg", (a, b))  # 不带指纹的共享名不再出现

    def test_same_title_is_idempotent(self):
        self.assertEqual(guard.cover_filename("paper", "2026.11.17", VGG_SPEC, ".jpg"),
                         guard.cover_filename("paper", "2026.11.17", VGG_SPEC, ".jpg"))

    def test_make_cover_default_name_carries_fingerprint(self, ):
        # 不传 out_path 时也必须走唯一命名（verify / pipeline 都覆盖）
        self.assertTrue(guard.cover_filename("paper", "2026.11.17", VGG_SPEC, ".png")
                        .endswith(f"_{guard.cover_fingerprint(VGG_SPEC)}.png"))

    def test_method_tokens(self):
        self.assertEqual(guard.method_tokens(VGG), {"VideoGigaGAN"})
        self.assertEqual(guard.method_tokens(SR4IR), {"SR4IR"})
        self.assertEqual(guard.cover_identity(VGG_SPEC)["domain"][:1], ["视频超分"])


class TestPickConsistency(unittest.TestCase):
    """生成前一致性检查：跨篇串图 / 主角错配 / 无法确认一律拒绝，绝不凑数。"""

    def test_brand_fallback_always_consistent(self):
        # 无 hero 时封面文字就是本期标题 → 天然 100% 一致
        self.assertTrue(guard.check_pick_consistency(VGG_SPEC, None))

    def test_matching_hero_passes(self):
        feat = _item("VideoGigaGAN: Towards Detail-rich Video Super-Resolution")
        pick = _pick(caption="Figure 1: overview of VideoGigaGAN")
        self.assertTrue(guard.check_pick_consistency(
            VGG_SPEC, pick, featured_item=feat, other_items=[feat]))

    def test_cross_paper_hero_rejected(self):
        """VideoGigaGAN 稿选到 SR4IR 图 → 硬拒。"""
        feat = _item("VideoGigaGAN: Towards Detail-rich Video Super-Resolution")
        other = _item("SR4IR: Super-Resolution for Image Recognition")
        pick = _pick(caption="SR4IR framework overview", url="https://x/SR4IR.png")
        chk = guard.check_pick_consistency(
            VGG_SPEC, pick, featured_item=feat, other_items=[feat, other])
        self.assertFalse(chk)
        self.assertIn("跨篇串图", chk.reason)

    def test_featured_original_title_may_differ_without_false_positive(self):
        """成稿缩写与 arXiv 原题本就不同名（ReDDiT / DTG-Restore）不算错配。

        「主角原题 vs 成稿标题」互斥不是可靠反证：拿它当硬规则会误杀大批
        正常稿。归属凭证是 rank() 的 order==0 与 hero 自身的归属标签。
        """
        feat = _item("DTG-Restore: Diffusion Transformer for Low-Light Enhancement")
        pick = _pick(caption="Figure 1: overview of the proposed framework")
        spec = {"title": "CVPR 2025 | 扩散步数越少越难增强？ReDDiT 用2步完成低光照增强"}
        chk = guard.check_pick_consistency(spec, pick, featured_item=feat)
        self.assertTrue(chk, chk.reason)

    def test_hero_label_with_foreign_method_rejected(self):
        """图注自己写着别的方法名 → 确凿反证，拒绝。"""
        feat = _item("VideoGigaGAN: Towards Detail-rich Video Super-Resolution")
        pick = _pick(caption="SR4IR: task-driven perceptual loss framework")
        chk = guard.check_pick_consistency(VGG_SPEC, pick, featured_item=feat)
        self.assertFalse(chk)
        self.assertIn("封面图归属可疑", chk.reason)

    def test_owner_label_mismatch_rejected(self):
        feat = _item("VideoGigaGAN: Towards Detail-rich Video Super-Resolution")
        pick = _pick(caption="Figure 1", owner_title="SR4IR: ...")
        chk = guard.check_pick_consistency(VGG_SPEC, pick, featured_item=feat)
        self.assertFalse(chk)
        self.assertIn("归属标签不符", chk.reason)

    def test_hash_filename_is_not_false_evidence(self):
        """本地 hash 文件名含数字，不能被当成「别篇方法名」误拒。"""
        feat = _item("VideoGigaGAN: Towards Detail-rich Video Super-Resolution")
        pick = _pick(caption="Figure 1: overview", path="assets/4307a8c6f036f544.png")
        self.assertTrue(guard.check_pick_consistency(
            VGG_SPEC, pick, featured_item=feat, other_items=[feat]))


class TestOwnershipSidecar(unittest.TestCase):
    """归属标签落盘 / 复核：杜绝复用别篇封面。"""

    def test_roundtrip_and_cross_check(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cover_paper_x.jpg"
            p.write_bytes(b"fake")
            guard.write_ownership(p, VGG_SPEC, None, issue_id="paper-20261117-2562b6")
            self.assertTrue(guard.verify_ownership(p, VGG_SPEC).ok)
            cross = guard.verify_ownership(p, SR4IR_SPEC)
            self.assertFalse(cross.ok)
            self.assertIn("禁止复用", cross.reason)
            data = json.loads(guard.sidecar_path(p).read_text(encoding="utf-8"))
            self.assertEqual(data["core_methods"], ["VideoGigaGAN"])
            self.assertEqual(data["issue_id"], "paper-20261117-2562b6")

    def test_legacy_cover_without_sidecar_is_allowed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cover_paper_20261117.jpg"   # 存量共享封面，无 sidecar
            p.write_bytes(b"fake")
            self.assertTrue(guard.verify_ownership(p, VGG_SPEC).ok)


class TestTitleLineBreak(unittest.TestCase):
    """中文短句断行合理：禁则标点绝不落在行首。"""

    def test_punctuation_never_starts_a_line(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (1080, 460)))
        titles = [VGG, SR4IR,
                  "CVPR 2026 | Real-IISR：用自回归框架统一真实红外超分",
                  "CVPR 2026 | DUO-VSR：双流蒸馏把视频超分压到一步"]
        for w in (648, 700, 760):
            for t in titles:
                _, lines = cover_mod._fit_title(d, t, w)
                self.assertTrue(lines)
                for ln in lines:
                    self.assertNotIn(ln[0], cover_mod._HANGABLE_PUNCT,
                                     f"标点落行首：{ln!r}（{t} / w={w}）")
                self.assertEqual("".join(lines).replace(" ", ""), t.replace(" ", ""))

    def test_hang_punctuation_moves_colon_to_prev_line(self):
        self.assertEqual(
            cover_mod._hang_punctuation(["CVPR 2025 | VideoGigaGAN", "：流引导传播"]),
            ["CVPR 2025 | VideoGigaGAN：", "流引导传播"])


class TestIndustryWordBreak(unittest.TestCase):
    """常见产业/人事双字词不许被劈到两行（2026-11-25 工程师回归专题）。

    中文标题没有空格分词，DP 只看均衡度时会把「辞 | 职」切开；这类词必须
    显式登记进 _NO_BREAK_BIGRAMS，让断点退到语义边界。
    """

    def test_industry_bigrams_registered(self):
        for word in ("辞职", "离职", "回归", "工程师"[:2], "英伟", "伟达", "具身"):
            self.assertIn(word, cover_mod._NO_BREAK_BIGRAMS, f"未登记：{word}")

    def test_resignation_stays_on_one_line(self):
        from PIL import Image, ImageDraw

        d = ImageDraw.Draw(Image.new("RGB", (1080, 460)))
        title = "“我走得有点仓促”：为黄仁勋辞职的工程师，11 天后考虑回英伟达"
        _, lines = cover_mod._fit_title(d, title, 648)
        self.assertEqual(len(lines), 2)
        self.assertIn("辞职", lines[0], f"「辞职」被劈开：{lines}")
        self.assertFalse(any(ln.startswith("职") for ln in lines), f"「职」落行首：{lines}")
        self.assertEqual("".join(lines).replace(" ", ""), title.replace(" ", ""))

    def test_existing_titles_no_regression(self):
        """补词表不能把既有技术标题的断行搞坏：每个标题仍完整且不落标点。"""
        from PIL import Image, ImageDraw

        d = ImageDraw.Draw(Image.new("RGB", (1080, 460)))
        titles = [VGG, SR4IR,
                  "CVPR 2026 | Real-IISR：用自回归框架统一真实红外超分",
                  "CVPR 2026 | DUO-VSR：双流蒸馏把视频超分压到一步"]
        for w in (648, 700, 760):
            for t in titles:
                _, lines = cover_mod._fit_title(d, t, w)
                self.assertEqual("".join(lines).replace(" ", ""), t.replace(" ", ""))
                for ln in lines:
                    self.assertNotIn(ln[0], cover_mod._HANGABLE_PUNCT)


class TestBoomerangArchive(unittest.TestCase):
    """定向专题入库终态：标题/封面/图片/免责清理都对得上（有正式库才跑）。"""

    def test_issue_is_archive_ready(self):
        root = Path(__file__).resolve().parents[1]
        dbp = root / "data" / "llcv.db"
        if not dbp.exists():
            self.skipTest("无本地数据库（CI 环境）")
        import sqlite3

        con = sqlite3.connect(str(dbp))
        row = con.execute(
            "SELECT title, cover_path, html, markdown, status FROM issues WHERE issue_id=?",
            ("industry-20261125-05303a",)).fetchone()
        con.close()
        if not row:
            self.skipTest("专题稿不存在（CI 环境）")
        title, cover, html, md, status = row
        self.assertIn("我走得有点仓促", title)
        self.assertIn("辞职的工程师", title)
        self.assertIn("cover_industry_20261125_", str(cover))
        self.assertNotIn("编者说明", html + md)
        self.assertNotIn("不构成投资建议", html + md)
        cdn = re.findall(r'<img[^>]+src="(https://mmbiz\.qpic\.cn/[^"]+)"', html)
        self.assertEqual(len(cdn), 7, "7 张正文图必须全部是微信 CDN HTTPS 地址")
        self.assertNotIn("http://mmbiz.qpic.cn", html)
        self.assertIn(status, ("rendered", "edited", "pushed"))


class TestPromptRule(unittest.TestCase):
    """铁律必须写进 Prompt，且覆盖四类关键点。"""

    def test_spec_guide_covers_four_rules(self):
        g = prompts.SPEC_GUIDE
        for kw in ("100% 强一致", "跨篇串图", "断行合理", "绝不强行凑数", "归属标签"):
            self.assertIn(kw, g, f"Prompt 缺规则：{kw}")

    def test_guard_constant_mirrors_prompt(self):
        for kw in ("100% 强一致", "跨篇", "断行", "生成前一致性检查"):
            self.assertIn(kw, guard.COVER_CONSISTENCY_RULE)


class TestFixedIssue(unittest.TestCase):
    """定向修复终态：库内 cover_path 指向专属文件，且归属复核通过。"""

    def test_db_and_preview_are_consistent(self):
        root = Path(__file__).resolve().parents[1]
        dbp = root / "data" / "llcv.db"
        if not dbp.exists():
            self.skipTest("无本地数据库（CI 环境）")
        conn = sqlite3.connect(str(dbp))
        row = conn.execute(
            "SELECT cover_path, status, rev FROM issues WHERE issue_id=?",
            ("paper-20261117-2562b6",)).fetchone()
        conn.close()
        if not row:
            self.skipTest("历史稿不存在（CI 环境）")
        cover_path, status, rev = row
        self.assertNotIn("cover_paper_20261117.jpg", str(cover_path),
                         "不得再指向被 SR4IR 覆盖的共享文件")
        self.assertIn("cover_paper_20261117_", str(cover_path))
        # "edited" = 修好待重推；"pushed" = 修复后已重推成功（2026-11-24 重推落地）。
        # 两种都是「专属封面已生效」的合法终态，共享封面才会走到这里。
        self.assertIn(status, ("pushed", "edited"))
        self.assertGreaterEqual(int(rev or 0), 1)
        rel = cover_path.replace("\\", "/").split("out/")[-1]
        f = root / "out" / rel
        if f.exists():
            self.assertTrue(guard.verify_ownership(f, VGG_SPEC).ok)
            self.assertFalse(guard.verify_ownership(f, SR4IR_SPEC).ok)
        pv = root / "out" / "preview_paper-20261117-2562b6.html"
        if pv.exists():
            self.assertIn("cover_paper_20261117_", pv.read_text(encoding="utf-8"))
            self.assertNotIn('src="assets/cover_paper_20261117.jpg"',
                             pv.read_text(encoding="utf-8"))


RIP_SPEC = {"title": "NTIRE 2026 离岸流分割：冠军用复合指标换来 0.27 分领先",
            "subtitle": "", "digest": "拆解 NTIRE 2026 RipDetSeg 挑战赛"}
RETOUCH_SPEC = {"title": "NTIRE 2026 摄影修图迁移：冠军用元学习稳住测试时优化",
                "subtitle": "", "digest": "摄影修图迁移赛道精读"}


class TestCrossLingualTopic(unittest.TestCase):
    """2026-11-26：中文赛题稿必须能识破英文官方图注的跨篇串图。"""

    def setUp(self):
        self.feat = SimpleNamespace(
            title="NTIRE 2026 Rip Current Detection and Segmentation (RipDetSeg)")
        self.other = SimpleNamespace(
            title="NTIRE 2026 摄影修图迁移：冠军用元学习稳住测试时优化")

    def _pick(self, caption, url=""):
        return SimpleNamespace(caption=caption, url=url, source="",
                               owner_title="", path="")

    def test_english_official_caption_from_other_track_rejected(self):
        pick = self._pick("NTIRE 2026 Photo Retouching Transfer official banner",
                          "https://x/retouching.png")
        chk = guard.check_pick_consistency(
            RIP_SPEC, pick, featured_item=self.feat,
            other_items=[self.feat, self.other])
        self.assertFalse(chk, "英文图注串图必须拦截")
        self.assertIn("跨篇串图", chk.reason)

    def test_chinese_caption_from_other_track_rejected(self):
        pick = self._pick("摄影修图迁移赛道数据集样例")
        chk = guard.check_pick_consistency(
            RIP_SPEC, pick, featured_item=self.feat,
            other_items=[self.feat, self.other])
        self.assertFalse(chk)

    def test_own_track_english_caption_passes(self):
        pick = self._pick("Rip Current Segmentation dataset sample",
                          "https://x/ripdetseg.png")
        self.assertTrue(guard.check_pick_consistency(
            RIP_SPEC, pick, featured_item=self.feat,
            other_items=[self.feat, self.other]))

    def test_neutral_caption_passes_without_false_positive(self):
        pick = self._pick("Figure 1: overview of the proposed framework")
        self.assertTrue(guard.check_pick_consistency(
            RIP_SPEC, pick, featured_item=self.feat, other_items=[self.feat]))

    def test_topic_tokens_normalizes_english_alias(self):
        toks = guard.topic_tokens("Photo Retouching Transfer / Rip Current Segmentation")
        self.assertIn("修图", toks)
        self.assertIn("离岸流", toks)
        self.assertIn("分割", toks)

    def test_all_three_modules_declared(self):
        self.assertEqual(set(guard.COVER_MODULES), {"paper", "industry", "contest"})


class TestCoverExclusive(unittest.TestCase):
    """全模块硬闸门：同一封面被多篇稿引用 = 跨期串图，推送前必须拒。"""

    OWNERS = [("i1", "离岸流分割", "out\\assets\\cover_contest_20261123.jpg"),
              ("i2", "摄影修图迁移", "out/assets/cover_contest_20261123.jpg"),
              ("i3", "恶劣条件 3D 重建", "out\\assets\\cover_contest_20261123.jpg"),
              ("i4", "另一篇", "out\\assets\\cover_contest_20261123_unique.jpg")]

    def test_shared_cover_is_rejected(self):
        chk = guard.assert_cover_exclusive(
            "out\\assets\\cover_contest_20261123.jpg", "i1", self.OWNERS)
        self.assertFalse(chk)
        self.assertIn("跨期串图", chk.reason)
        self.assertIn("2 篇", chk.reason)

    def test_unique_cover_passes(self):
        self.assertTrue(guard.assert_cover_exclusive(
            "out\\assets\\cover_contest_20261123_unique.jpg", "i4", self.OWNERS))

    def test_path_normalization_handles_slashes(self):
        chk = guard.assert_cover_exclusive(
            "out/assets/cover_contest_20261123.jpg", "i2", self.OWNERS)
        self.assertFalse(chk, "POSIX / Windows 路径写法必须归一后比较")

    def test_empty_path_is_skipped(self):
        self.assertTrue(guard.assert_cover_exclusive("", "i1", self.OWNERS))


class TestCoverAsset(unittest.TestCase):
    """无效链接拦截：缺文件 / 坏图 / 尺寸过小一律拒推。"""

    def test_missing_file_rejected(self):
        chk = guard.verify_cover_asset("out/__definitely_missing__.jpg")
        self.assertFalse(chk)
        self.assertIn("不存在", chk.reason)

    def test_corrupt_file_rejected(self, ):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "broken.jpg"
            p.write_bytes(b"not an image")
            chk = guard.verify_cover_asset(p)
        self.assertFalse(chk)
        self.assertIn("不可解析", chk.reason)

    def test_too_small_rejected(self):
        from PIL import Image
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "tiny.png"
            Image.new("RGB", (80, 40)).save(p)
            chk = guard.verify_cover_asset(p)
        self.assertFalse(chk)
        self.assertIn("尺寸过小", chk.reason)

    def test_real_cover_passes(self):
        root = Path(__file__).resolve().parents[1]
        p = root / "out" / "assets" / "cover_contest_20261123_f60373.jpg"
        if not p.exists():
            self.skipTest("修复产物不存在（CI 环境）")
        self.assertTrue(guard.verify_cover_asset(p, root=root))


class TestPromptAllModules(unittest.TestCase):
    """封面铁律是所有模块 PROMPT 的公共规格，不是论文模块专属。"""

    def test_every_prompt_inherits_consistency_rule(self):
        for key, text in prompts.PROMPTS.items():
            self.assertIn("100% 强一致", text, key)
            self.assertIn("跨篇串图", text, key)
            self.assertIn("校验失败拦截", text, key)

    def test_rule_names_all_three_modules(self):
        self.assertIn("学术论文 / 业界进展 / 赛事竞赛", prompts.SPEC_GUIDE)
        self.assertIn("学术论文 / 业界进展 / 赛事竞赛", guard.COVER_CONSISTENCY_RULE)

    def test_single_source_of_truth(self):
        self.assertIn(guard.COVER_CONSISTENCY_RULE, prompts.SPEC_GUIDE)


class TestFixedContestIssues(unittest.TestCase):
    """2026-11-26 定向修复终态：两篇 NTIRE 稿各有专属主题封面。"""

    CASES = {
        "contest-20261123-bda1a4": (RIP_SPEC, "hero_rip_current"),
        "contest-20261123-7a6d89": (RETOUCH_SPEC, "hero_retouching"),
    }

    def test_db_preview_and_ownership_consistent(self):
        root = Path(__file__).resolve().parents[1]
        dbp = root / "data" / "llcv.db"
        if not dbp.exists():
            self.skipTest("无本地数据库（CI 环境）")
        conn = sqlite3.connect(str(dbp))
        for iid, (spec, _) in self.CASES.items():
            row = conn.execute(
                "SELECT cover_path, status, rev FROM issues WHERE issue_id=?",
                (iid,)).fetchone()
            if not row:
                continue
            cover_path, status, rev = row
            self.assertNotIn("cover_contest_20261123.jpg", cover_path, iid)
            self.assertIn("cover_contest_20261123_", cover_path, iid)
            self.assertIn(status, ("edited", "pushed"), iid)
            self.assertGreaterEqual(int(rev or 0), 1, iid)
            rel = cover_path.replace("\\", "/").split("out/")[-1]
            f = root / "out" / rel
            if f.exists():
                self.assertTrue(guard.verify_ownership(f, spec).ok, iid)
                self.assertTrue(guard.verify_cover_asset(f, root=root).ok, iid)
            pv = root / "out" / f"preview_{iid}.html"
            if pv.exists():
                t = pv.read_text(encoding="utf-8")
                self.assertNotIn("cover_contest_20261123.jpg", t, iid)
        conn.close()

    def test_incident_shared_group_is_dissolved(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "data" / "llcv.db").exists():
            self.skipTest("无本地数据库（CI 环境）")
        import sys
        sys.path.insert(0, str(root))
        from scripts.fix_shared_covers import audit
        shared = [g["cover_path"] for g in audit()["shared"]]
        self.assertFalse(any("cover_contest_20261123.jpg" in p for p in shared),
                         "事故共享封面必须已从共用列表消失")

    def test_three_push_gates_all_pass(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "data" / "llcv.db").exists():
            self.skipTest("无本地数据库（CI 环境）")
        from app import db
        from app.config import ROOT
        owners = db.cover_owners()
        for iid, (spec, _) in self.CASES.items():
            article = db.get_article(iid)
            if not article:
                continue
            self.assertTrue(guard.verify_ownership(article.cover_path, spec).ok, iid)
            self.assertTrue(guard.assert_cover_exclusive(
                article.cover_path, iid, owners).ok, iid)
            self.assertTrue(guard.verify_cover_asset(
                article.cover_path, root=ROOT).ok, iid)


class TestModelAliasBreak(unittest.TestCase):
    """「型号 + 空格 + 别名」绝不跨行（2026-11-28 权重空间编程专题）。

    封面把「GPT-6 Astra」从空格处劈成「…GPT-6 / Astra 直接改写…」，
    读者会误以为是两款模型。规则只焊死带数字/连字符的 ASCII 型号 +
    单个空格 + ASCII 纯字母别名，其余标题断行不变。
    """

    def test_units_fuse_model_alias(self):
        units = cover_mod._units("MIT 让 GPT-6 Astra 直接改写 Qwen 权重")
        self.assertIn("GPT-6 Astra", units)
        self.assertNotIn("Astra", units)

    def test_pure_letters_and_cjk_not_fused(self):
        # 纯字母型号词不触发：「CVPR 2025」仍可自然断
        units = cover_mod._units("CVPR 2025 | VideoGigaGAN 视频超分")
        self.assertIn("CVPR", units)
        self.assertIn("2025", units)
        # 型号后面跟中文不触发，中文仍可自然断
        units2 = cover_mod._units("Qwen3.5-0.8B 权重编辑")
        self.assertNotIn("Qwen3.5-0.8B 权重", units2)

    def test_wsp_title_model_never_split(self):
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (1080, 460)))
        title = "不训练、不反向传播：MIT 让 GPT-6 Astra 直接改写 Qwen 权重"
        _, lines = cover_mod._fit_title(d, title, 648)
        self.assertIn("GPT-6 Astra", "".join(lines))
        self.assertFalse(any(ln.startswith("Astra") for ln in lines),
                         f"型号别名被劈到行首：{lines}")
        self.assertEqual("".join(lines).replace(" ", ""), title.replace(" ", ""))

    def test_historical_titles_no_regression(self):
        """熔合规则不能改变既有标题的断行结果。"""
        from PIL import Image, ImageDraw
        d = ImageDraw.Draw(Image.new("RGB", (1080, 460)))
        titles = [
            "CVPR 2025 | VideoGigaGAN：流引导传播重塑视频超分细节",
            "NTIRE 2026 离岸流分割：冠军用复合指标换来 0.27 分领先",
            "王者归来！黄仁勋重返英伟达，股票飙涨30%",
            "“我走得有点仓促”：为黄仁勋辞职的工程师，11 天后考虑回英伟达",
            "Qwen3.5-0.8B 权重编辑实验",
        ]
        expected = {
            titles[0]: ["CVPR 2025 | VideoGigaGAN：", "流引导传播重塑视频超分细节"],
            titles[1]: ["NTIRE 2026 离岸流分割：", "冠军用复合指标换来 0.27 分领先"],
            titles[2]: ["王者归来！黄仁勋重返", "英伟达，股票飙涨30%"],
            titles[4]: ["Qwen3.5-0.8B 权重编辑实验"],
        }
        for t in titles:
            _, lines = cover_mod._fit_title(d, t, 648)
            self.assertEqual("".join(lines).replace(" ", ""), t.replace(" ", ""))
            if t in expected:
                self.assertEqual(lines, expected[t], f"断行回归：{t}")


if __name__ == "__main__":
    unittest.main()
