"""Offline tests: a scripted fake model drives the pipeline end to end.

Run: python -m unittest test_pipeline -v
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["HOTSPOT_DB"] = str(Path(tempfile.mkdtemp()) / "test.sqlite3")
for key in ("ANTHROPIC_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
            "MOONSHOT_API_KEY", "ARK_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(key, None)

import game_sources
import games
import llm
import pipeline
import serve
import trends

ITEM = {"id": "dy-7709413956435570146", "title": "共鸣之战最后一天，小猫咪冲啊", "text": "", "tags": ["王者荣耀", "共鸣之战"],
        "publisher": "鬼岛", "published": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "views": 3956761, "likes": 133316,
        "url": "https://www.douyin.com/video/7709413956435570146", "game": "wzry"}


def segment(start, end, part, voice="来看这波操作"):
    return {"start": start, "end": end, "part": part, "visual": "自录对局", "voiceover": voice, "caption": "字幕"}


GOOD = {"name": "A · 名场面", "angle": "回顾", "seconds": 40, "tags": ["王者荣耀", "共鸣之战"],
        "segments": [segment(0, 5, "开场"), segment(5, 32, "实录"), segment(32, 40, "结尾")]}


class FakeModel:
    name = "fake"

    def __init__(self, verify, score=85, scripts=None, web_search=True):
        self.web_search = web_search
        self.answers = {"trust": verify, "score": {"score": score, "reason": "受众重合", "angle": "名场面回顾", "risks": [],
                                                   "breakdown": {"why_hot": "限时玩法告别", "format": "猫咪英雄 + 告别式字幕",
                                                                 "borrow": ["最后一次的告别句式"], "lifespan": "1–3 天"}},
                        "scripts": {"scripts": scripts if scripts is not None else [GOOD, {**GOOD, "name": "B"}]}}
        self.calls = []
        self.prompts = []
        self.systems = []

    def research(self, system, prompt, max_searches=4):
        self.calls.append("research")
        return "核验笔记"

    def json(self, system, prompt, schema):
        key = next(k for k in ("trust", "score", "scripts") if k in schema["properties"])
        self.calls.append(key)
        self.prompts.append(prompt)
        self.systems.append(system)
        return self.answers[key]


class RuleTests(unittest.TestCase):
    def test_good_script_passes(self):
        self.assertEqual(pipeline.script_errors(GOOD), [])

    def test_script_rules(self):
        cases = {
            "时长": {**GOOD, "seconds": 70},
            "首尾相接": {**GOOD, "segments": [segment(0, 5, "开场"), segment(6, 32, "实录"), segment(32, 40, "结尾")]},
            "开场钩子": {**GOOD, "segments": [segment(0, 8, "开场"), segment(8, 32, "实录"), segment(32, 40, "结尾")]},
            "开场 → 实录 → 结尾": {**GOOD, "segments": [segment(0, 5, "开场"), segment(5, 40, "结尾")]},
            "话题标签": {**GOOD, "tags": ["共鸣之战"]},
            "限制词": {**GOOD, "segments": [segment(0, 5, "开场", "代练包赢"), segment(5, 32, "实录"), segment(32, 40, "结尾")]},
            "口播": {**GOOD, "segments": [segment(0, 5, "开场", "字" * 300), segment(5, 32, "实录"), segment(32, 40, "结尾")]},
        }
        for expected, script in cases.items():
            with self.subTest(expected):
                self.assertTrue(any(expected in e for e in pipeline.script_errors(script)), pipeline.script_errors(script))

    def test_verified_claim_without_link_is_downgraded(self):
        result = pipeline.verify_rules({"trust": "可信", "reason": "", "sources": [],
                                        "claims": [{"text": "11 月 15 日结束", "status": "已证实", "source_url": ""}]}, True)
        self.assertEqual(result["trust"], "存疑")
        self.assertEqual(result["claims"][0]["status"], "未证实")

    def test_contradiction_means_false(self):
        result = pipeline.verify_rules({"trust": "存疑", "reason": "", "sources": [],
                                        "claims": [{"text": "全服免费送皮肤", "status": "与来源矛盾", "source_url": "https://pvp.qq.com/"}]}, True)
        self.assertEqual(result["trust"], "不实")

    def test_false_without_evidence_goes_to_human(self):
        self.assertEqual(pipeline.verify_rules({"trust": "不实", "reason": "", "claims": [], "sources": []}, True)["trust"], "存疑")

    def test_no_web_search_cannot_confirm(self):
        result = pipeline.verify_rules({"trust": "可信", "reason": "", "sources": [],
                                        "claims": [{"text": "x", "status": "已证实", "source_url": "https://a.cn"}]}, False)
        self.assertEqual(result["trust"], "存疑")

    def test_parse_json_tolerates_fences(self):
        self.assertEqual(llm.parse_json('结果如下：\n```json\n{"a": 1}\n```'), {"a": 1})


class FlowTests(unittest.TestCase):
    def test_credible_and_fitting_item_gets_two_checked_scripts(self):
        model = FakeModel({"trust": "可信", "reason": "玩家创作，无事实说法", "claims": [], "sources": []})
        result = pipeline.analyze(model, ITEM, [ITEM])
        self.assertTrue(result["match"]["eligible"])
        self.assertEqual(len(result["scripts"]), 2)
        self.assertTrue(all(s["checks"] == [] for s in result["scripts"]))
        self.assertIn("00–05 秒｜开场", result["scripts"][0]["body"])
        self.assertEqual(model.calls, ["research", "trust", "score", "scripts"])

    def test_low_score_stops_before_generation(self):
        model = FakeModel({"trust": "可信", "reason": "", "claims": [], "sources": []}, score=50)
        result = pipeline.analyze(model, ITEM, [ITEM])
        self.assertFalse(result["match"]["eligible"])
        self.assertNotIn("scripts", result)

    def test_doubtful_item_skips_matching(self):
        model = FakeModel({"trust": "存疑", "reason": "爆料未证实", "claims": [], "sources": []})
        result = pipeline.analyze(model, ITEM, [ITEM])
        self.assertIsNone(result["match"])
        self.assertEqual(model.calls, ["research", "trust"])

    def test_failed_checks_trigger_one_rewrite(self):
        bad = {**GOOD, "seconds": 90}
        model = FakeModel({"trust": "可信", "reason": "", "claims": [], "sources": []}, scripts=[bad, bad])
        result = pipeline.analyze(model, ITEM, [ITEM])
        self.assertEqual(model.calls.count("scripts"), 2)
        self.assertTrue(all(s["checks"] for s in result["scripts"]))  # still failing is shown to the reviewer


class StorageTests(unittest.TestCase):
    def test_results_are_cached_and_exposed_in_snapshot(self):
        serve.init_db()
        with serve.connect() as db:
            db.execute("INSERT OR REPLACE INTO videos VALUES (?,?,?,?)",
                       (ITEM["id"], json.dumps({**ITEM, "media_type": "video", "source": "test", "rank": None}), "t", "t"))
        model = FakeModel({"trust": "可信", "reason": "", "claims": [], "sources": []})
        original = llm.provider
        llm.provider = lambda: model
        try:
            first = serve.analyze_pending()
            second = serve.analyze_pending()
        finally:
            llm.provider = original
        self.assertEqual(first["analyzed"], 1)
        self.assertEqual(second["analyzed"], 0)          # cached, no second model call
        item = serve.snapshot()["items"][0]
        self.assertEqual(item["ai"]["verify"]["trust"], "可信")
        self.assertEqual(serve.snapshot()["rules"]["threshold"], 70)

    def test_no_key_is_reported_not_raised(self):
        serve.init_db()
        with serve.connect() as db:
            self.assertIn("未配置", pipeline.run(db, [ITEM])["error"])



class GameSourceTests(unittest.TestCase):
    def test_boards_keep_only_game_entries(self):
        weibo = {"ok": 1, "data": {"band_list": [{"word": "KPL总决赛名场面", "num": 900}, {"word": "金价大跌", "num": 800},
                                                 {"word": "王者荣耀新英雄", "num": 700, "is_ad": 1}]}}
        self.assertEqual([i["title"] for i in game_sources.weibo(weibo)], ["KPL总决赛名场面"])
        douyin = {"status_code": 0, "word_list": [{"word": "U17男足", "hot_value": 1}, {"word": "王者荣耀周年庆", "hot_value": 2}]}
        found = list(game_sources.douyin(douyin))
        self.assertEqual((found[0]["title"], found[0]["rank"]), ("王者荣耀周年庆", 2))
        bili = {"code": 0, "data": {"list": [{"bvid": "BV1", "title": "峡谷名场面", "stat": {"view": 5}, "owner": {"name": "up"}},
                                             {"bvid": "BV2", "title": "做饭", "stat": {}}]}}
        self.assertEqual([i["id"] for i in game_sources.bilibili(bili)], ["bili-BV1"])

    def test_bad_payload_raises(self):
        with self.assertRaises(ValueError):
            list(game_sources.weibo({"ok": 0}))


class FilterTests(unittest.TestCase):
    def recent(self, days=1):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_xiaohongshu_video_without_views_uses_interactions(self):
        note = {"id": "xhs-" + "a" * 24, "media_type": "video", "views": None, "likes": 2000, "comments": 80,
                "published": self.recent()}
        self.assertIsNone(trends.rejection(note))
        self.assertEqual(trends.rejection({**note, "likes": 10}), "互动低于门槛")

    def test_trend_on_board_passes(self):
        self.assertIsNone(trends.rejection({"id": "wbt-x", "kind": "trend", "title": "KPL总决赛"}))

    def test_douyin_without_play_count_falls_back_to_interactions(self):
        video = {"id": "dy-1", "media_type": "video", "views": None, "likes": 60000, "comments": 5000,
                 "published": self.recent()}
        self.assertIsNone(trends.rejection(video))
        self.assertEqual(trends.rejection({**video, "likes": 900}), "未取得播放量，且互动低于门槛")
        self.assertEqual(trends.rejection({**video, "views": 5000}), "播放量低于 10 万")

    def test_blocklist_rejects_account_trading_promotions(self):
        promo = {"id": "dy-2", "media_type": "video", "views": 900000, "likes": 80000, "comments": 4000,
                 "published": self.recent(), "title": "战力这么高 #王者荣耀 @7881平台游戏账号"}
        self.assertEqual(trends.rejection(promo), "命中来源黑名单（7881）")
        self.assertEqual(trends.rejection({**promo, "title": "", "tags": ["代练"]}), "命中来源黑名单（代练）")

    def test_search_results_need_any_game_keyword(self):
        rows = [{"id": "7708314105476021625", "published_at": self.recent(), "text": "最近我一直找打王者什么位置最开心",
                 "hashtags": [], "metrics": {"likes": 5}, "kind": "video"},
                {"id": "7709821448987566042", "published_at": self.recent(), "text": "不愧是全国第一打野！",
                 "hashtags": [], "metrics": {}, "kind": "video"}]
        original = trends.request_rows
        trends.request_rows = lambda platform, payload: rows
        try:
            found = [item["id"] for item in trends.collect("douyin")]
        finally:
            trends.request_rows = original
        self.assertEqual(found, ["dy-7708314105476021625"])


class BreakdownTests(unittest.TestCase):
    def test_breakdown_reaches_script_prompt(self):
        model = FakeModel({"trust": "可信", "reason": "", "claims": [], "sources": []})
        result = pipeline.analyze(model, ITEM, [ITEM])
        self.assertEqual(result["match"]["breakdown"]["lifespan"], "1–3 天")
        self.assertIn("最后一次的告别句式", model.prompts[-1])


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        serve.init_db()

    def body(self, version, action="confirm", actor="小王"):
        return {"id": "dy-review", "base_version": version, "data": {"confirmed": {"by": actor}}, "action": action, "actor": actor}

    def test_stale_write_is_rejected_with_current_state(self):
        first, status = serve.save_review(self.body(0))
        self.assertEqual((status, first["version"]), (200, 1))
        conflict, status = serve.save_review(self.body(0, actor="小李"))
        self.assertEqual(status, 409)
        self.assertEqual(conflict["current"]["by"], "小王")
        _, status = serve.save_review(self.body(1, action="reopen", actor="小李"))
        self.assertEqual(status, 200)
        log = serve.events("dy-review")["events"]
        self.assertEqual([(e["actor"], e["action"]) for e in log], [("小李", "重新打开"), ("小王", "确认选题")])

    def test_actor_and_action_are_required(self):
        self.assertEqual(serve.save_review({**self.body(0), "actor": ""})[1], 400)
        self.assertEqual(serve.save_review({**self.body(0), "action": "delete"})[1], 400)


class HttpTests(unittest.TestCase):
    def test_team_token_guards_api(self):
        import threading
        from http.server import ThreadingHTTPServer
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen
        serve.init_db()
        serve.TOKEN = "secret"
        server = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as denied:
                urlopen(base + "/api/reviews")
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
            with urlopen(Request(base + "/api/reviews", headers={"X-Team-Token": "secret"})) as ok:
                self.assertIn("reviews", json.load(ok))
            with urlopen(base + "/") as page:          # the page itself loads, then asks for the token
                self.assertEqual(page.status, 200)
        finally:
            serve.TOKEN = ""
            server.shutdown()
            server.server_close()



class CompatibleClientTests(unittest.TestCase):
    """OpenAI-compatible client (Gemini free tier etc.) against a scripted fake HTTP layer."""

    def run_with(self, responses):
        import io
        from email.message import Message
        from urllib.error import HTTPError
        sent, waits = [], []

        def fake_urlopen(req, timeout):
            sent.append(json.loads(req.data))
            status = responses.pop(0)
            if status == 200:
                return io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"ok": true}'}}]}).encode())
            headers = Message()
            if status == 429:
                headers["Retry-After"] = "7"
            raise HTTPError(req.full_url, status, "err", headers, io.BytesIO(b"{}"))

        originals = llm.urlopen, llm.SLEEP
        llm.urlopen, llm.SLEEP = fake_urlopen, waits.append
        try:
            client = llm.Compatible("k", "https://example.invalid/v1", "gemini-3.5-flash-lite", "Gemini")
            return client.json("sys", "prompt", {"type": "object"}), sent, waits
        finally:
            llm.urlopen, llm.SLEEP = originals

    def test_rate_limit_waits_then_succeeds(self):
        result, sent, waits = self.run_with([429, 200])
        self.assertEqual((result, waits), ({"ok": True}, [7.0]))

    def test_json_mode_rejected_retries_without_it(self):
        result, sent, _ = self.run_with([400, 200])
        self.assertIn("response_format", sent[0])
        self.assertNotIn("response_format", sent[1])

    def test_gives_up_after_retries(self):
        with self.assertRaises(llm.ModelError) as raised:
            self.run_with([429] * (llm.RETRIES + 1))
        self.assertIn("429", str(raised.exception))

    def test_gemini_key_selects_gemini(self):
        os.environ["GEMINI_API_KEY"] = "test"
        try:
            model = llm.provider()
        finally:
            os.environ.pop("GEMINI_API_KEY")
        self.assertEqual((model.name, model.web_search), ("Gemini · gemini-3.5-flash-lite", False))
        self.assertTrue(model.base_url.endswith("/v1beta/openai"))



class DeepSeekTests(unittest.TestCase):
    def fake(self, replies):
        import io
        sent = []

        def fake_urlopen(req, timeout):
            sent.append(json.loads(req.data))
            return io.BytesIO(b"\n\n" + json.dumps(replies.pop(0)).encode())   # DeepSeek keep-alive blank lines

        return fake_urlopen, sent

    def reply(self, content, **message):
        return {"choices": [{"finish_reason": "stop", "message": {"content": content, **message}}]}

    def test_research_uses_server_search_and_lists_real_sources(self):
        fake_urlopen, sent = self.fake([self.reply(
            "核验笔记：版本公告已发布。",
            annotations=[{"type": "url_citation", "url_citation": {"url": "https://pvp.qq.com/a", "title": "官网公告"}}],
            server_tool_calls=[{"calls": [{"sources": [{"url": "https://news.example/b", "title": "媒体报道"}]}]}])])
        original = llm.urlopen
        llm.urlopen = fake_urlopen
        try:
            notes = llm.DeepSeek("k", "deepseek-flash").research("sys", "prompt")
        finally:
            llm.urlopen = original
        body = sent[0]
        self.assertEqual(body["model"], "deepseek-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertIn({"type": "deepseek_web_search"}, body["tools"])
        self.assertNotIn("response_format", body)          # web search rejects JSON mode
        self.assertIn("https://pvp.qq.com/a", notes)
        self.assertIn("https://news.example/b", notes)

    def test_blank_json_reply_is_retried(self):
        fake_urlopen, sent = self.fake([self.reply("   "), self.reply('{"score": 80}')])
        original = llm.urlopen
        llm.urlopen = fake_urlopen
        try:
            result = llm.DeepSeek("k", "deepseek-flash").json("sys", "prompt", {"type": "object"})
        finally:
            llm.urlopen = original
        self.assertEqual(result, {"score": 80})
        self.assertEqual(len(sent), 2)
        self.assertIn("json", sent[0]["messages"][0]["content"])   # DeepSeek JSON mode requires the word

    def test_deepseek_key_selects_web_search_provider(self):
        os.environ["DEEPSEEK_API_KEY"] = "test"
        try:
            model = llm.provider()
        finally:
            os.environ.pop("DEEPSEEK_API_KEY")
        self.assertEqual((model.name, model.web_search), ("DeepSeek · deepseek-flash", True))



class MultiGameTests(unittest.TestCase):
    YS = {"id": "xhs-" + "b" * 24, "game": "ys", "title": "原神新版本角色PV", "text": "", "tags": ["原神"],
          "url": "https://www.xiaohongshu.com/explore/x", "published": datetime.now(timezone.utc).isoformat()}

    def test_profiles_load_and_detect_games(self):
        self.assertEqual(list(games.GAMES), ["wzry", "hpjy", "ys", "jcc"])
        self.assertEqual(games.detect("KPL总决赛"), "wzry")
        self.assertEqual(games.detect("原神 4.0 版本 PV"), "ys")
        self.assertEqual(games.detect("云顶之弈新赛季"), "jcc")
        self.assertIsNone(games.detect("金价大跌"))
        self.assertEqual(games.by_query()["和平精英"], "hpjy")

    def test_prompts_and_rules_follow_the_items_game(self):
        model = FakeModel({"trust": "可信", "reason": "", "claims": [], "sources": []},
                          scripts=[{**GOOD, "tags": ["原神", "提瓦特"]}, {**GOOD, "name": "B", "tags": ["原神", "游戏日常"]}])
        result = pipeline.analyze(model, self.YS, [self.YS, ITEM])
        self.assertTrue(all("《原神》" in system for system in model.systems))
        self.assertNotIn("王者荣耀", model.prompts[0])                  # other games are not cross-checked
        self.assertEqual([s["checks"] for s in result["scripts"]], [[], []])
        self.assertTrue(any("原神" in e for e in pipeline.script_errors(GOOD, games.get("ys"))))  # #王者荣耀 is wrong here

    def test_game_specific_taboos_reach_the_prompt(self):
        self.assertIn("抽卡概率", pipeline.match_system(games.get("ys")))
        self.assertNotIn("抽卡概率", pipeline.match_system(games.get("wzry")))

    def test_apify_results_are_tagged_by_their_search_query(self):
        recent = datetime.now(timezone.utc).isoformat()
        rows = [{"id": "a" * 24, "input": "原神", "published_at": recent, "title": "提瓦特风景摄影", "metrics": {}},
                {"id": "c" * 24, "input": "金铲铲之战", "published_at": recent, "title": "今天吃什么", "metrics": {}}]
        original = trends.request_rows
        trends.request_rows = lambda platform, payload: rows
        try:
            found = {item["id"]: item["game"] for item in trends.collect("xiaohongshu")}
        finally:
            trends.request_rows = original
        self.assertEqual(found, {"xhs-" + "a" * 24: "ys"})                # second row names no 金铲铲 keyword

    def test_apify_runs_once_per_game_and_survives_one_failure(self):
        calls = []

        def fake(platform, payload):
            calls.append(payload["searchQueries"])
            if payload["searchQueries"] == ["原神"]:
                raise ValueError("HTTP 500")
            return [{"id": payload["searchQueries"][0]}]          # the free plan omits nothing else we need

        original = trends.request_rows
        trends.request_rows = fake
        try:
            rows = trends.rows("douyin")
        finally:
            trends.request_rows = original
        self.assertEqual(calls, [["王者荣耀"], ["和平精英"], ["原神"], ["金铲铲之战"]])
        self.assertEqual([r["input"] for r in rows], ["王者荣耀", "和平精英", "金铲铲之战"])

    def test_boards_tag_each_entry_with_its_game(self):
        weibo = {"ok": 1, "data": {"band_list": [{"word": "和平精英新地图", "num": 9}, {"word": "原神音乐会", "num": 8}]}}
        self.assertEqual([i["game"] for i in game_sources.weibo(weibo)], ["hpjy", "ys"])

    def test_topics_are_grouped_per_game(self):
        recent = datetime.now(timezone.utc).isoformat()
        posts = [{"id": f"xhs-{n}", "kind": "post", "game": g, "title": "x", "tags": ["风景摄影"], "published": recent}
                 for n, g in enumerate(["ys", "ys", "jcc", "jcc"])]
        titles = sorted(t["title"] for t in trends.group_topics(posts))
        self.assertEqual(titles, ["原神｜风景摄影", "金铲铲之战｜风景摄影"])


if __name__ == "__main__":
    unittest.main()
