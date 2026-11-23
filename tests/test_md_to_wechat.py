"""md_to_wechat：本地 Markdown → 公众号富文本，正文零篡改契约。

背景事故：把外部稿件（ACM MM 撤稿解读）转推文时，最容易犯的错是「顺手润色」
和「图片外链直接塞进正文」（CSDN 图床在微信端是裂图）。这组测试把两条铁律
钉死：①渲染前后可见文本逐字相等（标记符号除外）；②图片只换 src，不碰文字。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import md_to_wechat as m2w  # noqa: E402

SAMPLE = """# 样例标题

2026 年 11 月，**某会议**出现一起争议。

一篇题为 *《Some Paper Title》*（中文副题）的论文，误差仅 $1.19 \\times 10^{-7}$，
参考文献见 `[12]`。

> **编者说明**：本文仅整理公开可核查的内容。

## 01. 四大核心质疑

### 1. 对比实验基准不对等

| 模型          | 论文 4K | 公开 4K                 | 差距      |
| ------------- | ------- | ----------------------- | --------- |
| Retinexformer | 15.75   | 37.5191（UHD-LOL 重训） | −21.77 dB |
| Zero-DCE++    | 18.16   | 15.58（多源一致）       | +2.58 dB  |

*(注：表格参考自公开讨论数据)*

<img src="https://i-blog.csdnimg.cn/direct/abc.png#pic_center" width="60%" alt="示意图" align="center" />

---

