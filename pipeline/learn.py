"""Teaching-first video and podcast picks for Detour.

Videos: Gemini chooses one learning topic per category (rotating, never repeated),
we search YouTube for it and keep real explainers (well watched, 5-45 min, no news),
then Gemini picks the best one and writes a one-line "what you'll learn".

Podcasts: latest episodes of story-driven, explain-something shows; Gemini picks the
two most gripping, and episodes with chapter timestamps start after the intro/sponsor read.
"""
from __future__ import annotations

import html
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# What each Watch category should teach. Indian channels only belong in "india".
CATEGORY_BRIEF = {
    "india": "Indian history, culture, geography or how India works (only India-native topics)",
    "money": "personal finance and investing fundamentals a student can use: terms, how to start, how markets and money work",
    "business": "how companies, startups, fintech and products actually work; strategy and famous case studies",
    "ai": "how AI and modern tech work under the hood, and how to use AI tools well",
    "mind": "human behaviour and psychology: social skills, attraction and dating dynamics, charisma, habits, persuasion, emotions",
    "philosophy": "big ideas and practical philosophy for living: Stoicism, ethics, meaning, famous thinkers",
    "history": "gripping world history explained as a story: empires, wars, turning points, famous figures",
    "science": "fascinating science explained simply: space, the body, physics, nature",
}

STOP = {"how", "what", "why", "the", "and", "for", "with", "your", "from", "into", "that", "this", "work", "works",
        "understanding", "explained", "about", "their", "they", "when", "does", "using", "make", "makes",
        "psychology", "first", "people", "life", "history", "story", "science", "simple", "guide", "basics", "power"}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}|\d{2,}", text.lower()) if w not in STOP}


def similar_topic(a: str, b: str) -> bool:
    """True when two topics are about the same thing (shared key words), e.g. two 'rule of 72' phrasings."""
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return a.strip().lower() == b.strip().lower()
    shared = len(ka & kb)
    return shared >= 2 or shared / min(len(ka), len(kb)) >= 0.5


# One random angle per category per day pushes Gemini off its favourite topics.
CATEGORY_ANGLES = {
    "india": ["an ancient or medieval empire", "a freedom-struggle story", "how an Indian institution works",
              "Indian geography or a region's story", "a famous Indian scientist or inventor", "an architectural wonder",
              "a turning-point battle or treaty", "Indian economy or a policy explained", "culture, food or language origins"],
    "money": ["a common money mistake", "how a financial product works", "a famous market crash or bubble",
              "behavioural finance", "taxes and salary basics", "how banks and lending work", "investing strategies",
              "economics of everyday things", "first salary and budgeting"],
    "business": ["a famous company's rise or fall", "a pricing or marketing trick", "how a startup raised money",
                 "supply chains and logistics", "a business model teardown", "negotiation and sales", "brand strategy"],
    "ai": ["how a specific AI tool works", "AI for productivity", "robots and hardware", "cybersecurity and hacking",
           "how the internet works", "AI ethics and risks", "computer science fundamentals", "the history of computing"],
    "mind": ["attraction and dating", "confidence and body language", "conversation skills", "habits and discipline",
             "a famous psychology experiment", "persuasion and influence", "emotions and stress", "friendship and loneliness",
             "memory and learning"],
    "philosophy": ["a famous thinker's life and idea", "an ethical dilemma", "Eastern philosophy", "meaning and happiness",
                   "free will and the self", "political philosophy", "logic and arguments"],
    "history": ["a mystery of history", "an empire's fall", "a spy or heist story", "an explorer's journey",
                "a war's turning point", "a revolution", "an invention that changed the world", "a famous leader's downfall"],
    "science": ["space and the universe", "the human body", "animals and evolution", "physics made simple",
                "a disaster explained by science", "chemistry in daily life", "earth, weather and climate", "medicine breakthroughs"],
}


NEWSY = re.compile(r"\b(live|livestream|breaking|news|update|reacts?|reaction|trailer|teaser|promo|podcast ep|#shorts|shorts)\b", re.I)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
INDIA_ANGLE = re.compile(r"\b(hindi|india|indian|nifty|sensex|rupees?|lakhs?|crores?)\b", re.I)
BLOCKED_CHANNELS = {"Mohak Mangal"}  # opinion/rant content he rejected


