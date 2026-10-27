import asyncio
import time
import unittest
from types import SimpleNamespace

from app.llm.client import extract_json, validate_spec
from app.llm.mock import DEMO_SPECS
from app.llm.prompts import (
    NO_NUMERIC_RETRY,
    drop_unsupported_tables,
    has_numeric_material,
    material_number_pool,
    quant_retry_instruction,
    thin_evidence,
    unsupported_prose_numbers,
    unsupported_table_numbers,
)
from app.render import wechat
from app.render.markdown import spec_to_markdown
from app.sources.arxiv import _safe_download, extract_result_snippets, extract_tables


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

    def test_duplicate_leading_brace(self):
        # 网关模型的常见伪影：正文前多吐一个 {（实测输出就是 '{\n{\n  "title"...'）
        self.assertEqual(extract_json('{\n{\n  "title": "x", "sections": []}')["title"], "x")

    def test_bad_snippet_then_good(self):
        self.assertEqual(extract_json('{"title": , }\n{"title": "y"}')["title"], "y")

    def test_truncated(self):
        with self.assertRaises(Exception):
            extract_json('{"title": "x", "sections": [{"type": "p"')

    def test_empty(self):
        with self.assertRaises(Exception):
            extract_json("")


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
        self.assertIn("一句话总结", html)

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


class TestEvidenceExtraction(unittest.TestCase):
    """对比表裁列不能把本文方法（Ours 列）裁掉，数字结论句要能抽出来。"""

    TABLE_HTML = """
    <html><body>
    <h1 class="ltx_title_document">Freqformer: Frequency-Aware Transformer for Demoireing</h1>
    <figure class="ltx_table">
      <figcaption class="ltx_caption">Table 1: Quantitative comparison on FHDMi.</figcaption>
      <table>
        <tr><th>Dataset</th><th>Metrics</th><th>Input</th><th>DMCNN</th><th>MDDM</th>
            <th>WDNet</th><th>MopNet</th><th>MBCNN</th><th>FHDe2Net</th>
            <th>ESDNet</th><th>ESDNet-L</th><th>Freqformer</th></tr>
        <tr><td>FHDMi</td><td>PSNR</td><td>20.30</td><td>23.52</td><td>24.21</td>
            <td>24.73</td><td>24.92</td><td>25.01</td><td>25.10</td>
            <td>25.15</td><td>25.22</td><td>25.26</td></tr>
        <tr><td>FHDMi</td><td>SSIM</td><td>0.7380</td><td>0.8011</td><td>0.8210</td>
            <td>0.8302</td><td>0.8400</td><td>0.8455</td><td>0.8470</td>
            <td>0.8490</td><td>0.8512</td><td>0.8518</td></tr>
      </table>
    </figure>
    <p>Our Freqformer achieves 25.26 dB PSNR on FHDMi, outperforming ESDNet-L by 0.04 dB
       while using only 6.065M parameters.</p>
    <p>We describe the remaining implementation details of the training pipeline here.</p>
    </body></html>
    """

    def test_wide_table_keeps_method_column(self):
        tables = extract_tables(self.TABLE_HTML)
        self.assertEqual(len(tables), 1)
        header = tables[0]["header"]
        self.assertIn("Freqformer", header)
        self.assertLessEqual(len(header), 9)
        # 本文方法那一列的数字必须还在，不能被裁列一起丢掉
        self.assertEqual(tables[0]["rows"][0][header.index("Freqformer")], "25.26")
        self.assertEqual(tables[0]["rows"][1][header.index("Freqformer")], "0.8518")

    def test_unknown_method_falls_back_to_last_column(self):
        html = self.TABLE_HTML.replace("Freqformer: Frequency-Aware", "A Study of")
        tables = extract_tables(html)
        self.assertEqual(len(tables), 1)
        self.assertIn(tables[0]["header"][-1], ("Freqformer", ""))
        self.assertEqual(tables[0]["rows"][0][-1], "25.26")

    def test_snippets_need_metric_and_number(self):
        snippets = extract_result_snippets(self.TABLE_HTML)
        self.assertEqual(len(snippets), 1)
        self.assertIn("25.26", snippets[0])
        self.assertIn("PSNR", snippets[0])


