"""Collect a snapshot and build static GitHub Pages files."""
import json
import os
import shutil
from pathlib import Path
from serve import analyze_pending, collect, init_db, snapshot

root = Path(__file__).resolve().parent
site = root / "_site"
site.mkdir(exist_ok=True)
init_db()
# HOTSPOT_SOURCES: "game" (free sources, hourly), "game,apify" (daily), empty = no sampling
groups = tuple(g for g in os.getenv("HOTSPOT_SOURCES", "").split(",") if g in ("game", "apify"))
status = collect(groups) if groups else {"ai": analyze_pending()}
print(json.dumps(status, ensure_ascii=False))
data = snapshot()
if data["run"] is not None:
    data["run"]["ai"] = status["ai"]  # show this build's AI status even when no new sample was taken
(site / "hotspots.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
shutil.copyfile(root / "index.html", site / "index.html")
(site / ".nojekyll").touch()
