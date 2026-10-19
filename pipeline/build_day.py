"""Build one day of Detour content.

Fetches news + video + podcast feeds, asks Gemini for words / loop cards / news
bullets, validates the result and writes content/latest.json plus a dated copy.

Runs with the standard library only. If GEMINI_API_KEY is missing or Gemini
fails, words and loop cards come from pipeline/seed/ and news falls back to
raw headlines, so the app always gets a usable day.

    python pipeline/build_day.py            # build today (IST)
    python pipeline/build_day.py --dry-run  # print, don't write
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import learn  # noqa: E402
import trending  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIPE = ROOT / "pipeline"
CONTENT = ROOT / "content"
HISTORY = PIPE / "history.json"
VIDEO_POOL = PIPE / "video_pool.json"   # every candidate seen on good days; used when YouTube refuses us
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = "Mozilla/5.0 (DetourDaily; personal use)"

WORDS_PER_DAY = 9
LOOP_PER_DAY = 8
LOOP_TYPES = ["slang", "psych", "acronym", "paradox", "meme"]
WORD_THEMES = [
    "describing people and personalities", "arguments, debate and persuasion", "work, ambition and productivity",
    "emotions and moods", "money, value and deals", "friendship, dating and social life", "thinking, ideas and logic",
    "criticism and disagreement", "change, progress and decline", "speaking and writing well", "honesty, deception and trust",
    "intensity, size and amount", "time, speed and pace", "success, failure and luck", "humour, wit and teasing",
    "power, leadership and influence", "health, energy and the body", "beauty, style and taste",
]
LOOP_ANGLES = [
    "the newest Gen Z and Gen Alpha internet slang of 2025-2026", "dating apps and modern relationship terms",
    "gaming and streamer culture", "money, crypto and hustle-culture lingo",
    "therapy-speak and mental-health terms people use casually", "Indian internet slang and desi meme culture",
    "office and corporate jargon young people joke about", "fitness, looks and self-improvement culture",
    "pop culture, anime and fandom references", "cognitive biases and psychology effects",
    "tech, startup and AI buzzwords", "philosophy thought experiments and famous paradoxes",
    "viral meme formats and reaction phrases",
]
INDIA_BULLETS = (8, 10)
WORLD_BULLETS = (5, 6)


# ---------------------------------------------------------------- fetching

def fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _date(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def parse_feed(raw: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom into a list of plain dicts."""
    root = ET.fromstring(raw)
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for it in items:
        link = _text(it, "link")
        if not link:
            le = _child(it, "link")
            link = le.get("href", "") if le is not None else ""
        enclosure = _child(it, "enclosure")
        thumb = ""
        group = _child(it, "group")  # YouTube media:group
        if group is not None:
            t = _child(group, "thumbnail")
            thumb = t.get("url", "") if t is not None else ""
        out.append({
            "title": _clean(_text(it, "title")),
            "link": link.strip(),
            "summary": _clean(_text(it, "description") or _text(it, "summary"))[:400],
            "date": _date(_text(it, "pubDate") or _text(it, "published") or _text(it, "updated")),
            "videoId": _text(it, "videoId"),
            "audio": enclosure.get("url", "") if enclosure is not None else "",
            "duration": _text(it, "duration"),
            "thumb": thumb,
        })
    return out


def fetch_many(jobs: dict[str, str]) -> dict[str, list[dict]]:
    """Fetch + parse many feeds in parallel. Failures become empty lists."""
    def one(key):
        try:
            return key, parse_feed(fetch(jobs[key]))
        except Exception as e:  # noqa: BLE001 - one bad feed must not kill the day
            print(f"  ! feed failed: {key}: {e}", file=sys.stderr)
            return key, []
    with cf.ThreadPoolExecutor(12) as ex:
        return dict(ex.map(one, jobs))


def fetch_channel(cid: str) -> list[dict]:
    """A channel's recent long-form uploads.

    YouTube intermittently 404s requests from data-centre IPs (GitHub runners), so retry with
    backoff and fall back to the plain channel feed (which includes Shorts; those are dropped).
    """
    urls = [
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{cid[2:]}",
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
    ]
    last: Exception | None = None
    for attempt in range(3):
        for url in urls:
            try:
                return [i for i in parse_feed(fetch(url)) if "/shorts/" not in i["link"]]
            except Exception as e:  # noqa: BLE001
                last = e
        time.sleep(3 * (attempt + 1))
    raise last or RuntimeError("no feed")


