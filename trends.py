"""Collect and summarize recent high-engagement Wangzhe posts."""
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.request import Request, urlopen

import social
from apify_pilot import ACTORS

SOURCE = {"douyin": "抖音高互动 · Apify", "xiaohongshu": "小红书高互动 · Apify"}
THEMES = {
    "刀锋宝贝": ("刀锋宝贝",),
    "全球英雄派对": ("全球英雄派对",),
    "共鸣之战": ("共鸣之战", "朵莉布"),
    "KPL 赛事": ("KPL", "北京WB", "北京JDG"),
    "S45 上分与出装": ("上分", "出装", "巅峰赛", "梯度"),
    "斗地主": ("斗地主",),
    "农场玩法": ("农场", "种菜"),
}
GENERIC = {"王者荣耀", "王者", "游戏", "手游", "搞笑", "电子竞技", "推荐", "热门", "游戏日常", "王者荣耀日常", "上热门"}

def request_rows(platform, payload):
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise ValueError("未配置 APIFY_TOKEN")
    actor, _ = ACTORS[platform]
    req = Request(
        f"https://api.apify.com/v2/actors/dataxapi~{actor}/run-sync-get-dataset-items?maxTotalChargeUsd=0.05&timeout=120",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(req, timeout=140) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError("Apify 未返回作品列表")
    return data

def rows(platform):
    _, options = ACTORS[platform]
    return request_rows(platform, {"searchQueries": ["王者荣耀"], "publishedWithin": "week",
                                   "sort": "popular", **options})

def post_date(value):
    try:
        date = datetime.fromisoformat(value)
        return social.recent(int(date.timestamp())) if date.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None

def collect(platform):
    prefix, pattern = ("dy-", r"\d{15,22}") if platform == "douyin" else ("xhs-", r"[a-f0-9]{24}")
    for row in rows(platform):
        if not isinstance(row, dict):
            continue
        post_id = str(row.get("id", ""))
        published = post_date(row.get("published_at"))
        body = str(row.get("text") or "")
        title = str(row.get("title") or body).strip()
        if not (published and title and re.fullmatch(pattern, post_id)):
            continue
        tags = [tag.strip().lstrip("#")[:40] for tag in row.get("hashtags") or [] if isinstance(tag, str)]
        if not ("王者荣耀" in title or "王者荣耀" in body or "王者荣耀" in tags
                or any(alias.casefold() in (title+" "+body+" "+" ".join(tags)).casefold()
                       for theme, aliases in THEMES.items() if theme != "S45 上分与出装" for alias in aliases)):
            continue
        stats = row.get("metrics") or {}
        media_type = row.get("type")
        if platform == "douyin" and row.get("kind") == "video":
            media_type = media_type or "video"
        yield dict(id=prefix + post_id, title=title[:180], publisher=(row.get("author") or {}).get("name") or SOURCE[platform],
                   published=published, views=social.count(stats.get("views")), rank=None, media_type=media_type,
                   match_type="话题 #王者荣耀" if "王者荣耀" in tags else "关键词 王者荣耀",
                   likes=social.count(stats.get("likes")), comments=social.count(stats.get("comments")),
                   saves=social.count(stats.get("favorites")), shares=social.count(stats.get("shares")),
                   tags=tags[:30], source=SOURCE[platform])

MIN_VIDEO_VIEWS = 100_000
MIN_NOTE_LIKES = 1_000
MIN_NOTE_COMMENTS = 50

def rejection(item):
    if not post_date(item.get("published")):
        return "超过 7 天或缺少发布时间"
    if item.get("media_type") == "video":
        views = item.get("views")
        if views is None:
            return "缺少可核验播放量"
        if views < MIN_VIDEO_VIEWS:
            return "播放量低于 10 万"
        return None
    if item.get("media_type") in {"image", "note"}:
        if (item.get("likes") or 0) < MIN_NOTE_LIKES or (item.get("comments") or 0) < MIN_NOTE_COMMENTS:
            return "图文互动低于门槛"
        return None
    return "作品类型未确认"

def scan(platform):
    rejected = defaultdict(int)
    accepted = []
    candidates = list(collect(platform))
    if platform == "douyin":
        missing = [item for item in candidates if item["views"] is None]
        if missing:
            try:
                stats = request_rows(platform, {"playCountVideoUrls": [item["id"][3:] for item in missing[:8]]})
                totals = {
                    str(row.get("id")): social.count((row.get("metrics") or {}).get("views"))
                    for row in stats if isinstance(row, dict)
                }
                for item in missing:
                    item["views"] = totals.get(item["id"][3:])
            except (OSError, ValueError, TypeError):
                pass
    for item in candidates:
        reason = rejection(item)
        if reason:
            rejected[reason] += 1
        else:
            accepted.append(item)
    return accepted, {"candidates": len(candidates), "rejected": dict(rejected)}

def group_topics(items):
    buckets = defaultdict(dict)
    for item in items:
        if not item["id"].startswith(("dy-", "xhs-", "BV", "av")) or not post_date(item.get("published")):
            continue
        tags = item.get("tags") or []
        words = " ".join([item["title"], *tags]).casefold()
        labels = [name for name, aliases in THEMES.items() if any(alias.casefold() in words for alias in aliases)]
        if not labels:
            labels = [tag for tag in tags if re.fullmatch(r"[\w·]{3,24}", tag)
                      and tag not in GENERIC and not tag.startswith("抖音")]
        for label in set(labels):
            buckets[label][item["id"]] = item
    result = []
    for label, posts in buckets.items():
        if len(posts) < 2:
            continue
        platforms = defaultdict(list)
        for item in posts.values():
            platform = "抖音" if item["id"].startswith("dy-") else "小红书" if item["id"].startswith("xhs-") else "B站"
            platforms[platform].append(item)
        summaries = []
        for name, entries in sorted(platforms.items()):
            entries.sort(key=lambda item: (item.get("likes") or 0, item.get("comments") or 0,
                                           item.get("views") or 0), reverse=True)
            summaries.append({"name": name, "count": len(entries), "examples": entries[:2]})
        result.append({"title": label, "count": len(posts), "platforms": summaries})
    return sorted(result, key=lambda topic: (-len(topic["platforms"]), -topic["count"], topic["title"]))[:8]