# ---------------------------------------------------------------- YouTube search

def yt_search(query: str) -> list[dict]:
    """Video results for a query, from the search page's embedded JSON (no API key)."""
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
           + "&sp=EgIQAQ%253D%253D")  # filter: videos only
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    last: Exception | None = None
    for attempt in range(3):
        try:
            page = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
            m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
            if not m:
                raise RuntimeError("no ytInitialData (blocked or consent page)")
            return list(_video_renderers(json.loads(m.group(1))))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise last or RuntimeError("search failed")


def _video_renderers(o):
    if isinstance(o, dict):
        if "videoRenderer" in o:
            v = o["videoRenderer"]
            vid = v.get("videoId")
            if vid:
                yield {
                    "id": vid,
                    "title": "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])),
                    "channel": "".join(r.get("text", "") for r in v.get("ownerText", {}).get("runs", [])),
                    "seconds": _hms(v.get("lengthText", {}).get("simpleText", "")),
                    "views": _views(v.get("viewCountText", {}).get("simpleText", "")),
                    "age": v.get("publishedTimeText", {}).get("simpleText", ""),
                }
        for x in o.values():
            yield from _video_renderers(x)
    elif isinstance(o, list):
        for x in o:
            yield from _video_renderers(x)


def _hms(s: str) -> int:
    n = 0
    for part in s.split(":"):
        if not part.isdigit():
            return 0
        n = n * 60 + int(part)
    return n


def _views(s: str) -> int:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


def good_candidates(results: list[dict], category: str, seen: set[str]) -> list[dict]:
    out = []
    for r in results:
        if r["id"] in seen or not (300 <= r["seconds"] <= 2700) or r["views"] < 50_000:
            continue
        if NEWSY.search(r["title"]):
            continue
        if r["channel"] in BLOCKED_CHANNELS:
            continue
        # Universal topics come from global explainers; Indian creators/angles only for India topics.
        if category != "india" and (DEVANAGARI.search(r["title"]) or INDIA_ANGLE.search(r["title"])):
            continue
        out.append(r)
    out.sort(key=lambda r: r["views"], reverse=True)
    return out[:6]


def _fallback_topics(day_ordinal: int, done: set[str]) -> list[dict]:
    seed = json.loads((_here() / "seed" / "topics.json").read_text(encoding="utf-8"))
    rng = random.Random(day_ordinal)
    out = []
    for cat, topics in seed.items():
        fresh = [t for t in topics if not any(similar_topic(t, d) for d in done)] or topics
        t = rng.choice(fresh)
        out.append({"category": cat, "topic": t, "query": t + " explained"})
    return out


def _here():
    from pathlib import Path
    return Path(__file__).resolve().parent


