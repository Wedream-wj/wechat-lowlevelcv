"""后台 UI 兜底：给刚推上去的草稿勾「文字原创」、选合集「每日推送」。

微信开放接口没有这两个参数（见 `attrs.py` 顶部说明），只能回 mp.weixin.qq.com
后台点。这里用 Playwright（可选依赖）走一遍：

    草稿箱 → 按标题定位草稿 → 编辑 → 声明原创选「文字原创」 → 合集选「每日推送」

设计原则：
- **绝不抛异常**：推草稿已经成功了，UI 补勾失败只能降级成「待办清单」，不能反过来
  把一次成功的推送标成失败；
- **定位用文本优先**：后台 DOM 的 class 是编译产物（weui-desktop-xxx 会变），文案
  「草稿箱 / 编辑 / 文字原创 / 合集」相对稳定，所以每个动作都准备多组选择器逐个试；
- **缺合集要吼一声**：找不到「每日推送」时返回明确原因（去后台新建合集），而不是
  假装选上了。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import settings
from ..sources.headless import HeadlessUnavailable, find_browser
from .attrs import PublishAttrs

HOME_URL = "https://mp.weixin.qq.com/"
NAV_TIMEOUT_MS = 20_000
CLICK_TIMEOUT_MS = 6_000


def _load_playwright():
    """惰性导入：没装 playwright 的部署不该因为 import 就崩。"""
    try:
        from playwright.async_api import async_playwright
    except Exception:  # ImportError / 平台不支持
        return None
    return async_playwright


def _browser_exe() -> str | None:
    """优先复用系统已装的 Edge/Chrome（同 app/sources/headless.py 的查找逻辑），
    这样只装 playwright 的 Python 包就够，不用再下 150MB 的 chromium。"""
    try:
        return find_browser()
    except HeadlessUnavailable:
        return None


@dataclass
class FillResult:
    """UI 补勾结果。ok=False 不代表推送失败，只代表这两项要人工补。"""

    ok: bool = False
    skipped: bool = False
    applied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    steps: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.skipped:
            return f"后台自动勾选已跳过：{self.reason}"
        if self.ok:
            return "后台已自动勾选：" + "、".join(self.applied)
        parts = [f"后台自动勾选未完成：{self.reason or '未知原因'}"]
        if self.missing:
            parts.append("缺项：" + "、".join(self.missing))
        if self.applied:
            parts.append("已勾选：" + "、".join(self.applied))
        return "；".join(parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "applied": self.applied,
            "missing": self.missing,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 选择器
# 文本型选择器写在前面：后台改版时 class 几乎必变，文案不一定变。
_DRAFT_BOX = ('text=草稿箱', 'a:has-text("草稿箱")', '[class*="menu"] :text("草稿箱")')
_EDIT_BTN = ('text=编辑', 'a:has-text("编辑")', 'button:has-text("编辑")')
_ORIGINAL_PANEL = ('text=声明原创', 'text=原创声明', 'button:has-text("原创")', 'text=文章设置')
_COLLECTION_PANEL = ('text=合集', 'text=添加合集', 'text=选择合集')


def _title_selector(title: str) -> tuple[str, ...]:
    """草稿列表里按标题定位。标题含引号/括号时用 :text-is 兜住精确匹配。"""
    safe = title.strip().replace('"', '\\"')
    return (f'text="{safe}"', f':text-is("{safe}")', f'[title="{safe}"]')


async def _click_any(page, selectors: tuple[str, ...] | list[str],
                     *, timeout: int = CLICK_TIMEOUT_MS) -> str | None:
    """按顺序试选择器，点中第一个可见的即返回该选择器，全失败返回 None。"""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() == 0:
                continue
            await loc.first.click(timeout=timeout)
            return sel
        except Exception:
            continue
    return None


async def _apply(page, title: str, attrs: PublishAttrs) -> FillResult:
    """在已登录的页面上完成「勾原创 + 选合集」。测试直接喂假 page。"""
    result = FillResult()

    async def step(label: str, selectors) -> bool:
        hit = await _click_any(page, selectors)
        result.steps.append(f"{label}: {'✓ ' + hit if hit else '✗ 未找到'}")
        return hit is not None

    if not await step("打开草稿箱", _DRAFT_BOX):
        result.reason = "没找到「草稿箱」入口（可能未登录，或后台改版）"
        return result
    if not await step("定位草稿", _title_selector(title)):
        result.reason = f"草稿箱里没找到标题为「{title}」的草稿"
        return result
    if not await step("进入编辑", _EDIT_BTN):
        result.reason = "草稿条目上没找到「编辑」按钮"
        return result

    # 原创声明：先开面板，再点具体类型（「文字原创」）
    await step("打开原创声明", _ORIGINAL_PANEL)
    if await step(f"勾选原创类型「{attrs.original_type}」",
                  (f'text="{attrs.original_type}"', f'label:has-text("{attrs.original_type}")',
                   f'input[value="{attrs.original_type}"]')):
        result.applied.append(f"原创={attrs.original_type}")
    else:
        result.missing.append(attrs.original_type)

    # 合集归属：先开下拉，再在里面找「每日推送」
    await step("打开合集下拉", _COLLECTION_PANEL)
    if await step(f"选择合集「{attrs.collection}」",
                  (f'text="{attrs.collection}"', f':text-is("{attrs.collection}")',
                   f'li:has-text("{attrs.collection}")')):
        result.applied.append(f"合集={attrs.collection}")
    else:
        result.missing.append(attrs.collection)
        result.reason = (f"合集下拉里没有「{attrs.collection}」："
                         "到公众号后台「内容管理 → 合集」新建该合集后，重新执行本步骤")

    result.ok = not result.missing
    if result.ok:
        result.reason = ""
    elif not result.reason:
        result.reason = "部分选项未点中（后台 DOM 可能已改版）"
    return result


async def fill_draft_attrs(title: str, attrs: PublishAttrs, *,
                           enabled: bool | None = None) -> FillResult:
    """入口：拿登录态打开后台，给标题为 `title` 的草稿补勾两个属性。"""
    if enabled is None:
        enabled = settings.wx_ui_fill
    if not enabled:
        return FillResult(
            skipped=True,
            reason="未开启 WX_UI_FILL=1（默认关闭：需要已登录的浏览器存储态）",
        )

    async_playwright = _load_playwright()
    if async_playwright is None:
        return FillResult(
            skipped=True,
            reason="未安装 playwright：pip install playwright"
                   "（浏览器复用系统 Edge/Chrome，无需再下 chromium）",
        )

    state = settings.wx_storage_state
    exe = _browser_exe()
    try:
        async with async_playwright() as pw:
            # 有系统 Edge/Chrome 就用它，否则回落到 playwright 自带的 chromium
            launch_kwargs: dict = {"headless": True}
            if exe:
                launch_kwargs["executable_path"] = exe
            browser = await pw.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                storage_state=state if state else None,
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
            await page.goto(HOME_URL, timeout=NAV_TIMEOUT_MS)
            result = await _apply(page, title, attrs)
            await context.close()
            await browser.close()
            return result
    except Exception as exc:  # 浏览器起不来、登录态失效……一律降级
        return FillResult(reason=f"浏览器自动化异常：{type(exc).__name__}: {exc}")
