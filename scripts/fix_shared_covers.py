"""全模块封面一致性审计与修复工具（论文 / 业界 / 赛事）。

背景（2026-11-26）：指纹命名守卫落地前，封面按 `cover_<module>_<date>` 命名，
同日多稿共用同一文件，后出稿把前面各篇的封面覆盖，列表 / 预览全部张冠李戴。
本工具把「封面必须是本期独占且主题相符的文件」变成可执行、可批量的运维动作。

用法：
  # 只读体检：列出所有共用封面组、损坏文件、归属不符
  python -m scripts.fix_shared_covers --audit

  # 单篇修复（默认 dry-run，加 --apply 才写库）；不传 hero 时用品牌底图，
  # 封面文字即本期标题，天然 100% 一致；传 --hero 时用契合主题的主视觉
  python -m scripts.fix_shared_covers --issue contest-20261123-bda1a4 --apply
  python -m scripts.fix_shared_covers --issue contest-20261123-7a6d89 \
      --hero articles/figures3/hero_retouching.png --apply

  # 存量批量治理（品牌底图逐篇重绘；默认 dry-run，--apply 才执行）
  python -m scripts.fix_shared_covers --all-shared --limit 10
  python -m scripts.fix_shared_covers --all-shared --apply

修复动作：make_cover 按成稿标题指纹出专属文件 → write_ownership 写 sidecar →
verify_ownership 复核 → 更新 issues.cover_path / status / rev / edited_at →
同步预览页封面引用。正文 Markdown / 富文本一字不动（封面不在正文里）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.config import ROOT, settings
from app.render import cover as cover_mod
from app.render import cover_guard

DB = ROOT / "data" / "llcv.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def _date_of(issue_id: str, created_at: str) -> str:
    m = re.search(r"-(\d{8})-", issue_id)
    if m:
        d = m.group(1)
        return f"{d[:4]}.{d[4:6]}.{d[6:]}"
    try:
        return datetime.fromisoformat(created_at).strftime("%Y.%m.%d")
    except Exception:
        return datetime.now().strftime("%Y.%m.%d")


def audit() -> dict:
    """与 /api/covers/audit 同口径的只读体检。"""
    con = _connect()
    rows = con.execute(
        "SELECT issue_id, title, cover_path FROM issues "
        "WHERE cover_path IS NOT NULL AND cover_path != ''").fetchall()
    con.close()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[str(r["cover_path"]).strip().replace("\\", "/").lower()].append(r)
    shared = [{"cover_path": v[0]["cover_path"], "count": len(v),
               "titles": [x["title"][:34] for x in v]}
              for v in groups.values() if len(v) > 1]
    return {"total": len(rows), "unique": len(groups), "shared": shared}


def _update_archive(con: sqlite3.Connection, issue_id: str, old_cover: str,
                    new_cover: Path, status_changed: bool) -> None:
    """库内路径 + 预览页引用同步；正文 md/html 不含封面引用，保持零改动。"""
    rel = os.path.relpath(new_cover, ROOT).replace("/", "\\")
    if status_changed:
        con.execute(
            "UPDATE issues SET cover_path=?, status='edited', rev=rev+1, "
            "edited_at=datetime('now','localtime'), error='' WHERE issue_id=?",
            (rel, issue_id))
    else:
        con.execute("UPDATE issues SET cover_path=?, error='' WHERE issue_id=?",
                    (rel, issue_id))
    pv = settings.out_dir / f"preview_{issue_id}.html"
    if pv.exists():
        old_name = Path(str(old_cover).replace("\\", "/")).name
        new_name = new_cover.name
        t = pv.read_text(encoding="utf-8")
        hits = t.count(old_name)
        if hits:
            pv.write_text(t.replace(old_name, new_name), encoding="utf-8")
        print(f"    预览页封面引用替换 {hits} 处：{old_name} → {new_name}")
    print(f"    db cover_path → {rel}")


def fix_one(issue_id: str, hero: str | None = None, apply: bool = False) -> dict:
    """为单篇稿生成「带本期标题指纹」的专属封面并同步存档。"""
    con = _connect()
    row = con.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"期号不存在：{issue_id}")
    spec = {"title": row["title"], "subtitle": row["subtitle"] or "",
            "digest": row["digest"] or ""}
    date_str = _date_of(issue_id, row["created_at"])
    old_cover = str(row["cover_path"] or "")
    print(f"  [{issue_id}] {row['title'][:40]}")
    print(f"    旧封面：{old_cover}  | status={row['status']} rev={row['rev']}")
    print(f"    hero：{hero or '（无，品牌底图：封面文字即本期标题）'}")
    if not apply:
        con.close()
        return {"issue_id": issue_id, "dry_run": True}

    cover = Path(cover_mod.make_cover(spec, row["module"], date_str, hero=hero))
    cover_guard.write_ownership(cover, spec, None,
                                cover_guard.cover_identity(spec), issue_id=issue_id)
    chk = cover_guard.verify_ownership(cover, spec)
    assert chk.ok, f"归属复核失败：{chk.reason}"
    asset = cover_guard.verify_cover_asset(cover, root=ROOT)
    assert asset.ok, f"封面资产无效：{asset.reason}"
    exclusive = cover_guard.assert_cover_exclusive(
        cover, issue_id, [(r[0], r[1], r[2]) for r in con.execute(
            "SELECT issue_id, title, cover_path FROM issues")])
    assert exclusive.ok, f"新封面仍非独占：{exclusive.reason}"

    changed = row["status"] == "pushed"      # 已推过的稿改封面 → edited 待重推
    _update_archive(con, issue_id, old_cover, cover, changed)
    con.commit()
    con.close()
    print(f"    新封面：{cover.name}  | 归属复核 OK | 独占校验 OK | status "
          f"{'→ edited（待重推）' if changed else '保持 ' + str(row['status'])}")
    return {"issue_id": issue_id, "cover": str(cover), "ok": True}


def migrate_existing(issue_id: str, apply: bool = False) -> dict:
    """内容本就正确的稿：把共享文件复制成指纹专属文件，不重绘、不改状态/正文。

    用于同一共享组里「最后写入、封面内容恰好是自己标题」的那一篇，
    让它也退出共享路径，整组彻底解散。
    """
    con = _connect()
    row = con.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
    if not row:
        con.close()
        raise SystemExit(f"期号不存在：{issue_id}")
    spec = {"title": row["title"], "subtitle": "", "digest": row["digest"] or ""}
    old = ROOT / str(row["cover_path"]).replace("\\", "/")
    date_str = _date_of(issue_id, row["created_at"])
    suffix = old.suffix or ".jpg"
    dest = (settings.image_dir /
            cover_guard.cover_filename(row["module"], date_str, spec, suffix))
    print(f"  [{issue_id}] {row['title'][:40]}")
    print(f"    共享文件 → 独立副本：{old.name} → {dest.name}（内容零改动）")
    if not apply:
        con.close()
        return {"issue_id": issue_id, "dry_run": True}
    if not old.exists():
        con.close()
        raise SystemExit(f"原封面不存在：{old}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old, dest)
    cover_guard.write_ownership(dest, spec, None,
                                cover_guard.cover_identity(spec), issue_id=issue_id)
    assert cover_guard.verify_ownership(dest, spec).ok
    _update_archive(con, issue_id, str(row["cover_path"]), dest, status_changed=False)
    con.commit()
    con.close()
    print("    sidecar 已写、独占引用已生效（status / rev 不变）")
    return {"issue_id": issue_id, "cover": str(dest), "ok": True}


def main() -> None:
    ap = argparse.ArgumentParser(description="全模块封面一致性审计与修复")
    ap.add_argument("--audit", action="store_true", help="只读体检")
    ap.add_argument("--issue", help="指定期号修复")
    ap.add_argument("--hero", help="契合主题的主视觉图（不传则用品牌底图）")
    ap.add_argument("--migrate", help="指定期号做「共享文件 → 独立副本」迁移")
    ap.add_argument("--all-shared", action="store_true", help="批量修复所有共用组")
    ap.add_argument("--limit", type=int, default=0, help="批量修复最多处理稿数")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args()

    if args.audit or not (args.issue or args.migrate or args.all_shared):
        a = audit()
        print(f"封面引用 {a['total']} 条 / 唯一文件 {a['unique']} 个 / 共用组 {len(a['shared'])} 组")
        for g in sorted(a["shared"], key=lambda x: -x["count"]):
            print(f"  {g['cover_path']}  ×{g['count']}")
            for t in g["titles"]:
                print(f"      · {t}")
        if not args.issue and not args.migrate and not args.all_shared:
            return

    if args.issue:
        fix_one(args.issue, hero=args.hero, apply=args.apply)
    if args.migrate:
        migrate_existing(args.migrate, apply=args.apply)
    if args.all_shared:
        a = audit()
        con = _connect()
        ids = [r["issue_id"] for g in a["shared"] for r in con.execute(
            "SELECT issue_id, title FROM issues WHERE cover_path=?", (g["cover_path"],))]
        con.close()
        if args.limit:
            ids = ids[: args.limit]
        print(f"批量修复 {len(ids)} 篇（apply={args.apply}）：")
        for iid in ids:
            fix_one(iid, apply=args.apply)


if __name__ == "__main__":
    main()
