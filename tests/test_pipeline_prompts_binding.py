"""「学术论文 · 生成一期」NameError 回归测试（2026-11-28）。

事故现场：点击「生成一期」返回 `✗ 失败（pipeline）：name 'prompts' is not defined`。

根因：`app/pipeline.py` 在「素材缺本文方法（Ours）数值」的前置指令分支里按
`prompts.OURS_MISSING_NOTE` 取常量，但模块顶层只做了
`from .llm.prompts import (...)` 符号导入，从未绑定 `prompts` 这个模块名。
`run_module` 的兜底 except 会把任何异常包成 `{"stage": "pipeline", ...}`，
所以这个分支一命中，整期直接不出稿，前端只看到那句没有堆栈的报错。

这里锁死三件事：
1. `pipeline.prompts` 确实绑定到了 `app.llm.prompts`（报错里的名字有定义了）；
2. 全文件不再有「模块名.属性」式的未绑定引用（防同类问题在别的阶段复发）；
3. 素材缺 Ours 表时真管线仍能出稿、前置指令逐字送达，且非 demo 出稿会入库，
   历史出稿的分页 / 检索 / 预览 / 复制 Markdown / 编辑润色双向同步全部可用。
"""
from __future__ import annotations

import ast
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app import db, pipeline
from app.config import settings
from app.llm import prompts
from app.models import Item

PAPER_TITLE = (
    "Decoupling Direction and Magnitude: Language-Steered Flow Matching "
    "for Super-Resolution in the Dark"
)
METHOD = "DeLang-SR"


def _items() -> list[Item]:
    """两篇候选：主角带图，且**不带任何含 Ours 数值的实验表**。

    「素材缺本文方法数值」正是命中事故分支的现场（真实抓取里相当常见）。
    """
    main = Item(
        module="paper", title=PAPER_TITLE,
        url="https://arxiv.org/abs/2611.10485", source="arXiv",
        venue="NeurIPS", year="2026",
        authors=["Jiaxin Gao", "Ziyu Yue", "Yaohua Liu"],
        score=9.0, keywords=["super-resolution", "image restoration"],
        summary=("We propose DeLang-SR (Degradation-Language steered Super-Resolution), "
                 "a one-step flow-matching framework for image restoration."),
        images=["out/assets/delang-arch.png"],
        code_url="https://github.com/moriyaya/DeLang-SR",
    )
    rival = Item(
        module="paper",
        title="MUTE: Multi-Level Alignment Uncoupling Against Talking-Head "
              "Exploitation for Voice Protection",
        url="https://arxiv.org/abs/2611.11989", source="arXiv",
        venue="NeurIPS", year="2026",
        authors=["Donghyun Kim", "Jin Hong", "Seungmin Kim"], score=8.0,
        keywords=["super-resolution", "image restoration"],
        summary="Voice protection against talking-head exploitation.",
    )
    return [main, rival]


def _spec() -> dict:
    """按 OURS_MISSING_NOTE 的正确口径成稿：不输出 table 块、不写具体指标数值。"""
    return {
        "title": f"NeurIPS 2026 | {METHOD}：解耦方向与幅度，单步流匹配攻克暗光超分",
        "subtitle": "把低光退化翻译成语言先验",
        "digest": f"{METHOD} 把退化程度变成可控制的条件信号。",
        "lead": ["暗光超分的难点在于空间异质性，全局均匀的积分轨迹难以处理暗区退化。"],
        "sections": [
            {"type": "meta_card", "label": "PAPER INFO", "rows": [
                ["论文名称", PAPER_TITLE],
                ["发表期刊/会议", "NeurIPS 2026"],
                ["作者团队", "Jiaxin Gao, Ziyu Yue, Yaohua Liu"],
                ["开源代码", "https://github.com/moriyaya/DeLang-SR"],
            ]},
            {"type": "h2", "text": "01 | 研究背景与痛点"},
            {"type": "p", "text": "标准扩散与流匹配模型采用全局均匀的积分轨迹，难以处理暗区退化。"},
            {"type": "h2", "text": "02 | 核心创新与方法拆解"},
            {"type": "p", "text": f"{METHOD} 用退化语言引导速度场方向，用空间场控制幅度。"},
            {"type": "figure", "ref": "F1", "src": "assets/delang-arch.png",
             "caption": "图：整体架构。"},
            {"type": "h2", "text": "03 | 关键实验结果解读"},
            {"type": "p", "text": "作者在原文实验表格中报告了完整对比，定量指标详见原文表格；"
                                 "相对基线在暗区恢复上更稳。"},
            {"type": "h2", "text": "04 | 与 SOTA 的对比与分析"},
            {"type": "p", "text": f"对比 Restormer 等基线，{METHOD} 在暗区更稳，"
                                 "代价是单步采样需额外引入语言编码。"},
            {"type": "h2", "text": "05 | 写在最后"},
            {"type": "p", "text": "把退化程度变成可控制的条件信号，是这条路线的关键一步。"},
        ],
        "takeaway": f"{METHOD} 把流匹配的单步更新拆成方向与幅度两个控制量。",
    }


