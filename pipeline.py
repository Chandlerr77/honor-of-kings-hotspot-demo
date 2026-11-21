"""AI pipeline: verify -> match -> generate, with program rules around each model step.

Division of labour (see the C1 design):
  model   : extract and check claims, score game fit, write script drafts
  rules   : downgrade unsupported verdicts, enforce the score threshold,
            check duration / structure / banned words / speech rate, cache results
  people  : review 存疑 items, confirm the topic, approve the final script (in index.html)
"""
import json
import os
import re
from datetime import datetime, timezone

import games
import llm

PLATFORM = games.PLATFORM
VERSION = "v3"          # bump when prompts change so cached analyses are redone
THRESHOLD = 70
AI_SINCE = games.RULES.get("ai_since", "")   # hotspots first seen before this are not sent to the model


def backlog(item):
    """True for hotspots collected before AI_SINCE: left for people, never analyzed automatically."""
    return bool(AI_SINCE) and str(item.get("first_seen") or "") < AI_SINCE
MAX_ITEMS = int(os.getenv("HOTSPOT_AI_MAX_ITEMS", "10"))
TRUST = ("可信", "存疑", "不实")
CLAIM = ("已证实", "未证实", "与来源矛盾")
PARTS = ("开场", "实录", "结尾")
LIFESPAN = ("几小时", "1–3 天", "一周以上")
REJECT_REASONS = ["不符合游戏设定", "品牌或合规风险", "热点已过时", "事实不准确", "创意或质量不足", "素材无法实现"]


def _obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": required or list(properties), "additionalProperties": False}


_S = {"type": "string"}
VERIFY_SCHEMA = _obj({
    "trust": {"type": "string", "enum": list(TRUST)},
    "summary": _S,
    "reason": _S,
    "claims": {"type": "array", "items": _obj({
        "text": _S, "status": {"type": "string", "enum": list(CLAIM)}, "source_url": _S})},
    "sources": {"type": "array", "items": _obj({"title": _S, "url": _S})},
})
MATCH_SCHEMA = _obj({
    "score": {"type": "integer"}, "reason": _S, "angle": _S,
    "risks": {"type": "array", "items": _S},
    "breakdown": _obj({
        "why_hot": _S, "format": _S,
        "borrow": {"type": "array", "items": _S},
        "lifespan": {"type": "string", "enum": list(LIFESPAN)},
    }),
})
SCRIPT_SCHEMA = _obj({"scripts": {"type": "array", "items": _obj({
    "name": _S, "angle": _S, "borrowed": _S, "seconds": {"type": "integer"},
    "segments": {"type": "array", "items": _obj({
        "start": {"type": "integer"}, "end": {"type": "integer"},
        "part": {"type": "string", "enum": list(PARTS)},
        "visual": _S, "voiceover": _S, "caption": _S})},
    "tags": {"type": "array", "items": _S},
})}})


def now():
    return datetime.now(timezone.utc).isoformat()


KIND = {"post": "游戏圈作品（关键词搜索的高互动帖子）", "trend": "平台热榜上的游戏相关词条"}


def platform_name(item):
    return item.get("platform") or ("抖音" if item["id"].startswith("dy-") else "小红书" if item["id"].startswith("xhs-") else "B站")


def describe(item):
    metrics = {k: item.get(k) for k in ("views", "likes", "comments", "shares", "saves") if item.get(k) is not None}
    if item.get("heat") is not None:
        metrics["榜单热度"] = item["heat"]
    if item.get("rank"):
        metrics["榜单排名"] = item["rank"]
    return json.dumps({
        "游戏": games.get(item.get("game"))["game"],
        "类型": KIND.get(item.get("kind"), KIND["post"]),
        "平台": platform_name(item), "链接": item.get("url"), "作者": item.get("publisher"),
        "发布时间": item.get("published"), "互动数据": metrics,
        "标题": item.get("title"), "正文": item.get("text") or "", "话题标签": item.get("tags") or [],
    }, ensure_ascii=False, indent=1)


# ---------- 1. 真实性核验 ----------

