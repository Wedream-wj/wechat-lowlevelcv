"""赛事雷达源与单场精读的规则测试。

覆盖三类容易回归的点：
1. 四个新源的解析（雷达 JSON / ML Contests 属性 / Codabench DRF 页 / FlyAI 静态镜像）；
2. 质量门槛：课堂练习赛（奖金 Kudos、队伍个位数）不能进精读候选；
3. 报名状态判定：只看到「决赛在未来」不算还能报名。
"""
import json
import unittest
from datetime import datetime, timedelta

from app.filters import scoring
from app.models import Item
from app.sources import competitions as comp
from app.sources import contest_feeds as cf
from app.sources.headless import looks_like_honeypot


def _radar(count=2) -> str:
    rows = [
        {
            "id": "a1",
            "name": "2026 图像复原挑战赛",
            "organizer": "某实验室",
            "official_url": "https://example.com/a1",
            "type": "数据算法赛",
            "tags": ["图像复原", "超分辨率"],
            "city": "线上",
            "prize": "¥ 50,000",
            "start": "2026-10-01",
            "deadline": "2026-12-01",
            "end": "2026-12-15",
            "signup": "open",
            "description": "面向真实退化的图像复原赛题。",
            "detail": "# 一、赛事背景\n老照片退化复杂。\n# 二、赛事任务\n输入退化图，输出复原图。",
            "featured": True,
        },
        {
            "id": "a2",
            "name": "已结束的比赛",
            "organizer": "某公司",
            "official_url": "https://example.com/a2",
            "signup": "closed",
            "deadline": "2026-12-01",
        },
        {
            "id": "a3",
            "name": "只有结束时间的比赛",
            "organizer": "某公司",
            "official_url": "https://example.com/a3",
            "signup": "open",
            "deadline": None,
            "end": "2099-01-01",
            "description": "desc",
        },
    ][:count]
    return "window.__DATA__ = " + json.dumps({"competitions": rows}, ensure_ascii=False) + ";"


class TestRadar(unittest.TestCase):
    def test_parse_keeps_open_and_maps_fields(self):
        items = cf.parse_radar(_radar(3))
        names = [i.title for i in items]
        self.assertIn("2026 图像复原挑战赛", names)
        self.assertNotIn("已结束的比赛", names)  # signup=closed 直接丢
        self.assertIn("只有结束时间的比赛", names)  # deadline 缺失时回退到 end
        first = next(i for i in items if i.title == "2026 图像复原挑战赛")
        self.assertEqual(first.extra["platform"], "某实验室")
        self.assertEqual(first.extra["prize"], "¥ 50,000")
        # 官方赛题正文要原样带进素材，这是精读的事实底座
        self.assertIn("老照片退化复杂", first.extra["contest_detail"]["official_brief"])
        # 有正文 + featured + 真实奖金的条目分数应高于裸条目
        bare = next(i for i in items if i.title == "只有结束时间的比赛")
        self.assertGreater(first.score, bare.score)

    def test_garbage_input_is_safe(self):
        self.assertEqual(cf.parse_radar("not json"), [])
        self.assertEqual(cf.parse_radar(""), [])


class TestMlContests(unittest.TestCase):
    HTML = (
        '<html><body><div id="competitions" data-competitions="'
        '[{&quot;name&quot;:&quot;LLM Inference Sprint&quot;,'
        '&quot;url&quot;:&quot;https://example.com/mlc&quot;,'
        '&quot;tags&quot;:[&quot;llm&quot;],'
        '&quot;registration-deadline&quot;:&quot;15 Nov __FUTURE__&quot;,'
        '&quot;deadline&quot;:&quot;15 Nov __FUTURE__&quot;,'
        '&quot;prize&quot;:&quot;$10,000&quot;,'
        '&quot;platform&quot;:&quot;Independent&quot;},'
        '{&quot;name&quot;:&quot;Old One&quot;,'
        '&quot;url&quot;:&quot;https://example.com/old&quot;,'
        '&quot;deadline&quot;:&quot;01 Jan 2020&quot;}]'
        '"></div></body></html>'
    )

    def test_parse_and_drop_expired(self):
        # 夹具里全是 &quot;，不能用 str.format（会把它们当占位符），改用 token 替换
        future = (datetime.now() + timedelta(days=40)).year
        items = cf.parse_mlcontests(self.HTML.replace("__FUTURE__", str(future)))
        self.assertEqual([i.title for i in items], ["LLM Inference Sprint"])
        self.assertIn(str(future), items[0].extra["deadline"])
        self.assertEqual(items[0].extra["prize"], "$10,000")


