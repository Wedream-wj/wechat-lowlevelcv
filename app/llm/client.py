"""大模型客户端：任何 OpenAI 兼容端点都能用（DeepSeek / 通义千问 / 智谱 / Moonshot / vLLM）。

关键处理：
- 强制 JSON 输出 + 本地 JSON 修复（模型偶尔会包 ```json、前后加解释，或正文前多吐一个 `{`）；
- 失败重试一次，保留原始素材与上一次输出，让它在同一批事实上重写；
- 推理模型可经 LLM_REASONING_EFFORT=none 关闭思考，思维链与正文共享 max_tokens 时必需；
- 没有 API Key 时自动降级到 mock，保证整条流水线可离线演示。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from ..config import settings
from ..models import Item
from ..sources.base import _ssl_verify
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


def _match_braces(text: str, start: int) -> tuple[str, bool]:
    """从 text[start] 的 '{' 开始配对，返回（片段, 是否闭合）。字符串内的花括号不算数。"""
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
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
                return text[start : i + 1], True
    return text[start:], False


def extract_json(text: str) -> dict:
    """从模型输出里抠出第一个能解析的 JSON 对象。

    推理模型偶尔会在正文前多吐一个 '{'（实测形如 "{\\n{\\n  \\"title\\": ..."），
    只从第一个 '{' 做配对会永远闭合不了；这里对每个 '{' 起点都试一遍。
    """
    if not text:
        raise LLMError("模型返回为空")
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    starts: list[int] = []
    in_str = escape = False
    for i, ch in enumerate(cleaned):
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
            starts.append(i)
    if not starts:
        raise LLMError(f"找不到 JSON：{text[:200]}")
    saw_closed = False
    first_error: json.JSONDecodeError | None = None
    for start in starts:
        snippet, closed = _match_braces(cleaned, start)
        if not closed:
            continue
        saw_closed = True
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            continue
        if isinstance(data, dict):
            return data
    if saw_closed:
        raise LLMError(f"JSON 不合法：{first_error}")
    raise LLMError("JSON 括号不闭合（输出很可能被截断）")


def validate_spec(spec: dict) -> dict:
    """最低限度校验，避免空稿把渲染器搞崩。"""
    if not isinstance(spec.get("title"), str) or not spec["title"].strip():
        raise LLMError("缺少 title")
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise LLMError("缺少 sections")
    allowed = {"h2", "h3", "p", "bullets", "numbered", "quote", "highlight", "meta_card",
               "figure", "table", "contest_card", "code", "divider"}
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
        if btype == "code" and not str(block.get("code", "")).strip():
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
            # TLS 策略与抓取侧一致（trust_system_certs）；trust_env 决定是否走系统上网代理：
            # 内网网关被公司代理劫持成 HTML 拦截页时，.env 设 LLM_TRUST_ENV=0 直连。
            self._client = AsyncOpenAI(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                timeout=settings.llm_timeout,
                max_retries=2,
                http_client=httpx.AsyncClient(
                    trust_env=settings.llm_trust_env,
                    verify=_ssl_verify(),
                ),
            )

    @property
    def mode(self) -> str:
        return f"live:{settings.llm_model}" if self.enabled else "mock"

    @staticmethod
    def _brief(exc: Exception) -> str:
        """把 SDK 异常压缩成一行可读信息（上网代理的拦截页是一大坨 HTML，必须收口）。"""
        text = re.sub(r"\s+", " ", str(exc)).strip()
        if "<html" in text.lower() or "<!doctype" in text.lower():
            m = re.search(r"<title>([^<]*)</title>", text, re.IGNORECASE)
            label = m.group(1).strip() if m else "HTML 页面"
            hint = (
                "（疑似上网代理劫持；内网网关请在 .env 设 LLM_TRUST_ENV=0 直连）"
                if settings.llm_trust_env
                else ""
            )
            return f"端点返回了 HTML 而非 API 响应：{label}{hint}"
        return text[:160] or type(exc).__name__

    async def _chat(self, messages: list[dict[str, str]],
                    on_progress: Callable[[str, int], None] | None = None) -> tuple[str, dict]:
        """流式调用并聚合正文。

        on_progress(kind, chars)：kind 为 reasoning（思维链）或 content（正文），
        chars 是该类内容的累计字符数。推理模型的思考阶段可能长达数分钟，
        没有这个回调前端只能看着进度条停在 50% 不动。
        """
        assert self._client is not None
        started = time.time()
        extra_body = {}
        if settings.llm_reasoning_effort:
            # 思维链和正文共享 max_tokens：实测同一批论文素材，开思考时正文 4868 字符
            # 却用了 4 万字符推理、505s，还常把 JSON 截断；关思考后 59s、正文更完整。
            extra_body["reasoning_effort"] = settings.llm_reasoning_effort
        try:
            # 流式聚合：慢模型生成大 JSON 的总时长可达数分钟，非流式会把 timeout
            # 当总时长上限而中途掐断；流式下 timeout 只约束相邻 chunk 的间隔，
            # 只要模型在持续输出就不会误杀。usage 由最后一个 chunk 携带。
            stream = await self._client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                temperature=settings.llm_temperature,
                max_tokens=settings.llm_max_tokens,
                response_format={"type": "json_object"},
                extra_headers={"X-Client-Name": "llcv-studio"},
                extra_body=extra_body or None,
                stream=True,
                stream_options={"include_usage": True},
            )
            parts: list[str] = []
            usage_obj = None
            finish = None
            content_chars = 0
            reasoning_chars = 0
            last_reasoning_sent = 0
            last_content_sent = 0

            def tick(kind: str, chars: int, last: int) -> bool:
                """每累计 800 字符上报一次，够前端刷新又不至于刷屏。"""
                if on_progress is None or chars - last < 800:
                    return False
                try:
                    on_progress(kind, chars)
                except Exception:  # 进度回调只影响展示，绝不能反向打断生成
                    pass
                return True

            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta:
                        # 思维链：vLLM/SGLang 系网关放在 reasoning，部分端点用
                        # reasoning_content；openai SDK 把这类非标准字段收进 model_extra。
                        extra = getattr(delta, "model_extra", None) or {}
                        thought = extra.get("reasoning") or extra.get("reasoning_content")
                        if thought:
                            reasoning_chars += len(thought)
                            if tick("reasoning", reasoning_chars, last_reasoning_sent):
                                last_reasoning_sent = reasoning_chars
                        if delta.content:
                            parts.append(delta.content)
                            content_chars += len(delta.content)
                            if tick("content", content_chars, last_content_sent):
                                last_content_sent = content_chars
                    if chunk.choices[0].finish_reason:
                        finish = chunk.choices[0].finish_reason
                if getattr(chunk, "usage", None):
                    usage_obj = chunk.usage
        except Exception as exc:  # openai SDK 的异常统一收口成 LLMError，保持消息可读
            raise LLMError(f"{type(exc).__name__}: {self._brief(exc)}") from exc
        content = "".join(parts)
        return content, {
            "prompt": getattr(usage_obj, "prompt_tokens", 0) or 0,
            "completion": getattr(usage_obj, "completion_tokens", 0) or 0,
            "finish": finish,
            "seconds": round(time.time() - started, 1),
            "content_chars": content_chars,
            "reasoning_chars": reasoning_chars,
        }

    async def write(self, module: str, items: list[Item], figures: dict[str, dict], date_str: str,
                    on_progress: Callable[[str, int], None] | None = None,
                    extra_instruction: str = "", variant: str | None = None) -> dict:
        if not self.enabled:
            from .mock import mock_spec

            return mock_spec(module, items, figures, variant=variant)

        user_prompt = build_user_prompt(module, items, figures, date_str, variant=variant)
        if extra_instruction:
            user_prompt += "\n\n【本次必须修正】\n" + extra_instruction
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        raw, meta = await self._chat(messages, on_progress=on_progress)
        usage.add(module, meta["prompt"], meta["completion"])
        try:
            return validate_spec(extract_json(raw))
        except LLMError as first_error:
            if meta.get("finish") == "length":
                # 输出被 max_tokens 硬截断：重试只会再截一次，直接给出可操作的报错
                raise LLMError(
                    f"输出被 LLM_MAX_TOKENS={settings.llm_max_tokens} 截断"
                    f"（finish_reason=length，正文 {meta['content_chars']} 字符 / "
                    f"思考 {meta['reasoning_chars']} 字符，{first_error}）。"
                    f"推理模型的思维链与正文共享 token 预算：请在 .env 设 "
                    f"LLM_REASONING_EFFORT=none 关闭思考，或调大 LLM_MAX_TOKENS 后重试"
                ) from first_error
            # 修复重试要在同一批素材上重写：只回传「上一次输出片段」会让模型丢掉
            # prompt 里的事实凭空编，且片段取的是开头，恰好看不到尾部被截断的位置。
            messages += [
                {"role": "assistant", "content": raw or "（上一次输出为空）"},
                {"role": "user", "content": (
                    f"上面的输出不是合法的 ArticleSpec JSON（错误：{first_error}）。"
                    "请基于同一批素材重新输出完整 JSON，不要任何解释。"
                )},
            ]
            if on_progress:
                try:
                    on_progress("retry", 1)
                except Exception:
                    pass
            raw2, meta2 = await self._chat(messages, on_progress=on_progress)
            usage.add(module, meta2["prompt"], meta2["completion"])
            try:
                return validate_spec(extract_json(raw2))
            except LLMError as exc:
                raise LLMError(
                    f"模型两次输出均不合法：{exc}"
                    f"（第二次正文 {meta2['content_chars']} 字符 / 思考 "
                    f"{meta2['reasoning_chars']} 字符 / finish={meta2.get('finish')} / "
                    f"耗时 {meta2['seconds']}s）"
                ) from exc

    async def rewrite_title(self, spec: dict, featured_item) -> str | None:
        """跑分型标题的定向重写：只重出标题（几十 token），失败返回 None 保留原题。

        与整篇重写不同，标题重写足够便宜，可以在成稿后按检测结果随时触发。
        """
        if not self.enabled:
            return None
        from .prompts import build_title_rewrite_messages

        raw, _meta = await self._chat(build_title_rewrite_messages(spec, featured_item))
        usage.add("paper-title", _meta["prompt"], _meta["completion"])
        try:
            data = extract_json(raw)
            title = str(data.get("title") or "").strip()
            return title or None
        except LLMError:
            return None