def verify_system(game):
    return f"""你是《{game['game']}》营销团队的热点核验员。热点必须真实、及时、查得到来源。
程序已经确认：这条作品有原始链接、发布于近 7 天、平台返回的互动数据达到门槛。你要判断的是它所依赖的事实是否可靠。

步骤：
0. 用一句话写清这个热点在说什么事（summary）。如果是热榜词条，词条在榜已由平台确认，你要先查清它指的是什么事件。
1. 列出作品里会被营销内容引用的事实性说法：版本更新、新角色或皮肤、活动、赛事结果、官方表态、具体数据等。
   纯玩家创作、搞笑、情绪表达、玩梗不属于事实性说法，可以没有。
2. 逐条找官方或权威来源核对。只认你实际查到的页面；查不到就是“未证实”，不要凭记忆补链接。
3. 结论：可信＝没有事实性说法，或全部已证实；存疑＝有说法未证实、属于爆料或传言；不实＝有说法与官方或权威来源矛盾。"""


def verify(model, item, corpus):
    game = games.get(item.get("game"))
    system = verify_system(game)
    others = [f"- [{platform_name(o)}] {o['title']}" for o in corpus
              if o["id"] != item["id"] and o.get("game") == item.get("game")][:30]
    prompt = (f"待核验作品：\n{describe(item)}\n\n同批采到的其他作品（可用于交叉比对是否多平台都在讨论）：\n"
              + ("\n".join(others) or "（无）"))
    if model.web_search:
        notes = model.research(system, prompt + "\n\n请联网核对后写出核验笔记，每条说法附上实际查到的来源链接。")
        result = model.json(system, f"{prompt}\n\n核验笔记：\n{notes}\n\n把核验笔记整理成结论。", VERIFY_SCHEMA)
    else:
        result = model.json(system + "\n本次无法联网，所有事实性说法只能标“未证实”。", prompt, VERIFY_SCHEMA)
    return verify_rules(result, model.web_search)


def verify_rules(result, searched):
    """Program guard: a verdict is only as strong as the evidence attached to it."""
    trust = result.get("trust") if result.get("trust") in TRUST else "存疑"
    notes = []
    claims = []
    for claim in result.get("claims") or []:
        status = claim.get("status") if claim.get("status") in CLAIM else "未证实"
        url = str(claim.get("source_url") or "").strip()
        if status != "未证实" and not url.startswith("http"):
            status = "未证实"
            notes.append("有说法缺少来源链接，按未证实处理")
        if not searched and status == "已证实":
            status = "未证实"
        claims.append({"text": str(claim.get("text", ""))[:200], "status": status, "source_url": url})
    if any(c["status"] == "与来源矛盾" for c in claims):
        trust = "不实"
    elif trust == "可信" and any(c["status"] == "未证实" for c in claims):
        trust = "存疑"
        notes.append("存在未证实说法，转人工复核")
    elif trust == "不实":
        trust = "存疑"
        notes.append("判为不实却没有列出矛盾说法，转人工复核")
    sources = [{"title": str(s.get("title", ""))[:120], "url": s["url"]}
               for s in result.get("sources") or [] if str(s.get("url", "")).startswith("http")]
    return {"trust": trust, "summary": str(result.get("summary", ""))[:200], "reason": str(result.get("reason", ""))[:500],
            "claims": claims, "sources": sources[:6], "rule_notes": sorted(set(notes)),
            "searched": searched}


# ---------- 2. 游戏匹配打分 ----------

def match_system(game):
    setting = {k: game[k] for k in ("summary", "world", "audience", "tone", "good_fit")}
    setting["taboos"] = games.taboos(game)
    return f"""你是《{game['game']}》的营销策划，判断一条热点是否值得这款游戏在{PLATFORM['name']}上跟进。
游戏设定与营销边界：
{json.dumps(setting, ensure_ascii=False, indent=1)}

打分 0–100：受众重合度、与游戏元素的结合是否自然、热度与时效、品牌风险。{THRESHOLD} 分以上才值得做。
触碰营销边界的热点不超过 40 分，并在 risks 写明。angle 写一句可执行的切入角度。
同时拆解爆点（breakdown），这决定脚本能不能真的“蹭”上热点：
- why_hot：它为什么火，戳中了什么情绪或需求
- format：它火的形式，例如梗的句式、BGM、拍法、剪辑节奏、评论区玩法
- borrow：2–4 个我们的视频可以直接借用的元素，要具体到能拍
- lifespan：按现在的势头还能热多久"""


