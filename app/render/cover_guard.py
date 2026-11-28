"""封面图「图文 100% 强一致」守卫（论文 / 业界 / 赛事三模块共用）。

事故复盘 ①（2026-11-24）：《CVPR 2025 | VideoGigaGAN》历史稿的首张封面显示成
「CVPR 2024 | SR4IR」——两篇同为 2026-11-17 的 paper 稿共用
`cover_paper_20261117.jpg`，后出稿把前者覆盖，列表/预览读到的是别篇的图。

事故复盘 ②（2026-11-26）：两篇 2026-11-23 的 contest 稿《离岸流分割》《摄影修图
迁移》共用 `cover_contest_20261123.jpg`，文件里写的是同日第三篇《恶劣条件 3D 重建》
的标题。指纹命名只保护了守卫落地后的新稿，**存量共享封面仍需唯一性闸门兜住**。

五层防线（对应主编铁律，全部对 paper / industry / contest 生效）：
1. **物理唯一命名**：封面文件名带「成稿标题指纹」，同日不同标题绝不共用路径；
   同一标题复跑落到同一文件（幂等），不会漂移。
2. **生成前一致性检查** `check_pick_consistency`：选定 hero 之后、画封面之前，
   核对归属标签与成稿标题的核心方法 / 中文赛题 / 新闻主体是否一致；出现别篇
   方法名或别篇赛题且无本期线索 → 判「跨篇串图」直接拒绝，绝不强行凑数。
3. **归属标签落盘 / 复核**：`write_ownership` 写 sidecar，`verify_ownership`
   在「复用 / 预览 / 推送」前核对「这张封面属于哪篇文章」，不匹配就拦下。
4. **封面路径唯一性** `assert_cover_exclusive`：全库扫描，同一个封面文件被两篇
   以上不同期号引用 = 跨期串图（存量事故的直因），推送前硬拦，必须重新生成。
5. **封面资产有效性** `verify_cover_asset`：文件缺失 / 损坏 / 尺寸不足 = 无效
   链接，一律拒推，杜绝草稿箱里的裂图与占位图。

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

# 「封面图与正文 100% 强一致」对这三个模块一视同仁，任何一条规则都不得按模块绕过
COVER_MODULES = ("paper", "industry", "contest")

# 中文主题词：赛题 / 任务 / 新闻主体。method_tokens 只认英文专名，中文标题
# （「离岸流分割」「摄影修图迁移」「黄仁勋重返英伟达」）全靠这张表参与反证。
# 只用于「别篇线索」的反向比对：词典漏收只是不拦截，绝不产生误伤。
_TOPIC_WORDS = frozenset((
    # 赛事赛题 / 底层视觉任务
    "离岸流", "实例分割", "语义分割", "分割", "目标检测", "去雾", "去模糊", "低光",
    "暗光", "夜景", "超分", "光场", "修图", "风格迁移", "色彩迁移", "迁移",
    "深伪", "深伪检测", "质量评估", "画质", "复原", "增强", "压缩", "插帧",
    "高光谱", "去噪", "去雨", "去摩尔纹", "着色", "hdr", "事件相机", "3d重建",
    "三维重建", "测试时优化", "元学习", "测试时", "图像质量", "参考图",
    # 产业主体 / 公司（用于「别篇新闻主体」反证）
    "英伟达", "黄仁勋", "昇腾", "鸿蒙", "华为云", "寒武纪", "摩尔线程",
    "momenta", "minimax", "mistral", "deepseek", "anthropic", "openai",
    "马斯克", "xai", "qwen", "runway", "roboparty", "matos",
))

# 官方视觉的图注 / URL 常是英文（"Photo Retouching Transfer"、"Rip Current
# Segmentation"），只认中文词典会漏放跨语言串图。这里把英文别名映射回中文主题词，
# 使 topic_tokens 对中英两种归属文本给出同一套签名。别名一律取长串，避免 "sr"
# 这类短词在 "SR4IR" / "VSR" 里误命中。
_TOPIC_ALIASES: dict[str, tuple[str, ...]] = {
    "离岸流": ("rip current", "ripdetseg", "ripvis"),
    "分割": ("segmentation", "instance mask"),
    "修图": ("retouching", "photo retouching", "photo editing"),
    "迁移": ("retouching transfer", "style transfer", "color transfer"),
    "元学习": ("meta-learning", "meta learning"),
    "测试时优化": ("test-time optimization", "test time optimization"),
    "深伪": ("deepfake",),
    "质量评估": ("image quality assessment", "quality assessment"),
    "超分": ("super-resolution", "super resolution"),
    "去雾": ("dehazing", "night dehazing"),
    "去模糊": ("deblurring",),
    "低光": ("low-light", "low light"),
    "复原": ("image restoration",),
    "增强": ("image enhancement",),
    "重建": ("3d reconstruction",),
    "光场": ("light field",),
    "高光谱": ("hyperspectral",),
    "去噪": ("denoising",),
    "3d重建": ("3dgs", "gaussian splatting"),
    "hdr": ("high dynamic range",),
    "目标检测": ("object detection",),
    "风格迁移": ("style transfer",),
}


def topic_tokens(text: str) -> set[str]:
    """本期主题签名 = 英文专名式方法名 + 中文赛题 / 任务 / 主体词 + 英文别名归一。

    `check_pick_consistency` 用它替代纯英文 method_tokens，让三模块的中文标题
    与英文官方图注也能参与「跨篇串图」反证（2026-11-26 两篇 NTIRE 稿错配补丁）。
    """
    out = method_tokens(text or "")
    low = (text or "").lower()
    out |= {w for w in _TOPIC_WORDS if w in low}
    for canon, aliases in _TOPIC_ALIASES.items():
        if any(a in low for a in aliases):
            out.add(canon)
    return out


def _norm_cover_path(path: str | Path) -> str:
    """Windows / POSIX 路径归一：反斜杠统一、去前后空白，便于比较归属。"""
    return str(path or "").strip().replace("\\", "/").lower()


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
    # 三模块统一用「主题签名」：英文专名 + 中文赛题 / 任务 / 新闻主体。
    # 只认英文 method_tokens 时，「离岸流分割」「摄影修图迁移」这类中文赛题稿
    # 的封面张冠李戴完全检测不到（2026-11-26 事故），故这里换成 topic_tokens。
    title_topics = topic_tokens(ident["title"])
    if pick is None:
        # 品牌渐变底图：封面文字就是本期标题，天然 100% 一致
        return CoverCheck(True, "品牌底图（无 hero），封面文字即本期标题", ident)

    blob = _blob_of(pick)
    core_blob = " ".join(str(spec.get(k) or "") for k in ("title", "subtitle", "digest"))
    if featured_item is not None:
        core_blob += " " + str(getattr(featured_item, "title", "") or "")
    core_topics = {m.lower() for m in topic_tokens(core_blob)}
    # 归属文本也过主题签名：英文官方图注经 _TOPIC_ALIASES 归一成中文主题词，
    # 与中文标题做集合交集，跨语言串图（"Photo Retouching Transfer"）同样拦得住。
    blob_topics = {m.lower() for m in topic_tokens(blob)}
    hit_core = sorted(core_topics & blob_topics)

    # 1) 别篇主题词池（本期其他候选素材标题里的专名 / 赛题 / 主体，去掉本期自己）
    foreign: set[str] = set()
    for it in other_items or []:
        if featured_item is not None and it is featured_item:
            continue
        foreign |= {m.lower() for m in topic_tokens(str(getattr(it, "title", "") or ""))}
    foreign -= core_topics
    hit_foreign = sorted(foreign & blob_topics)

    if hit_foreign and not hit_core:
        return CoverCheck(
            False,
            f"跨篇串图：封面归属含别篇主题「{hit_foreign[0]}」，"
            f"与本期核心主题 {sorted(core_topics)[:6] or '（标题无缩写）'} 不一致，已拒绝",
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

    # 3) hero 图「自身归属文本」里的主题与成稿互斥 → 拒。
    #    注意不能用「主角原题 vs 成稿标题」判互斥：成稿标题常是编辑另起的中文名
    #    （ReDDiT / DTG-Restore、DeLang-SR / MUTE），两者本就不同名却完全同一篇，
    #    拿它当反证会误杀大批正常稿。真正的反证只能来自封面图自己带的标签
    #    （图注 / URL 明确写着别的方法名或赛题），rank() 的 order==0 才是归属凭证。
    hero_topics = blob_topics
    title_lower = {m.lower() for m in title_topics}
    if title_lower and hero_topics and not (title_lower & (hero_topics | set(hit_core))):
        return CoverCheck(
            False,
            f"封面图归属可疑：图注 / 来源里的主题 {sorted(hero_topics)} "
            f"与成稿核心主题 {sorted(title_topics)} 互斥，无法确认匹配度，按铁律不凑数",
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


# ---------------------------------------------------------------- 全模块硬闸门
def assert_cover_exclusive(cover_path: str | Path, issue_id: str,
                           owners: list[tuple[str, str, str]] | None = None) -> CoverCheck:
    """全模块硬约束：一个封面文件只能属于一篇稿（跨期串图直接拒）。

    `owners` 是 [(issue_id, title, cover_path)]，由 `db.cover_owners()` 提供；
    不传时只读本函数自带的空列表（单测用）。指纹命名只保护守卫落地后的新稿，
    存量共享封面（2026-11-26 的 `cover_contest_20261123.jpg` 被 3 篇共用）必须
    靠这道全库唯一性闸门在「推草稿」前拦住——无 sidecar 也照样拦得住。
    """
    key = _norm_cover_path(cover_path)
    if not key:
        return CoverCheck(True, "无封面路径（唯一性不适用）")
    peers = [(iid, title) for iid, title, path in (owners or [])
             if iid != issue_id and _norm_cover_path(path) == key]
    if peers:
        detail = "、".join(f"{iid}《{title[:22]}》" for iid, title in peers[:3])
        return CoverCheck(
            False,
            f"封面跨期串图：该封面还被 {len(peers)} 篇其它稿引用（{detail}），"
            "禁止复用；请重新生成带本期标题指纹的专属封面后再推草稿")
    return CoverCheck(True, "ok")


def verify_cover_asset(cover_path: str | Path, root: str | Path | None = None) -> CoverCheck:
    """无效链接拦截：封面文件必须存在、可解析、尺寸合规，否则微信端必裂图。"""
    raw = str(cover_path or "").strip()
    if not raw:
        return CoverCheck(False, "封面路径为空")
    p = Path(raw.replace("\\", "/"))
    candidates = [p] if p.is_absolute() else [p]
    if root is not None and not p.is_absolute():
        candidates.append(Path(root) / p)
    target = next((c for c in candidates if c.exists()), None)
    if target is None:
        return CoverCheck(False, f"封面文件不存在（无效链接）：{raw[:64]}")
    try:
        from PIL import Image
        with Image.open(target) as im:
            im.verify()
            w, h = im.size
    except Exception as exc:                                   # noqa: BLE001
        return CoverCheck(False, f"封面文件不可解析（损坏 / 非图片）：{type(exc).__name__}")
    if w < 400 or h < 160:
        return CoverCheck(False, f"封面尺寸过小（{w}x{h}），微信端会显示异常")
    return CoverCheck(True, "ok")


# ---------------------------------------------------------------- Prompt 条款
COVER_CONSISTENCY_RULE = """【封面图与正文 100% 强一致（最高优先级，违反判整篇不合格）】
适用模块：学术论文 / 业界进展 / 赛事竞赛，三模块一视同仁，任何一条都不得绕过。
- **主题与实体严格对齐**：封面 / 主视觉呈现的视觉主题、论文缩写与方法名称、赛题与
  任务方向、新闻主体，必须与本期成稿标题及正文核心内容高度一致。写 VideoGigaGAN
  就只能是 VideoGigaGAN 的架构图，写「离岸流分割」就只能是海岸 / 海洋视觉分割，
  写「摄影修图迁移」就只能是修图前后对比 / 色彩迁移，写「黄仁勋重返英伟达」就只能
  是英伟达 / 黄仁勋主题；严禁张冠李戴、严禁混入无关论文、无关企业、无关赛题的图片。
- **杜绝跨篇串图**：严禁复用其它论文、其它期、其它任务的封面图；一期多稿时每篇
  封面独立命名（带本期标题指纹）、独立归属 sidecar，禁止多篇共用一个图片文件。
- **断行与链接**：封面文字若涉及中文短句，必须断行合理、无标点落在行首、无错位、
  无溢出；封面文件必须真实存在且可正常加载，禁止无效链接、占位图与裂图。
- **生成前一致性检查**：选定封面后核对其归属标签（图注 / 来源 / 所属素材）是否与
  当前文章标题、核心方法、赛题或主体一致；若无法确认匹配度，必须重新检索 / 重新
  生成契合配图，绝不强行凑数——此时用只含本期标题的品牌底图封面，同样 100% 一致。
- **校验失败拦截**：系统会在封面落盘、全库唯一性扫描与推草稿前做多道归属复核，
  任何一道不通过即拒推，必须重新生成契合封面，不得带着不符图片出稿。
"""
