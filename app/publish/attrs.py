"""草稿发布属性：原创声明与合集归属的固化、归一、断言与审计。

**事实前提**（2026-11 核对微信开放社区）：草稿箱接口 `draft/add` 与发布接口
`freepublish/submit` 都**没有**原创声明、来源声明、合集参数，社区技术运营的
回复是「不支持」。所以「每次推送自动带上这两项」只能分三层落地：

1. **固化**：`PublishAttrs.from_settings()` 读 `WX_ORIGINAL_TYPE` / `WX_COLLECTION`，
   缺省即「文字原创」/「每日推送」；调用方不许自己拼字符串，改默认值只改一处。
2. **断言**：`build_draft_payload` + `assert_draft_payload` 把属性写进 draft 载荷
   （`WX_DRAFT_EXTRA_FIELDS=1` 时直接下发，接口一旦支持即生效），推送前逐项核验，
   值被谁改歪了当场抛 `PublishAttrError`，绝不带着错的声明发出去。
3. **兜底**：官方接口给不了的部分交 `publish.mp_ui`，用 Playwright 回后台点选；
   点不上时给出可执行的待办清单，而不是静默当成「已完成」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings

# 微信后台原创声明面板里的三个选项（外加一个显式的「不声明」）
ORIGINAL_TEXT = "文字原创"
ORIGINAL_IMAGE = "图片原创"
ORIGINAL_VIDEO = "视频原创"
ORIGINAL_NONE = "不声明原创"
ORIGINAL_CHOICES: tuple[str, ...] = (ORIGINAL_TEXT, ORIGINAL_IMAGE, ORIGINAL_VIDEO, ORIGINAL_NONE)

DEFAULT_ORIGINAL = ORIGINAL_TEXT
DEFAULT_COLLECTION = "每日推送"

# 配置里写成别名也要认：环境变量手写时最容易写成「原创」「图文原创」「text」
_ORIGINAL_ALIASES: dict[str, str] = {
    "文字原创": ORIGINAL_TEXT,
    "图文原创": ORIGINAL_TEXT,
    "文字": ORIGINAL_TEXT,
    "原创": ORIGINAL_TEXT,
    "text": ORIGINAL_TEXT,
    "text_original": ORIGINAL_TEXT,
    "word": ORIGINAL_TEXT,
    "图片原创": ORIGINAL_IMAGE,
    "图片": ORIGINAL_IMAGE,
    "image": ORIGINAL_IMAGE,
    "video": ORIGINAL_VIDEO,
    "视频原创": ORIGINAL_VIDEO,
    "视频": ORIGINAL_VIDEO,
    "不声明原创": ORIGINAL_NONE,
    "不声明": ORIGINAL_NONE,
    "none": ORIGINAL_NONE,
}

# 微信 draft/add 的必填项：少任何一个都会在推草稿时炸，提前断言好过事后翻日志
_REQUIRED_ARTICLE_FIELDS: tuple[str, ...] = ("title", "thumb_media_id", "content")


class PublishAttrError(ValueError):
    """发布属性不合法 / 载荷与声明不一致。"""


def normalize_original(value: str | None) -> str:
    """把配置里的原创类型归一到微信面板的规范文案；空值回落到「文字原创」。"""
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_ORIGINAL
    hit = _ORIGINAL_ALIASES.get(raw.replace(" ", "").lower())
    if hit:
        return hit
    raise PublishAttrError(
        f"原创声明类型「{raw}」不合法：只能是 {' / '.join(ORIGINAL_CHOICES)}"
        f"（.env 里的 WX_ORIGINAL_TYPE，默认「{DEFAULT_ORIGINAL}」）"
    )


def normalize_collection(value: str | None) -> str:
    """合集名去掉首尾空白；空值回落到「每日推送」——合集不允许留空。"""
    return (value or "").strip() or DEFAULT_COLLECTION


@dataclass(frozen=True)
class PublishAttrs:
    """一期草稿的发布属性。构造时即完成归一，之后只读。"""

    original_type: str = DEFAULT_ORIGINAL
    collection: str = DEFAULT_COLLECTION

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_type", normalize_original(self.original_type))
        object.__setattr__(self, "collection", normalize_collection(self.collection))

    @classmethod
    def from_settings(cls) -> "PublishAttrs":
        return cls(original_type=settings.wx_original_type, collection=settings.wx_collection)

    def payload(self) -> dict[str, str]:
        """写进 draft/add payload 的两个字段（仅在接口支持时下发）。"""
        return {"original_type": self.original_type, "collection": self.collection}

    @property
    def describe(self) -> str:
        return f"原创声明={self.original_type}｜合集={self.collection}"

    def todo(self) -> list[str]:
        """官方接口勾不了的项 → 后台待办。推送结果里直接展示，避免「以为已经声明了」。"""
        return [
            f"后台草稿箱打开该草稿 → 声明原创选「{self.original_type}」",
            f"合集选「{self.collection}」（若后台没有该合集，先到「合集」里新建）",
        ]


def include_attrs_default() -> bool:
    return bool(settings.wx_draft_extra_fields)


def build_draft_payload(article: dict, attrs: PublishAttrs,
                        *, include_attrs: bool | None = None) -> tuple[dict, bool]:
    """构造 `{"articles": [article]}` 载荷；属性是否并入由配置/入参决定。

    返回 (payload, 是否真的并入了属性)，后者一并交给断言与审计快照。
    """
    include = include_attrs_default() if include_attrs is None else include_attrs
    body = dict(article)
    if include:
        body.update(attrs.payload())
    return {"articles": [body]}, include


def assert_draft_payload(payload: dict, attrs: PublishAttrs,
                         *, include_attrs: bool) -> dict:
    """推送前断言：必填字段齐全、属性未被改歪。返回 articles[0] 便于继续使用。"""
    articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list) or not articles:
        raise PublishAttrError("draft/add 载荷缺少 articles 数组（微信会回 40007）")
    article = articles[0]
    if not isinstance(article, dict):
        raise PublishAttrError(f"draft/add 载荷的 articles[0] 不是对象：{type(article).__name__}")
    lacking = [k for k in _REQUIRED_ARTICLE_FIELDS if not str(article.get(k) or "").strip()]
    if lacking:
        raise PublishAttrError(f"draft/add 载荷缺少必填字段：{', '.join(lacking)}")

    # 归一化后再验一遍：属性对象可能被别处直接构造，绕过 from_settings
    if normalize_original(attrs.original_type) != attrs.original_type:
        raise PublishAttrError(
            f"原创声明未归一：「{attrs.original_type}」→ 应为「{normalize_original(attrs.original_type)}」"
        )
    if not attrs.collection.strip():
        raise PublishAttrError("合集归属为空：默认必须是「每日推送」")

    if include_attrs:
        for key, want in attrs.payload().items():
            got = str(article.get(key) or "")
            if got != want:
                raise PublishAttrError(
                    f"draft/add 载荷的 {key} 与发布属性不一致：载荷={got!r}，应为={want!r}"
                )
    return article


@dataclass
class DraftRecord:
    """一次推送的属性快照，落盘留痕（推完想查「这期到底声明了什么」）。"""

    issue_id: str
    attrs: PublishAttrs
    media_id: str = ""
    include_attrs: bool = False
    ui_fill: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "issue_id": self.issue_id,
            "media_id": self.media_id,
            "original_type": self.attrs.original_type,
            "collection": self.attrs.collection,
            "sent_in_payload": self.include_attrs,
            "ui_fill": self.ui_fill,
            "todo": self.attrs.todo(),
        }


def snapshot_path(issue_id: str) -> Path:
    return settings.out_dir / "assets" / f"draft_attrs_{issue_id}.json"


def write_snapshot(record: DraftRecord) -> Path:
    path = snapshot_path(record.issue_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