def build_videos(key: str | None, gemini: Callable, prompt: Callable, hist: dict, day_ordinal: int,
                 categories: list[str], pool: dict) -> dict:
    seen = set(hist.get("videos", []))
    done = {t.lower() for t in hist.get("topics", [])}

    # Gemini tends to return the very same topics every day, so ask for several candidates per
    # category with a rotating angle, and reject anything that resembles a past topic in code.
    past = hist.get("topics", [])
    rng = random.Random(day_ordinal)
    topics: list[dict] = []
    if key:
        angles = "\n".join(f"- {c}: {CATEGORY_BRIEF[c]}. Today's angle: {rng.choice(CATEGORY_ANGLES[c])}"
                           for c in categories)
        got = gemini(prompt("topics", briefs=angles, avoid="; ".join(past[-300:]) or "(none)"), key)
        if not isinstance(got, list):
            got = []
        for cat in categories:
            options = [t for t in got if isinstance(t, dict) and t.get("category") == cat
                       and isinstance(t.get("topic"), str) and isinstance(t.get("query"), str)]
            fresh = next((t for t in options if not any(similar_topic(t["topic"], p_) for p_ in past)), None)
            if fresh:
                topics.append(fresh)
            elif options:
                print(f"  ! {cat}: all {len(options)} Gemini topics were repeats", file=sys.stderr)
    have = {t["category"] for t in topics}
    for t in _fallback_topics(day_ordinal, done):
        if t["category"] not in have and t["category"] in categories:
            topics.append(t)
    by_cat = {t["category"]: t for t in topics}

    cands: dict[str, list[dict]] = {}
    for cat in categories:
        t = by_cat.get(cat)
        if not t:
            continue
        try:
            found = good_candidates(yt_search(t["query"]), cat, seen)
            if len(found) < 2:  # query too narrow: broaden once
                found = good_candidates(yt_search(t["topic"]), cat, seen) or found
        except Exception as e:  # noqa: BLE001
            print(f"  ! youtube search failed for {cat}: {e}", file=sys.stderr)
            found = []
        if found:
            cands[cat] = found
        time.sleep(1.5)  # be gentle; bursts get blocked
    print(f"  youtube search: {len(cands)}/{len(categories)} topics found videos")

    picks: dict[str, dict] = {}
    if key and cands:
        listing = []
        for cat, vids in cands.items():
            listing.append(f"## {cat}: {by_cat[cat]['topic']}")
            listing += [f"[{v['id']}] {v['title']} | {v['channel']} | {v['seconds'] // 60} min | {v['views']:,} views"
                        for v in vids]
        got = gemini(prompt("videopick", listing="\n".join(listing)), key)
        for g in got if isinstance(got, list) else []:
            if not isinstance(g, dict):
                continue
            cat, vid = g.get("category"), g.get("id")
            match = next((v for v in cands.get(cat, []) if v["id"] == vid), None)
            if match and isinstance(g.get("learn"), str):
                picks[cat] = {**match, "learn": g["learn"].strip()}
    for cat, vids in cands.items():
        if cat not in picks:
            picks[cat] = {**vids[0], "learn": by_cat[cat]["topic"]}

    videos = []
    for cat in categories:
        p = picks.get(cat)
        if p:
            videos.append({
                "id": p["id"], "title": p["title"], "channel": p["channel"], "category": cat,
                "url": f"https://www.youtube.com/watch?v={p['id']}",
                "thumbnail": f"https://i.ytimg.com/vi/{p['id']}/hqdefault.jpg",
                "published": "", "learn": p["learn"], "minutes": p["seconds"] // 60,
            })
        else:
            # Search blocked for this topic: fall back to an unwatched video saved on a good day.
            spare = [v for v in pool.values() if v.get("category") == cat and v["id"] not in seen]
            if spare:
                videos.append(random.Random(day_ordinal).choice(spare))
    for v in videos:
        pool[v["id"]] = v
    used_topics = [by_cat[v["category"]]["topic"] for v in videos if v["category"] in by_cat]
    hist.setdefault("topics", []).extend(used_topics)

    if not videos:
        return {"hero": None, "more": []}
    hero_i = day_ordinal % len(videos)
    return {"hero": videos[hero_i], "more": videos[:hero_i] + videos[hero_i + 1:]}


# ---------------------------------------------------------------- podcasts

SKIP_CHAPTER = re.compile(r"intro|sponsor|\bads?\b|advert|promo|patreon|support the show|welcome|cold open|preview|teaser", re.I)
TIMESTAMP = re.compile(r"\(?\b((?:\d{1,2}:)?\d{1,2}:\d{2})\b\)?\s*[-–—:|]?\s*")


def _strip(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def start_after_intro(description: str) -> int | None:
    """If the show notes list chapters and the first is an intro/sponsor read, start at the first real one."""
    marks = list(TIMESTAMP.finditer(description))
    if len(marks) < 3:
        return None
    chapters = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(description)
        label = description[m.end():end].strip()[:80]
        chapters.append((_hms(m.group(1)), label))
    if not chapters or chapters[0][0] > 60 or not SKIP_CHAPTER.search(chapters[0][1]):
        return None
    for secs, label in chapters[1:]:
        if secs > 40 * 60:
            return None
        if not SKIP_CHAPTER.search(label):
            return secs
    return None


def _duration(s: str) -> int | None:
    if not s:
        return None
    return int(s) if s.isdigit() else (_hms(s) or None)


def podcast_feed(url: str, n: int) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=30).read())
    out = []
    for item in root.iter("item"):
        def get(tag):
            for c in item:
                if c.tag.rsplit("}", 1)[-1] == tag:
                    return c
            return None
        enc = get("enclosure")
        if enc is None or not enc.get("url"):
            continue
        desc = _strip((get("encoded").text if get("encoded") is not None else "")
                      or (get("description").text if get("description") is not None else "")
                      or (get("summary").text if get("summary") is not None else ""))
        dur = get("duration")
        out.append({
            "title": html.unescape((get("title").text or "").strip()) if get("title") is not None else "",
            "audioUrl": enc.get("url"),
            "link": (get("link").text or "").strip() if get("link") is not None else "",
            "durationSec": _duration((dur.text or "").strip()) if dur is not None else None,
            "desc": desc,
        })
        if len(out) >= n:
            break
    return out


