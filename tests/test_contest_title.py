"""赛事标题「先自审再动笔」测试。

覆盖用户本轮铁律：
① 黑话自审：生造隐喻（「拉进湿实验门口」）与论文术语（拓扑空间/流形）一律判不合格；
② 空话自审：门口 / 之巅 / 元年 / 新纪元 等删掉不影响语义的词，不得出现在标题里；
③ 可读性：标题必须交代规模数字与「这场比赛考什么」，且带赛事锚点；
④ 通俗化重构：模型重写 → 去装饰 → 公式化重拼，三级兜底且绝不把标题改坏。
"""
from __future__ import annotations

import asyncio

import pytest

from app.llm import mock as mock_mod
from app.llm.title_audit import (
    MAX_CONTEST_TITLE,
    compose_contest_title,
    contest_title_issues,
    has_hollow,
    material_hooks,
    polish_title,
)
from app.models import Item
from app.pipeline import _sanity_check_contest_title

# 用户给出的三条真实标题：第一条是事故样本，后两条是编辑部认可的标杆
BAD = "76 个生物任务、9 大领域：BioXArena 把 LLM 智能体拉进湿实验门口"
GOOD_A = "生物版 ImageNet 来了！76 个任务、9 大领域，BioXArena 开启 AI 智能体“湿实验”首秀"
GOOD_B = "大模型如何做生物实验？BioXArena 赛事开赛：用 76 个真实任务测试 AI 智能体"
NO_ANCHOR = "50 个任务、100 次测试，成功率定生死"


def _item(**kw) -> Item:
    base = dict(
        module="contest",
        title="BioXArena: A Benchmark for LLM Agents in Biology (76 tasks, 9 domains)",
        url="https://example.com/biox",
        source="example",
        category="生物智能体",
        summary="BioXArena 覆盖 9 大领域共 76 个生物任务，测试 LLM 智能体能否完成湿实验流程。",
        extra={"contest_detail": {"official_brief": "The challenge spans 9 domains and 76 tasks."}},
    )
    base.update(kw)
    return Item(**base)


class TestAudit:
    def test_bad_title_blocks_on_hollow_and_jargon(self):
        issues = contest_title_issues(BAD, _item())
        joined = " ".join(issues)
        assert "门口" in joined
        assert "生造隐喻" in joined

    def test_editorial_examples_pass(self):
        """用户给的正面样例必须零问题——规则不能比编辑更严。"""
        for title in (GOOD_A, GOOD_B):
            assert contest_title_issues(title, _item()) == []

    def test_missing_anchor_rejected(self):
        issues = contest_title_issues(NO_ANCHOR, _item())
        assert any("赛事名" in i for i in issues)

    def test_hollow_word_alone_is_enough(self):
        # 数字、锚点、动作都在，只有「之巅」这种空话也要拦下
        title = "76 个任务、9 大领域，BioXArena 站上智能体之巅"
        assert any("之巅" in i for i in contest_title_issues(title, _item()))
        assert has_hollow(title)

    def test_jargon_terms_without_hollow(self):
        title = "BioXArena 开赛：把智能体端到端对齐拓扑空间"
        issues = contest_title_issues(title, _item())
        assert any("拓扑空间" in i for i in issues)
        assert has_hollow(title)

    def test_over_length(self):
        long_title = ("大模型如何做生物实验？BioXArena 赛事开赛：用 76 个真实任务测试 AI 智能体"
                      "，覆盖基因组、蛋白结构、细胞成像等九大方向")
        assert len(long_title) > MAX_CONTEST_TITLE
        assert any("上限" in i for i in contest_title_issues(long_title, _item()))

    def test_empty_title(self):
        assert contest_title_issues("", _item()) == ["标题为空"]

    def test_no_material_number_hook_not_required(self):
        """素材里本来就没有规模数字时，不许因为「缺数字」去刁难标题。"""
        item = _item(title="某某杯图像复原挑战赛", summary="面向真实退化的图像复原赛题。",
                     extra={})
        title = "某某杯图像复原挑战赛：把低质照片复原到可交付质量"
        assert not any("缺量化" in i for i in contest_title_issues(title, item))

    def test_live_regression_simplified_title_passes(self):
        """实跑回归：量词表缺「队/万」、锚点判定过严曾把这条好标题误判（已修）。"""
        item = _item(
            title="知乎知学堂大模型挑战赛：大模型复杂表格识别与内容理解",
            summary="赛事吸引超过 20000 团队报名，覆盖表格识别与内容理解两类任务。",
            extra={"platform": "天池", "contest_detail": {"official_brief": "over 20000 teams"}},
        )
        title = "1727 队抢 2 万奖金：天池这场表格赛考的不是 OCR"
        assert contest_title_issues(title, item) == []