# ---------------------------------------------------------------- history

def load_history() -> dict:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"words": [], "loop": [], "videos": [], "podcasts": [], "topics": []}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- gemini

def gemini(prompt: str, key: str) -> dict | list | None:
    # Aliases track current models. Flash-Lite first: on the free tier Flash is often overloaded or out of quota.
    models = [os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest"), "gemini-flash-latest"]
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
        ],
    }
    for model in dict.fromkeys(models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = json.loads(r.read())
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                got = json.loads(text)
                # Sometimes the array comes wrapped, e.g. {"words": [...]}.
                if isinstance(got, dict) and len(got) == 1 and isinstance(next(iter(got.values())), list):
                    got = next(iter(got.values()))
                return got
            except urllib.error.HTTPError as e:
                print(f"  ! gemini {model} attempt {attempt + 1}: {e}", file=sys.stderr)
                if e.code in (404, 429):
                    break  # retired model, or its free daily quota is used up: go to the next model
                if e.code in (500, 503):
                    time.sleep(10 * (attempt + 1))  # briefly overloaded; worth a retry
            except Exception as e:  # noqa: BLE001
                print(f"  ! gemini {model} attempt {attempt + 1}: {e}", file=sys.stderr)
    return None


def prompt(name: str, **kw) -> str:
    text = (PIPE / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ---------------------------------------------------------------- sections

def _oldest_first(items: list[dict], key, history: list[str]) -> list[dict]:
    """Items never shown first (shuffled by caller), then the ones shown longest ago."""
    order = {k: i for i, k in enumerate(history)}  # later index = shown more recently
    return sorted(items, key=lambda it: order.get(key(it), -1))


def build_words(key: str | None, hist: dict, rng: random.Random) -> list[dict]:
    seen = set(hist["words"])
    words: list[dict] = []
    if key:
        # Gemini Lite keeps suggesting the same "safe" words, so ask for many candidates around a
        # different theme each day, keep only unseen ones, and ask once more if too few are new.
        themes = rng.sample(WORD_THEMES, 2)
        for attempt, theme in enumerate(themes):
            letters = ", ".join(rng.sample("BCDEFGHILMNOPRSTV", 3))
            shown = [t.replace("-", " ") for t in hist["words"][-500:]] + [w["word"] for w in words]
            got = gemini(prompt("words", count="30", focus=theme, letters=letters, avoid=", ".join(shown) or "(none)"), key)
            fresh = 0
            for w in got if isinstance(got, list) else []:
                if len(words) >= WORDS_PER_DAY:
                    break
                if valid_word(w) and slug(w["word"]) not in seen and all(slug(w["word"]) != slug(x["word"]) for x in words):
                    words.append(w)
                    fresh += 1
            print(f"  gemini words ({theme}): {fresh} new")
            if fresh == 0:
                print(f"  ! gemini words sample: {json.dumps(got, ensure_ascii=False)[:300]}", file=sys.stderr)
            if len(words) >= 5:
                break
    if len(words) < WORDS_PER_DAY:
        # Top up from the starter list: unseen first, then the ones shown longest ago. Never come up short.
        seed = json.loads((PIPE / "seed" / "words.json").read_text(encoding="utf-8"))
        rng.shuffle(seed)
        have = {slug(w["word"]) for w in words}
        for w in _oldest_first(seed, lambda w: slug(w["word"]), hist["words"]):
            if len(words) >= WORDS_PER_DAY:
                break
            if slug(w["word"]) not in have:
                words.append(w)
                have.add(slug(w["word"]))
    return words[:WORDS_PER_DAY]


def valid_word(w) -> bool:
    """Accept a word if it has the essentials; fix up key-name variants Gemini sometimes uses."""
    if not isinstance(w, dict):
        return False
    for alt in ("instead_of", "insteadof", "replaces", "plainWord", "plain_word"):
        if alt in w and not w.get("insteadOf"):
            w["insteadOf"] = w.pop(alt)
    if "definition" in w and not w.get("meaning"):
        w["meaning"] = w.pop("definition")
    w.setdefault("insteadOf", "")
    return all(isinstance(w.get(k), str) and w[k].strip() for k in ("word", "meaning", "example"))


def build_loop(key: str | None, hist: dict, rng: random.Random) -> list[dict]:
    seen = set(hist["loop"])
    cards: list[dict] = []
    if key:
        # Same trick as words: a fresh angle each day and a big candidate pool, then filter out repeats.
        angles = rng.sample(LOOP_ANGLES, 2)
        for angle in angles:
            shown = [t.replace("-", " ") for t in hist["loop"][-700:]] + [c["term"] for c in cards]
            got = gemini(prompt("loop", count="24", focus=angle, avoid=", ".join(shown) or "(none)"), key)
            fresh: list[dict] = []
            for c in got if isinstance(got, list) else []:
                if valid_card(c) and slug(c["term"]) not in seen and all(slug(c["term"]) != slug(x["term"]) for x in cards + fresh):
                    fresh.append(c)
            # Spread across types: round-robin until the deck is full.
            by_type = {t: [c for c in fresh if c["type"] == t] for t in LOOP_TYPES}
            while len(cards) < LOOP_PER_DAY and any(by_type.values()):
                for t in LOOP_TYPES:
                    if by_type[t] and len(cards) < LOOP_PER_DAY:
                        cards.append(by_type[t].pop(0))
            print(f"  gemini loop cards ({angle}): {len(fresh)} new candidates, deck now {len(cards)}")
            if not fresh:
                print(f"  ! gemini loop sample: {json.dumps(got, ensure_ascii=False)[:300]}", file=sys.stderr)
            if len(cards) >= 6:
                break
    if len(cards) < LOOP_PER_DAY:
        # Top up from the starter cards, round-robin across types for variety; unseen first, then oldest.
        seed = json.loads((PIPE / "seed" / "loop.json").read_text(encoding="utf-8"))
        rng.shuffle(seed)
        have = {slug(c["term"]) for c in cards}
        ordered = [c for c in _oldest_first(seed, lambda c: slug(c["term"]), hist["loop"]) if slug(c["term"]) not in have]
        by_type = {t: [c for c in ordered if c["type"] == t] for t in LOOP_TYPES}
        while len(cards) < LOOP_PER_DAY and any(by_type.values()):
            for t in LOOP_TYPES:
                if by_type[t] and len(cards) < LOOP_PER_DAY:
                    cards.append(by_type[t].pop(0))
    for c in cards:
        c["id"] = slug(c["term"])
        c["nsfw"] = bool(c.get("nsfw", False))
        c.setdefault("tone", "casual")
    return cards[:LOOP_PER_DAY]


def valid_card(c) -> bool:
    return (isinstance(c, dict) and c.get("type") in LOOP_TYPES
            and all(isinstance(c.get(k), str) and c[k].strip()
                    for k in ("term", "meaning", "example", "context")))


def _similar(a: str, b: str) -> bool:
    wa = {w for w in re.findall(r"[a-z]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) > 0.5


def headline_pool(feeds: dict[str, list[dict]], names: dict[str, str], now: dt.datetime) -> list[dict]:
    """Recent headlines, round-robin across sources, near-duplicates removed."""
    per_src = []
    for key, items in feeds.items():
        fresh = [i for i in items if i["title"] and (i["date"] is None or now - i["date"] < dt.timedelta(hours=36))]
        per_src.append([{**i, "source": names[key]} for i in fresh[:25]])
    pool: list[dict] = []
    for row in range(25):
        for src in per_src:
            if row < len(src) and not any(_similar(src[row]["title"], p["title"]) for p in pool):
                pool.append(src[row])
    return pool


def build_news(key: str | None, sources: dict, now: dt.datetime) -> dict:
    out = {}
    for region, (lo, hi) in (("india", INDIA_BULLETS), ("world", WORLD_BULLETS)):
        srcs = sources["news"][region]
        feeds = fetch_many({f"{region}{i}": s["url"] for i, s in enumerate(srcs)})
        names = {f"{region}{i}": s["name"] for i, s in enumerate(srcs)}
        pool = headline_pool(feeds, names, now)[:45]
        bullets: list[dict] = []
        if key and pool:
            listing = "\n".join(f"[{i}] ({p['source']}) {p['title']}: {p['summary'][:220]}"
                                for i, p in enumerate(pool))
            got = gemini(prompt("news", region="India" if region == "india" else "the world (outside India)",
                                lo=str(lo), hi=str(hi), headlines=listing), key)
            for b in (got or []):
                if isinstance(b, dict) and isinstance(b.get("text"), str) and isinstance(b.get("ref"), int) \
                        and 0 <= b["ref"] < len(pool):
                    p = pool[b["ref"]]
                    bullets.append({"text": b["text"].strip(), "source": p["source"], "url": p["link"]})
        if len(bullets) < lo:
            bullets = [{"text": p["title"], "source": p["source"], "url": p["link"]} for p in pool[:hi]]
        out[region] = bullets[:hi]
    return out


def build_videos(sources: dict, hist: dict, day: dt.date, now: dt.datetime, pool: dict) -> dict:
    chans = sources["channels"]

    def one(c):
        try:
            return c["id"], fetch_channel(c["id"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! youtube failed: {c['name']}: {e}", file=sys.stderr)
            return c["id"], []
    with cf.ThreadPoolExecutor(4) as ex:  # gentle; bursts get blocked sooner
        feeds = dict(ex.map(one, chans))
    ok = sum(1 for v in feeds.values() if v)
    print(f"  youtube: {ok}/{len(chans)} channels fetched")
    seen = set(hist["videos"])
    by_cat: dict[str, list[dict]] = {}
    for c in chans:
        for it in feeds.get(c["id"], []):
            vid = it["videoId"]
            if not vid or vid in seen or it["date"] is None or now - it["date"] > dt.timedelta(days=150):
                continue
            if NOISE.search(it["title"]):
                continue
            by_cat.setdefault(c["category"], []).append({
                "id": vid,
                "title": it["title"],
                "channel": c["name"],
                "category": c["category"],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "published": it["date"].date().isoformat(),
            })
    # Remember every candidate; on a morning YouTube refuses us, unwatched ones from the pool fill in.
    for vids in by_cat.values():
        for v in vids:
            pool[v["id"]] = v
    cutoff = (now - dt.timedelta(days=150)).date().isoformat()
    for vid in [k for k, v in pool.items() if v.get("published", "") < cutoff]:
        del pool[vid]
    for cat in sources["categories"]:
        if not by_cat.get(cat):
            spare = [v for v in pool.values() if v["category"] == cat and v["id"] not in seen]
            if spare:
                print(f"  using {len(spare)} pooled videos for {cat}")
                by_cat[cat] = spare
    for vids in by_cat.values():
        # freshest first, but at most 2 per channel so one prolific channel can't dominate
        vids.sort(key=lambda v: v["published"], reverse=True)
        count: dict[str, int] = {}
        capped = []
        for v in vids:
            count[v["channel"]] = count.get(v["channel"], 0) + 1
            if count[v["channel"]] <= 2:
                capped.append(v)
        vids[:] = capped
    # One video per category, every day, so each topic gets a slot. The hero rotates through them.
    cats = sources["categories"]
    picks = {}
    for cat in cats:
        if by_cat.get(cat):
            # a little randomness inside the category so it isn't always the newest upload
            options = by_cat[cat][:6]
            picks[cat] = random.Random(f"{day.toordinal()}-{cat}").choice(options)
    if not picks:
        return {"hero": None, "more": []}
    available = [c for c in cats if c in picks]
    hero_cat = available[day.toordinal() % len(available)]
    return {"hero": picks[hero_cat], "more": [picks[c] for c in available if c != hero_cat]}


INDIA = re.compile(
    r"\b(India|Indian|Delhi|Mumbai|Bombay|Calcutta|Kolkata|Madras|Chennai|Bangalore|Bengaluru|Hyderabad|Mughal|"
    r"British Raj|Bengal|Bengali|Punjab|Punjabi|Gandhi|Nehru|Maratha|Sikh|Kashmir|Goa|Kerala|Gujarat|Bihar|Assam|"
    r"Karnataka|Mysore|Akbar|Ashoka|Chola|Vijayanagara|ISRO|Tamil|Hindi|Marathi|Telugu|Rajput|Nizam)\b")


def build_on_this_day(key: str | None, day: dt.date) -> list[dict]:
    """Up to 3 moments from Indian history plus 2 from the world, from Wikipedia's On This Day."""
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/all/{day.month:02d}/{day.day:02d}"
    try:
        data = json.loads(fetch(url))
    except Exception as e:  # noqa: BLE001
        print(f"  ! on this day failed: {e}", file=sys.stderr)
        return []

    def item(e: dict, kind: str) -> dict:
        page = (e.get("pages") or [{}])[0]
        return {
            "year": e.get("year"),
            "kind": kind,
            "text": e["text"],
            "url": page.get("content_urls", {}).get("mobile", {}).get("page", ""),
            "about": " ".join((p.get("description") or "") for p in (e.get("pages") or [])[:2]),
        }

    india, world = [], []
    for kind, key_ in (("event", "selected"), ("event", "events"), ("born", "births"), ("died", "deaths")):
        for e in data.get(key_, []):
            if not isinstance(e.get("year"), int) or not e.get("text"):
                continue
            it = item(e, kind)
            if INDIA.search(it["text"] + " " + it["about"]):
                if not any(x["text"] == it["text"] for x in india):
                    india.append(it)
            elif kind == "event" and key_ == "selected":
                world.append(it)

    picked: list[dict] | None = None
    if key and (india or world):
        listing = "\n".join(f"[{i}] ({x['kind']}, {x['year']}) {x['text']}" for i, x in enumerate(india + world))
        got = gemini(prompt("onthisday", items=listing, india=str(len(india))), key)
        pool = india + world
        if isinstance(got, list):
            picked = []
            for g in got:
                if isinstance(g, dict) and isinstance(g.get("ref"), int) and 0 <= g["ref"] < len(pool) \
                        and isinstance(g.get("text"), str):
                    src = pool[g["ref"]]
                    picked.append({**src, "text": g["text"].strip(), "india": g["ref"] < len(india)})
    if not picked:
        # Events beat birthdays; older beats newer, so it reads like history rather than trivia.
        india.sort(key=lambda x: (x["kind"] != "event", x["year"]))
        picked = [{**x, "india": True} for x in india[:3]] + [{**x, "india": False} for x in world[:2]]
    for x in picked:
        x.pop("about", None)
    return picked[:5]


# Titles that are promos, live streams or daily news roundups rather than something worth watching.
NOISE = re.compile(r"\b(live|livestream|current affairs|trailer|promo|teaser|premieres?|every monday)\b|#shorts", re.I)


def _seconds(d: str) -> int | None:
    if not d:
        return None
    if d.isdigit():
        return int(d)
    parts = [int(p) for p in d.split(":") if p.isdigit()]
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs or None


def build_podcasts(sources: dict, hist: dict, day: dt.date) -> list[dict]:
    pods = sources["podcasts"]
    feeds = fetch_many({p["name"]: p["url"] for p in pods})
    seen = set(hist["podcasts"])
    start = day.toordinal() % len(pods)
    out = []
    for p in pods[start:] + pods[:start]:
        for it in feeds.get(p["name"], [])[:15]:
            if it["audio"] and it["audio"] not in seen:
                out.append({
                    "id": slug(p["name"] + "-" + it["title"])[:80],
                    "title": it["title"],
                    "show": p["name"],
                    "category": p["category"],
                    "audioUrl": it["audio"],
                    "link": it["link"],
                    "durationSec": _seconds(it["duration"]),
                })
                break
        if len(out) == 2:
            break
    return out


# ---------------------------------------------------------------- main

def with_fun(videos: dict, fun: dict | None) -> dict:
    """The fun pick goes last in Watch and is never the day's main video."""
    if fun:
        videos["more"] = videos["more"] + [fun]
    return videos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-if-done", action="store_true", help="exit if today's content already exists")
    ap.add_argument("--videos-only", action="store_true", help="refresh only today's videos, keep everything else")
    ap.add_argument("--refresh-cards", action="store_true", help="on a same-day rerun, regenerate words and loop cards too")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    # The day turns over at 06:00 IST, matching the app: a 01:00 run still belongs to yesterday.
    day = (now.astimezone(IST) - dt.timedelta(hours=6)).date()
    if args.skip_if_done and (CONTENT / "latest.json").exists():
        if json.loads((CONTENT / "latest.json").read_text(encoding="utf-8")).get("date") == day.isoformat():
            print(f"{day} already built; nothing to do")
            return 0
    key = os.environ.get("GEMINI_API_KEY") or None
    rng = random.Random(day.toordinal())
    sources = json.loads((PIPE / "sources.json").read_text(encoding="utf-8"))
    hist = load_history()
    print(f"Building {day} (gemini: {'on' if key else 'off'})")

    # A second run on the same day keeps the words and cards already shown; news, videos and history refresh.
    prev = None
    if (CONTENT / "latest.json").exists():
        prev = json.loads((CONTENT / "latest.json").read_text(encoding="utf-8"))
        if prev.get("date") != day.isoformat():
            prev = None
    keep_cards = bool(prev) and not args.refresh_cards
    pool = json.loads(VIDEO_POOL.read_text(encoding="utf-8")) if VIDEO_POOL.exists() else {}
    if args.videos_only:
        if not prev:
            print("--videos-only needs today's content to exist already", file=sys.stderr)
            return 1
        content = {**prev, "generatedAt": now.isoformat(timespec="seconds"),
                   "videos": learn.build_videos(key, gemini, prompt, hist, day.toordinal(), sources["categories"], pool)}
    else:
        content = {
            "schema": 1,
            "date": day.isoformat(),
            "generatedAt": now.isoformat(timespec="seconds"),
            "words": prev["words"] if keep_cards else build_words(key, hist, rng),
            "loop": prev["loop"] if keep_cards else build_loop(key, hist, rng),
            "news": build_news(key, sources, now),
            "videos": with_fun(learn.build_videos(key, gemini, prompt, hist, day.toordinal(), sources["categories"], pool),
                               learn.build_fun(sources.get("fun", []), hist, day.toordinal())),
            "podcasts": learn.build_podcasts(key, gemini, prompt, sources["podcasts"], hist, day.toordinal()),
            "onThisDay": build_on_this_day(key, day),
            "trending": trending.build_trending(key, gemini, prompt),
        }

    problems = validate(content)
    if problems:
        print("Validation failed:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    summary = {k: len(v) if isinstance(v, list) else None for k, v in content.items()}
    print(f"  words={summary['words']} loop={summary['loop']} "
          f"india={len(content['news']['india'])} world={len(content['news']['world'])} "
          f"videos={1 + len(content['videos']['more'])} podcasts={summary['podcasts']} "
          f"onThisDay={len(content['onThisDay'])}")

    if args.dry_run:
        print(json.dumps(content, indent=2, ensure_ascii=False)[:3000])
        return 0

    CONTENT.mkdir(exist_ok=True)
    text = json.dumps(content, indent=2, ensure_ascii=False)
    (CONTENT / "latest.json").write_text(text, encoding="utf-8")
    (CONTENT / f"{day.isoformat()}.json").write_text(text, encoding="utf-8")

    if not keep_cards and not args.videos_only:
        hist["words"] += [slug(w["word"]) for w in content["words"]]
        hist["loop"] += [c["id"] for c in content["loop"]]
    hist["videos"] += [v["id"] for v in [content["videos"]["hero"], *content["videos"]["more"]] if v]
    if not args.videos_only:
        hist["podcasts"] += [p["audioUrl"] for p in content["podcasts"]]
    HISTORY.write_text(json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8")
    VIDEO_POOL.write_text(json.dumps(pool, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote content/latest.json and content/{day.isoformat()}.json")
    return 0


def validate(c: dict) -> list[str]:
    """Only an empty day blocks publishing; a thin section is better than yesterday's content."""
    thin = []
    for name, n, want in (("words", len(c["words"]), 5), ("loop cards", len(c["loop"]), 5),
                          ("India bullets", len(c["news"]["india"]), 3), ("world bullets", len(c["news"]["world"]), 3)):
        if n < want:
            thin.append(f"only {n} {name}")
    if not c["videos"]["hero"]:
        thin.append("no videos")
    if thin:
        print("  ! thin today (publishing anyway): " + ", ".join(thin), file=sys.stderr)
    empty = not c["words"] and not c["loop"] and not c["news"]["india"] and not c["news"]["world"]
    return ["nothing at all to publish"] if empty else []


if __name__ == "__main__":
    sys.exit(main())
