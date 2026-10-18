"""大模型客户端：任何 OpenAI 兼容端点都能用（DeepSeek / 通义千问 / 智谱 / Moonshot / vLLM）。

关键处理：
- 强制 JSON 输出 + 本地 JSON 修复（模型偶尔会包 ```json 或前后加解释）；
- 失败重试一次，第二次把「上一次不合法」的片段回传，让它自己修；
- 没有 API Key 时自动降级到 mock，保证整条流水线可离线演示。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from ..config import settings
from ..models import Item
from .prompts import SYSTEM, build_user_prompt

try:  # 允许在没装 openai 的环境里 import 本模块（mock 模式）
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    cost_cny: float = 0.0
    history: list[dict] = field(default_factory=list)

    def add(self, module: str, prompt: int, completion: int, price_in: float = 0.001, price_out: float = 0.002):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.calls += 1
        # 粗略估算：按 1 元 / 百万 输入 token、2 元 / 百万 输出 token
        cost = prompt / 1e6 * price_in + completion / 1e6 * price_out
        self.cost_cny += cost
        self.history.append({"module": module, "prompt": prompt, "completion": completion, "cost": round(cost, 4)})


usage = Usage()


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个完整 JSON 对象。"""
    if not text:
        raise LLMError("模型返回为空")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start < 0:
        raise LLMError(f"找不到 JSON：{text[:200]}")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                snippet = cleaned[start : i + 1]
                try:
                    return json.loads(snippet)
                except json.JSONDecodeError as exc:
                    raise LLMError(f"JSON 不合法：{exc}") from exc
    raise LLMError("JSON 括号不闭合")


def validate_spec(spec: dict) -> dict:
    """最低限度校验，避免空稿把渲染器搞崩。"""
    if not isinstance(spec.get("title"), str) or not spec["title"].strip():
        raise LLMError("缺少 title")
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise LLMError("缺少 sections")
    allowed = {"h2", "h3", "p", "bullets", "numbered", "quote", "highlight", "meta_card",
               "figure", "table", "contest_card", "divider"}
    cleaned: list[dict] = []
    for block in sections:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype not in allowed:
            continue
        if btype in {"p", "h2", "h3", "quote"} and not str(block.get("text", "")).strip():
            continue
        if btype in {"bullets", "numbered"} and not block.get("items"):
            continue
        if btype == "meta_card" and not block.get("rows"):
            continue
        cleaned.append(block)
    if not cleaned:
        raise LLMError("sections 全部非法")
    spec["sections"] = cleaned
    spec.setdefault("lead", [])
    if isinstance(spec["lead"], str):
        spec["lead"] = [spec["lead"]]
    spec.setdefault("digest", "")
    spec.setdefault("subtitle", "")
    spec.setdefault("takeaway", "")
    spec.setdefault("sources", [])
    return spec


class LLMClient:
    def __init__(self) -> None:
        self.enabled = settings.llm_ready and AsyncOpenAI is not None
        self._client = None
        if self.enabled:
            self._client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=2,
            )

    @property
    def mode(self) -> str:
        return f"live:{settings.llm_model}" if self.enabled else "mock"

    async def _chat(self, system: str, user: str) -> tuple[str, dict]:
        assert self._client is not None
        started = time.time()
        resp = await self._client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            response_format={"type": "json_object"},
            extra_headers={"X-Client-Name": "llcv-studio"},
        )
        content = resp.choices[0].message.content or ""
        tokens = getattr(resp, "usage", None)
        return content, {
            "prompt": getattr(tokens, "prompt_tokens", 0) or 0,
            "completion": getattr(tokens, "completion_tokens", 0) or 0,
            "seconds": round(time.time() - started, 1),
        }

    async def write(self, module: str, items: list[Item], figures: dict[str, dict], date_str: str) -> dict:
        if not self.enabled:
            from .mock import mock_spec

            return mock_spec(module, items, figures)

        user_prompt = build_user_prompt(module, items, figures, date_str)
        raw, meta = await self._chat(SYSTEM, user_prompt)
        usage.add(module, meta["prompt"], meta["completion"])
        try:
            return validate_spec(extract_json(raw))
        except LLMError as first_error:
            repair = (
                "你上一次的输出不是合法的 ArticleSpec JSON（错误："
                f"{first_error}）。请修正并只输出完整 JSON，不要任何解释。上一次输出片段：\n{raw[:1500]}"
            )
            raw2, meta2 = await self._chat(SYSTEM, repair)
            usage.add(module, meta2["prompt"], meta2["completion"])
            try:
                return validate_spec(extract_json(raw2))
            except LLMError as exc:
                raise LLMError(f"模型两次输出均不合法：{exc}") from exc
