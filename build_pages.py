"""Collect a snapshot and build static GitHub Pages files."""
import json
import shutil
from pathlib import Path
from serve import collect, init_db, snapshot

root = Path(__file__).resolve().parent
site = root / "_site"
site.mkdir(exist_ok=True)
init_db()
status = collect()
print(json.dumps(status, ensure_ascii=False))
(site / "hotspots.json").write_text(json.dumps(snapshot(), ensure_ascii=False), encoding="utf-8")
shutil.copyfile(root / "index.html", site / "index.html")
(site / ".nojekyll").touch()

