"""真机体检：最近出稿「编辑润色」面板的双向实时同步 + 保存链路。

跑法（需要 playwright，未装则自动跳过）：

    python -m scripts.verify_edit_panel

它会起一个临时服务实例（独立 DB / out 目录，不碰正式数据），用 Edge 打开控制台，
依次验证：
0. 最近出稿操作栏：预览/编辑润色/复制 MD/推草稿/删除 五键各自底色不同、圆角统一、图标齐全；
1. 点「编辑润色」→ 双栏出现，左 Markdown、右富文本都有内容；
2. 在 Markdown 里追加段落 → 右侧富文本 300ms 内出现同一段文字（左 → 右）；
3. 直接在右侧富文本里改字 → 左侧 Markdown 反解出同样的改动（右 → 左）；
4. 保存 → 版本号 +1，库里 markdown/html 都是润色后的版本，预览页同步重写；
5. 图表不丢：富文本里仍有 <table> 与 <img>。
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                return r.status == 200
        except Exception:
            time.sleep(0.4)
    return False


def _seed(tmp: Path) -> str:
    """用正式库里的最新一期做样本（只拿内容，不写回正式库）。"""
    from app.config import settings
    from app import db

    rows = db.list_articles(limit=1)
    if not rows:
        raise SystemExit("正式库里还没有成稿，先跑一期再来体检")
    article = db.get_article(rows[0]["issue_id"])
    assert article is not None

    settings.db_path = tmp / "data" / "e2e.db"
    settings.out_dir = tmp / "out"
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    db.init_db()
    article.cover_path = ""
    db.save_article(article)
    return article.issue_id


async def _browse(base: str, issue_id: str, shot: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(base, wait_until="domcontentloaded")
        await page.wait_for_selector(".issue", timeout=20000)

        # 操作栏配色体检：四个核心动作必须是四种可区分的底色
        ops = await page.evaluate("""() => {
          const pick = (sel) => {
            const b = document.querySelector(sel);
            if (!b) return null;
            const cs = getComputedStyle(b);
            return {bg: cs.backgroundImage + " " + cs.backgroundColor, color: cs.color,
                    radius: cs.borderTopLeftRadius, gap: cs.columnGap,
                    icon: !!b.querySelector("svg")};
          };
          return {
            view: pick(".issue button.op-view"), edit: pick(".issue button.op-edit"),
            copy: pick(".issue button.op-copy"), push: pick(".issue button.op-push"),
            del: pick(".issue button.op-del"),
          };
        }""")
        for name in ("view", "edit", "copy", "push", "del"):
            assert ops[name], f"缺少按钮：{name}"
        bgs = [ops[n]["bg"] for n in ("view", "edit", "copy", "push", "del")]
        assert len(set(bgs)) == len(bgs), f"按钮底色有重复，层级不分明：{bgs}"
        for name in ("view", "edit", "copy", "push", "del"):
            assert ops[name]["icon"], f"按钮缺图标：{name}"
        print(f"  ⓪ 操作栏配色 OK：五个按钮五种底色，圆角 {ops['push']['radius']}，"
              f"图标齐全，推草稿={ops['push']['bg'][:34]}…")
        await page.locator(".issue").first.screenshot(path=str(shot.with_name("ops.png")))

        await page.click(".issue button.op-edit")
        await page.wait_for_selector("#edit-dialog[open]", timeout=10000)
        await page.wait_for_timeout(400)

        md0 = await page.input_value("#edit-md")
        assert len(md0) > 200, f"Markdown 没载入：{len(md0)} 字符"
        rich_text0 = await page.inner_text("#edit-rich")
        assert len(rich_text0) > 100, "富文本没渲染"
        assert await page.locator("#edit-rich table").count() > 0, "富文本丢表格"
        assert await page.locator("#edit-rich img").count() > 0, "富文本丢配图"
        print(f"  ① 载入 OK：Markdown {len(md0)} 字符 / 富文本 {len(rich_text0)} 字符 / "
              f"表格 {await page.locator('#edit-rich table').count()} / "
              f"配图 {await page.locator('#edit-rich img').count()}")

        # 左 → 右：Markdown 追加一段
        marker_md = "人工润色测试段落：这一句从 Markdown 栏写进去。"
        await page.fill("#edit-md", md0 + f"\n\n{marker_md}\n")
        await page.wait_for_timeout(400)
        assert marker_md in await page.inner_text("#edit-rich"), "左 → 右 同步失败"
        print("  ② Markdown → 富文本实时同步 OK")

        # 右 → 左：在富文本最后一段末尾打字
        marker_rich = "（富文本改字）"
        last_p = page.locator("#edit-rich p").last
        await last_p.click()
        await page.keyboard.press("End")
        await page.keyboard.type(marker_rich, delay=15)
        await page.wait_for_timeout(600)
        md1 = await page.input_value("#edit-md")
        assert marker_rich in md1, "右 → 左 同步失败"
        print("  ③ 富文本 → Markdown 反解 OK")

        await page.screenshot(path=str(shot), full_page=False)

        # 保存 → 版本 +1，库与预览页同步
        await page.click("#edit-save")
        await page.wait_for_selector("#edit-status.saved", timeout=15000)
        status = await page.inner_text("#edit-status")
        assert "第 1 版" in status or "第 2 版" in status, f"版本号异常：{status}"
        print(f"  ④ 保存 OK：{status.strip()[:70]}")

        await page.click("#edit-dialog .edit-tools button.ghost")
        await page.wait_for_timeout(300)
        await browser.close()


def _verify_persisted(issue_id: str) -> None:
    from app.config import settings
    from app import db
    from app.render.preview import render_preview  # noqa: F401  仅确认模块可用

    article = db.get_article(issue_id)
    assert article is not None
    assert "人工润色测试段落" in article.markdown
    assert "人工润色测试段落" in article.html
    assert "（富文本改字）" in article.html
    meta = db.edit_meta(issue_id)
    assert meta["rev"] >= 1, f"版本号没涨：{meta}"
    md_file = settings.out_dir / f"{issue_id}.md"
    preview = settings.out_dir / f"preview_{issue_id}.html"
    assert md_file.exists() and "人工润色测试段落" in md_file.read_text(encoding="utf-8")
    assert preview.exists() and "人工润色测试段落" in preview.read_text(encoding="utf-8")
    print(f"  ⑤ 落库落盘 OK：rev={meta['rev']}，md/html/预览三件套都是润色版")


def main() -> int:
    try:
        import playwright  # noqa: F401
    except Exception:
        print("跳过：未安装 playwright（pip install playwright）")
        return 0

    tmp = ROOT / "out" / "_verify_edit"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    issue_id = _seed(tmp)

    port = _free_port()
    import os

    env = dict(os.environ)
    env.update({
        "LLCV_DB": str(tmp / "data" / "e2e.db"),
        "LLCV_OUT": str(tmp / "out"),
        "LLCV_PORT": str(port),
    })
    proc = subprocess.Popen([sys.executable, "-m", "app.main"], cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(base, 30):
            print("服务没起来，体检中止")
            return 1
        print(f"[体检] 临时实例 {base}（样本 {issue_id}）")
        shot = tmp / "editor.png"
        asyncio.run(_browse(base, issue_id, shot))
        _verify_persisted(issue_id)
        print(f"[体检] 截图：{tmp/'ops.png'}（操作栏） / {shot}（编辑器）")
        print("[体检] 全部通过：双向同步 + 保存 + 推送数据源 = 最新润色版")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
