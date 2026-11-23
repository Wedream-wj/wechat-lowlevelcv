"""封面图「图文 100% 强一致」守卫。

事故复盘（2026-11-24）：《CVPR 2025 | VideoGigaGAN》历史稿的首张封面显示成
「CVPR 2024 | SR4IR」——两篇同为 2026-11-17 的 paper 稿共用
`cover_paper_20261117.jpg`，后出稿的 SR4IR 把前者覆盖，列表/预览读到的是别篇的图。

三层防线（对应主编铁律）：
1. **物理唯一命名**：封面文件名带「成稿标题指纹」，同日不同标题绝不共用路径；
   同一标题复跑落到同一文件（幂等），不会漂移。
2. **生成前一致性检查** `check_pick_consistency`：选定 hero 之后、画封面之前，
   核对归属标签（caption / url / 来源素材）与成稿标题的核心方法是否一致；
   出现别篇论文方法名且无本期方法名 → 判「跨篇串图」直接拒绝，绝不强行凑数。
3. **归属标签落盘 / 复核**：`write_ownership` 写 sidecar，`verify_ownership`
   在「复用 / 预览 / 推送」前核对「这张封面属于哪篇文章」，不匹配就拦下。

封面主标题的中文短句断行由 `cover._hang_punctuation` 保证（禁则标点不落行首）。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 常见指标 / 硬件 / 会议缩写：出现在任何标题里都不该被当成「某篇论文的方法名」
_STOP = {
    "psnr", "ssim", "lpips", "niqe", "musiq", "maniqa", "clipiqa", "dists",
    "fid", "arxiv", "api", "gpu", "cpu", "vram", "tpu", "cvpr", "iccv", "eccv",
    "neurips", "icml", "iclr", "acm", "mm", "ieee", "tpami", "vsr", "sr",
    "gan", "cnn", "vit", "sota", "ntire", "torch", "pytorch", "html", "url",
}
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
# 中文任务领域词：核对「视频超分」「去模糊」这类领域是否与封面归属冲突
_DOMAIN_WORDS = ("视频超分", "图像超分", "超分", "去模糊", "去噪", "低光增强",
                 "高光谱", "逆问题", "量化", "蒸馏", "修复", "重建", "识别")


def _is_name(seg: str) -> bool:
    """专名式：全大写（SR4IR / SFD）、驼峰（VideoGigaGAN / TinySR）、含数字。"""
    if len(seg) < 2:
        return False
    if any(ch.isdigit() for ch in seg):
        return True
    if seg.isupper():
        return True
    return seg[1:] != seg[1:].lower()


def method_tokens(text: str) -> set[str]:
    """抽「专有名词式」方法名 token：VideoGigaGAN / SR4IR / Real-IISR / SFD。"""
    out: set[str] = set()
    for tok in _TOKEN.findall(text or ""):
        if len(tok) < 3 or tok.lower() in _STOP:
            continue
        if any(_is_name(seg) for seg in tok.split("-")):
            out.add(tok)
    return out


def cover_fingerprint(spec: dict) -> str:
    """成稿标题的稳定指纹（标题相同 → 同一文件，标题不同 → 不同文件）。"""
    title = str(spec.get("title") or "").strip()
    return hashlib.sha1(title.encode("utf-8")).hexdigest()[:6] or "local"


def cover_filename(module: str, date_str: str, spec: dict, suffix: str) -> str:
    """带归属指纹的封面文件名：同日多篇绝不互相覆盖。

    `cover_paper_20261117_2562b6.jpg` —— 末段是成稿标题 sha1 前 6 位。
    """
    day = str(date_str or "").replace(".", "") or "00000000"
    return f"cover_{module}_{day}_{cover_fingerprint(spec)}{suffix}"


def cover_identity(spec: dict, featured_item=None) -> dict:
    """本期「核心实体」：成稿标题的方法名 + 主角原题的方法名 + 任务领域。"""
    title = str(spec.get("title") or "")
    blob = " ".join(str(spec.get(k) or "") for k in ("title", "subtitle", "digest"))
    core = method_tokens(title)
    if featured_item is not None:
        core |= method_tokens(str(getattr(featured_item, "title", "") or ""))
    domain = [w for w in _DOMAIN_WORDS if w in blob]
    return {
        "title": title,
        "core_methods": sorted(core),
        "domain": domain,
        "fingerprint": cover_fingerprint(spec),
    }


@dataclass
class CoverCheck:
    ok: bool
    reason: str = "ok"
    identity: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


def _blob_of(pick) -> str:
    # 只看「归属文本」，不看下载后的本地文件名：hash 文件名（4307a8c6…）含数字，
    # 会被专名规则误判成方法名，是假反证。
    parts = [getattr(pick, "caption", ""), getattr(pick, "url", ""),
             getattr(pick, "source", ""), getattr(pick, "owner_title", "")]
    return " ".join(str(p or "") for p in parts).lower()


def check_pick_consistency(spec: dict, pick, featured_item=None,
                           other_items: list | None = None) -> CoverCheck:
    """生成前一致性检查：封面归属必须与本期标题 / 核心方法对齐。

    匹配度凭证是「本期主角素材」（rank() 已用 order==0 保证 hero 只出自主角），
    不需要靠猜图片文字，因此不会误伤「Figure 1: overview」这类正常架构图图注。

    - **跨篇串图（硬拒）**：hero 归属文本里出现别篇论文方法名，且不含本期方法名。
    - **主角错配（硬拒 / 不凑数）**：成稿标题声明了核心方法（如 VideoGigaGAN），
      但封面主角素材的原题里没有该方法的任何线索 → 无法确认匹配度，宁可退回
      品牌底图（封面文字即本期标题，天然 100% 一致），绝不强行凑数。
    - 通过时返回 identity，供归属标签 sidecar 落盘。
    """
    ident = cover_identity(spec, featured_item)
    title_methods = method_tokens(ident["title"])
    if pick is None:
        # 品牌渐变底图：封面文字就是本期标题，天然 100% 一致
        return CoverCheck(True, "品牌底图（无 hero），封面文字即本期标题", ident)

    blob = _blob_of(pick)
    title_lower = {m.lower() for m in title_methods}
    core_lower = {m.lower() for m in ident["core_methods"]}
    hit_core = [m for m in core_lower if m in blob]

    # 1) 别篇论文方法名池（本期其他候选素材标题里的专名，去掉本期自己）
    foreign: set[str] = set()
    for it in other_items or []:
        if featured_item is not None and it is featured_item:
            continue
        foreign |= method_tokens(str(getattr(it, "title", "") or ""))
    foreign = {f for f in foreign if f.lower() not in core_lower}
    hit_foreign = sorted(f for f in foreign if f.lower() in blob)

    if hit_foreign and not hit_core:
        return CoverCheck(
            False,
            f"跨篇串图：封面归属含别篇方法名「{hit_foreign[0]}」，"
            f"与本期核心方法 {ident['core_methods'] or '（标题无缩写）'} 不一致，已拒绝",
            ident)

    # 2) 归属标签：pick 明确带 owner_title 且不是本期主角 → 拒
    owner = str(getattr(pick, "owner_title", "") or "")
    if owner and featured_item is not None:
        feat_title = str(getattr(featured_item, "title", "") or "")
        if owner.strip() != feat_title.strip() and not hit_core:
            return CoverCheck(
                False,
                f"归属标签不符：封面来自「{owner[:36]}」，本期主角是「{feat_title[:36]}」，"
                "无法确认匹配度，按铁律不凑数",
                ident)

    # 3) hero 图「自身归属文本」里的方法名与成稿互斥 → 拒。
    #    注意不能用「主角原题 vs 成稿标题」判互斥：成稿标题常是编辑另起的中文名
    #    （ReDDiT / DTG-Restore、DeLang-SR / MUTE），两者本就不同名却完全同一篇，
    #    拿它当反证会误杀大批正常稿。真正的反证只能来自封面图自己带的标签
    #    （图注 / URL 明确写着别的方法名），rank() 的 order==0 才是归属凭证。
    hero_methods = method_tokens(blob)
    if title_methods and hero_methods and not (
            {m.lower() for m in title_methods}
            & ({m.lower() for m in hero_methods} | set(hit_core))):
        return CoverCheck(
            False,
            f"封面图归属可疑：图注 / 来源里的方法名 {sorted(hero_methods)} "
            f"与成稿核心方法 {sorted(title_methods)} 互斥，无法确认匹配度，按铁律不凑数",
            ident)

    return CoverCheck(True, "ok", ident)


# ---------------------------------------------------------------- 归属标签 sidecar
def sidecar_path(cover_path: str | Path) -> Path:
    p = Path(cover_path)
    return p.with_suffix(p.suffix + ".json")


def write_ownership(cover_path: str | Path, spec: dict, pick=None,
                    identity: dict | None = None, issue_id: str = "") -> str:
    """封面旁写一份归属标签：这张图属于哪篇文章、核心方法是什么、指纹多少。"""
    ident = identity or cover_identity(spec)
    data = {
        "owner_title": ident.get("title", ""),
        "core_methods": ident.get("core_methods", []),
        "domain": ident.get("domain", []),
        "fingerprint": ident.get("fingerprint", ""),
        "issue_id": issue_id,
        "hero": {
            "caption": str(getattr(pick, "caption", "") or ""),
            "url": str(getattr(pick, "url", "") or ""),
            "source": str(getattr(pick, "source", "") or ""),
        } if pick is not None else None,
    }
    out = sidecar_path(cover_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


def verify_ownership(cover_path: str | Path, spec: dict) -> CoverCheck:
    """复用 / 预览 / 推送前核对归属：sidecar 里的文章必须与当前成稿一致。

    返回 ok=False 时调用方必须停止复用该封面（重新生成）。没有 sidecar 的
    历史封面按「未知」放行，但 reason 会标注，避免误伤存量稿件。
    """
    side = sidecar_path(cover_path)
    if not side.exists():
        return CoverCheck(True, "无归属标签（历史封面），按存量放行")
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return CoverCheck(False, "归属标签损坏，禁止复用")
    want = cover_identity(spec)
    got_fp = str(data.get("fingerprint") or "")
    got_title = str(data.get("owner_title") or "")
    if got_fp and got_fp != want["fingerprint"]:
        return CoverCheck(
            False,
            f"封面归属不符：该图属于「{got_title[:36]}」，"
            f"当前文章是「{want['title'][:36]}」，禁止复用", want)
    if got_title and got_title.strip() != want["title"].strip():
        return CoverCheck(False, f"封面归属标题不一致：{got_title[:36]}", want)
    return CoverCheck(True, "ok", want)


# ---------------------------------------------------------------- Prompt 条款
COVER_CONSISTENCY_RULE = """【封面图与正文 100% 强一致（最高优先级，违反判整篇不合格）】
- 封面 / 主视觉所呈现的视觉主题、论文缩写与方法名称、任务领域，必须与本期成稿标题的
  核心方法严格对齐：写 VideoGigaGAN 就只能是 VideoGigaGAN 的架构图 / 效果图，
  绝不能混入 SR4IR 或任何其它论文、会议、公司的图片。
- 严禁跨篇 / 跨任务串图：不得复用其它论文、其它期、其它任务的封面；一期多稿时，
  每篇封面独立命名、独立归属，禁止共用同一个图片文件。
- 封面文字若涉及中文短句，必须断行合理、无标点落在行首、无错位、无溢出。
- 生成前一致性检查：选定封面后核对其归属标签（图注 / 来源 / 所属素材）是否与
  当前文章标题、核心方法一致；若无法确认匹配度，宁可重新检索或生成契合配图，
  也绝不强行凑数——此时用只含本期标题的品牌底图封面，同样 100% 一致。
"""
