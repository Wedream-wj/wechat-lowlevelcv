"""赛事 / Baseline 稿「观点内化 + 去口语化」测试。

覆盖用户本轮四条铁律：
① 观点内化：社区复盘 / CSDN / 知乎 的内容必须消化成主笔判断，禁止「社区复盘指出」式转述；
② 去标签化：平台名只能出现在 sources，正文出现即不合格；
③ 严谨文风：「结论互相打架 / 这个指标太卷了 / 大佬们 / 碾压」一律替换成学术口径；
④ 结构固化：05 节从「社区复盘」更名为工程视角的「复现要点」，收尾仍是「写在最后」。
"""
from __future__ import annotations

from app.llm import prompts
from app.spec_tools import (
    normalize_section_titles,
    normalize_wording,
    scrub_community_attribution,
)

PLATFORMS = ("CSDN", "知乎", "掘金", "博客园", "简书")


def _p(text: str) -> dict:
    return {"sections": [{"type": "p", "text": text}]}


def _clean(text: str) -> str:
    spec = _p(text)
    scrub_community_attribution(spec)
    return spec["sections"][0]["text"]


def _no_platform(text: str) -> bool:
    return not any(p in text for p in PLATFORMS)


class TestCommunityInternalization:
    """转述外壳必须剥掉，事实本体必须留下。"""

    def test_attribution_prefix_stripped(self):
        out = _clean("社区复盘指出，冠军方案靠三阶段训练把分数又抬了 0.3 dB。")
        assert out == "冠军方案靠三阶段训练把分数又抬了 0.3 dB。"
        assert "社区" not in out

    def test_attribution_variants(self):
        cases = {
            "社区明确提到，验证集必须按设备切分。": "验证集必须按设备切分。",
            "社区认为，结构改动的收益低于数据侧动作。": "结构改动的收益低于数据侧动作。",
            "有博主总结，频域分支要单独调学习率。": "频域分支要单独调学习率。",
            "据社区复盘，这套方案的瓶颈在推理时延。": "这套方案的瓶颈在推理时延。",
        }
        for text, want in cases.items():
            assert _clean(text) == want

    def test_csdn_author_attribution(self):
        out = _clean("CSDN 博主总结，频域分支的学习率必须单独设。")
        assert "CSDN" not in out and "博主" not in out
        assert out == "频域分支的学习率必须单独设。"

    def test_zhihu_sentence_recast(self):
        out = _clean("知乎上有回答称，多尺度集成收益最大。")
        assert _no_platform(out) and "回答" not in out
        assert out.startswith("多尺度集成")

    def test_rephrase_secondhand_into_judgement(self):
        """用户点名的那句：二手描述 → 主笔判断。"""
        out = _clean("社区把频域自适应细化描述为低高频分离优化恢复。")
        assert out == "频域自适应的核心在于低高频分离优化恢复。"

    def test_quote_shell_removed(self):
        out = _clean("正如知乎上那篇高赞回答所说，TTA 才是关键。")
        assert _no_platform(out)
        assert out == "TTA 才是关键。"

    def test_platform_in_middle_neutralized(self):
        out = _clean("在 CSDN 上，这套数据清洗流程被反复讨论。")
        assert _no_platform(out)
        assert "公开复现记录" in out
        assert out.startswith("在公开复现记录上")

    def test_lead_and_takeaway_processed(self):
        spec = {
            "lead": ["社区复盘指出，这条赛道的难点在数据对齐。"],
            "sections": [{"type": "p", "text": "正文。"}],
            "takeaway": "检索结果显示，先对齐评测口径再谈结构。",
        }
        assert scrub_community_attribution(spec) >= 2
        assert "社区" not in spec["lead"][0]
        assert "检索结果" not in spec["takeaway"]

    def test_plain_sentence_untouched(self):
        """正常句子不能被误伤（「社区」出现在非转述语境时不改）。"""
        for text in (
            "冠军方案把频域与空间两条支路并列，避免了互相干扰。",
            "验证集按设备切分后，线上分数与本地验证的相关性明显提高。",
            "该赛道要求提交可复现的推理脚本，且限制单次推理时延。",
        ):
            assert _clean(text) == text

    def test_idempotent(self):
        spec = _p("社区复盘指出，CSDN 上的复现贴提到 TTA 收益最大。")
        scrub_community_attribution(spec)
        first = spec["sections"][0]["text"]
        scrub_community_attribution(spec)
        assert spec["sections"][0]["text"] == first