class TestEvidenceGate(unittest.TestCase):
    def test_thin_evidence(self):
        spec = {"sections": [
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "p", "text": "素材未提供具体数值，无法量化。"},
            {"type": "h2", "text": "04 | 与 SOTA 的对比与代价"},
            {"type": "bullets", "items": ["论文没有给出对比。"]},
        ]}
        self.assertTrue(thin_evidence(spec))

    def test_evidence_with_table(self):
        spec = {"sections": [
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "table", "header": ["方法", "PSNR"], "rows": [["Freqformer", "25.26"]]},
            {"type": "h2", "text": "04 | 与 SOTA 的对比与代价"},
            {"type": "p", "text": "参数量仅 6.065M。"},
        ]}
        self.assertFalse(thin_evidence(spec))

    def test_evidence_outside_range_ignored(self):
        # 别的小节有数字不算数：闸门只盯 03/04
        spec = {"sections": [
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "p", "text": "用了 3 个分支。"},
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "p", "text": "提升明显。"},
        ]}
        self.assertTrue(thin_evidence(spec))

    def test_numeric_material(self):
        plain = SimpleNamespace(extra={})
        with_table = SimpleNamespace(extra={"tables": [{"header": ["a"], "rows": []}]})
        with_snippets = SimpleNamespace(extra={"result_snippets": ["PSNR 25.26"]})
        self.assertFalse(has_numeric_material([plain]))
        self.assertTrue(has_numeric_material([plain, with_table]))
        self.assertTrue(has_numeric_material([with_snippets]))


class TestFabricationCheck(unittest.TestCase):
    """表格里的数字必须能在素材中找到（模型自造对比表是实测过的坑）。"""

    def setUp(self):
        self.item = SimpleNamespace(
            title="Freqformer",
            summary="",
            extra={
                "tables": [
                    {"header": ["Dataset", "Input", "Freqformer"],
                     "rows": [["FHDMi", "20.30", "25.26"], ["FHDMi", "0.7380", "0.8518"]]},
                ],
                "result_snippets": ["using only 6.065M parameters"],
            },
        )

    def test_pool_has_material_numbers(self):
        pool = material_number_pool([self.item])
        self.assertTrue({"25.26", "0.8518", "6.065", "0.7380"} <= pool)

    def test_copied_table_passes(self):
        spec = {"sections": [
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "table", "header": ["方法", "PSNR"], "rows": [["Freqformer", "25.26"]]},
        ]}
        self.assertEqual(unsupported_table_numbers(spec, [self.item]), [])

    def test_fabricated_table_detected(self):
        # 实测输出：模型把 25.26 改写成 26.42、LPIPS 0.153，还配了不存在的对手数字
        spec = {"sections": [
            {"type": "table", "header": ["方法", "PSNR", "LPIPS"],
             "rows": [["Mopnet", "26.42", "0.153"], ["Freqformer", "25.26", "0.134"]]},
        ]}
        bad = unsupported_table_numbers(spec, [self.item])
        self.assertEqual(sorted(bad), ["0.134", "0.153", "26.42"])

    def test_text_numbers_not_checked(self):
        # 只校验表格：正文里的「3 个模块」这类数字不该误伤
        spec = {"sections": [{"type": "p", "text": "分成 3 个模块。"}]}
        self.assertEqual(unsupported_table_numbers(spec, [self.item]), [])

    def test_drop_unsupported_tables(self):
        spec = {"sections": [
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "table", "header": ["方法", "PSNR"], "rows": [["Mopnet", "26.42"]]},
            {"type": "table", "header": ["方法", "PSNR"], "rows": [["Freqformer", "25.26"]]},
            {"type": "p", "text": "解读。"},
        ]}
        self.assertEqual(drop_unsupported_tables(spec, [self.item]), 1)
        self.assertEqual(len(spec["sections"]), 3)
        self.assertEqual(spec["sections"][1]["rows"], [["Freqformer", "25.26"]])


