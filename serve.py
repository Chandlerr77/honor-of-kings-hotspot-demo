"""Hotspot service: sampling, AI pipeline, and the shared review store. Python 3.10+.

Environment:
  HOTSPOT_HOST / HOTSPOT_PORT        bind address, default 127.0.0.1:8765
  HOTSPOT_TEAM_TOKEN                 shared team password; required when HOTSPOT_HOST is not loopback
  HOTSPOT_INTERVAL_SECONDS           free game sources, default 1800 (0 = only on demand)
  HOTSPOT_APIFY_INTERVAL_SECONDS     Apify search (billed), default 0 = only on demand
"""
import hmac
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import game_sources
import games
import pipeline
import trends

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv("HOTSPOT_DB", str(ROOT / ".data" / "apify-hotspots.sqlite3")))
TOKEN = os.getenv("HOTSPOT_TEAM_TOKEN", "")
COLLECT_LOCK = threading.Lock()
REVIEW_LOCK = threading.Lock()
TREND_HOURS = 48              # a hot-list entry stays in the queue this long after it was last on the board
REVIEW_ACTIONS = {"review": "人工复核", "confirm": "确认选题", "generate": "生成脚本", "save": "保存修改",
                  "reject": "驳回脚本", "approve": "确认终稿", "reopen": "重新打开"}
SOURCES = {
    "game": [(name, label, game_sources.scan) for name, label in game_sources.SOURCE.items()],
    "apify": [(name, label, trends.scan) for name, label in trends.SOURCE.items()],
}


