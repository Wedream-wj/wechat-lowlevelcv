"""图片格式归一：webp → jpg/png，体积压到微信上限以内。

对应线上事故：业界稿正文引用量子位的 webp 配图，预览一切正常，
点「推草稿」时 `/cgi-bin/media/uploadimg` 回 40005 invalid file type hint。
"""
import io
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app.imaging import (
    CONTENT_IMAGE_MAX_BYTES,
    is_wechat_image,
    normalize,
    normalize_file,
)
from app.publish.wechat_api import ERRCODE_HINTS, WeChatClient


def _img_bytes(fmt: str, size=(64, 48), color=(30, 120, 200), alpha=False) -> bytes:
    mode = "RGBA" if alpha else "RGB"
    im = Image.new(mode, size, color + ((255,) if alpha else ()))
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


class TestNormalize(unittest.TestCase):
    def test_webp_becomes_png_or_jpeg(self):
        data, ext, mime = normalize(_img_bytes("WEBP"), max_bytes=CONTENT_IMAGE_MAX_BYTES,
                                    filename="ca8835.webp")
        self.assertIn(ext, (".png", ".jpg"))
        self.assertEqual(mime, "image/png" if ext == ".png" else "image/jpeg")
        self.assertLessEqual(len(data), CONTENT_IMAGE_MAX_BYTES)
        # 转出来必须真能被解码，且还是 64x48
        im = Image.open(io.BytesIO(data))
        self.assertEqual(im.size, (64, 48))

    def test_webp_with_alpha_keeps_png(self):
        # 必须有真实的半透明像素：全不透明的 RGBA 存成 WEBP 时 alpha 会被丢掉，
        # 加载回来就是 RGB，测不到「保留透明」这条分支
        im = Image.new("RGB", (64, 48), (30, 120, 200))
        im.putalpha(Image.linear_gradient("L").resize((64, 48)))
        buf = io.BytesIO()
        im.save(buf, format="WEBP")
        _, ext, _ = normalize(buf.getvalue(), max_bytes=CONTENT_IMAGE_MAX_BYTES,
                              filename="a.webp")
        self.assertEqual(ext, ".png")

    def test_jpeg_passthrough_when_small(self):
        raw = _img_bytes("JPEG")
        data, ext, mime = normalize(raw, max_bytes=CONTENT_IMAGE_MAX_BYTES, filename="a.jpg")
        self.assertIs(data, raw)  # 不二次压缩，避免越压越糊
        self.assertEqual((ext, mime), (".jpg", "image/jpeg"))

    def test_png_passthrough_when_small(self):
        raw = _img_bytes("PNG")
        data, ext, _ = normalize(raw, max_bytes=CONTENT_IMAGE_MAX_BYTES, filename="a.png")
        self.assertEqual((data, ext), (raw, ".png"))

    def test_oversize_is_compressed_under_limit(self):
        # 2000x2000 噪声图：PNG 会远超 1MB
        import os

        im = Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        raw = buf.getvalue()
        self.assertGreater(len(raw), CONTENT_IMAGE_MAX_BYTES)
        data, ext, _ = normalize(raw, max_bytes=CONTENT_IMAGE_MAX_BYTES, filename="big.png")
        self.assertLessEqual(len(data), CONTENT_IMAGE_MAX_BYTES)
        self.assertIn(ext, (".jpg", ".png"))

    def test_gif_becomes_static_image(self):
        _, ext, _ = normalize(_img_bytes("GIF"), max_bytes=CONTENT_IMAGE_MAX_BYTES,
                              filename="a.gif")
        self.assertIn(ext, (".png", ".jpg"))

    def test_garbage_raises_value_error(self):
        with self.assertRaises(ValueError):
            normalize(b"not an image", max_bytes=CONTENT_IMAGE_MAX_BYTES, filename="a.webp")

    def test_normalize_file_renames_extension(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "abc.webp"
            p.write_bytes(_img_bytes("WEBP"))
            data, name, mime = normalize_file(p, max_bytes=CONTENT_IMAGE_MAX_BYTES)
            self.assertTrue(name.startswith("abc."))
            self.assertNotEqual(Path(name).suffix, ".webp")
            self.assertTrue(mime.startswith("image/"))

    def test_is_wechat_image(self):
        self.assertTrue(is_wechat_image("a.PNG"))
        self.assertTrue(is_wechat_image("a.jpeg"))
        self.assertFalse(is_wechat_image("a.webp"))


class TestUploadPath(unittest.TestCase):
    """上传前必须换掉不合法格式，且用换过的文件名 + mime 发给微信。"""

    def _run(self, file_path: Path):
        calls = []

        async def fake_post(path, *, params=None, json_body=None, files=None, force_token=False):
            calls.append((path, files))
            return {"url": "http://mmbiz.qpic.cn/ok"} if "uploadimg" in path else {"media_id": "m1"}

        async def main():
            client = WeChatClient()
            client._post = fake_post  # type: ignore[assignment]
            url = await client.upload_content_image(file_path)
            await client._client.aclose()
            return url

        import asyncio

        url = asyncio.run(main())
        return url, calls

    def test_webp_uploaded_as_png(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "pic.webp"
            p.write_bytes(_img_bytes("WEBP"))
            url, calls = self._run(p)
        self.assertTrue(url.endswith("/ok"))
        path, files = calls[0]
        name, data, mime = files["media"]
        self.assertNotEqual(Path(name).suffix, ".webp")
        self.assertIn(mime, ("image/png", "image/jpeg"))
        self.assertTrue(data)

    def test_40005_has_actionable_hint(self):
        self.assertIn(40005, ERRCODE_HINTS)
        self.assertIn("jpg/png", ERRCODE_HINTS[40005])


if __name__ == "__main__":
    unittest.main()