class TestProseDiffCheck(unittest.TestCase):
    """正文里的差值必须是素材表内两数相减的结果（实测把 2.78 写成 2.81 被放行过）。"""

    def setUp(self):
        self.item = SimpleNamespace(
            title="GMODiff",
            summary="",
            extra={"tables": [
                {"header": ["Metric", "AFUNet", "DiffHDR", "Ours"],
                 "rows": [["PU21-PSNR", "36.91", "35.43", "36.68"],
                          ["DISTS", "0.0120", "0.0125", "0.0103"],
                          ["MUSIQ", "60.66", "60.39", "60.96"]]},
                {"header": ["Metric", "AFUNet", "Ours"],
                 "rows": [["PU21-PSNR", "27.78", "26.86"],
                          ["HDR-VDP2", "66.46", "67.91"],
                          ["MUSIQ", "52.70", "55.48"]]},
            ]},
        )

    def _spec(self, text: str) -> dict:
        return {"sections": [
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "p", "text": text},
            {"type": "h2", "text": "04 | 与 SOTA 的对比与代价"},
            {"type": "p", "text": "点名最强对手 AFUNet。"},
        ]}

    def test_correct_diffs_pass(self):
        text = "PSNR 36.68，比 AFUNet 的 36.91 低 0.23 dB；MUSIQ 高 2.78，DISTS 低 0.0017。"
        self.assertEqual(unsupported_prose_numbers(self._spec(text), [self.item]), [])

    def test_wrong_diff_is_flagged(self):
        text = "MUSIQ 55.48 比 52.70 高 2.81"
        self.assertEqual(unsupported_prose_numbers(self._spec(text), [self.item]), ["2.81"])

    def test_percent_and_ratio_skipped(self):
        text = "码率节省 43.5%，实现 100 倍加速，约 2.46 倍开销。"
        self.assertEqual(unsupported_prose_numbers(self._spec(text), [self.item]), [])

    def test_only_evidence_sections_are_scanned(self):
        spec = {"sections": [{"type": "h2", "text": "02 | 核心创新与方法拆解"},
                             {"type": "p", "text": "单卡耗时 3.14 秒。"}]}
        self.assertEqual(unsupported_prose_numbers(spec, [self.item]), [])


class TestRetryInstruction(unittest.TestCase):
    """二次重写指令的分支选择：素材无数字 vs 表格编了数字。"""

    def test_no_numeric_material_bans_tables(self):
        self.assertEqual(quant_retry_instruction([], False), NO_NUMERIC_RETRY)
        self.assertIn("删除所有 table 块", NO_NUMERIC_RETRY)

    def test_bad_numbers_are_named(self):
        text = quant_retry_instruction(["26.42", "0.153"], True)
        self.assertIn("26.42", text)
        self.assertIn("编造", text)


class TestSafeDownload(unittest.TestCase):
    """图片下载必须有整包硬期限：httpx 的读间隔超时挡不住慢速持续传输。"""

    def test_slow_transfer_is_cut_off(self):
        class SlowClient:
            async def download_image(self, url, referer=""):
                await asyncio.sleep(30)
                return "never"

        started = time.monotonic()
        got = asyncio.run(_safe_download(SlowClient(), "http://x", "", timeout=0.05))
        self.assertEqual(got, "")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_error_returns_empty(self):
        class BrokenClient:
            async def download_image(self, url, referer=""):
                raise RuntimeError("boom")

        self.assertEqual(asyncio.run(_safe_download(BrokenClient(), "http://x", "")), "")


if __name__ == "__main__":
    unittest.main()


class TestSummaryMutex(unittest.TestCase):
    """「总结」节与「核心结论」highlight 互斥的代码级强制。"""

    def test_dropped_when_summary_present(self):
        from app.llm.prompts import drop_redundant_summary
        spec = {"sections": [
            {"type": "h2", "text": "05 | 总结"},
            {"type": "highlight", "title": "核心结论", "text": "x"},
            {"type": "highlight", "title": "避坑提醒", "text": "y"},
        ]}
        self.assertTrue(drop_redundant_summary(spec))
        titles = [b.get("title") for b in spec["sections"] if b.get("type") == "highlight"]
        self.assertEqual(titles, ["避坑提醒"])

    def test_kept_when_no_summary(self):
        from app.llm.prompts import drop_redundant_summary
        spec = {"sections": [
            {"type": "h2", "text": "04 | 与 SOTA 的对比与分析"},
            {"type": "highlight", "title": "核心结论", "text": "x"},
        ]}
        self.assertFalse(drop_redundant_summary(spec))
        self.assertEqual(len(spec["sections"]), 2)
