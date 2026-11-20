"""Recent social candidates and provider metrics for the hotspot queue."""
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.error import HTTPError
from urllib.parse import urlencode

QUERY = "王者荣耀"
DAYS = 7
DOUYIN = "抖音关键词"
XHS = "小红书关键词"
BILI = "B站关键词"

def count(value):
    return int(value) if re.fullmatch(r"\d{1,18}", str(value)) else None

def recent(value):
    ts = count(value)
    if ts is None:
        return None
    if ts > 10**11:
        ts /= 1000
    if ts > 10**10:
        return None
    when = datetime.fromtimestamp(ts, timezone.utc)
    now = datetime.now(timezone.utc)
    return when.isoformat() if now - timedelta(days=DAYS) <= when <= now else None

def explain_denial(error):
    body = error.read(8192).decode("utf-8", errors="replace").lower()
    if "cloudflare" in body or error.headers.get("CF-Mitigated") == "challenge":
        return "TikHub 403：请求被网关拦截"
    if "email" in body and any(word in body for word in ("verif", "activat")) or "邮箱" in body:
        return "TikHub 403：响应指向邮箱验证"
    if ("account" in body and "disabled" in body) or "账号已禁用" in body:
        return "TikHub 403：响应指向账号停用"
    if any(word in body for word in ("permission", "scope", "权限")):
        return "TikHub 403：响应指向接口或 Key 权限"
    return "TikHub 403：服务端未返回可识别的拒绝原因"

def provider(path, params, *, post=False):
    key = os.getenv("TIKHUB_API_KEY")
    if not key:
        raise ValueError("未配置 TIKHUB_API_KEY")
    url = "https://api.tikhub.io/api/v1/" + path
    body = json.dumps(params).encode() if post else None
    if not post:
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read(5_000_000))
    except HTTPError as error:
        if error.code == 403:
            raise ValueError(explain_denial(error)) from None
        raise
    if payload.get("code") != 200 or not isinstance(payload.get("data"), (dict, list)):
        raise ValueError("数据供应商未返回有效结果")
    return payload["data"]

def payload_rows(value, names):
    if isinstance(value, list):
        return value
    for key in names:
        rows = value.get(key)
        if isinstance(rows, list):
            return rows
    child = value.get("data")
    if isinstance(child, (dict, list)):
        return payload_rows(child, names)
    raise ValueError("搜索响应中未找到作品列表")

def douyin():
    data = provider("douyin/search/fetch_video_search_v2", {"keyword": QUERY, "cursor": 0}, post=True)
    for entry in payload_rows(data, ("data", "aweme_list", "items")):
        if not isinstance(entry, dict):
            continue
        item = entry.get("aweme_info") or entry
        if not isinstance(item, dict) or not item.get("video"):
            continue
        video_id = str(item.get("aweme_id", ""))
        published = recent(item.get("create_time"))
        text = item.get("desc") or ""
        tags = [tag.get("cha_name") for tag in item.get("cha_list") or [] if isinstance(tag, dict)]
        tags += [tag.get("hashtag_name") for tag in item.get("text_extra") or [] if isinstance(tag, dict)]
        hit = QUERY in tags or re.search(r"#王者荣耀(?=[\s#，。！、,.!?]|$)", text)
        if not (published and re.fullmatch(r"\d{15,22}", video_id) and (hit or QUERY in text)):
            continue
        stats = item.get("statistics") or {}
        yield dict(id="dy-" + video_id, title=(text or "王者荣耀话题视频")[:180],
                   publisher=(item.get("author") or {}).get("nickname", "抖音创作者"),
                   published=published, views=count(stats.get("play_count")), rank=None,
                   likes=count(stats.get("digg_count")), comments=count(stats.get("comment_count")),
                   shares=count(stats.get("share_count")), saves=count(stats.get("collect_count")),
                   match_type="话题 #王者荣耀" if hit else "关键词 王者荣耀", source=DOUYIN)

def xiaohongshu():
    data = provider("xiaohongshu/app_v2/search_notes", {"keyword": QUERY, "page": 1, "sort_type": "general"})
    for entry in payload_rows(data, ("items", "notes", "note_list")):
        if not isinstance(entry, dict):
            continue
        item = entry.get("note_card") or entry.get("note") or entry
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("note_id") or item.get("id") or entry.get("id") or "")
        published = recent(item.get("time") or item.get("create_time") or item.get("publish_time"))
        title = item.get("display_title") or item.get("title") or item.get("desc") or ""
        if not (published and re.fullmatch(r"[0-9a-f]{24}", note_id) and title):
            continue
        stats = item.get("interact_info") or item
        yield dict(id="xhs-" + note_id, title=title.strip()[:180], publisher=(item.get("user") or {}).get("nickname", "小红书作者"),
                   published=published, views=count(stats.get("play_count")), rank=None,
                   likes=count(stats.get("liked_count") or stats.get("likes")), comments=count(stats.get("comment_count")),
                   saves=count(stats.get("collected_count")), shares=count(stats.get("share_count")),
                   match_type="关键词 王者荣耀", source=XHS)

def bilibili(search):
    url = "https://api.bilibili.com/x/web-interface/search/type?" + urlencode({
        "search_type": "video", "keyword": QUERY, "order": "pubdate", "page": 1,
    })
    for item in search(url).get("result") or []:
        bvid = item.get("bvid") or ""
        published = recent(item.get("pubdate"))
        if published and re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
            title = unescape(re.sub(r"</?em\b[^>]*>", "", item["title"]))
            yield dict(id=bvid, title=title, publisher=item.get("author", "B站创作者"),
                       published=published, views=count(item.get("play")), rank=None,
                       match_type="关键词 王者荣耀", source=BILI)
