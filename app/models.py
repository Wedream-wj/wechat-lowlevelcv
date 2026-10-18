"""领域模型。抓取层只产出 `Item`，后续筛选、写作、排版都围绕它流转。"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def normalize_title(title: str) -> str:
    """用于去重的归一化：去标点、去空格、去大小写差异。"""
    title = re.sub(r"<[^>]+>", "", title or "")
    title = re.sub(r"[\s\u3000]+", "", title)
    title = re.sub(r"[^\w\u4e00-\u9fff]", "", title)
    return title.lower()


@dataclass
class Figure:
    url: str
    caption: str = ""
    role: str = "other"  # arch | result | cover | other
    local_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Item:
    """一条原始素材（论文 / 资讯 / 赛事）。"""

    module: str                     # paper | industry | contest
    title: str
    url: str
    source: str
    published_at: datetime | None = None
    summary: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    year: str = ""
    category: str = ""              # 任务方向，如「图像超分」
    code_url: str = ""
    image_url: str = ""
    images: list[str] = field(default_factory=list)
    score: float = 0.0
    keywords: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    # ---------------- 派生属性 ----------------
    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(normalize_title(self.title).encode("utf-8")).hexdigest()[:16]

    @property
    def year_int(self) -> int:
        try:
            return int(self.year)
        except (TypeError, ValueError):
            m = re.search(r"(20\d{2})", self.published_at.isoformat() if self.published_at else "")
            return int(m.group(1)) if m else 0

    def text_blob(self) -> str:
        return " ".join([self.title, self.summary, " ".join(self.keywords), self.venue, self.category])

    def to_row(self) -> dict:
        data = asdict(self)
        data["published_at"] = self.published_at.isoformat() if self.published_at else None
        data["fingerprint"] = self.fingerprint
        data["item_id"] = f"{self.module}:{self.fingerprint}"
        return data

    @classmethod
    def from_row(cls, row: dict) -> "Item":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        data = {k: v for k, v in row.items() if k in allowed}
        if isinstance(data.get("published_at"), str) and data["published_at"]:
            try:
                data["published_at"] = datetime.fromisoformat(data["published_at"])
            except ValueError:
                data["published_at"] = None
        return cls(**data)


@dataclass
class Article:
    """一期成稿。"""

    module: str
    title: str
    subtitle: str = ""
    digest: str = ""
    markdown: str = ""
    html: str = ""
    cover_path: str = ""
    items: list[Item] = field(default_factory=list)
    issue_id: str = ""
    created_at: datetime = field(default_factory=now_utc)
    status: str = "draft"           # draft | rendered | pushed | failed
    wx_media_id: str = ""
    error: str = ""

    def to_row(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "module": self.module,
            "title": self.title,
            "subtitle": self.subtitle,
            "digest": self.digest,
            "markdown": self.markdown,
            "html": self.html,
            "cover_path": self.cover_path,
            "items_json": [i.to_row() for i in self.items],
            "created_at": self.created_at.isoformat(),
            "status": self.status,
            "wx_media_id": self.wx_media_id,
            "error": self.error,
        }