def build_podcasts(key: str | None, gemini: Callable, prompt: Callable, shows: list[dict], hist: dict,
                   day_ordinal: int) -> list[dict]:
    seen = set(hist.get("podcasts", []))
    cands = []
    for s in shows:
        try:
            for ep in podcast_feed(s["url"], 5):
                if ep["audioUrl"] in seen:
                    continue
                d = ep["durationSec"]
                if d and not (8 * 60 <= d <= 90 * 60):  # bedtime length: not a 3-hour marathon
                    continue
                cands.append({**ep, "show": s["name"], "category": s["category"]})
        except Exception as e:  # noqa: BLE001
            print(f"  ! podcast feed failed: {s['name']}: {e}", file=sys.stderr)

    chosen: list[tuple[dict, str]] = []
    if key and cands:
        listing = "\n".join(
            f"[{i}] {c['show']}: {c['title']} ({(c['durationSec'] or 0) // 60} min) - {re.sub(r'\s+', ' ', c['desc'])[:220]}"
            for i, c in enumerate(cands))
        got = gemini(prompt("podcastpick", listing=listing), key)
        for g in got if isinstance(got, list) else []:
            if isinstance(g, dict) and isinstance(g.get("ref"), int) and 0 <= g["ref"] < len(cands) \
                    and isinstance(g.get("why"), str):
                chosen.append((cands[g["ref"]], g["why"].strip()))
    if len(chosen) < 2:
        rng = random.Random(day_ordinal)
        rest = [c for c in cands if all(c is not x for x, _ in chosen)]
        rng.shuffle(rest)
        chosen += [(c, "") for c in rest[: 2 - len(chosen)]]

    out = []
    for c, why in chosen[:2]:
        out.append({
            "id": re.sub(r"[^a-z0-9]+", "-", (c["show"] + "-" + c["title"]).lower()).strip("-")[:80],
            "title": c["title"], "show": c["show"], "category": c["category"],
            "audioUrl": c["audioUrl"], "link": c["link"], "durationSec": c["durationSec"],
            "startSec": start_after_intro(c["desc"]), "why": why,
        })
    return out


# ---------------------------------------------------------------- just for fun

def build_fun(shows: list[dict], hist: dict, day_ordinal: int) -> dict | None:
    """One genuinely loved comedy episode/set a day, rotating through popular shows.

    Only the show's official channel (re-uploads are often cut or low quality) and only
    videos with a big audience: a million-plus views is the best public sign an episode
    is good all the way through, not just two clips.
    """
    seen = set(hist.get("videos", []))
    for i in range(len(shows)):
        show = shows[(day_ordinal + i) % len(shows)]
        try:
            results = yt_search(show["query"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! fun search failed for {show['name']}: {e}", file=sys.stderr)
            continue
        ok = [r for r in results
              if show["channel"].lower() in r["channel"].lower() and r["id"] not in seen
              and 420 <= r["seconds"] <= 6000 and r["views"] >= 1_000_000]
        if ok:
            r = max(ok, key=lambda r: r["views"])
            print(f"  fun: {show['name']} ({r['views']:,} views)")
            return {
                "id": r["id"], "title": r["title"], "channel": r["channel"], "category": "fun",
                "url": f"https://www.youtube.com/watch?v={r['id']}",
                "thumbnail": f"https://i.ytimg.com/vi/{r['id']}/hqdefault.jpg",
                "published": "", "learn": "", "minutes": r["seconds"] // 60,
            }
    return None
