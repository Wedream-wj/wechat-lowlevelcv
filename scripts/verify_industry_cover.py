"""业界封面策略实跑验证：以「Momenta 姐妹公司 700TOPS 芯片」快讯为样本。

跑法：python scripts/verify_industry_cover.py
验证链路：
1. 量子位首页 → 抓到 Momenta 芯片条目（列表缩略图很小）；
2. enrich 回抓正文 → 正文配图进 cover_candidates；
3. cover_select.pick_cover 按新优先级选图（产品/发布会现场 > 主视觉 > logo）；
4. make_cover 渲染 2.35:1 封面（含语义断行）；
5. 无图场景 → 高阶科技感保底底图。
产物：out/_industry_cover_*.png + out/cover_industry.json
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.sources import chinese_media
from app.sources.base import HttpClient
from app.render import cover as cover_mod
from app.render import cover_select

TITLE = "Momenta姐妹公司首秀：700TOPS芯片Day 0跑通世界模型"
SUBTITLE = "世界模型首次指导设计芯片，99% 算力可用"


async def main() -> int:
    async with HttpClient() as client:
        print("== 1. 抓量子位首页 ==")
        html = await client.text("https://www.qbitai.com/")
        items = chinese_media.parse_qbitai(html)
        hits = [i for i in items if "700TOPS" in i.title or "世界模型" in i.title and "芯片" in i.title]
        if not hits:
            hits = items[:1]
        lead = hits[0]
        print(f"   条目：{lead.title}")
        print(f"   链接：{lead.url}")
        print(f"   列表缩略图：{lead.image_url[:70]}")

        print("== 2. enrich 回抓正文与配图 ==")
        n = await chinese_media.enrich(client, [lead])
        cands = lead.extra.get("cover_candidates") or []
        print(f"   正文抽取 {len(lead.extra.get('full_text') or '')} 字，"
              f"配图候选 {len(cands)} 张")
        for i, fig in enumerate(cands[:4]):
            print(f"   [{i}] {fig['url'][:80]}")

        print("== 3. 选图（新优先级）==")
        pick = await cover_select.pick_cover(client, [lead], "industry")
        if pick:
            print(f"   ✔ {pick.describe()}")
            for note in pick.notes:
                print(f"   · {note}")
        else:
            print("   ✘ 无可用图（不应发生：救场层存在）")

        print("== 4. 渲染封面（语义断行）==")
        spec = {
            "title": TITLE,
            "subtitle": SUBTITLE,
            "digest": "世界模型首次指导设计芯片，Day 0 跑通端到端流程。",
            "lead": [
                "当同行还在为「设计一颗 NPU 需要一个百人团队加两年工期」头疼时，"
                "这家 Momenta 姐妹公司把世界模型塞进了设计闭环：Day 0 流片当天就跑通了端到端推理。",
                "700TOPS 超大核 NPU 的账面算力并不新鲜——新鲜的是官方敢给出「99% 算力可用」这个反常识的数字，"
                "而这背后的方法，是把芯片设计本身交给了一个世界模型。",
            ],
            "sections": [
                {"type": "meta_card", "label": "INDUSTRY SNAPSHOT", "rows": [
                    ["事件", "Momenta 姐妹公司发布 700TOPS 超大核 NPU，世界模型 Day 0 跑通"],
                    ["时间", "2026-10-31 发布"],
                    ["影响面", "自动驾驶与端侧推理的算力选型"],
                ]},
                {"type": "h2", "text": "01 | 发生了什么"},
                {"type": "p", "text":
                    "这家公司发布了超大核 NPU 芯片，标称算力 700TOPS，并宣布在设计阶段引入世界模型："
                    "芯片流片当天（Day 0）即完成端到端推理链路验证，官方称可用算力占比达 99%。"},
                {"type": "h2", "text": "02 | 技术上看，真正的变化在哪"},
                {"type": "p", "text":
                    "传统芯片设计的验证周期以月计，设计-流片-驱动的串行流程把「架构假设」与「真实负载」隔得很远；"
                    "用世界模型在流片前模拟真实负载，等于把验证前置到了设计闭环内部，"
                    "99% 可用算力对应的是「设计假设与真实负载对齐」的程度。"},
                {"type": "h2", "text": "03 | 写在最后"},
                {"type": "p", "text":
                    "这条动态真正值得记录的，不是 700TOPS 这个数字本身，而是「设计闭环」的思路："
                    "当算力的增长开始放缓，把验证与设计之间的时滞压掉，就是另一种形式的性能释放。"},
            ],
            "takeaway": "看懂「99% 可用算力」的口径，比记住 700TOPS 更重要。",
            "sources": [{"name": lead.title, "url": lead.url}],
        }
        if pick:
            path = cover_mod.make_cover(spec, "industry", "2026.11.01",
                                        hero=pick.path, hero_fit=pick.fit)
            print(f"   ✔ 实景封面：{path}")
        # 保底底图（同一标题）
        fb = cover_mod.make_cover(spec, "industry", "2026.11.01",
                                  out_path=settings.out_dir / "_industry_cover_fallback.png")
        print(f"   ✔ 科技感保底底图：{fb}")

        print("== 5. 生成完整预览页 ==")
        from app.render import wechat
        from app.render.markdown import spec_to_markdown
        from app.render.preview import render_preview
        figures = {}
        index = 0
        for fig in cands[:2]:
            local = await client.download_image(fig["url"], referer=lead.url)
            if not local:
                continue
            index += 1
            rel = Path(local).name
            figures[f"F{index}"] = {
                "path": f"assets/{rel}", "url": fig["url"],
                "caption": fig.get("caption") or "发布会现场（来源：量子位）",
                "title": lead.title, "item_id": "industry:momenta",
            }
        spec["sections"] = spec["sections"][:2] + [
            {"type": "figure", "ref": "F1", "caption": "发布会现场（来源：量子位）"},
        ] + spec["sections"][2:]
        body = wechat.render_body(spec, figures, "industry", "2026.11.01")
        markdown = spec_to_markdown(spec, figures, "industry")
        from app import db  # noqa: F401  (preview 只需 out 目录)
        cover_hero_rel = (f"assets/{Path(pick.path).name}" if pick
                          else f"assets/{Path(fb).name}")
        preview = render_preview(
            spec, body, markdown, "industry",
            cover_hero_rel,
            "demo-industry-momenta", "2026.11.01", "业界观察")
        out_html = settings.out_dir / "preview_demo-industry-momenta.html"
        out_html.write_text(preview, encoding="utf-8")
        (settings.out_dir / "demo-industry-momenta.md").write_text(markdown, encoding="utf-8")
        (settings.out_dir / "demo-industry-momenta.html").write_text(body, encoding="utf-8")
        print(f"   ✔ 预览页：{out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
