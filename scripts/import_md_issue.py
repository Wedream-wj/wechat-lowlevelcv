"""把外部 Markdown 成稿导入「历史出稿」。

为什么不直接调 `pipeline`：那条链路是「素材 → 模型成稿」，会重写正文；
外部稿件（已经人工定稿）只需要登记归档，正文必须原样保留。

导入内容（与流水线出稿完全同构，控制台的列表/预览/编辑器/推送都能直接用）：
- `issues` 一行：module / title / digest / markdown / html / cover_path / items_json / status
- `out/<issue_id>.md`、`out/<issue_id>.html`、`out/preview_<issue_id>.html` 三个产物

正文与图片地址由 `scripts.md_to_wechat` 产出（已过零篡改闸门、图片已换微信 CDN），
本脚本只做登记，不再改写任何文字。

用法：
    python -m scripts.import_md_issue "D:/path/a.md" --module industry
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Article, Item  # noqa: E402
from app.render.preview import render_preview  # noqa: E402
from scripts import md_to_wechat as m2w  # noqa: E402

MODULE_LABEL = {"paper": "论文精读", "industry": "业界观察", "contest": "赛事速递"}


def _issue_id(module: str, src: Path, stamp: datetime) -> str:
    """按「模块-日期-源文件指纹」生成期号：同一个源重复导入是覆盖，不是新增一期。"""
    digest = hashlib.sha1(str(src).encode("utf-8")).hexdigest()[:6]
    return f"{module}-{stamp.strftime('%Y%m%d')}-{digest}"


def _cover_name(module: str, today: str, issue_id: str, suffix: str = ".png") -> str:
    """封面文件名必须带期号指纹：同一天多期共用「模块+日期」会互相覆盖——
    事故：撤稿稿的封面被同日另一期的自动封面顶掉，列表里展示成完全无关的内容。"""
    sha = issue_id.rsplit("-", 1)[-1][:6] or "local"
    return f"cover_{module}_{today}_{sha}{suffix}"


def _digest(blocks: list[m2w.Block]) -> str:
    """摘要：优先「文章摘要」小节后的第一段，其次正文第一段（截 120 字）。"""
    for idx, b in enumerate(blocks):
        if b.kind == "h" and "摘要" in b.text:
            for nxt in blocks[idx + 1:]:
                if nxt.kind == "p" and nxt.plain():
                    return nxt.plain()[:120]
    for b in blocks:
        if b.kind == "p" and len(b.plain()) > 40:
            return b.plain()[:120]
    return ""


def _reuse_urls(src: Path) -> dict[str, str]:
    """复用 `md2wx` 已上传的 CDN 地址：同一张图重复上传会白占素材库，也没必要。"""
    return m2w.reuse_uploaded(src)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="外部 Markdown 成稿导入历史出稿")
    ap.add_argument("markdown", help="Markdown 源文件")
    ap.add_argument("--module", default="industry", choices=list(MODULE_LABEL))
    ap.add_argument("--issue-id", default="", help="不填则按模块+日期+源文件指纹生成")
    ap.add_argument("--no-upload", action="store_true", help="不传图，直接用本地图（仅调试）")
    args = ap.parse_args(argv)

    src = Path(args.markdown).expanduser().resolve()
    text = src.read_text(encoding="utf-8")
    blocks = m2w.parse_blocks(text)
    problems = m2w.verify_fidelity(blocks)
    if problems:
        print("✗ 正文保真校验未通过，拒绝入库：")
        for p in problems[:8]:
            print("   ", p)
        return 3

    title = next((b.text for b in blocks if b.kind == "h" and b.level == 1), src.stem)
    mapping = await m2w.fetch_images(blocks, src.parent, settings.out_dir / "md2wx" / "images")
    urls: dict[str, str] = {}
    if not args.no_upload:
        urls = _reuse_urls(src)
        missing = {k: v for k, v in mapping.items() if k not in urls}
        if missing:
            fresh, err = await m2w.upload_images(missing)
            if err:
                print("✗ 图片上传失败，拒绝入库（微信端会裂图）：", err)
                return 4
            urls.update(fresh)

    body = "".join(m2w.render_block(b) for b in blocks)
    content_body = m2w._replace_img_src(body, blocks, urls) if urls else body
    content = (f'<section style="font-family:{m2w.W.FONT};font-size:15px;line-height:1.9;'
               f'color:{m2w.W.INK};">{content_body}</section>')
    markdown = m2w.build_markdown(blocks, urls, title)

    stamp = datetime.now()
    issue_id = args.issue_id or _issue_id(args.module, src, stamp)
    today = stamp.strftime("%Y%m%d")
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    existing = db.get_article(issue_id)

    # 卡片摘要：修订入库时正文可能删掉了「文章摘要」小节（按用户指令精简），
    # 但摘要是卡片元数据、不是正文——原稿已有摘要就沿用，不要回退成正文首段。
    digest = (existing.digest if existing and existing.digest else "") or _digest(blocks)

    # 预览页用本地副本图：微信 CDN 带 Referer 会回「此图片来自微信公众平台」占位图，
    # 控制台预览点开就是一片裂图；本地副本放在 out/assets/<期号>/，经 /out/ 静态目录访问。
    local_dir = settings.out_dir / "assets" / issue_id
    local_urls = m2w.local_preview_srcs(blocks, mapping, local_dir)
    preview_body = (m2w._replace_img_src(body, blocks,
                                         {k: f"assets/{issue_id}/{v}" for k, v in local_urls.items()})
                    if local_urls else body)

    # 封面：cover_<module>_<date>_<指纹>.<ext>，取正文首图（结构图/现场图）。
    # 指纹取自期号尾段——同一天多期共用「模块+日期」会互相覆盖（事故：撤稿稿的
    # 封面被同日另一期的自动封面顶掉，展示成完全无关的内容），所以必须带指纹。
    cover_path = ""
    first = next((mapping[b.src] for b in blocks if b.kind == "img" and b.src in mapping), None)
    if first:
        suffix = first.suffix.lower() if first.suffix.lower() in {".png", ".jpg", ".jpeg"} else ".png"
        cover = settings.out_dir / "assets" / _cover_name(args.module, today, issue_id, suffix)
        cover.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(first, cover)
        cover_path = str(cover)

    (settings.out_dir / f"{issue_id}.md").write_text(markdown, encoding="utf-8")
    (settings.out_dir / f"{issue_id}.html").write_text(content, encoding="utf-8")

    spec = {
        "title": title,
        "subtitle": "",
        "digest": digest,
        "lead": [],
        "sections": [{"type": b.kind, "text": b.plain()} for b in blocks if b.kind in {"p", "quote"}],
        "sources": [],
    }
    preview = render_preview(
        spec, preview_body, markdown, args.module,
        cover=Path(cover_path).resolve().relative_to(settings.out_dir.resolve()).as_posix()
        if cover_path else "",
        issue_id=issue_id, date_str=stamp.strftime("%Y.%m.%d"),
        module_label=MODULE_LABEL[args.module],
    )
    (settings.out_dir / f"preview_{issue_id}.html").write_text(preview, encoding="utf-8")

    existing_status = existing.status if existing else ""
    article = Article(
        module=args.module,
        title=title,
        subtitle="",
        digest=spec["digest"],
        markdown=markdown,
        html=content,
        cover_path=cover_path,
        items=[Item(module=args.module, title=title, url="", source=f"本地稿件 · {src.name}",
                    summary=spec["digest"], extra={"local_source": str(src)})],
        issue_id=issue_id,
        created_at=existing.created_at if existing else stamp,
        status=existing_status or "rendered",
    )
    db.save_article(article)
    if existing:
        # 修订入库：rev 自增、记编辑时间；推过的稿子被改动必须标「待重推」，
        # 否则后台草稿箱里还是旧版，控制台无从提示
        db.update_article_content(issue_id, markdown=markdown, html=content, title=title,
                                  digest=spec["digest"], edited_at=stamp)
        revised_status = "edited" if existing_status == "pushed" else (existing_status or "rendered")
        with db.connect() as conn:
            conn.execute("UPDATE issues SET cover_path=?, status=? WHERE issue_id=?",
                         (cover_path, revised_status, issue_id))
        print(f"✓ 已修订入库：{issue_id}（rev+1）")
    else:
        # 新导入固定为「已渲染」；归档列表按 rev/status 展示「草稿/已润色/已推送」
        with db.connect() as conn:
            conn.execute("UPDATE issues SET status='rendered', rev=0 WHERE issue_id=?", (issue_id,))
        print(f"✓ 已入库：{issue_id}")
    print(f"  {MODULE_LABEL[args.module]}｜{title}")
    print(f"  摘要：{spec['digest'][:48]}…")
    print(f"  图片：{len(urls)}/{len(mapping)} 张已上微信 CDN")
    print(f"  产物：{issue_id}.md / {issue_id}.html / preview_{issue_id}.html")
    print(f"  预览：/preview/{issue_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
