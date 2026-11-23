"""历史出稿作者栏体检与清洗：`Paper Info` 里不许出现废话作者。

事故原型：「作者团队 = 由论文作者团队共同完成」。这类行不含任何具体姓名，
读者检索不到人，等于把信息卡变成了摆设——比缺这一行更伤专业度。

三条处置规则（写死，不吃网络例外）：
1. 现有值里能抽出真实人名 → 规范成「名 姓」清单、逗号分隔、超 6 人加「等」；
2. 现有值是废话（抽不出人名）→ 先多源补全（arXiv → OpenAlex → Crossref，
   标题相似度 ≥ 0.88 才认）；补到就换成真名清单；
3. 补不到 → **整行删掉**（宁缺毋滥），绝不留「由…共同完成」这种话。

用法：
    python -m scripts.audit_paper_authors                # 只体检，打印清单
    python -m scripts.audit_paper_authors --fix          # 体检 + 落库修复（自动备份）
    python -m scripts.audit_paper_authors --offline      # 不联网，只做规则 1 / 3
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from app import db, edit
from app.config import settings
from app.models import Article
from app.render.md_parse import parse_markdown
from app.sources.base import HttpClient
from app.sources.authors import arxiv_id_of, resolve_authors, title_match
from app.spec_tools import (
    _meta_kind,
    format_authors_line,
    is_placeholder_authors,
    parse_author_names,
)

VERDICT_FAKE = "废话/占位"
VERDICT_FORMAT = "句式/顺序待规范"
VERDICT_OK = "合格"
# 「由 … 共同完成」这类包装不含额外信息；人名已经在里面了，但读起来像通稿
_WRAPPER_RE = re.compile(r"^\s*(?:本文|该论文|论文)?\s*(?:由|作者团队由)|共同完成|等人组成|等研究人员")
# 「Chen, Junyang」倒装：**整条**作者条目是「姓, 名」（不能按子串判——
# 「Calvin-Khang Ta, Praneet Singh」里的「Ta, Praneet」只是两个正常条目的接缝）
_INVERTED_RE = re.compile(r"^[A-Z][\w'’\-]+\s*,\s*[A-Z][\w'’\-]+$")


def _paper_title(spec: dict) -> str:
    """信息卡里的论文英文原名——补全作者时必须拿它去查，中文标题查不到任何库。"""
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "meta_card":
            continue
        for row in block.get("rows") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2 and _meta_kind(str(row[0])) == "论文名称":
                return str(row[1]).strip()
    return ""


def _author_row(spec: dict) -> tuple[list | None, str]:
    """返回 (作者行对象, 值)；稿内没有作者行时返回 (None, "")。"""
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "meta_card":
            continue
        for row in block.get("rows") or []:
            if isinstance(row, (list, tuple)) and len(row) >= 2 and _meta_kind(str(row[0])) == "作者团队":
                return list(row), str(row[1]).strip()
    return None, ""


def _rewrite(markdown: str, value: str | None) -> str:
    """把 Markdown 里的作者行改成 value；value 为 None 时整行删除。"""
    lines = markdown.splitlines()
    out: list[str] = []
    hit = False
    for line in lines:
        m = edit._META_AUTHOR_LINE_RE.match(line)
        if m:
            hit = True
            if value:
                out.append(f"- **作者团队**：{value}")
            continue
        out.append(line)
    text = "\n".join(out)
    if not hit and value:
        # 稿里压根没有作者行：紧跟「论文名称 / 论文标题」那一行插入（旧稿行名不统一）
        for i, line in enumerate(out):
            if re.match(r"^\s*[-*]\s*\*\*\s*(?:论文名称|论文标题|论文题目|论文名)\s*\*\*", line):
                out.insert(i + 1, f"- **作者团队**：{value}")
                hit = True
                break
        text = "\n".join(out)
    return text


# 网络补全的额外闸门：arXiv/OpenAlex 的模糊检索会返回「同一主题的另一篇」，
# 0.88 只够排除完全无关的命中，落到信息卡上必须更严（补错作者比缺作者严重得多）。
MIN_FILL_MATCH = 0.93


def _normalize_existing(value: str, names: list[str]) -> str:
    """把已有作者行规范成统一写法，但**不减少信息**：原有几位就写几位。

    新生成稿件按 6 人上限截断（Prompt 规则），历史存档只做「顿号 → 逗号」这类
    格式统一，绝不因为格式化把第 7、8 位作者丢掉。
    """
    had_etc = bool(re.search(r"等\s*(?:研究人员|人|作者|学者)?\s*(?:共同完成|组成)?\s*[。.]?$", value))
    return ", ".join(names) + (" 等" if had_etc else "")


def _judge(value: str) -> tuple[str, list[str]]:
    """判定作者行：废话 → 补/删；含真名但有包装或倒装 → 规范写法；其余算合格。

    只挑「有信息损失」的问题动手：顿号 / 逗号这类纯分隔符差异不管，
    历史稿不该为了排版偏好反复升 rev。
    """
    names = parse_author_names(value)
    if not names:
        return VERDICT_FAKE, []
    entries = [e.strip() for e in re.split(r"[、，;；]|,\s*(?=[A-Z])", value) if e.strip()]
    if _WRAPPER_RE.search(value) or any(_INVERTED_RE.match(e) for e in entries):
        return VERDICT_FORMAT, names
    if value.strip() != _normalize_existing(value, names):
        return VERDICT_FORMAT, names          # 顿号 / 排版不统一：统一成「, 」写法
    return VERDICT_OK, names


async def _fill(client: HttpClient, article: Article, paper_title: str) -> dict:
    """多源补全：先在**同一篇**的本地素材里找（标题必须对得上），再走网络。

    只认标题相似度 ≥ 0.75 的候选——一期素材常含 3 条不同论文，拿错一条就是
    张冠李戴（历史上已经出过一次事故，这里绝不图省事取第一条）。
    """
    best, score, best_title = None, 0.0, ""
    for item in article.items:
        names = [str(a).strip() for a in (item.authors or []) if str(a).strip()]
        if not names:
            continue
        s = title_match(paper_title, item.title)
        if s > score:
            best, score, best_title = names, s, item.title
    # 本地素材只有 1 位作者时不算「取到了」——PDF 元数据常只抓到通讯作者，
    # 这种情况继续走网络补全（MDKD 事故：6 人只写 1 人）。
    if best and score >= 0.75 and len(best) >= 2:
        return {"authors": best, "source": "本地素材", "title": best_title}
    target = paper_title
    arxiv_id = ""
    for item in article.items:
        if title_match(paper_title, item.title) >= 0.75:
            arxiv_id = arxiv_id_of(item)
            target = item.title
            break
    try:
        got = await resolve_authors(client, target, arxiv_id)
    except Exception as exc:      # 网络异常不该中断体检
        return {"authors": best or [], "source": f"失败({type(exc).__name__})",
                "title": best_title}
    if not got.get("authors") and best:
        return {"authors": best, "source": "本地素材（仅 1 位）", "title": best_title}
    return got


async def run(fix: bool, offline: bool) -> int:
    rows = db.list_articles("paper", limit=1000)
    print(f"扫描论文稿 {len(rows)} 期\n" + "=" * 78)

    problems: list[dict] = []
    fixed = removed = 0
    async with HttpClient() as client:
        for row in rows:
            article = db.get_article(row["issue_id"])
            if not article or not article.markdown:
                continue
            spec, _figs = parse_markdown(article.markdown)
            _r, value = _author_row(spec)
            verdict, names = _judge(value)
            if verdict == VERDICT_OK:
                continue

            title = str(spec.get("title") or article.title or "")
            paper_title = _paper_title(spec) or title
            new_value: str | None = None
            source = ""
            got: dict = {}
            if offline:
                source = "offline"
            elif verdict == VERDICT_FORMAT:
                # 人名本来就在，只是写法不统一：直接规范化，不去网上再查一遍
                # （重查会把原来的第 7、8 位作者按 6 人上限截断，等于丢信息）
                source = "原值规范化"
            else:
                got = await _fill(client, article, paper_title)
                hit_title = str(got.get("title") or "")
                if hit_title and title_match(paper_title, hit_title) < MIN_FILL_MATCH:
                    got = {}           # 命中不是同一篇：宁可空着，也不把别人挂上去
                    source = "命中标题不符，放弃"
                filled = parse_author_names("、".join(got.get("authors") or []))
                if filled:
                    new_value = format_authors_line(filled)
                    source = got.get("source") or ""
            if new_value is None and names:
                new_value = _normalize_existing(value, names)   # 规则 1：只规范写法，不丢人
                source = source or "原值规范化"

            hit_title = str(got.get("title") or "") if not offline else ""
            action = ("补全真人名" if new_value else "删除该行")
            problems.append({
                "issue_id": article.issue_id, "title": title[:40], "verdict": verdict,
                "before": value or "（缺行）", "after": new_value or "（删除）",
                "action": action, "source": source, "hit_title": hit_title,
            })
            print(f"\n· {article.issue_id}  {title[:44]}")
            print(f"  判定：{verdict}｜处置：{action}（{source or '无权威来源'}）")
            print(f"  原值：{value or '（缺行）'}")
            print(f"  新值：{new_value or '（删除该行）'}")
            if hit_title and not offline:
                print(f"  命中原文：{hit_title[:88]}")

            if not fix:
                continue
            new_md = _rewrite(article.markdown, new_value)
            if new_md.strip() == article.markdown.strip():
                continue
            backup = settings.out_dir / "_author_backup"
            backup.mkdir(parents=True, exist_ok=True)
            (backup / f"{article.issue_id}.md").write_text(article.markdown, encoding="utf-8")
            payload = edit.save_edit(article, new_md)
            if new_value:
                fixed += 1
            else:
                removed += 1
            print(f"  ✅ 已落库 rev={payload.get('rev')}（原稿备份 out/_author_backup/）")

    print("\n" + "=" * 78)
    if not problems:
        print("结果：全部论文稿的作者栏都含具体人名，零废话。")
        return 0
    print(f"结果：{len(problems)} 期存在问题作者栏")
    for item in problems:
        print(f"  - {item['issue_id']}｜{item['verdict']}｜{item['action']}｜{item['title']}")
    if fix:
        print(f"已修复：补全 {fixed} 期、删除行 {removed} 期"
              f"（原稿备份在 out/_author_backup/）")
    else:
        print("（加 --fix 执行修复）")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="论文稿作者栏零废话体检与清洗")
    ap.add_argument("--fix", action="store_true", help="执行修复并落库（默认只体检）")
    ap.add_argument("--offline", action="store_true", help="不联网，只用规则 1/3")
    args = ap.parse_args(argv)
    return asyncio.run(run(args.fix, args.offline))


if __name__ == "__main__":
    sys.exit(main())
