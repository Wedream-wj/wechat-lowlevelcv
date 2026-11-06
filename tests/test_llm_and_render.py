import asyncio
import time
import unittest
from types import SimpleNamespace

from app.llm.client import extract_json, recover_title, validate_spec
from app.llm.mock import DEMO_SPECS
from app.llm.prompts import (
    NO_NUMERIC_RETRY,
    _method_tokens,
    drop_unsupported_tables,
    find_summary_offtopic,
    has_numeric_material,
    material_number_pool,
    prune_summary_offtopic,
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


class TestTitleRecovery(unittest.TestCase):
    """title 缺失时的降级提取：正文 5000+ 字符不该因为一个键名整篇作废。"""

    SECTIONS = [{"type": "p", "text": "正文"}]

    def test_valid_title_untouched(self):
        spec = {"title": "原标题", "sections": self.SECTIONS}
        self.assertFalse(recover_title(spec, "", None))
        self.assertEqual(spec["title"], "原标题")

    def test_alias_key(self):
        # 实测症状：模型把标题写成中文键名「标题」
        spec = {"标题": "别名键标题", "sections": self.SECTIONS}
        self.assertTrue(recover_title(spec, "", None))
        self.assertEqual(spec["title"], "别名键标题")

    def test_alias_key_headline(self):
        spec = {"headline": "Headline Title", "sections": self.SECTIONS}
        self.assertTrue(recover_title(spec, "", None))
        self.assertEqual(spec["title"], "Headline Title")

    def test_nested_object_unwrap(self):
        # 模型把整份规格包进了一个子对象
        raw = '{"article": {"title": "嵌套标题", "sections": [{"type": "p", "text": "x"}]}}'
        spec = extract_json(raw)
        self.assertTrue(recover_title(spec, raw, None))
        self.assertEqual(spec["title"], "嵌套标题")
        self.assertEqual(spec["sections"][0]["text"], "x")

    def test_nested_object_recovers_inner_title_from_markdown(self):
        # 子对象既没有 title 键，JSON 外又有 markdown 大标题
        raw = '# 总标题\n{"article": {"sections": [{"type": "p", "text": "x"}]}}'
        spec = extract_json(raw)
        self.assertTrue(recover_title(spec, raw, None))
        self.assertEqual(spec["title"], "总标题")
        self.assertEqual(spec["sections"][0]["text"], "x")

    def test_markdown_heading_outside_json(self):
        raw = '# 如何打破迭代瓶颈？\n{"sections": [{"type": "p", "text": "x"}]}'
        spec = extract_json(raw)
        self.assertTrue(recover_title(spec, raw, None))
        self.assertEqual(spec["title"], "如何打破迭代瓶颈？")

    def test_bold_heading_outside_json(self):
        raw = '**加粗标题行**\n{"sections": [{"type": "p", "text": "x"}]}'
        spec = extract_json(raw)
        self.assertTrue(recover_title(spec, raw, None))
        self.assertEqual(spec["title"], "加粗标题行")

    def test_items_metadata_fallback(self):
        spec = {"sections": self.SECTIONS}
        items = [SimpleNamespace(title="素材论文标题")]
        self.assertTrue(recover_title(spec, "", items))
        self.assertEqual(spec["title"], "素材论文标题")

    def test_recovered_title_passes_validate(self):
        # 降级提取后应能通过 validate_spec，不再抛「缺少 title」
        spec = {"标题": "别名标题", "sections": [{"type": "p", "text": "x"}]}
        recover_title(spec, "", None)
        self.assertEqual(validate_spec(spec)["title"], "别名标题")


class TestSummaryPurity(unittest.TestCase):
    """「总结」纯粹性：正文只精读 A，总结不得突然冒出 B/C 并横向拉踩。"""

    def _items(self):
        featured = SimpleNamespace(
            title="Toward Real-world Infrared Image Super-Resolution: "
                  "A Unified Autoregressive Framework and Benchmark Dataset",
            summary="Real-IISR 用 VAR 自回归主干统一真实红外超分，热图与边缘图经 DINOv3 编码。",
            extra={})
        others = [
            SimpleNamespace(
                title="TinySR: Shallow Diffusion Transformers for Real-World "
                      "Image Super-Resolution",
                summary="", extra={}),
            SimpleNamespace(
                title="PeCA: 基于调色板上下文辅助推理的动画视频测试期油漆桶着色",
                summary="", extra={}),
        ]
        return [featured, *others]

    def _spec(self):
        return {
            "title": "CVPR 2026 | Real-IISR：自回归框架统一真实红外超分",
            "takeaway": "Real-IISR 把红外先验抬到语义层，LPIPS 0.1997 优于扩散派。",
            "sections": [
                {"type": "h2", "text": "01 | 论文速览"},
                {"type": "p", "text": "Real-IISR 的方法拆解，通篇只讲这一篇。"},
                {"type": "h2", "text": "05 | 总结"},
                {"type": "bullets", "items": [
                    "TinySR：把扩散主干做浅以降低推理成本。",
                    "Real-IISR：VAR 自回归 + 语义结构先验。",
                    "PeCA：调色板上下文的测试期推理。",
                ]},
                {"type": "p", "text": "三篇里 Real-IISR 价值最高。TinySR 走压缩路线。"},
                {"type": "h2", "text": "06 | 写在最后"},
                {"type": "p", "text": "全文的思想落点。"},
            ],
        }

    def test_method_tokens_narrow(self):
        # 窄判定：专有名词式方法名才进池，普通词（Toward/Diffusion）不算
        toks = _method_tokens("Toward Real-world Image Super-Resolution TinySR Real-IISR NTIRE")
        self.assertEqual(toks, {"TinySR", "Real-IISR", "NTIRE"})

    def test_offtopic_detected(self):
        hits = find_summary_offtopic(self._spec(), self._items())
        self.assertEqual(sorted(hits), ["PeCA", "TinySR"])

    def test_prune_drops_offtopic_bullets_and_sentences(self):
        spec = self._spec()
        pruned = prune_summary_offtopic(spec, self._items())
        self.assertEqual(sorted(pruned), ["PeCA", "TinySR"])
        summary = next(b for b in spec["sections"]
                       if b.get("type") == "h2" and "总结" in b.get("text", ""))
        idx = spec["sections"].index(summary)
        nxt = next(i for i, b in enumerate(spec["sections"][idx + 1:], idx + 1)
                   if b.get("type") == "h2")
        body = spec["sections"][idx + 1:nxt]
        bullets = next(b for b in body if b.get("type") == "bullets")
        self.assertEqual(bullets["items"], ["Real-IISR：VAR 自回归 + 语义结构先验。"])
        para = next(b for b in body if b.get("type") == "p")
        self.assertEqual(para["text"], "三篇里 Real-IISR 价值最高。")
        # 正文与 takeaway 未被误伤
        self.assertIn("通篇只讲这一篇", spec["sections"][1]["text"])
        self.assertIn("Real-IISR", spec["takeaway"])

    def test_multipaper_body_counts_as_coverage(self):
        # 多篇并列精读：TinySR 与 PeCA 都在 01–03 节被正式拆解过，
        # 总结逐篇归纳是正当的，一个都不剪
        spec = self._spec()
        spec["sections"].insert(2, {"type": "h2", "text": "02 | TinySR 的浅扩散主干"})
        spec["sections"].insert(3, {"type": "p", "text": "TinySR 把扩散主干做浅，成本大降。"})
        spec["sections"].insert(4, {"type": "h2", "text": "03 | PeCA 的测试期适配"})
        spec["sections"].insert(5, {"type": "p", "text": "PeCA 用调色板上下文做时空推理。"})
        self.assertEqual(find_summary_offtopic(spec, self._items()), [])

    def test_takeaway_offtopic_cleared(self):
        spec = self._spec()
        spec["takeaway"] = "TinySR 与 PeCA 各有侧重，Real-IISR 最亮。"
        self.assertTrue(prune_summary_offtopic(spec, self._items()))
        self.assertEqual(spec["takeaway"], "")

    def test_clean_summary_untouched(self):
        spec = self._spec()
        for b in spec["sections"]:
            if b.get("type") == "bullets":
                b["items"] = [b["items"][1]]
        spec["sections"] = [b for b in spec["sections"]
                            if not (b.get("type") == "p" and "三篇里" in str(b.get("text")))]
        self.assertEqual(find_summary_offtopic(spec, self._items()), [])
        self.assertFalse(prune_summary_offtopic(spec, self._items()))


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


class TestAcademicWording(unittest.TestCase):
    """学术化文风兜底：口语比喻/口语动词只在 paper 模块替换，其他模块不动。"""

    def test_paper_module_replacements(self):
        from app.spec_tools import normalize_wording

        spec = {"sections": [
            {"type": "p", "text": "代价写在效率上，什么场景下会被反超？"},
            {"type": "bullets", "items": ["PSNR 是让出去的，最强对手要分指标看。",
                                          "框架 + 基准一起给，这个设计很讨巧。"]},
        ]}
        hits = normalize_wording(spec, "paper")
        self.assertGreaterEqual(hits, 6)
        self.assertEqual(spec["sections"][0]["text"],
                         "方法的局限性主要体现在计算效率与参数量上，方法的适用场景与潜在局限？")
        self.assertIn("属于主动的定量取舍", spec["sections"][1]["items"][0])
        self.assertIn("最先进的方法", spec["sections"][1]["items"][0])
        self.assertIn("一同提出", spec["sections"][1]["items"][1])
        self.assertIn("设计得颇为经济", spec["sections"][1]["items"][1])
        self.assertNotIn("最强对手", str(spec))
        self.assertNotIn("一起给", str(spec))

    def test_other_module_untouched(self):
        from app.spec_tools import normalize_wording

        spec = {"sections": [{"type": "p", "text": "最强对手在榜单上被反超。"}]}
        normalize_wording(spec, "contest")
        self.assertIn("最强对手", spec["sections"][0]["text"])

    def test_generic_fixes_still_global(self):
        from app.spec_tools import normalize_wording

        spec = {"sections": [{"type": "p", "text": "这套代码是抄作业来的。"}]}
        normalize_wording(spec, "contest")
        self.assertIn("借鉴", spec["sections"][0]["text"])
        self.assertNotIn("抄作业", spec["sections"][0]["text"])


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


class TestPaperTitleVenueAbbrev(unittest.TestCase):
    """论文标题必须用国际通用简称（PR/KBS/TPAMI），且分隔符统一为「 | 」。"""

    def test_full_venue_is_abbreviated(self):
        from app.llm.prompts import enforce_title_rules

        spec = {"title": "Pattern Recognition 2026｜WeaCount：空间感知解耦，攻克恶劣天气人群计数"}
        self.assertTrue(enforce_title_rules(spec, "paper"))
        self.assertEqual(spec["title"],
                         "PR 2026 | WeaCount：空间感知解耦，攻克恶劣天气人群计数")

    def test_other_known_venues(self):
        from app.llm.prompts import enforce_title_rules

        for raw, expected in (
            ("Knowledge-Based Systems 2025 | XXX", "KBS 2025 | XXX"),
            ("IEEE Transactions on Pattern Analysis and Machine Intelligence 2024｜YYY",
             "TPAMI 2024 | YYY"),
        ):
            spec = {"title": raw}
            self.assertTrue(enforce_title_rules(spec, "paper"))
            self.assertEqual(spec["title"], expected)

    def test_untouched_when_not_paper_or_no_venue(self):
        from app.llm.prompts import enforce_title_rules

        spec = {"title": "Pattern Recognition 2026｜WeaCount"}
        self.assertFalse(enforce_title_rules(spec, "industry"))
        self.assertEqual(spec["title"], "Pattern Recognition 2026｜WeaCount")

        spec = {"title": "无惧复杂失真！XXX 让低光增强一次到位"}
        self.assertFalse(enforce_title_rules(spec, "paper"))
        self.assertEqual(spec["title"], "无惧复杂失真！XXX 让低光增强一次到位")
