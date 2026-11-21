"""Free game-circle sources, no key required.

  上榜词条   : 微博热搜、抖音热榜、B站热门中与游戏相关的条目 — a game topic that broke
               out onto a platform-wide board, with heat vouched for by the platform

Only game-related entries are kept (keywords in game_profile.json).
"""
import json
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import social

PROFILE = json.loads((Path(__file__).resolve().parent / "game_profile.json").read_text(encoding="utf-8"))
KEYWORDS = [k.casefold() for k in PROFILE["keywords"]]
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
SOURCE = {"weibo": "微博热搜", "douyin": "抖音热榜", "bilibili": "B站热门"}


def fetch(url, referer=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return json.loads(response.read(5_000_000))


def game_related(*texts):
    blob = " ".join(str(t or "") for t in texts).casefold()
    return any(k in blob for k in KEYWORDS)


def to_iso(timestamp):
    ts = social.count(timestamp)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000 if ts > 10**11 else ts, timezone.utc).isoformat()


def topic_id(prefix, word):
    return prefix + sha1(word.encode("utf-8")).hexdigest()[:12]


def weibo(data):
    rows = (data.get("data") or {}).get("band_list") if isinstance(data, dict) else None
    if data.get("ok") != 1 or not isinstance(rows, list):
        raise ValueError("微博热搜返回格式异常")
    rank = 0
    for row in rows:
        if not isinstance(row, dict) or row.get("is_ad") or not row.get("word"):
            continue
        rank += 1
        word = str(row["word"]).strip()
        if game_related(word, row.get("note")):
            yield dict(id=topic_id("wbt-", word), kind="trend", platform="微博", title=word[:80],
                       text=str(row.get("category") or ""), heat=social.count(row.get("num")), rank=rank,
                       published=to_iso(row.get("onboard_time")),
                       url="https://s.weibo.com/weibo?q=" + quote(f"#{word}#"),
                       publisher=SOURCE["weibo"], source=SOURCE["weibo"])


def douyin(data):
    rows = data.get("word_list") if isinstance(data, dict) else None
    if data.get("status_code") != 0 or not isinstance(rows, list):
        raise ValueError("抖音热榜返回格式异常")
    for rank, row in enumerate((r for r in rows if isinstance(r, dict) and r.get("word")), 1):
        word = str(row["word"]).strip()
        if game_related(word):
            yield dict(id=topic_id("dyt-", word), kind="trend", platform="抖音", title=word[:80], text="",
                       heat=social.count(row.get("hot_value")), rank=rank, published=None,
                       url="https://www.douyin.com/search/" + quote(word),
                       publisher=SOURCE["douyin"], source=SOURCE["douyin"])


def bilibili(data):
    rows = (data.get("data") or {}).get("list") if isinstance(data, dict) else None
    if data.get("code") == -352:
        raise ValueError("B站风控拦截（-352），本轮跳过")   # intermittent; not worked around
    if data.get("code") != 0 or not isinstance(rows, list):
        raise ValueError("B站热门返回格式异常")
    for rank, row in enumerate((r for r in rows if isinstance(r, dict) and r.get("bvid")), 1):
        if not game_related(row.get("title"), row.get("desc"), row.get("tname")):
            continue
        stat = row.get("stat") or {}
        yield dict(id="bili-" + str(row["bvid"]), kind="trend", platform="B站", title=str(row.get("title", ""))[:120],
                   text=str(row.get("desc") or "")[:600], heat=social.count(stat.get("view")), rank=rank,
                   published=to_iso(row.get("pubdate")), url="https://www.bilibili.com/video/" + str(row["bvid"]),
                   publisher=(row.get("owner") or {}).get("name") or SOURCE["bilibili"], source=SOURCE["bilibili"],
                   media_type="video", views=social.count(stat.get("view")), likes=social.count(stat.get("like")),
                   comments=social.count(stat.get("reply")), saves=social.count(stat.get("favorite")),
                   shares=social.count(stat.get("share")), tags=[t for t in [row.get("tname")] if t])


ENDPOINTS = {
    "weibo": ("https://weibo.com/ajax/statuses/hot_band", "https://weibo.com/", weibo),
    "douyin": ("https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/", None, douyin),
    "bilibili": ("https://api.bilibili.com/x/web-interface/popular?ps=50&pn=1", "https://www.bilibili.com/", bilibili),
}


def scan(name):
    url, referer, parse = ENDPOINTS[name]
    items = list(parse(fetch(url, referer)))
    return items, {"candidates": len(items), "rejected": {}}
