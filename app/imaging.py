"""图片格式归一：微信草稿箱只吃 jpg/png，webp/avif/gif 必须先转码。

实测约束（2026-10，本机真实调通）：
- `/cgi-bin/media/uploadimg` 仅接受 jpg/png；直接传 webp 会回
  `40005 invalid file type hint: [xxxx]`——方括号里那串是微信侧生成的随机标识，
  不是我们传的文件名，很容易被误读成「文件名有毒」；
- 正文图上限 1MB，封面素材上限 10MB。

所以在两处做归一：
1. 下载落盘时（sources/base.download_image）：webp/gif/avif 一律转 PNG，
   预览页、Markdown、推送三处看到的是同一份图；
2. 上传前（publish/wechat_api）：再兜一次，历史上已经存成 .webp 的旧稿
   照样能推，不必回头重生成。
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

# 微信接受的图片后缀（.jpeg 也认，但统一写成 .jpg 更省事）
WECHAT_EXTS = {".jpg", ".jpeg", ".png"}
CONTENT_IMAGE_MAX_BYTES = 1024 * 1024      # media/uploadimg：1MB
MATERIAL_IMAGE_MAX_BYTES = 10 * 1024 * 1024  # material/add_material：10MB

_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
_JPEG_QUALITIES = (88, 82, 76, 70, 62, 54)
_SCALES = (1.0, 0.85, 0.72, 0.6, 0.5)


def is_wechat_image(name: str | Path) -> bool:
    return Path(name).suffix.lower() in WECHAT_EXTS


def mime_of(ext: str) -> str:
    return _MIME.get(ext.lower(), "image/jpeg")


def _encode(im: Image.Image, ext: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if ext == ".png":
        im.save(buf, format="PNG", optimize=True)
    else:
        im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True,
                               progressive=True)
    return buf.getvalue()


def normalize(data: bytes, *, max_bytes: int, filename: str = "image") -> tuple[bytes, str, str]:
    """把任意位图转成微信可接受的 (bytes, 后缀, mime)。

    - 格式：有 alpha 走 PNG（保留透明），否则走 JPEG（照片体积小一个量级）；
    - 体积：先降质量，再等比缩边，直到塞进 max_bytes；
    - 已经是 jpg/png 且不超限的，原样返回，不做二次压缩（避免一压再压糊掉）。
    """
    ext = Path(filename).suffix.lower()
    if ext in WECHAT_EXTS and len(data) <= max_bytes:
        return data, (".jpg" if ext == ".jpeg" else ext), mime_of(ext)

    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as exc:  # 损坏的图 / svg 之类
        raise ValueError(f"无法解码为位图（{type(exc).__name__}）") from exc

    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    try:
        for scale in _SCALES:
            frame = im
            if scale < 1.0:
                size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
                frame = im.resize(size, Image.LANCZOS)
            if has_alpha:
                out = _encode(frame, ".png", 0)
                if len(out) <= max_bytes:
                    return out, ".png", _MIME[".png"]
            for quality in _JPEG_QUALITIES:
                out = _encode(frame, ".jpg", quality)
                if len(out) <= max_bytes:
                    return out, ".jpg", _MIME[".jpg"]
        # 兜底：最后一档质量的结果即便略超也返回，让微信去判（比直接失败可读）
        return _encode(im, ".jpg", _JPEG_QUALITIES[-1]), ".jpg", _MIME[".jpg"]
    finally:
        im.close()


def normalize_file(path: str | Path, *, max_bytes: int) -> tuple[bytes, str, str]:
    """读文件并归一，返回 (bytes, 上传用文件名, mime)。"""
    path = Path(path)
    data = path.read_bytes()
    out, ext, mime = normalize(data, max_bytes=max_bytes, filename=path.name)
    name = f"{path.stem}{ext}"
    return out, name, mime