class TestContestAcademicWording:
    """口语 / 街头表达必须换成研究者口径。"""

    def test_conflicting_conclusions(self):
        spec = _p("两边结论互相打架：PSNR 涨了，LPIPS 却退步。")
        normalize_wording(spec, "contest")
        out = spec["sections"][0]["text"]
        assert "打架" not in out
        assert "定量权衡矛盾" in out

    def test_over_competitive_metric(self):
        spec = _p("这个指标太卷了，边际收益越来越小。")
        normalize_wording(spec, "contest")
        out = spec["sections"][0]["text"]
        assert "卷" not in out
        assert "竞争已趋于饱和" in out

    def test_big_shots(self):
        spec = _p("大佬们都在堆集成。")
        normalize_wording(spec, "contest")
        assert "大佬" not in spec["sections"][0]["text"]

    def test_copy_away(self):
        spec = _p("先把它的数据管道抄走，再谈模型。")
        normalize_wording(spec, "contest")
        out = spec["sections"][0]["text"]
        assert "抄走" not in out
        assert "借鉴" in out

    def test_crushing_passive(self):
        spec = _p("在这个指标上，该方案被 SOTA 碾压。")
        normalize_wording(spec, "contest")
        out = spec["sections"][0]["text"]
        assert "碾压" not in out
        assert "落后于" in out

    def test_paper_rules_do_not_leak_into_contest(self):
        """论文专属的比喻替换不应在赛事稿里生效（「最强对手」在榜单语境是正常说法）。"""
        spec = _p("这一名次与最强对手只差 0.002。")
        normalize_wording(spec, "contest")
        assert "最强对手" in spec["sections"][0]["text"]


class TestSectionNaming:
    def test_community_section_renamed(self):
        spec = {"sections": [{"type": "h2", "text": "05 | 社区复盘：冠军选手怎么调"}]}
        assert normalize_section_titles(spec) == 1
        assert spec["sections"][0]["text"] == "05 | 复现要点：冠军方案的工程细节"

    def test_short_name_renamed(self):
        spec = {"sections": [{"type": "h2", "text": "05 | 社区复盘"}]}
        normalize_section_titles(spec)
        assert "社区" not in spec["sections"][0]["text"]
        assert "复现要点" in spec["sections"][0]["text"]

    def test_final_section_still_written_last(self):
        spec = {"sections": [{"type": "h2", "text": "06 | 写在最后"}]}
        normalize_section_titles(spec)
        assert spec["sections"][0]["text"] == "06 | 写在最后"


class TestPromptGuards:
    """Prompt 层的铁律不能只靠代码兜底，必须写进模板。"""

    def test_voice_rules_in_all_contest_prompts(self):
        for key in ("contest", "contest_baseline", "contest_ntire"):
            text = prompts.PROMPTS[key]
            assert "【观点内化铁律" in text, key
            assert "社区复盘指出" in text, key      # 作为禁止示例出现
            assert "CSDN" in text, key              # 平台名禁令

    def test_ntire_section_renamed(self):
        text = prompts.CONTEST_NTIRE_PROMPT
        assert '05 | 复现要点：冠军方案的工程细节' in text
        assert "05 | 社区复盘" not in text

    def test_ntire_no_longer_asks_for_attribution(self):
        text = prompts.CONTEST_NTIRE_PROMPT
        assert "注明来源社区" not in text
        assert "每条注明来源社区" not in text

    def test_system_has_internalization_rule(self):
        assert "严禁「转述痕迹」" in prompts.SYSTEM
        assert "CSDN 博主认为" in prompts.SYSTEM

    def test_system_bans_colloquialisms(self):
        for phrase in ("结论互相打架", "这个指标太卷了", "大佬们", "抄走"):
            assert phrase in prompts.SYSTEM, phrase