class TestHooks:
    def test_extracts_scale_phrases(self):
        hooks = material_hooks(_item())
        assert "9 大领域" in hooks
        assert "76 个生物任务" in hooks

    def test_no_dangling_particle(self):
        """「9 大领域共」这种把连词一起吃进去的残句不许当钩子。"""
        assert all(not h.endswith("共") for h in material_hooks(_item()))


class TestPolish:
    def test_drops_hollow_tail(self):
        out = polish_title(BAD)
        assert "门口" not in out
        assert out.startswith("76 个生物任务")

    def test_keeps_short_title_intact(self):
        assert polish_title("门口的元年") == "门口的元年"   # 削完太短 → 原样返回


class TestCompose:
    def test_formula_title_passes_audit(self):
        title = compose_contest_title(_item())
        assert "BioXArena" in title
        assert "76" in title
        assert contest_title_issues(title, _item()) == []
        assert len(title) <= MAX_CONTEST_TITLE

    def test_chinese_contest_name(self):
        item = Item(module="contest", title="知乎知学堂大模型复杂表格识别挑战赛",
                    url="u", source="s", category="表格识别",
                    summary="共 3 条赛道，总奖池 50 万元。")
        title = compose_contest_title(item)
        assert "知乎知学堂" in title
        assert "50 万元" in title
        assert contest_title_issues(title, item) == []

    def test_no_material_falls_back_to_open_sentence(self):
        item = Item(module="contest", title="某某杯挑战赛", url="u", source="s")
        title = compose_contest_title(item)
        assert "某某杯" in title and "开赛" in title


class _FakeLLM:
    def __init__(self, reply: str | None, enabled: bool = True):
        self.reply = reply
        self.enabled = enabled
        self.calls = 0

    async def rewrite_contest_title(self, spec, item, issues):
        self.calls += 1
        self.last_issues = issues
        return self.reply


class _BrokenLLM:
    enabled = True

    async def rewrite_contest_title(self, spec, item, issues):
        raise RuntimeError("端点 502")


class TestSanityCheckPipeline:
    def _run(self, spec, item, llm):
        logs: list[str] = []
        changed = asyncio.run(_sanity_check_contest_title(spec, item, llm, logs.append))
        return changed, logs

    def test_good_title_untouched(self):
        spec = {"title": GOOD_B}
        llm = _FakeLLM("不该被调用")
        changed, logs = self._run(spec, _item(), llm)
        assert changed is False
        assert spec["title"] == GOOD_B
        assert llm.calls == 0
        assert logs == []

    def test_rewrite_accepted_when_passes_audit(self):
        spec = {"title": BAD}
        llm = _FakeLLM(GOOD_B)
        changed, logs = self._run(spec, _item(), llm)
        assert changed is True
        assert spec["title"] == GOOD_B
        assert llm.calls == 1
        # 自审报告要原样交给模型，而不是让它猜哪里不合格
        assert any("门口" in i for i in llm.last_issues)
        assert any("通俗化" in line for line in logs)

    def test_rewrite_rejected_falls_back_deterministically(self):
        """模型重写回来仍是黑话 → 不接受，改用去装饰/公式化兜底。"""
        spec = {"title": BAD}
        llm = _FakeLLM("76 个任务、9 大领域：BioXArena 把智能体拉进湿实验门口")
        changed, _ = self._run(spec, _item(), llm)
        assert changed is True
        assert "门口" not in spec["title"]
        assert contest_title_issues(spec["title"], _item()) == []

    def test_llm_error_does_not_break_pipeline(self):
        spec = {"title": BAD}
        changed, logs = self._run(spec, _item(), _BrokenLLM())
        assert changed is True
        assert "门口" not in spec["title"]
        assert any("兜底" in line for line in logs)

    def test_llm_disabled_uses_deterministic_path(self):
        spec = {"title": BAD}
        llm = _FakeLLM(None, enabled=False)
        changed, _ = self._run(spec, _item(), llm)
        assert changed is True
        assert contest_title_issues(spec["title"], _item()) == []
        assert llm.calls == 0

    def test_no_candidate_keeps_original(self):
        """所有兜底都比原题更差时，宁可保留原题也不改坏。"""
        spec = {"title": NO_ANCHOR}
        # 素材里没有规模数字、没有方向词 → 公式化重拼也拼不出合格的标题
        item = Item(module="contest", title="完全无关的素材标题", url="u", source="s")
        changed, logs = self._run(spec, item, _FakeLLM(None, enabled=False))
        assert changed is False
        assert spec["title"] == NO_ANCHOR
        assert any("保留原题" in line for line in logs)

    def test_empty_title_noop(self):
        spec = {"title": "  "}
        changed, _ = self._run(spec, _item(), _FakeLLM("x"))
        assert changed is False


class TestMockTitle:
    def test_mock_contest_title_uses_formula(self):
        from app.llm.mock import mock_spec

        spec = mock_spec("contest", [_item()], {}, variant=None)
        assert "门槛" not in spec["title"]
        assert contest_title_issues(spec["title"], _item()) == []
        assert "BioXArena" in spec["title"]
