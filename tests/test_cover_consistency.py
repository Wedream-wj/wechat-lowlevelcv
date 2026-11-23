"""封面图「图文 100% 强一致」回归测试。

固化 2026-11-24 事故：《CVPR 2025 | VideoGigaGAN》历史稿首图被同日的
「CVPR 2024 | SR4IR」封面顶掉（共用 `cover_paper_20261117.jpg`）。
覆盖：唯一命名 / 生成前一致性 / 归属标签复核 / 中文断行 / Prompt 条款 / 存档终态。
"""
from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
