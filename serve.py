"""Local Honor of Kings sampling service. Python 3.10+, standard library only."""
import json
import trends
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("HOTSPOT_DB", str(ROOT / ".data" / "apify-hotspots.sqlite3")))
LOCK = threading.Lock()

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

def article_url(item_id):
    if item_id.startswith("dy-"):
        return "https://www.douyin.com/video/" + item_id[3:]
    if item_id.startswith("xhs-"):
        return "https://www.xiaohongshu.com/explore/" + item_id[4:]
    if item_id.startswith("taptap-"):
        return "https://www.taptap.cn/moment/" + item_id.removeprefix("taptap-")
    return "https://www.bilibili.com/video/" + item_id

def collect():
    if not LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        now = iso()
        seen, errors, counts, filtering = [], {}, {}, {}
        for platform, name in trends.SOURCE.items():
            try:
                items, filtering[name] = trends.scan(platform)
                counts[name] = len(items)
                seen.extend(items)
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors[name] = str(error)
        with connect() as db:
            for item in seen:
                existing = db.execute("SELECT data FROM videos WHERE id=?", (item["id"],)).fetchone()
                old = json.loads(existing[0]) if existing else {}
                for field in ("likes", "comments", "shares", "saves", "match_type", "tags"):
                    if item.get(field) is None and old.get(field) is not None:
                        item[field] = old[field]
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
            status = dict(sampled=now, counts=counts, errors=errors, filtering=filtering)
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
            if trends.rejection(item):
                continue
            observations = db.execute("""SELECT source,sampled,views,rank FROM observations
                WHERE id=? ORDER BY sampled DESC LIMIT 12""", (item["id"],)).fetchall()
            item.update(first_seen=first, last_seen=last,
                        url=article_url(item["id"]),
                        observations=[dict(source=source,sampled=sampled,views=views,rank=rank)
                                      for source,sampled,views,rank in observations])
            items.append(item)
    return dict(items=items, trends=trends.group_topics(items), run=json.loads(run[0]) if run else None, interval=None)

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
            name = self.path.lstrip("/") or "index.html"
            content_type = "application/json" if name.endswith(".json") else "text/html"
            self.reply((ROOT / name).read_bytes(), content_type + "; charset=utf-8")
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

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("HOTSPOT_PORT", "8765"))
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Open http://127.0.0.1:{port}", flush=True)
        server.serve_forever()
