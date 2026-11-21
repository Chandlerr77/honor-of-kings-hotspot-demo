"""Game profiles (games/*.json) and shared marketing rules (rules.json).

Adding a game = adding one JSON file: its setting, audience, taboos, the keywords that
identify its hotspots, the Apify search query and the hashtag every script must carry.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULES = json.loads((ROOT / "rules.json").read_text(encoding="utf-8"))
PLATFORM = RULES["platform"]
GAMES = {g["id"]: g for g in sorted(
    (json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "games").glob("*.json")),
    key=lambda g: g.get("order", 99))}
DEFAULT = next(iter(GAMES))


def get(game_id):
    return GAMES.get(game_id) or GAMES[DEFAULT]


def taboos(game):
    return RULES["common_taboos"] + game.get("taboos", [])


def banned_words(game):
    return RULES["banned_words"] + game.get("banned_words", [])


def hits(game, *texts):
    blob = " ".join(str(t or "") for t in texts).casefold()
    return sum(1 for k in game["keywords"] if k.casefold() in blob)


def detect(*texts):
    """The game a piece of text is about: most keyword hits, ties broken by profile order. None if no game."""
    best = max(GAMES.values(), key=lambda g: (hits(g, *texts), -g.get("order", 99)))
    return best["id"] if hits(best, *texts) else None


def by_query():
    """Apify search query -> game id."""
    return {g["search_query"]: g["id"] for g in GAMES.values()}


def summary():
    """Public list for the UI."""
    return [{"id": g["id"], "name": g["game"]} for g in GAMES.values()]
