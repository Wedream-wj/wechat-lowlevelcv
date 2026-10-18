"""SQLite 持久层（标准库实现，零额外依赖）。

三张表：
- items   素材池，按 fingerprint 去重，记录是否已被某期使用（避免重复选题）
- issues  成稿，保存 Markdown / HTML / 封面，可直接重渲染、重推草稿
- runs    每次流水线执行日志，方便排查「今天为什么没抓到东西」
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator

from .config import settings
from .models import Article, Item

_lock = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    module       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT,
    source       TEXT,
    published_at TEXT,
    score        REAL DEFAULT 0,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    used_issue   TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_module ON items(module, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_used ON items(used_issue);

CREATE TABLE IF NOT EXISTS issues (
    issue_id    TEXT PRIMARY KEY,
    module      TEXT NOT NULL,
    title       TEXT,
    subtitle    TEXT,
    digest      TEXT,
    markdown    TEXT,
    html        TEXT,
    cover_path  TEXT,
    items_json  TEXT,
    created_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'draft',
    wx_media_id TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_module ON issues(module, created_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    module      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


# 导入即建表（幂等），保证 CLI / 脚本 / 服务三种入口都不会缺表
init_db()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------- items
def save_items(items: Iterable[Item], module: str) -> tuple[int, int]:
    """写入素材池，返回 (新增数, 已存在数)。"""
    new = existed = 0
    with _lock, connect() as conn:
        for item in items:
            row = item.to_row()
            try:
                conn.execute(
                    """INSERT INTO items(item_id, module, fingerprint, title, url, source,
                                         published_at, score, payload, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        row["item_id"], module, row["fingerprint"], item.title, item.url,
                        item.source, row["published_at"], item.score,
                        json.dumps(row, ensure_ascii=False, default=str), _utcnow(),
                    ),
                )
                new += 1
            except sqlite3.IntegrityError:
                # 已存在：只刷新分数与摘要，不覆盖 used_issue
                conn.execute(
                    "UPDATE items SET score=?, payload=? WHERE item_id=?",
                    (item.score, json.dumps(row, ensure_ascii=False, default=str), row["item_id"]),
                )
                existed += 1
    return new, existed


def seen_fingerprints(module: str, days: int = 60) -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT fingerprint FROM items WHERE module=? AND created_at >= datetime('now', ?)",
            (module, f"-{days} days"),
        ).fetchall()
    return {r["fingerprint"] for r in rows}


def recent_items(module: str, limit: int = 50, unused_only: bool = False) -> list[Item]:
    sql = "SELECT payload FROM items WHERE module=?"
    if unused_only:
        sql += " AND used_issue IS NULL"
    sql += " ORDER BY created_at DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, (module, limit)).fetchall()
    return [Item.from_row(json.loads(r["payload"])) for r in rows]


def mark_used(items: Iterable[Item], issue_id: str) -> None:
    with _lock, connect() as conn:
        for item in items:
            conn.execute(
                "UPDATE items SET used_issue=? WHERE item_id=?",
                (issue_id, f"{item.module}:{item.fingerprint}"),
            )


def is_used(item: Item) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT used_issue FROM items WHERE item_id=?", (f"{item.module}:{item.fingerprint}",)
        ).fetchone()
    return bool(row and row["used_issue"])


# ---------------------------------------------------------------- issues
def save_article(article: Article) -> str:
    if not article.issue_id:
        article.issue_id = f"{article.module}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
    with _lock, connect() as conn:
        conn.execute(
            """INSERT INTO issues(issue_id, module, title, subtitle, digest, markdown, html,
                                  cover_path, items_json, created_at, status, wx_media_id, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(issue_id) DO UPDATE SET
                 title=excluded.title, subtitle=excluded.subtitle, digest=excluded.digest,
                 markdown=excluded.markdown, html=excluded.html, cover_path=excluded.cover_path,
                 items_json=excluded.items_json, status=excluded.status,
                 wx_media_id=excluded.wx_media_id, error=excluded.error""",
            (
                article.issue_id, article.module, article.title, article.subtitle, article.digest,
                article.markdown, article.html, article.cover_path,
                json.dumps([i.to_row() for i in article.items], ensure_ascii=False, default=str),
                article.created_at.isoformat(), article.status, article.wx_media_id, article.error,
            ),
        )
    return article.issue_id


def get_article(issue_id: str) -> Article | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM issues WHERE issue_id=?", (issue_id,)).fetchone()
    if not row:
        return None
    return Article(
        module=row["module"],
        title=row["title"] or "",
        subtitle=row["subtitle"] or "",
        digest=row["digest"] or "",
        markdown=row["markdown"] or "",
        html=row["html"] or "",
        cover_path=row["cover_path"] or "",
        items=[Item.from_row(x) for x in json.loads(row["items_json"] or "[]")],
        issue_id=row["issue_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        status=row["status"] or "draft",
        wx_media_id=row["wx_media_id"] or "",
        error=row["error"] or "",
    )


def update_article_status(issue_id: str, status: str, media_id: str = "", error: str = "") -> None:
    with _lock, connect() as conn:
        conn.execute(
            "UPDATE issues SET status=?, wx_media_id=?, error=? WHERE issue_id=?",
            (status, media_id, error, issue_id),
        )


def list_articles(module: str | None = None, limit: int = 30) -> list[dict]:
    sql = "SELECT issue_id, module, title, digest, created_at, status, wx_media_id FROM issues"
    args: list = []
    if module:
        sql += " WHERE module=?"
        args.append(module)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ------------------------------------------------------------------ runs
def log_run(module: str, status: str, detail: str, run_id: str | None = None,
            started_at: str | None = None) -> str:
    run_id = run_id or uuid.uuid4().hex[:12]
    with _lock, connect() as conn:
        conn.execute(
            """INSERT INTO runs(run_id, module, started_at, finished_at, status, detail)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at,
                 status=excluded.status, detail=excluded.detail""",
            (run_id, module, started_at or _utcnow(), _utcnow(), status, detail[:4000]),
        )
    return run_id


def stats() -> dict:
    with connect() as conn:
        items = conn.execute("SELECT module, COUNT(*) c FROM items GROUP BY module").fetchall()
        issues = conn.execute("SELECT module, COUNT(*) c FROM issues GROUP BY module").fetchall()
        last_run = conn.execute(
            "SELECT module, finished_at, status, detail FROM runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    return {
        "items": {r["module"]: r["c"] for r in items},
        "issues": {r["module"]: r["c"] for r in issues},
        "last_run": dict(last_run) if last_run else None,
    }
