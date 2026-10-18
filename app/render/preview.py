"""生成独立的预览页：手机画框 + 一键复制到公众号后台。

这个页面只用于本地预览/审稿，不会被推送到微信。
复制按钮把文章 body 的富文本写入剪贴板，粘到公众号编辑器即可保留样式。
"""
from __future__ import annotations

import html as _html
import json

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title} · 预览</title>
<style>
  :root {{ --ink:#1B1D22; --sub:#6B7280; --line:#E6E8EE; --primary:{primary}; }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:#F3F4F7; color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:28px 24px 60px; }}
  .top {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }}
  .brand {{ display:flex; align-items:center; gap:10px; font-weight:700; font-size:15px; }}
  .dot {{ width:10px; height:10px; border-radius:50%; background:var(--primary); }}
  .meta {{ font-size:12px; color:var(--sub); }}
  .layout {{ display:grid; grid-template-columns:minmax(320px,390px) 1fr; gap:26px; align-items:start; }}
  .phone {{
    background:#fff; border-radius:32px; padding:12px; box-shadow:0 18px 50px rgba(20,30,60,.12);
    position:sticky; top:20px;
  }}
  .screen {{ background:#fff; border-radius:24px; overflow:hidden; }}
  .statusbar {{ height:34px; display:flex; align-items:center; justify-content:space-between;
    padding:0 18px; font-size:11px; color:#333; background:#fff; }}
  .article {{ padding:18px 18px 26px; max-height:78vh; overflow-y:auto; }}
  .article h1 {{ font-size:21px; line-height:1.45; margin:4px 0 10px; font-weight:700; }}
  .byline {{ font-size:12px; color:#9AA0AE; margin-bottom:18px; }}
  .cover {{ width:100%; border-radius:8px; display:block; margin-bottom:20px; }}
  .panel {{ background:#fff; border-radius:14px; padding:18px 20px; box-shadow:0 6px 24px rgba(20,30,60,.06); }}
  .panel h2 {{ font-size:14px; margin:0 0 14px; letter-spacing:.5px; }}
  .row {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:14px; }}
  button {{
    border:1px solid var(--line); background:#fff; color:#2B2E36; border-radius:8px;
    padding:9px 14px; font-size:13px; cursor:pointer; transition:.15s;
  }}
  button.primary {{ background:var(--primary); border-color:var(--primary); color:#fff; }}
  button:hover {{ transform:translateY(-1px); }}
  .kv {{ display:grid; grid-template-columns:84px 1fr; gap:8px 10px; font-size:12.5px; color:#3A3F4B; }}
  .kv div:nth-child(odd) {{ color:#9AA0AE; }}
  pre {{
    background:#F7F8FA; border:1px solid var(--line); border-radius:8px; padding:12px;
    font-size:12px; max-height:320px; overflow:auto; white-space:pre-wrap; word-break:break-all;
  }}
  .hint {{ font-size:12px; color:var(--sub); line-height:1.8; }}
  .tag {{ display:inline-block; padding:2px 8px; border-radius:4px; background:#EEF2FF;
    color:var(--primary); font-size:11px; margin-right:6px; }}
  @media (max-width:900px) {{ .layout {{ grid-template-columns:1fr; }} .phone {{ position:static; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand"><span class="dot"></span>底层视觉论文笔记 · 出稿预览</div>
    <div class="meta">{module_label} / {date_str} / {issue_id}</div>
  </div>
  <div class="layout">
    <div class="phone">
      <div class="screen">
        <div class="statusbar"><span>9:41</span><span>●●● ▮</span></div>
        <div class="article" id="article">
          <img class="cover" src="{cover}" alt="cover" />
          <h1>{title}</h1>
          <div class="byline">底层视觉论文笔记 · {date_str}</div>
          {body}
        </div>
      </div>
    </div>
    <div>
      <div class="panel">
        <h2>审稿信息</h2>
        <div class="kv">
          <div>标题</div><div>{title}</div>
          <div>副标题</div><div>{subtitle}</div>
          <div>摘要</div><div>{digest}</div>
          <div>模块</div><div><span class="tag">{module_label}</span></div>
          <div>字数</div><div>{word_count} 字</div>
          <div>素材</div><div>{source_count} 条</div>
        </div>
        <div class="row" style="margin-top:18px;">
          <button class="primary" onclick="copyRich()">复制富文本到公众号</button>
          <button onclick="copyHtml()">复制 HTML 源码</button>
          <button onclick="toggleSource()">查看源码</button>
        </div>
        <p class="hint">
          复制后到公众号后台正文区直接粘贴，内联样式会保留。<br />
          图片需先经 <code>/api/issues/{issue_id}/push</code> 上传到微信素材库，本地预览里的图片在粘贴后需要重新上传。
        </p>
      </div>
      <div class="panel" style="margin-top:18px;display:none;" id="sourcePanel">
        <h2>HTML 源码（body）</h2>
        <pre id="source">{source_escaped}</pre>
      </div>
    </div>
  </div>
</div>
<script>
const BODY_HTML = {body_json};
async function copyRich() {{
  try {{
    const blob = new Blob([BODY_HTML], {{type: "text/html"}});
    await navigator.clipboard.write([new ClipboardItem({{"text/html": blob, "text/plain": new Blob([BODY_HTML], {{type:"text/plain"}})}})]);
    flash("已复制富文本，去公众号编辑器粘贴吧");
  }} catch (e) {{
    const div = document.createElement("div");
    div.innerHTML = BODY_HTML; div.contentEditable = "true";
    div.style.position = "fixed"; div.style.left = "-9999px";
    document.body.appendChild(div);
    const range = document.createRange(); range.selectNodeContents(div);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
    document.execCommand("copy"); document.body.removeChild(div);
    flash("已复制（兼容模式）");
  }}
}}
function copyHtml() {{
  navigator.clipboard.writeText(BODY_HTML).then(() => flash("HTML 源码已复制"));
}}
function toggleSource() {{
  const p = document.getElementById("sourcePanel");
  p.style.display = p.style.display === "none" ? "block" : "none";
}}
function flash(msg) {{
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = "position:fixed;left:50%;bottom:30px;transform:translateX(-50%);background:#151A24;color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;z-index:99";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 1800);
}}
</script>
</body>
</html>
"""


def render_preview(spec: dict, body_html: str, module: str, cover: str, issue_id: str,
                   date_str: str, module_label: str) -> str:
    text = "".join(str(x) for x in (spec.get("lead") or []))
    for block in spec.get("sections", []):
        text += str(block.get("text") or "")
        text += "".join(str(i) for i in block.get("items") or [])
    word_count = len("".join(text.split()))
    return TEMPLATE.format(
        title=_html.escape(spec.get("title", "")),
        subtitle=_html.escape(spec.get("subtitle", "")),
        digest=_html.escape(spec.get("digest", "")),
        module_label=module_label,
        date_str=date_str,
        issue_id=issue_id,
        cover=cover,
        body=body_html,
        word_count=word_count,
        source_count=len(spec.get("sources") or []),
        primary="#2A4BD7",
        source_escaped=_html.escape(body_html),
        body_json=json.dumps(body_html, ensure_ascii=False),
    )