def match(model, item, verdict):
    prompt = (f"热点：\n{describe(item)}\n\n这是什么事：{verdict.get('summary') or '见上'}\n"
              f"核验结论：{verdict['trust']}。{verdict['reason']}")
    result = model.json(match_system(games.get(item.get("game"))), prompt, MATCH_SCHEMA)
    breakdown = result.get("breakdown") or {}
    try:
        score = max(0, min(100, int(result.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {"score": score, "reason": str(result.get("reason", ""))[:400],
            "angle": str(result.get("angle", ""))[:200],
            "risks": [str(r)[:120] for r in result.get("risks") or []][:5],
            "breakdown": {"why_hot": str(breakdown.get("why_hot", ""))[:200], "format": str(breakdown.get("format", ""))[:200],
                          "borrow": [str(b)[:80] for b in breakdown.get("borrow") or []][:4],
                          "lifespan": breakdown.get("lifespan") if breakdown.get("lifespan") in LIFESPAN else ""},
            "eligible": verdict["trust"] == "可信" and score >= THRESHOLD}


# ---------- 3. 脚本生成 + 规则检查 ----------

def script_system(game):
    return f"""你是《{game['game']}》的短视频编导，为{PLATFORM['name']} {PLATFORM['format']}写 {PLATFORM['min_seconds']}–{PLATFORM['max_seconds']} 秒脚本。
游戏设定：{game['summary']} {game['world']}
语气：{game['tone']}
平台要求：{PLATFORM['notes']}
硬性规则：
- 写 2 版，切入角度不同；每版都要借用至少一个“可借用元素”，在 borrowed 写明借了什么、怎么用。
- 分段从 0 秒开始首尾相接，最后一段的 end 等于 seconds；第一段为“开场”且不超过 {PLATFORM['hook_seconds']} 秒，中间“实录”，最后“结尾”。
- 口播每秒不超过 {PLATFORM['max_chars_per_second']} 个字。
- tags {PLATFORM['min_tags']}–{PLATFORM['max_tags']} 个，不带 #，必须包含“{game['required_tag']}”。
- 不得出现这些词：{'、'.join(games.banned_words(game))}；不得触碰：{'；'.join(games.taboos(game))}。
- 只引用核验中已证实的事实；未证实的说法不要写进口播。画面用自录对局或官方公开素材，并写清楚拍什么。"""


def script_errors(script, game=None):
    game = game or games.get(None)
    errors = []
    seconds = script.get("seconds")
    segments = script.get("segments") or []
    if not isinstance(seconds, int) or not PLATFORM["min_seconds"] <= seconds <= PLATFORM["max_seconds"]:
        errors.append(f"时长需为 {PLATFORM['min_seconds']}–{PLATFORM['max_seconds']} 秒")
    cursor = 0
    for seg in segments:
        if seg.get("start") != cursor or not isinstance(seg.get("end"), int) or seg["end"] <= cursor:
            errors.append("分段时间需从 0 秒开始首尾相接")
            break
        cursor = seg["end"]
    else:
        if segments and cursor != seconds:
            errors.append("最后一段结束时间需等于总时长")
    parts = [seg.get("part") for seg in segments]
    if not segments or parts[0] != "开场" or parts[-1] != "结尾" or "实录" not in parts:
        errors.append("需按 开场 → 实录 → 结尾 组织")
    elif segments[0].get("end", 99) > PLATFORM["hook_seconds"]:
        errors.append(f"开场钩子需在 {PLATFORM['hook_seconds']} 秒内")
    speech = sum(len(re.sub(r"\s", "", seg.get("voiceover", ""))) for seg in segments)
    if isinstance(seconds, int) and speech > seconds * PLATFORM["max_chars_per_second"]:
        errors.append(f"口播 {speech} 字，超过每秒 {PLATFORM['max_chars_per_second']} 字")
    tags = [str(t).lstrip("#") for t in script.get("tags") or []]
    if not PLATFORM["min_tags"] <= len(tags) <= PLATFORM["max_tags"] or game["required_tag"] not in tags:
        errors.append(f"话题标签需 {PLATFORM['min_tags']}–{PLATFORM['max_tags']} 个且含 #{game['required_tag']}")
    text = json.dumps(script, ensure_ascii=False)
    hits = [w for w in games.banned_words(game) if w in text]
    if hits:
        errors.append("命中限制词：" + "、".join(hits))
    return errors


def render(script):
    lines = [f"{s['start']:02d}–{s['end']:02d} 秒｜{s['part']}：画面 {s['visual']}；口播「{s['voiceover']}」；字幕 {s['caption']}"
             for s in script["segments"]]
    lines.append(" ".join("#" + str(t).lstrip("#") for t in script["tags"]))
    return "\n".join(lines)


def generate(model, item, verdict, fit, review=None):
    game = games.get(item.get("game"))
    system = script_system(game)
    facts = [c["text"] for c in verdict.get("claims", []) if c["status"] == "已证实"]
    breakdown = fit.get("breakdown") or {}
    prompt = (f"热点：\n{describe(item)}\n\n已证实的事实：{facts or '无（不要引用任何事实性说法）'}\n"
              f"切入角度：{fit['angle']}\n匹配理由：{fit['reason']}\n需要回避：{fit['risks'] or '无'}\n"
              f"爆点：{breakdown.get('why_hot') or '未拆解'}；火的形式：{breakdown.get('format') or '未拆解'}；"
              f"可借用元素：{breakdown.get('borrow') or '无'}"
              + (f"\n运营复核意见：{review}" if review else ""))
    drafts = model.json(system, prompt, SCRIPT_SCHEMA).get("scripts") or []
    problems = {i: script_errors(s, game) for i, s in enumerate(drafts)}
    if len(drafts) < 2 or any(problems.values()):
        feedback = "\n".join(f"第 {i + 1} 版：{'；'.join(e)}" for i, e in problems.items() if e) or "需要 2 版"
        drafts = model.json(system, f"{prompt}\n\n上一稿没有通过程序检查，请修正后重写两版：\n{feedback}\n\n上一稿：\n"
                            + json.dumps(drafts, ensure_ascii=False), SCRIPT_SCHEMA).get("scripts") or []
    scripts = []
    for draft in drafts[:3]:
        errors = script_errors(draft, game)
        try:
            body = render(draft)
        except (KeyError, TypeError):
            continue
        scripts.append({"name": str(draft.get("name", "AI 草稿"))[:30], "angle": str(draft.get("angle", ""))[:120],
                        "borrowed": str(draft.get("borrowed", ""))[:160],
                        "seconds": draft.get("seconds"), "body": body, "checks": errors})
    if not scripts:
        raise llm.ModelError("脚本生成失败：模型没有给出可用分镜")
    return scripts


# ---------- orchestration ----------

def analyze(model, item, corpus):
    verdict = verify(model, item, corpus)
    result = {"status": "ok", "model": model.name, "version": VERSION, "analyzed": now(), "verify": verdict}
    if verdict["trust"] == "不实":
        return result
    if verdict["trust"] == "存疑":
        result["match"] = None   # 存疑 skips matching and goes straight to human review
        return result
    result["match"] = fit = match(model, item, verdict)
    if fit["eligible"]:
        result["scripts"] = generate(model, item, verdict, fit)
    return result


def run(db, items):
    """Analyze items that have no cached result. Returns a status dict for the run log."""
    try:
        model = llm.provider()
    except llm.ModelError as error:
        return {"model": None, "error": str(error)}
    if model is None:
        return {"model": None, "error": "未配置模型 API Key，跳过 AI 核验、匹配与生成"}
    done = {row[0] for row in db.execute("SELECT id FROM analyses WHERE version=?", (VERSION,))}
    # breakout topics first (most time-sensitive), then high-engagement posts
    order = {"trend": 0, "post": 1}
    skipped = sum(1 for i in items if i["id"] not in done and backlog(i))
    queue = sorted((i for i in items if i["id"] not in done and not backlog(i)),
                   key=lambda i: (order.get(i.get("kind"), 1), -(i.get("heat") or i.get("likes") or 0)))
    todo = queue[:MAX_ITEMS]
    analyzed, errors = 0, {}
    for item in todo:
        try:
            result = analyze(model, item, items)
        except llm.ModelError as error:
            errors[item["id"]] = str(error)
            if "429" in str(error):
                break        # rate limited: leave the rest for the next run instead of hammering the API
            continue
        db.execute("INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?)",
                   (item["id"], VERSION, json.dumps(result, ensure_ascii=False), now()))
        db.commit()
        analyzed += 1
    return {"model": model.name, "analyzed": analyzed, "pending": len(queue) - len(todo),
            "skipped": skipped, "errors": errors}


def regenerate(item, analysis, trust_note, score, reason):
    """Scripts for an item a human promoted from 存疑 to 可信 (local server only)."""
    model = llm.provider()
    if model is None:
        raise llm.ModelError("未配置模型 API Key")
    verdict = (analysis or {}).get("verify") or {"claims": []}
    fit = {"angle": reason, "reason": reason, "risks": [], "score": score,
           "breakdown": ((analysis or {}).get("match") or {}).get("breakdown") or {}}
    return generate(model, item, verdict, fit, review=trust_note)