def iso(when=None):
    return (when or datetime.now(timezone.utc)).isoformat()


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
        CREATE TABLE IF NOT EXISTS analyses (
          id TEXT NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL,
          PRIMARY KEY(id, version)
        );
        CREATE TABLE IF NOT EXISTS reviews (
          id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL, updated TEXT NOT NULL, updated_by TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, actor TEXT NOT NULL,
          action TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_by_id ON events(id, seq);
        """)


def article_url(item_id):
    if item_id.startswith("dy-"):
        return "https://www.douyin.com/video/" + item_id[3:]
    if item_id.startswith("xhs-"):
        return "https://www.xiaohongshu.com/explore/" + item_id[4:]
    return "https://www.bilibili.com/video/" + item_id


# ---------- sampling ----------

def store(db, item, now):
    existing = db.execute("SELECT data FROM videos WHERE id=?", (item["id"],)).fetchone()
    old = json.loads(existing[0]) if existing else {}
    for field in ("likes", "comments", "shares", "saves", "match_type", "tags", "text", "published", "game"):
        if item.get(field) is None and old.get(field) is not None:
            item[field] = old[field]
    if item.get("kind") == "trend" and not item.get("published"):
        item["published"] = now                     # board without timestamps: first time we saw it
    if item.get("views") is None and old.get("views") is not None:
        item["views"], item["views_at"] = old["views"], old.get("views_at")
    elif item.get("views") is not None:
        item["views_at"] = now
    db.execute("""INSERT INTO videos VALUES (?, ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET data=excluded.data,last_seen=excluded.last_seen""",
               (item["id"], json.dumps(item, ensure_ascii=False), now, now))
    metric = item.get("heat") if item.get("kind") == "trend" else item.get("views") if item.get("views_at") == now else None
    db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?)",
               (item["id"], item["source"], now, metric, item.get("rank")))


def collect(groups=("game",)):
    """Sample the given source groups, then run the AI pipeline on new items."""
    if not COLLECT_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        now = iso()
        seen, errors, counts, filtering = [], {}, {}, {}
        for group in groups:
            for name, label, scan in SOURCES[group]:
                try:
                    items, filtering[label] = scan(name)
                    counts[label] = len(items)
                    seen.extend(items)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    errors[label] = str(error) or type(error).__name__
        with connect() as db:
            for item in seen:
                store(db, item, now)
        status = dict(sampled=now, groups=list(groups), counts=counts, errors=errors, filtering=filtering,
                      ai=analyze_pending())
        with connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?)", (now, json.dumps(status, ensure_ascii=False)))
        return status
    finally:
        COLLECT_LOCK.release()


def analyze_pending():
    """Run the AI pipeline on queued items that have no cached analysis yet."""
    items = snapshot()["items"]
    with connect() as db:
        return pipeline.run(db, items)


def snapshot():
    cutoff = iso(datetime.now(timezone.utc) - timedelta(hours=TREND_HOURS))
    with connect() as db:
        rows = db.execute("SELECT data,first_seen,last_seen FROM videos ORDER BY last_seen DESC, id LIMIT 400").fetchall()
        run = db.execute("SELECT data FROM runs ORDER BY sampled DESC LIMIT 1").fetchone()
        analyses = dict(db.execute("SELECT id,data FROM analyses WHERE version=?", (pipeline.VERSION,)).fetchall())
        board_time = max((last for data, _, last in rows if '"kind": "trend"' in data), default=None)
        items = []
        for data, first, last in rows:
            item = json.loads(data)
            if trends.rejection(item) or (item.get("kind") == "trend" and last < cutoff):
                continue
            observations = db.execute("""SELECT source,sampled,views,rank FROM observations
                WHERE id=? ORDER BY sampled DESC LIMIT 12""", (item["id"],)).fetchall()
            item.update(first_seen=first, last_seen=last, url=item.get("url") or article_url(item["id"]),
                        kind=item.get("kind") or "post", game=item.get("game") or games.DEFAULT, on_board=item.get("kind") == "trend" and last == board_time,
                        ai=json.loads(analyses[item["id"]]) if item["id"] in analyses else None,
                        observations=[dict(source=source, sampled=sampled, views=views, rank=rank)
                                      for source, sampled, views, rank in observations])
            items.append(item)
    rules = dict(banned_words=games.RULES["banned_words"], threshold=pipeline.THRESHOLD,
                 min_seconds=pipeline.PLATFORM["min_seconds"], max_seconds=pipeline.PLATFORM["max_seconds"],
                 reject_reasons=pipeline.REJECT_REASONS)
    return dict(items=items, trends=trends.group_topics(items), run=json.loads(run[0]) if run else None,
                rules=rules, games=games.summary())


# ---------- shared review store ----------

def reviews():
    with connect() as db:
        rows = db.execute("SELECT id,data,version,updated,updated_by FROM reviews").fetchall()
    return {"reviews": {i: {"data": json.loads(d), "version": v, "updated": u, "by": b} for i, d, v, u, b in rows}}


def events(item_id):
    with connect() as db:
        rows = db.execute("SELECT actor,action,detail,at FROM events WHERE id=? ORDER BY seq DESC LIMIT 50",
                          (item_id,)).fetchall()
    return {"events": [{"actor": a, "action": REVIEW_ACTIONS.get(c, c), "detail": d, "at": t} for a, c, d, t in rows]}


def save_review(body):
    """Optimistic concurrency: the write wins only if nobody saved since the client loaded."""
    item_id = str(body.get("id") or "")[:80]
    actor = str(body.get("actor") or "").strip()[:40]
    action = body.get("action")
    data = body.get("data")
    if not item_id or not actor or action not in REVIEW_ACTIONS or not isinstance(data, dict):
        return {"error": "缺少热点、操作人或操作类型"}, 400
    if not isinstance(body.get("base_version"), int):
        return {"error": "缺少版本号"}, 400
    now = iso()
    with REVIEW_LOCK, connect() as db:
        row = db.execute("SELECT data,version,updated,updated_by FROM reviews WHERE id=?", (item_id,)).fetchone()
        current = row[1] if row else 0
        if body["base_version"] != current:
            return {"error": "conflict", "current": {"data": json.loads(row[0]), "version": row[1],
                                                     "updated": row[2], "by": row[3]}}, 409
        db.execute("""INSERT INTO reviews VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            data=excluded.data, version=excluded.version, updated=excluded.updated, updated_by=excluded.updated_by""",
                   (item_id, json.dumps(data, ensure_ascii=False), current + 1, now, actor))
        db.execute("INSERT INTO events (id,actor,action,detail,at) VALUES (?,?,?,?,?)",
                   (item_id, actor, action, str(body.get("detail") or "")[:500], now))
    return {"version": current + 1, "updated": now, "by": actor}, 200


def generate_for(body):
    """Scripts for an item an operator promoted from 存疑 to 可信 after review."""
    item = next((i for i in snapshot()["items"] if i["id"] == body.get("id")), None)
    if not item:
        return {"error": "找不到这条热点"}, 404
    try:
        score = int(body.get("score"))
    except (TypeError, ValueError):
        return {"error": "适配分无效"}, 400
    if score < pipeline.THRESHOLD:
        return {"error": f"适配分低于 {pipeline.THRESHOLD}"}, 400
    try:
        scripts = pipeline.regenerate(item, item.get("ai"), str(body.get("review", ""))[:500],
                                      score, str(body.get("match", ""))[:400])
    except pipeline.llm.ModelError as error:
        return {"error": str(error)}, 502
    return {"scripts": scripts}, 200


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def authorized(self):
        if TOKEN and not hmac.compare_digest(self.headers.get("X-Team-Token", ""), TOKEN):
            self.json({"error": "需要团队口令"}, 401)
            return False
        return True

    def body(self, limit):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > limit:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            self.reply((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if not url.path.startswith("/api/"):
            self.send_error(404)
            return
        if not self.authorized():
            return
        if url.path == "/api/hotspots":
            self.json(snapshot())
        elif url.path == "/api/reviews":
            self.json(reviews())
        elif url.path == "/api/events":
            self.json(events(parse_qs(url.query).get("id", [""])[0]))
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ("/api/collect", "/api/generate", "/api/reviews"):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
            self.send_error(403)
            return
        if not self.authorized():
            return
        try:
            body = self.body(400_000)
        except (ValueError, json.JSONDecodeError):
            self.json({"error": "请求格式错误或过大"}, 400)
            return
        if path == "/api/collect":
            groups = ("game", "apify") if body.get("apify") else ("game",)
            self.json(collect(groups))
        elif path == "/api/reviews":
            self.json(*save_review(body))
        else:
            self.json(*generate_for(body))


def scheduler(interval, apify_interval):
    """Free game sources every `interval` seconds; Apify (billed) every `apify_interval` seconds."""
    last_apify = 0.0
    while True:
        groups = ["game"]
        if apify_interval and time.time() - last_apify >= apify_interval:
            groups.append("apify")
            last_apify = time.time()
        status = collect(tuple(groups))
        print(f"[{iso()}] 采样 {status.get('counts', status)}", flush=True)
        if not interval:
            return
        time.sleep(interval)


if __name__ == "__main__":
    init_db()
    host = os.getenv("HOTSPOT_HOST", "127.0.0.1")
    port = int(os.getenv("HOTSPOT_PORT", "8765"))
    if host not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        raise SystemExit("对外开放时必须设置 HOTSPOT_TEAM_TOKEN（团队口令）")
    interval = max(60, int(os.getenv("HOTSPOT_INTERVAL_SECONDS", "1800"))) if os.getenv("HOTSPOT_INTERVAL_SECONDS", "1800") != "0" else 0
    apify_interval = int(os.getenv("HOTSPOT_APIFY_INTERVAL_SECONDS", "0"))
    threading.Thread(target=scheduler, args=(interval, apify_interval), daemon=True).start()
    with ThreadingHTTPServer((host, port), Handler) as server:
        print(f"Open http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}", flush=True)
        server.serve_forever()
