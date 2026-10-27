"""全局配置：环境变量 + config/topics.yaml。

设计原则：
1. 所有可调参数（关键词、阈值、RSS、排期）都放在 YAML 里，改选题不用改代码；
2. 密钥只走环境变量，避免误提交；
3. 提供一个 `settings.mock_llm` 开关，没有 API Key 时也能跑通全流程（输出占位稿）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.getenv("LLCV_CONFIG", ROOT / "config" / "topics.yaml"))


# ---------------------------------------------------------------- .env
def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 解析，避免为了一个功能引入额外依赖。"""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- YAML
@dataclass
class Topics:
    """选题词表与权重。"""

    raw: dict[str, Any]

    @property
    def categories(self) -> dict[str, dict]:
        return self.raw.get("categories", {})

    def keyword_weight(self, text: str) -> tuple[float, list[str]]:
        """返回 (命中权重, 命中词)。核心词 3 分、方向词 2 分、外围词 1 分。"""
        lowered = text.lower()
        score, hits = 0.0, []
        for word in self.raw.get("core_keywords", []):
            if word.lower() in lowered:
                score += 3.0
                hits.append(word)
        for word in self.raw.get("task_keywords", []):
            if word.lower() in lowered:
                score += 2.0
                hits.append(word)
        for word in self.raw.get("peripheral_keywords", []):
            if word.lower() in lowered:
                score += 1.0
                hits.append(word)
        return score, hits

    @property
    def industry(self) -> dict[str, Any]:
        return self.raw.get("industry", {})

    def industry_keyword_weight(self, text: str) -> tuple[float, list[str]]:
        """业界模块专用词表：core 3 分 / boost 1.2 分 / weak 0.5 分。

        为什么不复用 keyword_weight：那套词表是给底层视觉论文与赛事用的，
        一条「英伟达发布新一代 GPU」的产业新闻在里边一分都拿不到，
        业界稿就会退化成只剩论文源。这里单列一份泛 AI 产业词表。
        """
        cfg = self.industry.get("keywords", {})
        lowered = text.lower()
        score, hits = 0.0, []
        for key, weight in (("core", 3.0), ("boost", 1.2), ("weak", 0.5)):
            for word in cfg.get(key, []):
                if str(word).lower() in lowered:
                    score += weight
                    hits.append(str(word))
        return score, hits

    @property
    def industry_categories(self) -> dict[str, list[str]]:
        return self.industry.get("categories", {})

    @property
    def venues(self) -> dict[str, int]:
        return self.raw.get("venues", {})

    @property
    def arxiv_queries(self) -> list[str]:
        return self.raw.get("arxiv_queries", [])

    @property
    def rss_feeds(self) -> list[dict]:
        return self.raw.get("industry", {}).get("feeds", [])

    @property
    def pipelines(self) -> dict[str, Any]:
        return self.raw.get("pipelines", {})

    @property
    def schedule(self) -> dict[str, str]:
        return self.raw.get("schedule", {})


@dataclass
class Settings:
    # 大模型：任何 OpenAI 兼容端点
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://api.deepseek.com/v1"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "deepseek-chat"))
    llm_temperature: float = field(default_factory=lambda: float(_env("LLM_TEMPERATURE", "0.6")))
    llm_max_tokens: int = field(default_factory=lambda: int(_env("LLM_MAX_TOKENS", "8000")))
    llm_timeout: int = field(default_factory=lambda: int(_env("LLM_TIMEOUT", "180")))
    # 推理模型的思考强度（none/low/medium/high）。留空则不下发该参数，对不认这个
    # 字段的 OpenAI 兼容端点零影响；推理模型的思维链与正文共享 max_tokens，设 none
    # 可避免正文被思考挤空、JSON 被截断（实测同一篇论文 505s → 59s）。
    llm_reasoning_effort: str = field(default_factory=lambda: _env("LLM_REASONING_EFFORT", ""))
    # 是否让 LLM 请求遵循系统/环境的上网代理设置。内网网关（如 http://ai-sssc.…）
    # 会被公司代理劫持成拦截页，需在 .env 设 LLM_TRUST_ENV=0 直连；公网端点保持默认 1。
    llm_trust_env: bool = field(default_factory=lambda: _env_bool("LLM_TRUST_ENV", True))

    # 微信公众号
    wx_appid: str = field(default_factory=lambda: _env("WX_APPID"))
    wx_secret: str = field(default_factory=lambda: _env("WX_SECRET"))
    wx_author: str = field(default_factory=lambda: _env("WX_AUTHOR", "底层视觉论文笔记"))
    wx_thumb_media_id: str = field(default_factory=lambda: _env("WX_THUMB_MEDIA_ID"))
    wx_need_open_comment: int = field(default_factory=lambda: int(_env("WX_NEED_OPEN_COMMENT", "1")))

    # 运行
    out_dir: Path = field(default_factory=lambda: Path(_env("LLCV_OUT", str(ROOT / "out"))))
    db_path: Path = field(default_factory=lambda: Path(_env("LLCV_DB", str(ROOT / "data" / "llcv.db"))))
    public_base_url: str = field(default_factory=lambda: _env("LLCV_PUBLIC_BASE", "http://127.0.0.1:8000"))
    mock_llm: bool = field(default_factory=lambda: _env_bool("LLCV_MOCK_LLM", False))
    auto_publish: bool = field(default_factory=lambda: _env_bool("WX_AUTO_PUBLISH", False))
    timezone: str = field(default_factory=lambda: _env("TZ_NAME", "Asia/Shanghai"))

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key) and not self.mock_llm

    @property
    def wechat_ready(self) -> bool:
        return bool(self.wx_appid and self.wx_secret)

    @property
    def image_dir(self) -> Path:
        return self.out_dir / "assets"


@lru_cache(maxsize=1)
def get_topics() -> Topics:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"缺少配置文件：{CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return Topics(yaml.safe_load(fh) or {})


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    settings.image_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
topics = get_topics()