class TestCodabench(unittest.TestCase):
    def _drf(self, rows):
        payload = json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False, indent=2)
        return (
            "<html><body><pre class='prettyprint'>GET /api/competitions/</pre>"
            f"<pre class='prettyprint'>HTTP 200 OK\nContent-Type: application/json\n\n{payload}</pre>"
            "</body></html>"
        )

    def _row(self, **kw):
        base = {
            "id": 1, "title": "RoboWorld 2026 Track 3", "created_by": "x",
            "owner_display_name": "x", "created_when": datetime.now().isoformat(),
            "first_phase_start": datetime.now().isoformat(), "published": True,
            "participants_count": 12, "description": "VLA driving tasks", "reward": "",
        }
        base.update(kw)
        return base

    def test_extract_json_from_drf_page(self):
        payload = cf.extract_drf_json(self._drf([self._row()]))
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["count"], 1)

    def test_spam_and_stale_dropped(self):
        rows = [
            self._row(id=1, title="Best PayID Casinos Australia"),
            self._row(id=2, title="Old Bench", first_phase_start="2021-01-01T00:00:00Z"),
            self._row(id=3, title="Legit Challenge"),
        ]
        items = cf.parse_codabench(cf.extract_drf_json(self._drf(rows)))
        self.assertEqual([i.title for i in items], ["Legit Challenge"])
        self.assertTrue(items[0].extra["needs_verify"])  # 列表不给截止时间，要二次核验

    def test_honeypot_detection(self):
        self.assertTrue(looks_like_honeypot("<title>Air. \"Lighter than air,\" said.</title>"))
        self.assertTrue(looks_like_honeypot('<a href="/http:/iocaine:42069/x/">x</a>'))
        self.assertFalse(looks_like_honeypot("<html><h1>Codabench</h1></html>"))
        self.assertTrue(looks_like_honeypot(""))


class TestFlyAI(unittest.TestCase):
    HTML = """
    <html><body>
      <a href="https://www.flyai.com/d/Texture-Classification">
        <div class="compatibleIE">
          <div class="pro_title"><div class="name"><h2>图像纹理分类</h2><p class="noReg">可报名</p></div>
          <div class="bonus"><p>奖金池 ¥ 2,000</p></div></div>
          <div class="describe">47 类纹理图像分类，五千多张图。</div>
          <div class="pro_tip"><a class="level">简单</a><a class="level">视觉计算</a></div>
        </div>
      </a>
    </body></html>
    """

    def test_parse_evergreen(self):
        items = cf.parse_flyai(self.HTML)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.title, "图像纹理分类")
        self.assertEqual(item.extra["prize"], "奖金池 ¥ 2,000")
        self.assertTrue(item.extra["evergreen"])
        self.assertEqual(item.url, "https://www.flyai.com/d/Texture-Classification")


class TestQualityGate(unittest.TestCase):
    def _item(self, prize, teams):
        return Item(module="contest", title="X", url="https://e.com", source="Kaggle",
                    summary="s", extra={"prize": prize, "team_count": teams,
                                        "deadline": (datetime.now() + timedelta(days=30)).isoformat()})

    def test_practice_contests_dropped(self):
        kept = comp.filter_active([self._item("Kudos", 26), self._item("Kudos", 300)])
        self.assertEqual(len(kept), 1)  # 队伍多的练习赛仍保留
        self.assertEqual(kept[0].extra["team_count"], 300)

    def test_monetary_prize_kept(self):
        kept = comp.filter_active([self._item("¥ 280,000", 1854)])
        self.assertEqual(len(kept), 1)


class TestRegistrationClosed(unittest.TestCase):
    def test_state_words(self):
        self.assertTrue(comp.registration_closed("比赛时间 2026.09 ~ 2026.11 报名已结束 赛题与数据"))
        self.assertFalse(comp.registration_closed("报名截止 2099-01-01 23:59:59"))

    def test_past_registration_beats_future_final(self):
        # 决赛在未来、报名已过：仍判为关闭（实测天池踩过的坑）
        text = "报名截止 2026-01-05 23:59:59 决赛 2099-11-16"
        self.assertTrue(comp.registration_closed(text, now=datetime(2026, 10, 28)))

    def test_multiple_deadlines_take_earliest(self):
        text = "报名截止 2099-10-26 23:59:59 决赛报名截止 2099-11-16"
        self.assertFalse(comp.registration_closed(text, now=datetime(2026, 10, 28)))

    def test_countdown_beats_qualified_phase(self):
        # 实测天池：页面同时写「2026-05-31 报名与资格赛结束」和「报名剩 03 日 14 时」，
        # 前者是资格赛阶段，后者才是报名口状态。倒计时存在时不能判成报名已结束
        text = ("2026-01-28 报名与资格赛开始 2026-05-31 报名与资格赛结束 "
                "2026-06-01 排名赛开始 立即报名 报名剩 03 日 14 时")
        closed, hint = comp.registration_status(text, now=datetime(2026, 10, 28))
        self.assertFalse(closed)
        self.assertIn("报名剩", hint)

    def test_closed_page_has_no_hint(self):
        closed, hint = comp.registration_status("报名已结束 报名截止 2026-01-05",
                                               now=datetime(2026, 10, 28))
        self.assertTrue(closed)
        self.assertEqual(hint, "")


class TestSelectionSingleContest(unittest.TestCase):
    def test_top_n_limits_to_candidates(self):
        from app.config import topics

        items = [
            Item(module="contest", title=f"赛事 {i}", url=f"https://e.com/{i}",
                 source="天池", summary="desc", score=10 + i,
                 extra={"deadline": (datetime.now() + timedelta(days=20)).isoformat(),
                        "prize": "¥ 10,000"})
            for i in range(5)
        ]
        # max_per_category 默认 2，这里给不同 category 才能取满
        for idx, item in enumerate(items):
            item.category = f"方向{idx}"
        picked = scoring.select(items, topics, "contest", top_n=5)
        self.assertEqual(len(picked), 5)


if __name__ == "__main__":
    unittest.main()
