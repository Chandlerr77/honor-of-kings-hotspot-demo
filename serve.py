"""Local Honor of Kings sampling service. Python 3.10+, standard library only."""
import json
import os
import re
import sqlite3
import threading
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("HOTSPOT_DB", str(ROOT / ".data" / "hotspots.sqlite3")))
POPULAR = "https://api.bilibili.com/x/web-interface/popular?pn=1&ps=50"
SEED = "https://api.bilibili.com/x/web-interface/view?bvid=BV1HvWJ6WE8B"
KEYWORDS = re.compile(r"王者荣耀|王者峡谷|\bKPL\b|\bK甲\b", re.I)
VIDEO = re.compile(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)", re.I)
INTERVAL = max(60, int(os.getenv("HOTSPOT_INTERVAL_SECONDS", "1800")))
LOCK = threading.Lock()
FEEDS = {
    "B站综合热门 RSS": os.getenv("HOTSPOT_POPULAR_RSS", ""),
    "王者荣耀赛事账号 RSS": os.getenv("HOTSPOT_KPL_RSS", ""),
}

def iso(timestamp=None):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else datetime.now(timezone.utc).isoformat()

@contextmanager
def connect():
    db = sqlite3.connect(DB, timeout=15)
    try:
        with db:
            yield db
    finally:
        db.close()

def init_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
          id TEXT PRIMARY KEY, data TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations (
          id TEXT NOT NULL, source TEXT NOT NULL, sampled TEXT NOT NULL, views INTEGER, rank INTEGER,
          PRIMARY KEY(id, source, sampled)
        );
        CREATE TABLE IF NOT EXISTS runs (sampled TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

def fetch(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
    })
    with urllib.request.urlopen(request, timeout=12) as response:
        body = response.read(5_000_001)
    if len(body) > 5_000_000:
        raise ValueError("源响应超过 5 MB")
    return body

def bili(url):
    result = json.loads(fetch(url))
    if result["code"] != 0:
        raise ValueError(f"B站响应码 {result['code']}")
    return result["data"]

def popular():
    for rank, item in enumerate(bili(POPULAR)["list"], 1):
        if KEYWORDS.search(item["title"] + " " + item.get("desc", "")):
            yield video_item(item, "B站综合热门", rank)

def video_item(item, source, rank=None):
    bvid = item["bvid"]
    if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
        raise ValueError("无效视频 ID")
    return dict(id=bvid, title=item["title"], publisher=item["owner"]["name"],
                published=iso(item["pubdate"]) if item["pubdate"] else None,
                views=item.get("stat", {}).get("view"), rank=rank, source=source)

def rss(url, source):
    root = ET.fromstring(fetch(url))
    for rank, item in enumerate(root.findall("./channel/item"), 1):
        match = VIDEO.search(item.findtext("link", ""))
        title = item.findtext("title", "").strip()
        if not (match and title and ("赛事账号" in source or KEYWORDS.search(title))):
            continue
        published = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(published).isoformat() if published else None
        except (TypeError, ValueError, OverflowError):
            published = None
        yield dict(id=match[1], title=title, publisher=item.findtext("author", source),
                   published=published, views=None, rank=rank if "综合热门" in source else None, source=source)

def collect():
    if not LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        now = iso()
        seen, errors, counts = [], {}, {}
        loaders = {
            "B站综合热门": popular,
            "已知赛事回放核对": lambda: [video_item(bili(SEED), "已知赛事回放核对")],
        }
        for name, url in FEEDS.items():
            if url:
                loaders[name] = lambda url=url, name=name: rss(url, name)
        for name, load in loaders.items():
            try:
                items = list(load())
                counts[name] = len(items)
                seen.extend(items)
            except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
                errors[name] = str(error)
        with connect() as db:
            for item in seen:
                existing = db.execute("SELECT data FROM videos WHERE id=?", (item["id"],)).fetchone()
                old = json.loads(existing[0]) if existing else {}
                if item["views"] is None and old.get("views") is not None:
                    item["views"], item["views_at"] = old["views"], old["views_at"]
                elif item["views"] is not None:
                    item["views_at"] = now
                db.execute("""INSERT INTO videos VALUES (?, ?, ?, ?)
                  ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_seen=excluded.last_seen""",
                  (item["id"], json.dumps(item, ensure_ascii=False), now, now))
            for item in seen:
                db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?)",
                           (item["id"], item["source"], now,
                            item["views"] if item.get("views_at") == now else None, item["rank"]))
            status = dict(sampled=now, counts=counts, errors=errors)
            db.execute("INSERT INTO runs VALUES (?,?)", (now, json.dumps(status, ensure_ascii=False)))
        return status
    finally:
        LOCK.release()

def snapshot():
    with connect() as db:
        rows = db.execute("SELECT data,first_seen,last_seen FROM videos ORDER BY last_seen DESC, id LIMIT 100").fetchall()
        run = db.execute("SELECT data FROM runs ORDER BY sampled DESC LIMIT 1").fetchone()
        items = []
        for data, first, last in rows:
            item = json.loads(data)
            observations = db.execute("""SELECT source,sampled,views,rank FROM observations
                WHERE id=? ORDER BY sampled DESC LIMIT 12""", (item["id"],)).fetchall()
            item.update(first_seen=first, last_seen=last,
                        url="https://www.bilibili.com/video/" + item["id"],
                        observations=[dict(source=source,sampled=sampled,views=views,rank=rank)
                                      for source,sampled,views,rank in observations])
            items.append(item)
    return dict(items=items, run=json.loads(run[0]) if run else None, interval=INTERVAL)

class Handler(BaseHTTPRequestHandler):
    def reply(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.reply((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/hotspots":
            self.json(snapshot())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/collect":
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            self.send_error(403)
            return
        self.json(collect())

def scheduled():
    while True:
        try:
            collect()
        except sqlite3.Error as error:
            print(f"Storage error: {error}", flush=True)
        threading.Event().wait(INTERVAL)

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("HOTSPOT_PORT", "8765"))
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        threading.Thread(target=scheduled, daemon=True).start()
        print(f"Open http://127.0.0.1:{port}", flush=True)
        server.serve_forever()


