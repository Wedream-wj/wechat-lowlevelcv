"""无头浏览器取 DOM：给「必须有 JS 才出内容」的赛事站点兜底。

为什么需要这一层（2026-10 实测）：
- Codabench：直接 requests 会掉进 iocaine 蜜罐，返回《1984》随机段落 + 假链接，
  状态码还是 200 —— 用普通抓取只会把垃圾当数据；
- 天智杯官网（Vue SPA）：静态 HTML 只有一句 "doesn't work properly without JavaScript"；
- FlyAI 主站：瑞数验证码，连无头浏览器都拦（所以改用它的静态镜像 static.flyai.com）。

用系统已装的 Edge / Chrome 以 `--headless --dump-dom` 跑一遍，拿渲染后的 DOM。
不额外装 Playwright（省 150MB 且要下 chromium），找不到浏览器时抛 HeadlessUnavailable，
调用方降级跳过并把它写进源告警。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_WINDOWS_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_POSIX_CANDIDATES = (
    "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "chrome",
)


class HeadlessUnavailable(RuntimeError):
    """本机没有可用的 Chromium 内核浏览器。"""


def find_browser() -> str:
    """返回浏览器可执行文件路径；找不到抛 HeadlessUnavailable。"""
    override = os.getenv("HEADLESS_BROWSER", "").strip()
    if override and Path(override).exists():
        return override
    if sys.platform.startswith("win"):
        for path in _WINDOWS_CANDIDATES:
            if Path(path).exists():
                return path
    for name in _POSIX_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    raise HeadlessUnavailable("未找到 Edge/Chrome，可用 HEADLESS_BROWSER 指定路径")


def available() -> bool:
    try:
        find_browser()
        return True
    except HeadlessUnavailable:
        return False


def _run_sync(url: str, timeout: float, virtual_ms: int) -> str:
    exe = find_browser()
    cmd = [
        exe,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--mute-audio",
        f"--user-agent={UA}",
        f"--virtual-time-budget={virtual_ms}",
        "--dump-dom",
        url,
    ]
    kwargs: dict = {"capture_output": True, "timeout": timeout}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, **kwargs)  # noqa: S603 - 参数固定，URL 由调用方传入
    out = proc.stdout.decode("utf-8", "ignore")
    if len(out) < 200:
        err = proc.stderr.decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"无头渲染返回空内容（{len(out)} 字节）{err}")
    return out


async def render_dom(url: str, *, timeout: float = 45.0, virtual_ms: int = 12000) -> str:
    """渲染页面并返回 DOM 字符串（不阻塞事件循环）。"""
    return await asyncio.to_thread(_run_sync, url, timeout, virtual_ms)


async def render_text(url: str, *, timeout: float = 45.0, virtual_ms: int = 12000) -> str:
    """渲染后只取可见文本，便于做日期 / 关键词抽取。"""
    from .base import strip_html

    dom = await render_dom(url, timeout=timeout, virtual_ms=virtual_ms)
    return strip_html(dom, 8000)


def looks_like_honeypot(html: str) -> bool:
    """iocaine 蜜罐特征：标题/正文是《1984》片段，或带假 iocaine 链接。

    命中就当「站点拒绝非浏览器访问」，交给无头渲染，而不是把垃圾当数据。
    """
    if not html:
        return True
    low = html.lower()
    # 「Lighter than air」是 iocaine 固定的开场白，单条即可判定
    if "iocaine" in low or "lighter than air" in low:
        return True
    markers = ("big brother", "thought police", "winston", "savage", "ministry of truth")
    hits = sum(1 for m in markers if m in low)
    return hits >= 2
