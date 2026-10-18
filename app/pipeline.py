"""流水线编排：抓取 → 去重/筛选 → 配图 → 大模型成稿 → 排版 → 封面 → 入库 →（可选）草稿箱。

一条原则：**任何一步失败都要留下可读的原因**，因为这套系统大部分时间在无人值守地跑，
出问题时运营需要一眼看出是「没抓到」「筛没了」还是「模型抽风」。
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import db
from .config import settings, topics
from .filters import scoring
from .llm.client import LLMClient, LLMError
from .models import Article, Item
from .render import cover as cover_mod
from .render import wechat
from .render.markdown import spec_to_markdown
from .render.preview import render_preview
from .sources import MODULE_LABEL, collect
from .sources.base import HttpClient

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
    max_total = 4 if module == "paper" else 2
    index = 0
    for item in items:
        for src in (item.images or [])[: 2 if module == "paper" else 1]:
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
            caption = item.extra.get("figure_caption") or item.summary[:80] or item.title
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
                     date_str: str | None = None, top_n: int | None = None) -> RunResult:
    started = time.time()
    run_id = uuid.uuid4().hex[:12]
    started_iso = datetime.now().isoformat()
    errors: list[dict] = []
    date_str = date_str or datetime.now().strftime(DATE_FMT)

    def _log(msg: str) -> None:
        print(f"[pipeline:{module}] {msg}", flush=True)

    try:
        items, collect_errors = await collect(module)
        errors.extend(collect_errors)
        _log(f"抓取 {len(items)} 条，源告警 {len(collect_errors)} 个")
        if not items:
            raise PipelineError("所有数据源都没有返回内容（检查网络 / RSS 地址 / API 凭证）")

        # 注意顺序：先在「写库前」取已用指纹，否则刚抓到的条目会被当成已用过
        seen = db.seen_fingerprints(module, days=45)
        db.save_items(items, module)

        selected = scoring.select(items, topics, module, top_n=top_n, seen=seen)
        _log("选用 " + "；".join(f"{i.title[:24]}({i.score})" for i in selected))
        if not selected:
            detail = {
                "fetched": len(items),
                "top_scores": [
                    {"title": i.title[:60], "score": i.score, "hits": i.keywords[:4]}
                    for i in sorted(items, key=lambda x: x.score, reverse=True)[:5]
                ],
                "errors": errors,
            }
            raise PipelineError(f"抓到了 {len(items)} 条，但都被筛选规则挡掉了：{detail}")

        async with HttpClient() as client:
            figures = await build_figures(client, selected, module)
        _log(f"配图 {len(figures)} 张")

        llm = LLMClient()
        try:
            spec = await llm.write(module, selected, figures, date_str)
        except LLMError as exc:
            raise PipelineError(f"大模型成稿失败：{exc}") from exc
        _log(f"成稿完成（{llm.mode}）：{spec.get('title', '')}")

        cover_path = cover_mod.make_cover(spec, module, date_str)
        body = wechat.render_body(spec, figures, module, date_str)
        markdown = spec_to_markdown(spec, figures)

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
        issue_id = db.save_article(article)
        db.mark_used(selected, issue_id)

        preview = render_preview(
            spec,
            body,
            module,
            _relative_asset(cover_path),
            issue_id,
            date_str,
            MODULE_LABEL.get(module, module),
        )
        _preview_path(issue_id).write_text(preview, encoding="utf-8")

        # 附一份 Markdown 与 HTML 便于人工二次编辑
        (settings.out_dir / f"{issue_id}.md").write_text(markdown, encoding="utf-8")
        (settings.out_dir / f"{issue_id}.html").write_text(body, encoding="utf-8")

        should_publish = settings.auto_publish if publish is None else publish
        if should_publish:
            article = await push_issue(issue_id)

        stats = {
            "fetched": len(items),
            "selected": len(selected),
            "figures": len(figures),
            "preview": str(_preview_path(issue_id)),
            "llm_mode": llm.mode,
            "seconds": round(time.time() - started, 1),
            "errors": errors,
        }
        db.log_run(module, "success", str(stats), run_id, started_iso)
        return RunResult(article=article, errors=errors, stats=stats)

    except Exception as exc:
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
