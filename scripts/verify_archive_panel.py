"""真机体检：历史出稿的「分页 + 模糊检索 + 操作按钮」链路。

跑法（需要 playwright + Edge，未装会自动报错退出）：

    python -m scripts.verify_archive_panel

起一个临时服务实例（独立 DB / out 目录，不碰正式数据），先灌 27 期合成稿件，
再用浏览器依次验证：
1. 默认第一页 20 条、分页器显示「共 27 条 / 第 1/2 页」；
2. 点「下一页」→ 第二页 7 条，最新的仍在第 1 页、最早的落在末页；
3. 每页切 10 条 → 3 页、单页 10 条；
4. 搜板块名「学术论文」→ 只剩 paper 模块，命中词高亮为 <mark>；
5. 搜正文独有关键词 → 命中且带正文片段（arch-snip）；
6. 搜不存在词 → 空态提示，且分页器收起；
7. 清空检索 → 恢复全量，五枚操作按钮齐全。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODULES = [("paper", "学术论文"), ("industry", "业界进展"), ("contest", "赛事竞赛")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.4)
    return False


def _seed(tmp: Path) -> None:
    """往临时库灌 27 期：模块轮转、时间递减，其中一期带「PigMS」标题与独特正文。"""
    from app.config import settings
    from app import db
    from app.models import Article

    settings.db_path = tmp / "data" / "arch.db"
    settings.out_dir = tmp / "out"
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()

    base = datetime(2026, 11, 14, 12, 0, 0)
    for i in range(27):
        module, label = MODULES[i % 3]
        title = f"{label}样本稿 第 {i + 1} 期"
        body = f"# {title}\n\n这是第 {i + 1} 期样本正文，用于分页与检索体检。\n"
        if i == 5:
            title = "Sensors 2021 | PigMS R-CNN：软非极大值抑制破解群养猪粘连分割"
            body = ("# Sensors 2021 | PigMS R-CNN\n\n本文提出软非极大值抑制，"
                    "在群养猪粘连场景下把实例分割的 mAP 提升到 0.9 以上。\n")
        db.save_article(Article(
            module=module, title=title, digest=f"{label} 摘要 {i + 1}",
            markdown=body, html=f"<p>{title}</p>", status="rendered",
            issue_id=f"{module}-20261114-{i:03d}",
            created_at=base - timedelta(days=i),
        ))


async def _browse(base: str, shot: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.goto(base, wait_until="domcontentloaded")
        await page.wait_for_selector(".issue", timeout=20000)

        async def count() -> int:
            return await page.eval_on_selector_all(".issue", "els=>els.length")

        async def pager_text() -> str:
            return (await page.text_content("#arch-pager") or "").replace("\n", " ")

        # ① 默认第一页
        assert await count() == 20, f"首页条数异常：{await count()}"
        assert "共 27 条" in await pager_text(), await pager_text()
        first_title = await page.text_content(".issue h4")
        assert "第 1 期" in first_title, first_title
        print(f"  ① 默认页：20 条，分页器「{(await pager_text())[:46]}…」")

        # ② 下一页
        await page.click("#arch-pager button:has-text('下一页')")
        await page.wait_for_timeout(500)
        assert await count() == 7, f"末页条数异常：{await count()}"
        last_titles = await page.eval_on_selector_all(".issue h4", "els=>els.map(e=>e.textContent)")
        assert any("第 27 期" in t for t in last_titles), last_titles[-1]
        print(f"  ② 翻到第 2 页：7 条，最早一期「{last_titles[-1][:22]}」可见")

        # ③ 每页 10 条（自动回到第 1 页）
        await page.select_option("#arch-size", "10")
        await page.wait_for_timeout(500)
        assert await count() == 10, f"每页 10 条未生效：{await count()}"
        assert "第 1/3 页" in await pager_text(), await pager_text()
        print("  ③ 每页切 10 条：3 页、单页 10 条，页码已重置为 1")

        # ④ 搜板块中文名
        await page.fill("#arch-q", "学术论文")
        await page.wait_for_timeout(700)
        mods = await page.eval_on_selector_all(".issue .sub span:nth-child(2)",
                                               "els=>[...new Set(els.map(e=>e.textContent))]")
        assert mods == ["学术论文"], f"板块过滤不纯：{mods}"
        marks = await page.eval_on_selector_all(".issue mark", "els=>els.map(e=>e.textContent)")
        assert marks and all("学术论文" in m for m in marks[:3]), marks[:3]
        assert "命中 9 期" in (await page.text_content("#issue-count")), await page.text_content("#issue-count")
        print(f"  ④ 搜「学术论文」：9 期全为学术稿，标题命中高亮 {len(marks)} 处")

        # ⑤ 搜正文独有关键词
        await page.fill("#arch-q", "软非极大值抑制")
        await page.wait_for_timeout(700)
        assert await count() == 1, f"正文检索命中数异常：{await count()}"
        snip = await page.text_content(".arch-snip") or ""
        assert "软非极大值抑制" in snip, f"缺正文片段：{snip!r}"
        assert await page.eval_on_selector_all(".arch-snip mark", "els=>els.length") >= 1
        print(f"  ⑤ 搜正文关键词：命中 1 期，片段「{snip[:30]}…」已高亮")

        # ⑥ 无结果
        await page.fill("#arch-q", "zzz-不存在的关键词")
        await page.wait_for_timeout(700)
        assert await count() == 0
        empty = await page.text_content("#issues")
        assert "没有匹配" in empty, empty
        assert (await page.text_content("#arch-pager") or "").strip() == "", "空结果分页器未收起"
        print("  ⑥ 无结果：空态提示正确，分页器收起")

        # ⑦ 清空恢复 + 操作按钮齐全
        await page.fill("#arch-q", "")
        await page.wait_for_timeout(700)
        assert await count() == 10, f"清空后未恢复：{await count()}"
        ops = await page.eval_on_selector_all(
            ".issue:first-child .ops button",
            "els=>els.map(e=>({t:e.textContent.trim(), bg:getComputedStyle(e).backgroundColor}))")
        labels = [o["t"] for o in ops]
        for need in ("预览", "编辑润色", "复制 MD", "推草稿", "删除"):
            assert need in labels, f"缺按钮 {need}：{labels}"
        assert len({o["bg"] for o in ops}) >= 4, f"按钮配色重复：{ops}"
        print(f"  ⑦ 清空检索恢复 10 条；五键齐全 {labels}，底色互异 {len({o['bg'] for o in ops})} 种")

        await page.screenshot(path=str(shot), full_page=False)
        await browser.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="llcv_arch_"))
    try:
        _seed(tmp)
        port = _free_port()
        env = dict(os.environ)
        env.update({"LLCV_PORT": str(port), "LLCV_DB": str(tmp / "data" / "arch.db"),
                    "LLCV_OUT": str(tmp / "out")})
        proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=ROOT, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            if not _wait_http(base):
                raise RuntimeError("服务启动超时")
            shot = tmp / "archive.png"
            asyncio.run(_browse(base, shot))
            print(f"[体检] 截图：{shot}")
            keep = ROOT / "out" / "_verify_arch"
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy(shot, keep / "archive.png")
        finally:
            proc.terminate()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
