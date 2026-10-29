"""流水线编排：抓取 → 去重/筛选 → 配图 → 大模型成稿 → 排版 → 封面 → 入库 →（可选）草稿箱。

一条原则：**任何一步失败都要留下可读的原因**，因为这套系统大部分时间在无人值守地跑，
出问题时运营需要一眼看出是「没抓到」「筛没了」还是「模型抽风」。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db
from .config import settings, topics
from .filters import scoring
from .llm.client import LLMClient, LLMError
from .llm.prompts import (
    INDUSTRY_HOOK_RETRY,
    drop_redundant_summary,
    drop_unsupported_tables,
    enforce_title_rules,
    has_numeric_material,
    industry_lead_is_flat,
    quant_retry_instruction,
    thin_evidence,
    title_is_number_led,
    unsupported_prose_numbers,
    unsupported_table_numbers,
    _featured_item as _featured_item_for_title,
)
from .models import Article, Item
from .render import cover as cover_mod
from .render import wechat
from .render.markdown import spec_to_markdown
from .render.preview import render_preview
from .sources import MODULE_LABEL, arxiv, collect
from .sources import chinese_media as _chinese_media
from .sources import competitions as _competitions
from .sources import publication as _publication
from .sources.base import HttpClient
from .spec_tools import (
    MAX_TABLE_COLS,
    normalize_meta_cards,
    normalize_section_titles,
    prune_spec_tables,
    renumber_sections,
    strip_countdown_title,
)

DATE_FMT = "%Y.%m.%d"


class PipelineError(RuntimeError):
    pass


@dataclass
class RunResult:
    article: Article | None
    errors: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


async def build_figures(client: HttpClient, items: list[Item], module: str) -> dict[str, dict]:
    """把素材里的图片落到本地，并编成 F1/F2 引用表。"""
    figures: dict[str, dict] = {}
    max_total = 5 if module == "paper" else 2
    index = 0
    for item in items:
        for ordinal, src in enumerate((item.images or [])[: 3 if module == "paper" else 1]):
            if index >= max_total:
                return figures
            local = ""
            if src and Path(src).exists():
                local = src
            elif str(src).startswith("http"):
                local = await client.download_image(src, referer=item.url)
            if not local:
                continue
            index += 1
            ref = f"F{index}"
            # 论文素材：enrich 阶段把 images 重排成「架构图 → 结果图」并把对应图注
            # 写进 extra["figure_captions"]（与 images 等长）。未 enrich 的条目
            # （如熵视列表页缩略图）没有图注，用中性兜底——绝不拿摘要裸文本当图注
            if module == "paper":
                caps = item.extra.get("figure_captions") or []
                if ordinal < len(caps) and caps[ordinal]:
                    caption = caps[ordinal]
                elif ordinal == 0:
                    caption = item.extra.get("figure_caption") or "论文配图（自动抓取）"
                elif ordinal == 1:
                    caption = item.extra.get("figure_caption_2") or "实验结果对比图"
                else:
                    caption = "论文插图"
            else:
                caption = (item.summary or "")[:80] or item.title
            # 统一存相对 out/ 的路径：预览页与推送（rewrite_images 以 out_dir 为根解析）都能用
            try:
                rel = Path(os.path.relpath(local, settings.out_dir)).as_posix()
            except ValueError:
                rel = local
            figures[ref] = {
                "path": rel,
                "url": src,
                "caption": caption,
                "title": item.title,
                "item_id": f"{item.module}:{item.fingerprint}",
            }
    return figures


def _preview_path(issue_id: str) -> Path:
    return settings.out_dir / f"preview_{issue_id}.html"


def _relative_asset(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(os.path.relpath(path, settings.out_dir)).as_posix()
    except ValueError:
        return Path(path).as_uri()


async def run_module(module: str, *, publish: bool | None = None,
                     date_str: str | None = None, top_n: int | None = None,
                     demo: bool = False, variant: str | None = None,
                     progress: Callable[..., None] | None = None) -> RunResult:
    """demo=True：走完整抓取/筛选/配图/成稿/渲染链路，但不写库、不记已用、不推送。

    用于本地生成演示稿（scripts/demo.py），跑多少次都不影响线上选题与去重。

    variant：成稿变体。目前仅 module="contest" 支持 "baseline"——
    整篇只围绕一份往届竞赛 baseline 复现展开：讲透它对应的原赛题、解题思路与关键代码，
    不与当下在办赛事强行关联。

    progress(stage, pct, msg, event=False)：可选进度回调（控制台轮询用）。
    只在里程碑处发 event=True；进度回调自身绝不允许把流水线搞挂。
    """
    started = time.time()
    run_id = uuid.uuid4().hex[:12]
    started_iso = datetime.now().isoformat()
    errors: list[dict] = []
    date_str = date_str or datetime.now().strftime(DATE_FMT)

    def _log(msg: str) -> None:
        print(f"[pipeline:{module}] {msg}", flush=True)

    last_pct = 0

    def report(stage: str, pct: int, msg: str, event: bool = False) -> None:
        nonlocal last_pct
        if progress is None:
            return
        # 进度条只许前进：思考/写作阶段的字符数估算会有波动，回退看起来像卡死重来
        last_pct = max(last_pct, min(99, int(pct)))
        try:
            progress(stage, last_pct, msg, event)
        except Exception:  # 进度回调只影响展示，绝不能反向打断任务
            pass

    try:
        collect_cb = lambda p, m: report("抓取", 2 + int(p * 0.23), m, True)
        items, collect_errors = await collect(module, progress=collect_cb)
        errors.extend(collect_errors)
        _log(f"抓取 {len(items)} 条，源告警 {len(collect_errors)} 个")
        if not items:
            raise PipelineError("所有数据源都没有返回内容（检查网络 / RSS 地址 / API 凭证）")

        report("筛选", 28, f"抓取完成：{len(items)} 条素材，开始评分与去重", True)

        # 注意顺序：先在「写库前」取已用指纹，否则刚抓到的条目会被当成已用过
        # demo 模式不写库：指纹快照置空、素材不落库，重复跑不污染线上选题
        seen = set() if demo else db.seen_fingerprints(module, days=45)
        if not demo:
            db.save_items(items, module)

        # 防重第二道闸：候选标题与「最近出稿」历史记录做相似度比对。
        # fingerprint 只能拦同一条素材；同一篇论文被两个源收录、同一赛事换了
        # 标题后缀，会绕过指纹。历史记录由 save_article 自动归档在 issues.items_json。
        # 学习资源（baseline）是固定小池子，对它硬过滤必然走进「无内容可推」的
        # 死胡同——所以只做降档（见下方 variant 分支），硬过滤只作用于赛事/论文/业界条目。
        history_titles = db.recent_history_titles(module, days=90)
        history_dropped = 0
        if module == "contest":
            learning_all = [i for i in items if i.extra.get("is_learning_resource")]
            contests_all = [i for i in items if not i.extra.get("is_learning_resource")]
        else:
            learning_all, contests_all = [], list(items)
        learning_fresh = learning_all
        if history_titles:
            if contests_all:
                contests_all, dup_contest = scoring.dedupe_history(contests_all, history_titles)
            else:
                dup_contest = []
            if learning_all:
                learning_fresh, dup_learning = scoring.dedupe_history(learning_all, history_titles)
            else:
                dup_learning = []
            history_dropped = len(dup_contest) + len(dup_learning)
            dropped_all = dup_contest + dup_learning
            if dropped_all:
                _log(f"历史去重：挡掉 {len(dropped_all)} 条与最近出稿相似的候选："
                     + "；".join(t[:28] for t in dropped_all[:3]))
        items = contests_all + learning_fresh

        if module == "contest" and variant == "baseline":
            # 变体：整篇「竞赛 baseline 精读」。只选 baseline 本身，不强行搭配当下赛事。
            # 三档降级保证永远有稿可出（此前硬过滤曾把固定池清零导致 0 输出）：
            #   第 1 档：全新 baseline（历史出稿没写过、指纹也没用过）；
            #   第 2 档：与历史出稿相似但本条指纹未直接用过（如仓库目录改名）；
            #   第 3 档：全部学习资源兜底（含已用过的——宁可选一条写过的，也不能开天窗）。
            similar = [i for i in learning_all if i not in learning_fresh]

            def _unused(xs: list[Item]) -> list[Item]:
                return [i for i in xs if demo or i.fingerprint not in seen]

            tiers = [(1, _unused(learning_fresh)),
                     (2, _unused(similar)),
                     (3, sorted(learning_all, key=lambda x: (x.fingerprint in seen, -x.score)))]
            pool = next((xs for _, xs in tiers if xs), [])
            if pool:
                main_item = max(pool, key=lambda x: x.score)
                selected = [main_item]
                tier = next(no for no, xs in tiers if any(i is main_item for i in xs))
                _log(f"baseline 精读选题（第 {tier} 档）：{main_item.title[:40]}（{main_item.score} 分）")
            else:
                # 学习素材一条都没抓到（GitHub 限流/网络异常）：明确报因，不回退
                # 赛事速递——用户点的是「baseline 精读」，静默换内容反而误导
                raise PipelineError(
                    "baseline 学习素材抓取到 0 条：Datawhale 仓库接口可能限流"
                    "（匿名 GitHub API 仅 60 次/小时）或网络异常。稍后重试，"
                    "或在 .env 配置 GITHUB_TOKEN 提升配额至 5000 次/小时。"
                )
        elif module == "contest":
            # 默认（单场精读）：学习资源（往届 baseline）不属于赛事内容，不进入本篇；
            # 候选要多取几条——报名口关了的会被核验挡下，靠备选顺延（最多核验 5 条）
            want = max(int(top_n or 0), 5)
            selected = scoring.select(contests_all, topics, module, top_n=want, seen=seen)
        else:
            selected = scoring.select(items, topics, module, top_n=top_n, seen=seen)
        if not selected:
            # 失败要可读：漏斗说明每层挡了多少，再给最高分候选供运营对照阈值
            funnel = scoring.funnel_stats(items, topics, module, seen=seen)
            top = sorted(items, key=lambda x: x.score, reverse=True)[:3]
            top_desc = "；".join(
                f"{i.title[:32]}（{i.score}分，命中{i.keywords[:3] or '无'}）" for i in top
            ) or "（无候选）"
            raise PipelineError(
                f"抓到 {len(items) + history_dropped} 条但筛选后为 0。漏斗："
                f"{funnel['total']} 条中已用去重挡 {funnel['seen']}、"
                f"历史出稿相似挡 {history_dropped}、分数不足挡 {funnel['score']}、"
                f"关键词未命中挡 {funnel['keywords']}、超期挡 {funnel['freshness']}，"
                f"通过 {funnel['pass']}。最高分候选：{top_desc}。"
                f"源告警 {len(errors)} 个。"
            )
        _log("候选 " + "；".join(f"{i.title[:24]}({i.score})" for i in selected))
        if module == "contest" and variant != "baseline":
            # 精读只要一场：这里选出的是「按分数排的待核验候选」，最终由官方页核验收敛到 1 条
            report("筛选", 32, f"筛选完成：{len(selected)} 场候选（将核验后选定 1 场精读）", True)
        else:
            report("筛选", 32, f"筛选完成：选用 {len(selected)} 条 / 共 {len(items)} 条", True)

        report("配图", 35, "补抓论文实验数据表与结论数字…", True)
        async with HttpClient() as client:
            # enrich 必须在 select 之后：它很贵（每篇一趟 HTML），只给入选的几篇补
            if module == "paper":
                _log(f"补抓 {len(selected)} 篇论文的实验数据（每篇约 20–40s）")
                try:
                    await asyncio.wait_for(
                        arxiv.enrich_figures(client, selected, limit=len(selected)),
                        timeout=200.0,
                    )
                except asyncio.TimeoutError:
                    msg = "补抓实验数据总超时（>200s），本期可能缺少对比表"
                    errors.append({"source": "arXiv 实验数据", "error": msg})
                    _log(msg)
                except Exception as exc:  # 补数据失败不该让整期无稿
                    msg = f"补抓实验数据失败：{type(exc).__name__}: {exc}"
                    errors.append({"source": "arXiv 实验数据", "error": msg})
                    _log(msg)
                for item in selected:
                    tables = len(item.extra.get("tables") or [])
                    snippets = len(item.extra.get("result_snippets") or [])
                    _log(f"素材 {item.title[:20]}：表 {tables} 张、数值结论 {snippets} 条")
                # 发表状态查证：作者自述 → OpenReview → Semantic Scholar。
                # 已收录的把 venue 更正进标题与信息卡；纯预印本则标题严禁打 arXiv 标签。
                if selected:
                    report("配图", 40, "查证论文发表状态（会议/期刊收录）…", True)
                    for item in selected:
                        try:
                            pub = await _publication.resolve_publication(
                                client, item.title, item.extra.get("arxiv_id", ""),
                                item.extra.get("comment", ""),
                                item.extra.get("journal_ref", ""))
                        except Exception:
                            pub = {"checked": False, "reason": "查证调用异常"}
                        if pub.get("checked", True):
                            item.extra["publication"] = pub
                            if pub.get("accepted"):
                                _log(f"发表状态：{item.title[:24]} 已被 {pub['venue']} 正式收录（{pub['source']}）")
                            else:
                                _log(f"发表状态：{item.title[:24]} 检索确认仅 arXiv 预印本，标题将不带 arXiv 标签")
                        else:
                            _log(f"发表状态：{item.title[:24]} 未能查证（{pub.get('reason', '')}），按保守规则处理")
            report("配图", 42, "下载与整理配图…", True)
            # 业界稿：量子位首页只有一句摘要，入选后回抓正文，否则写出来全是空泛判断
            if module == "industry" and selected:
                report("配图", 44, "回抓产业媒体正文…", True)
                try:
                    enriched = await asyncio.wait_for(
                        _chinese_media.enrich(client, selected), timeout=60.0)
                    if enriched:
                        _log(f"补正文：{enriched} 条产业媒体素材")
                except asyncio.TimeoutError:
                    msg = "产业媒体正文回抓超时（>60s），本期按摘要撰写"
                    errors.append({"source": "产业媒体正文", "error": msg})
                    _log(msg)
                except Exception as exc:  # 补正文失败不该让整期无稿
                    msg = f"产业媒体正文回抓失败：{type(exc).__name__}: {exc}"
                    errors.append({"source": "产业媒体正文", "error": msg})
                    _log(msg)
            # baseline 精读变体：给学习资源条目补 README 摘录与文件清单（成稿的核心素材）
            if module == "contest" and variant == "baseline":
                res_item = next((i for i in selected if i.extra.get("is_learning_resource")), None)
                if res_item and res_item.extra.get("folder"):
                    report("配图", 44, "拉取 baseline 仓库的 README 与文件清单…", True)
                    detail = await _competitions.fetch_baseline_detail(
                        client, res_item.extra["folder"])
                    res_item.extra["baseline_detail"] = detail
                    _log(f"baseline 详情：files {len(detail.get('files') or [])} 个，"
                         f"README 摘录 {len(detail.get('readme_excerpt') or '')} 字")
            # 单场精读：拉官方页面做「时效核验 + 素材补全」，核验不过就顺延下一候选。
            # 只拉入选的这几条（最多 4 次），不会把整池赛事都跑一遍。
            if module == "contest" and variant != "baseline" and selected:
                report("配图", 44, "核验赛事官方页面（时效与赛题细节）…", True)
                chosen = None
                for cand in selected[:5]:
                    try:
                        detail = await asyncio.wait_for(
                            _competitions.fetch_contest_detail(client, cand), timeout=75.0)
                    except asyncio.TimeoutError:
                        detail = {"fetched": False, "error": "官方页核验超时"}
                    except Exception as exc:
                        detail = {"fetched": False, "error": f"{type(exc).__name__}: {exc}"}
                    cand.extra["contest_detail"] = detail
                    if detail.get("registration_closed"):
                        _log(f"核验：{cand.title[:28]} 官方页显示报名已截止，顺延下一候选")
                        continue
                    chosen = cand
                    _log(f"核验通过：{cand.title[:28]}（页面 {len(detail.get('page_excerpt') or '')} 字，"
                         f"时间线 {len(detail.get('timeline') or [])} 条，"
                         f"指标依据 {len(detail.get('metrics_hint') or [])} 条）")
                    break
                if chosen is None:
                    # 候选全是「报名已截止但赛程仍在」的场次：仍然成稿，但改成复盘视角
                    # （Prompt 会读 registration_closed 关掉报名入口与倒计时），不开天窗也不误导
                    chosen = selected[0]
                    chosen.extra.setdefault("contest_detail", {})["registration_closed"] = True
                    # days_left 是按 extra.deadline 算的，而天池这类页面的 deadline 常是
                    # 「整体比赛结束」，不是报名截止。报名已关时留着它，模型会写出
                    # 「3 天报名倒计时」这种与正文自相矛盾的标题，直接摘掉。
                    chosen.extra.pop("days_left", None)
                    _log("核验：候选报名均已截止，改为「赛题拆解 + 复盘」视角撰写")
                selected = [chosen]
                _log(f"单场精读选题：{chosen.title[:40]}（{chosen.score} 分）")
            figures = await build_figures(client, selected, module)
        _log(f"配图 {len(figures)} 张")
        report("配图", 48, f"配图完成：{len(figures)} 张", True)

        llm = LLMClient()
        try:
            report("成稿", 50, f"调用大模型成稿（{llm.mode}）…", True)

            def on_llm(kind: str, n: int) -> None:
                if kind == "reasoning":
                    # 推理模型先思考再动笔；不显示这段的话进度条会长时间停在 50%
                    report("成稿", min(50 + int(n * 20 / 20000), 70),
                           f"模型思考中… 已推理约 {n} 字符")
                elif kind == "retry":
                    report("成稿", 60, "上一次输出不是合法 JSON，正在让模型基于同一批素材重写…", True)
                else:
                    report("成稿", min(70 + int(n * 15 / 6000), 85),
                           f"模型写作中… 已输出约 {n} 字符")

            spec = await llm.write(module, selected, figures, date_str, on_progress=on_llm,
                                   variant=variant if module == "contest" else None)
            # 表格里的数字必须能在素材里找到；素材里一个数字都没有时，出现表格更是编的。
            # 素材有数据却写成「无法量化」也要补写。
            if module == "paper" and llm.enabled:
                has_numbers = has_numeric_material(selected)
                bad_numbers = unsupported_table_numbers(spec, selected)
                for tok in unsupported_prose_numbers(spec, selected):
                    if tok not in bad_numbers:
                        bad_numbers.append(tok)
                if bad_numbers or (has_numbers and thin_evidence(spec)):
                    if bad_numbers:
                        report("成稿", 86, "首稿实验解读里的数字对不上素材，要求照抄原文重写…", True)
                        _log("首稿含素材中找不到的数字：" + "、".join(bad_numbers[:6]))
                    else:
                        report("成稿", 86, "首稿没引用实验数值，要求模型补写量化对比…", True)
                        _log("首稿 03/04 节缺少数值证据，带补写指令重写一次")
                    try:
                        spec = await llm.write(
                            module, selected, figures, date_str,
                            on_progress=on_llm,
                            extra_instruction=quant_retry_instruction(bad_numbers, has_numbers),
                        )
                        dropped = drop_unsupported_tables(spec, selected)
                        if dropped:
                            _log(f"重写后仍有 {dropped} 张表格含未核实数字，已移除")
                        left = unsupported_prose_numbers(spec, selected)
                        if left:
                            _log("重写后仍有对不上的数字，保留原稿请人工核对：" + "、".join(left[:6]))
                    except LLMError as exc:
                        _log(f"量化重写失败，保留首稿：{exc}")
            # 业界稿开篇兜底：模型偶尔退回「近日/XX 宣布」的通稿式起手（Prompt 明令禁止）。
            # 检测到就定向重写一次导语，只改 lead、不动事实与结构。
            if module == "industry" and llm.enabled and industry_lead_is_flat(spec):
                report("成稿", 83, "开篇缺少钩子，定向重写导语…", True)
                _log(f"开篇为通稿式表述，重写导语：{str((spec.get('lead') or [''])[0])[:32]}")
                try:
                    spec = await llm.write(
                        module, selected, figures, date_str,
                        on_progress=on_llm,
                        extra_instruction=INDUSTRY_HOOK_RETRY,
                    )
                except LLMError as exc:
                    _log(f"导语重写失败，保留首稿：{exc}")
            # 「总结」节与「核心结论」highlight 互斥：Prompt 有规则但模型偶尔不遵守，
            # 代码层强制执行（首稿与重写两个出口都要过）
            if drop_redundant_summary(spec):
                _log("「总结」节与「核心结论」高亮并存，已按互斥规则移除高亮块")
            # 论文标题信誉度：标题严禁出现 arXiv 字样；venue 必须与发表状态查证
            # 结论一致（模型偶尔写错会议名），模型违规时代码层强制清洗/对齐
            if enforce_title_rules(spec, module, selected):
                _log(f"标题已按发表信誉度规则校正：{spec.get('title', '')[:44]}")
            # 跑分型标题兜底：Prompt 有禁令但模型偶尔退回数字开头——检测到就花
            # 几十 token 定向重写一次，重写结果同样要过 venue 与 arXiv 校验
            if module == "paper" and llm.enabled and title_is_number_led(spec):
                report("成稿", 84, "标题以数字开头，定向重写为动机驱动…", True)
                featured = _featured_item_for_title(spec.get("title", ""), selected)
                new_title = await llm.rewrite_title(spec, featured)
                if new_title and "arxiv" not in new_title.lower() and len(new_title) <= 44:
                    old = spec["title"]
                    spec["title"] = new_title
                    # venue 前缀在重写后可能丢失：再过一遍对齐逻辑
                    enforce_title_rules(spec, module, selected)
                    _log(f"跑分型标题已重写：{old[:24]}… → {spec['title'][:44]}")
                else:
                    _log(f"标题重写未产出合格结果（{str(new_title)[:24] or '空'}），保留原题")
            llm_mode = llm.mode
        except LLMError as exc:
            if demo:
                # demo 的目的是出样稿：大模型失败也降级 mock 完成整条链路，失败原因醒目带出
                from .llm.mock import mock_spec

                _log(f"大模型成稿失败，已降级 mock 出稿（正式运行会在此报错）：{exc}")
                spec = mock_spec(module, selected, figures, variant=variant)
                llm_mode = "mock（live 失败降级）"
            else:
                raise PipelineError(f"大模型成稿失败：{exc}") from exc
        # 两条出口（模型成稿 / 失败降级 mock）都要过的规格修正：
        # 1) 实验表列数：素材表在喂模型前已压到 5 列，但模型仍可能自己拼出超宽表；
        # 2) 章节标题：口语化叫法（「冷静一下」）收敛到栏目正式名（「写在最后」）；
        # 3) 章节编号：省略小节（如单篇时的「总结」）后必须整体前移，杜绝 04 → 06 断层；
        # 4) 业界信息卡：「主体 / 事件 / 规模或金额」三行并成一行，卡面只留「谁干了什么」。
        pruned = prune_spec_tables(spec)
        if pruned:
            _log(f"{pruned} 张表格超出移动端列数上限，已压到 {MAX_TABLE_COLS} 列"
                 "（被裁指标由正文文字承担）")
        if normalize_section_titles(spec):
            _log("章节标题已规范为栏目正式名（冷静一下 → 写在最后）")
        if normalize_meta_cards(spec, module):
            _log("业界信息卡已合并「主体 + 事件」为单行，规模/金额折入事件描述")
        if renumber_sections(spec):
            _log("章节编号已重排为连续序号："
                 + " / ".join(str(b.get("text", "")) for b in spec.get("sections") or []
                              if b.get("type") == "h2"))
        # 报名已关时，标题不能带「N 天报名倒计时」——赛程还在，但报名口关了，
        # 这种标题会和正文的「报名已结束」直接打架（Prompt 有禁令，这里兜底）
        if module == "contest" and selected:
            closed = bool((selected[0].extra.get("contest_detail") or {})
                          .get("registration_closed"))
            old_title = spec.get("title", "")
            if strip_countdown_title(spec, closed):
                _log(f"报名已关，标题去掉倒计时短语：{old_title[:28]}… → {spec['title'][:40]}")
        _log(f"成稿完成（{llm_mode}）：{spec.get('title', '')}")
        report("成稿", 85, f"成稿完成（{llm_mode}）：{spec.get('title', '')}", True)

        report("排版", 88, "渲染微信排版与预览页…", True)
        cover_path = cover_mod.make_cover(spec, module, date_str)
        body = wechat.render_body(spec, figures, module, date_str)
        markdown = spec_to_markdown(spec, figures, module)

        article = Article(
            module=module,
            title=spec.get("title", ""),
            subtitle=spec.get("subtitle", ""),
            digest=spec.get("digest", ""),
            markdown=markdown,
            html=body,
            cover_path=cover_path,
            items=selected,
            created_at=datetime.now(),
            status="rendered",
        )
        if demo:
            issue_id = f"demo-{module}-{datetime.now():%Y%m%d%H%M}"
            article.issue_id = issue_id
        else:
            issue_id = db.save_article(article)
            db.mark_used(selected, issue_id)

        preview = render_preview(
            spec,
            body,
            markdown,
            module,
            _relative_asset(cover_path),
            issue_id,
            date_str,
            MODULE_LABEL.get(module, module),
        )
        _preview_path(issue_id).write_text(preview, encoding="utf-8")
        report("排版", 97, f"预览已生成：preview_{issue_id}.html", True)

        # 附一份 Markdown 与 HTML 便于人工二次编辑
        (settings.out_dir / f"{issue_id}.md").write_text(markdown, encoding="utf-8")
        (settings.out_dir / f"{issue_id}.html").write_text(body, encoding="utf-8")

        should_publish = False if demo else (settings.auto_publish if publish is None else publish)
        if should_publish:
            article = await push_issue(issue_id)

        stats = {
            "fetched": len(items),
            "selected": len(selected),
            "figures": len(figures),
            "issue_id": issue_id,
            "preview": str(_preview_path(issue_id)),
            "llm_mode": llm_mode,
            "seconds": round(time.time() - started, 1),
            "errors": errors,
        }
        if not demo:
            db.log_run(module, "success", str(stats), run_id, started_iso)
        return RunResult(article=article, errors=errors, stats=stats)

    except Exception as exc:
        if not demo:
            db.log_run(module, "failed", f"{type(exc).__name__}: {exc}", run_id, started_iso)
        return RunResult(article=None, errors=errors + [{"stage": "pipeline", "error": str(exc)}],
                         stats={"seconds": round(time.time() - started, 1)})


async def push_issue(issue_id: str) -> Article:
    """把已渲染的一期推到公众号草稿箱。"""
    from .publish.wechat_api import WeChatClient, WeChatError

    article = db.get_article(issue_id)
    if not article:
        raise PipelineError(f"找不到期号 {issue_id}")

    try:
        async with WeChatClient() as wx:
            if not wx.ready:
                raise PipelineError("未配置 WX_APPID / WX_SECRET，无法推送草稿")
            content = await wx.rewrite_images(article.html)
            thumb = settings.wx_thumb_media_id
            if article.cover_path:
                thumb = await wx.upload_cover_material(article.cover_path)
            source_url = ""
            for block in article.items[:1]:
                source_url = block.url
            media_id = await wx.add_draft(
                title=article.title,
                author=settings.wx_author,
                digest=article.digest,
                content=content,
                thumb_media_id=thumb,
                source_url=source_url,
            )
        db.update_article_status(issue_id, "pushed", media_id=media_id)
        article.status = "pushed"
        article.wx_media_id = media_id
        return article
    except WeChatError as exc:
        db.update_article_status(issue_id, "failed", error=str(exc))
        raise PipelineError(f"微信接口失败：{exc}") from exc
