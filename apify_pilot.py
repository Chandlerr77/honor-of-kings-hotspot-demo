import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ACTORS = {
    "douyin": ("douyin-scraper", {"maxVideosPerQuery": 8, "playCounts": True}),
    "xiaohongshu": ("xiaohongshu-scraper", {"maxNotesPerQuery": 20}),
}

def summarize(rows):
    posts = [r for r in rows if isinstance(r, dict) and r.get("url") and r.get("id")][:5]
    checks = []
    for row in posts:
        stats = row.get("metrics") or {}
        checks.append({
            "published": row.get("published_at"),
            "has_link": True,
            "has_exact_tag": any(str(tag).lstrip("#") == "王者荣耀" for tag in row.get("hashtags") or []),
            "plays": stats.get("views"),
            **{key: stats.get(key) for key in ("likes", "comments", "shares", "favorites")},
        })
    return {"returned_rows": len(rows), "inspected": len(checks), "posts": checks}

def main():
    token = os.getenv("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN secret is missing; no Apify requests were sent.")
    errors = 0
    for platform, (actor, settings) in ACTORS.items():
        req = Request(
            f"https://api.apify.com/v2/actors/dataxapi~{actor}/run-sync-get-dataset-items?maxTotalChargeUsd=0.05&timeout=120",
            data=json.dumps({"searchQueries": ["王者荣耀"], "sort": "latest",
                             "publishedWithin": "week", **settings}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=140) as response:
                rows = json.load(response)
            if not isinstance(rows, list):
                raise ValueError("Unexpected dataset format")
            print(json.dumps({"platform": platform, **summarize(rows)}, ensure_ascii=False), flush=True)
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            status = error.code if isinstance(error, HTTPError) else type(error).__name__
            print(json.dumps({"platform": platform, "error": status}), flush=True)
            errors += 1
    return bool(errors)

if __name__ == "__main__":
    sys.exit(main())