def _unbound_attribute_bases(py_file: Path) -> list[tuple[int, str, str]]:
    """列出形如 `some_module.attr` 但 some_module 在文件里从未绑定过的引用。

    `from .llm.prompts import (A, B)` 只绑定 A / B，不绑定 `prompts`；如果代码里
    又写 `prompts.C`，运行到那一行才会 NameError。这类问题单测很难覆盖到，用
    AST 静态扫一遍最省事。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                    bound.add(a.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            args = node.args
            for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                bound.add(a.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for sub in ast.walk(node.optional_vars):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.NamedExpr):
            bound.add(node.target.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return [(n.lineno, n.value.id, n.attr) for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id not in bound]


class TestPromptsBinding(unittest.TestCase):
    def test_pipeline_binds_prompts_module(self):
        self.assertIs(pipeline.prompts, prompts)
        self.assertIsInstance(pipeline.prompts.OURS_MISSING_NOTE, str)
        self.assertIn("不要输出任何 table 块", pipeline.prompts.OURS_MISSING_NOTE)

    def test_no_unbound_module_attribute_refs(self):
        """防止别的阶段再写出一个「只在运行时才炸」的裸模块名。"""
        self.assertEqual(_unbound_attribute_bases(Path(pipeline.__file__)), [])


class _FakeLLM:
    """不走网络：返回按缺 Ours 数值口径写好的稿，并记录模型收到的前置指令。"""

    enabled = True
    mode = "mock"
    seen: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def write(self, module, items, figures, date_str, on_progress=None,
                    extra_instruction="", variant=None, focus=None):
        _FakeLLM.seen.append({
            "extra_instruction": extra_instruction,
            "focus_title": getattr(focus, "title", ""),
            "n_items": len(items),
        })
        return _spec()

    async def rewrite_industry_title(self, *a, **kw):
        return None

    async def rewrite_title(self, *a, **kw):
        return None


@contextmanager
def _patched_pipeline(items: list[Item]):
    """把外网依赖换成夹具，真管线其余环节（含 run_module 与 /api/run 入口）照跑。"""
    async def fake_collect(module, progress=None):
        return list(items), []

    async def fake_enrich(client, selected, limit=0):
        return None

    async def fake_publication(client, *a, **kw):
        return {"checked": True, "accepted": True, "venue": "NeurIPS", "source": "test"}

    async def fake_figures(client, selected, module):
        main = items[0]
        return {"F1": {"item_id": f"paper:{main.fingerprint}",
                       "caption": "图：整体架构。", "path": "assets/delang-arch.png",
                       "title": main.title}}

    async def fake_cover(*a, **kw):
        return None

    with patch.object(pipeline, "collect", fake_collect), \
            patch.object(pipeline, "LLMClient", _FakeLLM), \
            patch.object(pipeline.arxiv, "enrich_figures", fake_enrich), \
            patch.object(pipeline._publication, "resolve_publication", fake_publication), \
            patch.object(pipeline, "build_figures", fake_figures), \
            patch.object(pipeline.cover_select, "pick_cover", fake_cover):
        yield


class TestPaperMissingOursPipeline(unittest.IsolatedAsyncioTestCase):
    """真管线 + 假模型：素材缺 Ours 数值时也必须正常出稿入库。"""

    def setUp(self):
        from app.config import settings as s

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._old_db, self._old_out = s.db_path, s.out_dir
        s.db_path = root / "test.db"
        s.out_dir = root / "out"
        s.db_path.parent.mkdir(parents=True, exist_ok=True)
        s.out_dir.mkdir(parents=True, exist_ok=True)
        db.init_db()
        _FakeLLM.seen = []
        self._env = patch.dict(os.environ, {"LLCV_ADMIN_TOKEN": ""})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        settings.db_path, settings.out_dir = self._old_db, self._old_out
        self._tmp.cleanup()

    async def _run(self, *, demo: bool = True):
        with _patched_pipeline(_items()):
            return await pipeline.run_module("paper", demo=demo, publish=False)

    async def test_issue_generated_when_ours_table_missing(self):
        """事故回放：修复前这里会被兜底 except 包成 NameError，整期不出稿。"""
        result = await self._run(demo=True)
        self.assertIsNotNone(result.article, f"管线未出稿：{result.errors}")
        self.assertNotIn("prompts", str(result.errors))
        self.assertNotIn("NameError", str(result.errors))
        self.assertTrue(_FakeLLM.seen, "模型没有被调用")
        # 前置指令必须逐字送达：这是 Ours 零留白铁律在缺数据时的正确口径
        self.assertEqual(_FakeLLM.seen[0]["extra_instruction"], prompts.OURS_MISSING_NOTE)
        self.assertIn(PAPER_TITLE[:20], _FakeLLM.seen[0]["focus_title"])

    async def test_accident_reproduced_and_fixed_by_binding(self):
        """A/B 对照：同一夹具下解绑 prompts 必然复现事故，重新绑定即恢复出稿。

        这条把「报错到底是不是这一行造成的」钉死，避免以后有人以为修的是别处。
        """
        saved = pipeline.prompts
        try:
            del pipeline.prompts
            broken = await self._run(demo=True)
            self.assertIsNone(broken.article)
            self.assertIn("name 'prompts' is not defined", str(broken.errors))
        finally:
            pipeline.prompts = saved
        fixed = await self._run(demo=True)
        self.assertIsNotNone(fixed.article, fixed.errors)
        self.assertEqual(_FakeLLM.seen[0]["extra_instruction"], prompts.OURS_MISSING_NOTE)

    async def test_persisted_issue_visible_in_history(self):
        """非 demo 出稿要入库，且历史出稿的分页 / 检索 / 预览 / 复制 MD / 编辑全通。"""
        from fastapi.testclient import TestClient

        from app.api import app

        result = await self._run(demo=False)
        self.assertIsNotNone(result.article, f"管线未出稿：{result.errors}")
        issue_id = result.stats["issue_id"]
        self.assertTrue(issue_id)
        self.assertIsNotNone(db.get_article(issue_id), "期号没有入库")

        client = TestClient(app)
        listing = client.get("/api/issues", params={"module": "paper", "page": 1,
                                                    "page_size": 5})
        self.assertEqual(listing.status_code, 200)
        self.assertGreaterEqual(listing.json()["total"], 1)

        search = client.get("/api/issues", params={"module": "paper", "query": METHOD})
        self.assertEqual(search.status_code, 200)
        self.assertTrue(any(r["issue_id"] == issue_id for r in search.json()["items"]))

        one = client.get(f"/api/issues/{issue_id}")
        self.assertEqual(one.status_code, 200)

        md = client.get(f"/markdown/{issue_id}")
        self.assertEqual(md.status_code, 200)
        self.assertIn(METHOD, md.text)

        preview = client.get(f"/preview/{issue_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertIn('<base href="/out/">', preview.text)

        editor = client.get(f"/api/issues/{issue_id}/edit")
        self.assertEqual(editor.status_code, 200)
        original_md = editor.json()["markdown"]
        rev = editor.json()["rev"]
        self.assertIn(METHOD, original_md)

        marker = "编辑润色回归验证"
        saved = client.post(f"/api/issues/{issue_id}/content",
                            json={"markdown": original_md + f"\n\n{marker}。"})
        self.assertEqual(saved.status_code, 200, saved.text)

        editor2 = client.get(f"/api/issues/{issue_id}/edit")
        self.assertEqual(editor2.status_code, 200)
        self.assertEqual(editor2.json()["rev"], rev + 1)
        self.assertIn(marker, editor2.json()["markdown"])
        # 双向同步：库里的 Markdown、磁盘的复制 MD 接口、详情接口都要跟着变
        self.assertIn(marker, db.get_article(issue_id).markdown)
        self.assertIn(marker, client.get(f"/markdown/{issue_id}").text)
        self.assertNotIn("prompts", str(result.errors))


    async def test_run_endpoint_succeeds(self):
        """「生成一期」真实入口：POST /api/run/paper 必须 done，不能是 failed（pipeline）。"""
        from fastapi.testclient import TestClient

        from app.api import app

        _FakeLLM.seen = []
        with _patched_pipeline(_items()):
            client = TestClient(app)
            resp = client.post("/api/run/paper", json={"publish": False})
            self.assertEqual(resp.status_code, 200, resp.text)
            job = client.get(f"/api/jobs/{resp.json()['job_id']}").json()
        self.assertEqual(job["status"], "done", job.get("progress"))
        self.assertIn("完成", job["progress"]["stage"])
        self.assertNotIn("prompts", str(job.get("errors")))
        self.assertTrue(job.get("issue_id"))
        self.assertIsNotNone(db.get_article(job["issue_id"]))
        # 入口走的是非 demo 链路：期号必须真的进了历史出稿
        listing = client.get("/api/issues", params={"module": "paper"}).json()
        self.assertTrue(any(r["issue_id"] == job["issue_id"] for r in listing["items"]))


if __name__ == "__main__":
    unittest.main()