- 要点一
- 要点二 **加粗**
"""


class TestParse(unittest.TestCase):
    def test_block_kinds_in_order(self):
        blocks = m2w.parse_blocks(SAMPLE)
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["h", "p", "p", "quote", "h", "h", "table", "p",
                                 "img", "hr", "list"])
        h1 = blocks[0]
        self.assertEqual((h1.level, h1.text), (1, "样例标题"))
        img = next(b for b in blocks if b.kind == "img")
        self.assertEqual(img.width, "60%")
        self.assertEqual(img.alt, "示意图")
        self.assertIn("#pic_center", img.src)

    def test_table_cells(self):
        blocks = m2w.parse_blocks(SAMPLE)
        table = next(b for b in blocks if b.kind == "table")
        self.assertEqual(table.header, ["模型", "论文 4K", "公开 4K", "差距"])
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(table.rows[1][0], "Zero-DCE++")


class TestFidelity(unittest.TestCase):
    def test_verify_clean(self):
        blocks = m2w.parse_blocks(SAMPLE)
        self.assertEqual(m2w.verify_fidelity(blocks), [])

    def test_no_raw_markers_in_html(self):
        blocks = m2w.parse_blocks(SAMPLE)
        html = "".join(m2w.render_block(b) for b in blocks)
        text = m2w.html_text(html)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        # 斜体与公式的定界符不算正文，但公式内容必须原样保留
        self.assertIn("1.19 \\times 10^{-7}", text)
        self.assertIn("《Some Paper Title》", text)

    def test_bold_and_italic_styled(self):
        blocks = m2w.parse_blocks(SAMPLE)
        html = "".join(m2w.render_block(b) for b in blocks)
        self.assertIn("<strong", html)
        self.assertIn('font-style:italic;">《Some Paper Title》</span>', html)

    def test_math_keeps_raw_latex(self):
        blocks = m2w.parse_blocks(SAMPLE)
        html = "".join(m2w.render_block(b) for b in blocks)
        spans = re.findall(r'<span style="[^"]*font-style:italic[^"]*">([^<]*)</span>', html)
        self.assertTrue(any("\\times" in s for s in spans))      # 公式原样保留
        self.assertTrue(any("《Some Paper Title》" in s for s in spans))  # 斜体生效

    def test_hr_adds_no_text(self):
        blocks = m2w.parse_blocks(SAMPLE)
        hr = next(b for b in blocks if b.kind == "hr")
        self.assertEqual(m2w.html_text(m2w.render_block(hr)), "")

    def test_h1_not_in_body(self):
        blocks = m2w.parse_blocks(SAMPLE)
        html = "".join(m2w.render_block(b) for b in blocks)
        self.assertNotIn("样例标题", m2w.html_text(html))


class TestImageRewrite(unittest.TestCase):
    URL = "https://mmbiz.qpic.cn/mmbiz_png/xyz/0?from=appmsg"

    def _body(self) -> tuple[str, list]:
        blocks = m2w.parse_blocks(SAMPLE)
        return "".join(m2w.render_block(b) for b in blocks), blocks

    def test_only_src_changes(self):
        body, blocks = self._body()
        urls = {"https://i-blog.csdnimg.cn/direct/abc.png#pic_center": self.URL}
        before = m2w.html_text(body)
        new = m2w._replace_img_src(body, blocks, urls)
        self.assertIn(f'src="{self.URL}"', new)
        self.assertNotIn("i-blog.csdnimg.cn", new)
        self.assertEqual(m2w.html_text(new), before)   # 可见文本一字未动

    def test_markdown_output_only_swaps_img_src(self):
        blocks = m2w.parse_blocks(SAMPLE)
        urls = {"https://i-blog.csdnimg.cn/direct/abc.png#pic_center": self.URL}
        out = m2w.build_markdown(blocks, urls, "样例标题")
        self.assertIn(f'<img src="{self.URL}" width="60%"', out)
        # 除图片行与 H1（进标题字段）外逐行一致；表格行只比对单元格（对齐空格不算文字）
        def norm(line: str) -> str:
            if line.lstrip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    return "|".join("---" for _ in cells)   # 分隔行只看列数
                return "|".join(cells)
            return line.strip()

        src_lines = [norm(ln) for ln in SAMPLE.splitlines()
                     if not ln.lstrip().startswith("<img")
                     and not ln.startswith("# ")]
        out_lines = [norm(ln) for ln in out.splitlines()
                     if not ln.lstrip().startswith("<img") and ln != "# 样例标题"]
        self.assertEqual(src_lines, out_lines)


class TestDraftAttrs(unittest.TestCase):
    def test_default_attrs(self):
        from app.publish.attrs import PublishAttrs
        attrs = PublishAttrs.from_settings()
        self.assertEqual(attrs.original_type, "文字原创")
        self.assertEqual(attrs.collection, "每日推送")


class TestPreviewLocalImages(unittest.TestCase):
    """预览页防微信防盗链：正文稿走 CDN，预览页只许本地图。

    事故：微信 mmbiz.qpic.cn 带 Referer 访问会回「此图片来自微信公众平台
    未经允许不可引用」的 2KB 占位图（实测无 Referer 才是真图），预览页一引
    CDN 就是满屏裂图。钉死两条：①预览页 src 全部本地、无 mmbiz 域名；
    ②预览模板带 no-referrer 兜底。
    """

    def test_local_srcs_maps_to_relative_paths(self):
        import tempfile
        blocks = m2w.parse_blocks(SAMPLE)
        src_url = "https://i-blog.csdnimg.cn/direct/abc.png#pic_center"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            img = tmp / "local.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
            mapping = {src_url: img}
            dest = tmp / "preview_img"
            got = m2w.local_preview_srcs(blocks, mapping, dest)
            self.assertEqual(got, {src_url: "img01.png"})
            self.assertTrue((dest / "img01.png").exists())
            # 已在目标目录里的图直接引用原名，不重复拷贝
            got2 = m2w.local_preview_srcs(blocks, {src_url: dest / "img01.png"}, dest)
            self.assertEqual(got2, {src_url: "img01.png"})

    def test_preview_template_blocks_hotlink_placeholder(self):
        self.assertIn('name="referrer" content="no-referrer"', m2w.PREVIEW_TEMPLATE)
        from app.render import preview as pv
        self.assertIn('name="referrer" content="no-referrer"', pv.TEMPLATE)


if __name__ == "__main__":
    unittest.main()


class TestImport(unittest.TestCase):
    """import_md_issue：期号确定性 + 摘要取法。"""

    def test_issue_id_deterministic(self):
        from datetime import datetime
        from scripts.import_md_issue import _issue_id
        src = Path("D:/x/zzr-revisedv1.0.md")
        a = _issue_id("industry", src, datetime(2026, 11, 18, 23, 0))
        b = _issue_id("industry", src, datetime(2026, 11, 18, 9, 0))
        self.assertEqual(a, b)                       # 同源同日重复导入 = 覆盖
        self.assertRegex(a, r"^industry-20261118-[0-9a-f]{6}$")

    def test_digest_prefers_summary_section(self):
        from scripts.import_md_issue import _digest
        blocks = m2w.parse_blocks(
            "# 标题\n\n正文第一段够长吗？这里写上超过四十个字符的内容以便触发正文兜底逻辑。\n\n"
            "## 结语\n\n### 📝 文章摘要\n\n这是摘要段，应当优先于正文第一段被选中。\n")
        self.assertEqual(_digest(blocks), "这是摘要段，应当优先于正文第一段被选中。")

    def test_cover_name_carries_issue_fingerprint(self):
        """封面文件名带期号指纹：同日两期的封面不能互相覆盖（Momenta 封面顶掉撤稿稿事故）。"""
        from scripts.import_md_issue import _cover_name
        a = _cover_name("industry", "20261118", "industry-20261118-35eae5")
        b = _cover_name("industry", "20261118", "industry-20261118-7c3d21")
        self.assertNotEqual(a, b)                    # 同模块同日不同期 → 文件名必不同
        self.assertEqual(a, "cover_industry_20261118_35eae5.png")


class TestJensenFigures(unittest.TestCase):
    """自制信息图：手机优先宽度 + 专题稿零篡改契约（2026-11-23 黄仁勋专题）。

    事故：初版信息图用 1080px 画布 + 18px 说明文字，公众号正文按约 375px 全宽
    显示后小字只剩 6px，等于糊成一片。正文图统一 750px、最小字号 21px 才可读。
    """

    def test_figures_are_phone_readable(self):
        from PIL import Image
        from scripts import make_figures_jensen as F
        F.main()                                     # 幂等重绘，测试自包含
        self.assertEqual(F.W, 750)
        for name in ("fig01_timeline", "fig02_milestones", "fig03_earnings",
                     "fig04_marketcap", "fig05_supply_chain", "fig06_governance"):
            path = F.OUT / f"{name}.png"
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                self.assertEqual(im.width, 750)
                self.assertGreater(im.height, 600)
        with Image.open(F.OUT / "fig00_cover.png") as im:
            self.assertEqual(im.size, (1080, 460))  # 封面走系统 2.35:1 规范

    def test_article_contract(self):
        src = Path(__file__).resolve().parent.parent / "articles" / "jensen-return.md"
        if not src.exists():
            self.skipTest("专题稿不在当前工作区")
        text = src.read_text(encoding="utf-8")
        blocks = m2w.parse_blocks(text)
        self.assertEqual(m2w.verify_fidelity(blocks), [])
        imgs = [b.src for b in blocks if b.kind == "img"]
        self.assertEqual(len(imgs), 7)
        self.assertTrue(all((src.parent / s).exists() for s in imgs))
        self.assertNotIn("$", text)
        self.assertNotIn("\\times", text)
        self.assertEqual([len(b.rows) for b in blocks if b.kind == "table"], [5])
