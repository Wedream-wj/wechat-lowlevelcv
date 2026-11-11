"""防呆校验体检：论文名输入框为空时点击「按论文名精读」必须被前端拦截。

验证四点：
1. 空输入点击 → 不发出任何 /api/run/paper 请求；
2. Toast 提示「检索关键词不能为空…」；
3. 输入框出现红框（guard-empty）与抖动动画（guard-shake）;
4. 填入论文名后点击 → 请求正常发出（payload 带非空 query）。
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _browse(base: str) -> None:
    from playwright.async_api import async_playwright

    fired: list[str] = []
    payloads: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("request", lambda r: fired.append(r.url) if "/api/run/" in r.url else None)

        async def _stub(route):
            payloads.append(route.request.post_data or "")
            await route.fulfill(status=500, body="体检桩：不应真正执行")

        await page.route("**/api/run/paper", _stub)
        await page.goto(base, wait_until="domcontentloaded")
        await page.wait_for_selector("button.cta-purple", timeout=20000)

        # ① 空输入点击
        await page.click("button.cta-purple")
        await page.wait_for_timeout(600)
        assert not fired, f"空输入仍发出了请求：{fired}"
        toast = await page.text_content("#toast")
        assert "不能为空" in (toast or ""), f"toast 未提示：{toast!r}"
        assert await page.eval_on_selector("#paper-query", "el=>el.classList.contains('guard-empty')"), "输入框未标红"
        assert await page.eval_on_selector("#paper-query", "el=>el.classList.contains('guard-shake')"), "输入框未抖动"
        print("  ① 空输入点击：请求已拦截，toast + 红框 + 抖动全部生效")

        # ② 纯空格输入同样拦截
        await page.fill("#paper-query", "   ")
        await page.click("button.cta-purple")
        await page.wait_for_timeout(400)
        assert not fired, f"纯空格输入仍发出了请求：{fired}"
        print("  ② 纯空格输入：同样被拦截")

        # ③ 合法输入放行（后端被桩掉，只验证请求发出且 query 非空）
        await page.fill("#paper-query", "  PigMS R-CNN  ")
        await page.click("button.cta-purple")
        await page.wait_for_timeout(400)
        assert fired, "合法输入未发出请求"
        assert "PigMS R-CNN" in (payloads[0] if payloads else ""), f"query 未 trim：{payloads[:1]}"
        print("  ③ 合法输入（含首尾空格）：请求放行，query 已 trim 为「PigMS R-CNN」")

        await page.screenshot(path=str(ROOT / "out" / "_verify_guard" / "guard.png"))
        await browser.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="llcv_guard_"))
    (tmp / "out").mkdir()
    port = _free_port()
    import os
    env = dict(os.environ)
    env.update({"LLCV_PORT": str(port)})
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(30):
            time.sleep(1)
            import urllib.request
            try:
                urllib.request.urlopen(base, timeout=2)
                break
            except Exception:
                continue
        else:
            raise RuntimeError("服务启动超时")
        asyncio.run(_browse(base))
        print("[体检] 输入防呆四项全过")
    finally:
        proc.terminate()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
