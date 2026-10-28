"""The daily 'Read' block: a quote, a page from a book, and the book of the week.

Accuracy rule: nothing quoted is shown unless it's verified against the real book.
  - Classics come straight from Project Gutenberg's text (exact by construction).
  - Anything Gemini quotes from a modern book is checked with Open Library's full-text
    "search inside" (and Google Books if GOOGLE_BOOKS_KEY is set); the hit must be that book.
The book of the week is Gemini's own summary of ideas, not quotes, so it needs no check.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import sys
import urllib.parse
import urllib.request
from typing import Callable

UA = "Mozilla/5.0 (DetourDaily; personal use)"


def _get_json(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _same_book(hit_title: str, hit_authors: list[str], title: str, author: str) -> bool:
    ht, t = _norm(hit_title), _norm(title)
    short_t = " ".join(t.split()[:3])
    surname = _norm(author).split()[-1] if _norm(author) else ""
    title_ok = bool(short_t) and (short_t in ht or ht[:20] in t)
    author_ok = bool(surname) and any(surname in _norm(a) for a in hit_authors)
    return title_ok or (author_ok and bool(ht))


def _chunks(text: str) -> list[str]:
    """Two distinctive 9-word runs (one early, one middle) to search for."""
    words = re.sub(r"[\"“”‘’]", "", text).split()
    if len(words) < 9:
        return [" ".join(words)] if len(words) >= 5 else []
    mid = max(0, len(words) // 2 - 4)
    early = min(3, len(words) - 9)
    return list(dict.fromkeys([" ".join(words[early:early + 9]), " ".join(words[mid:mid + 9])]))


def verify_in_book(text: str, title: str, author: str, books_key: str | None) -> str | None:
    """Return which index confirmed the text appears in that book, or None."""
    for chunk in _chunks(text):
        q = urllib.parse.quote(f'"{chunk}"')
        try:
            hits = _get_json(f"https://openlibrary.org/search/inside.json?q={q}").get("hits", {}).get("hits", [])
            for h in hits[:10]:
                e = h.get("edition", {})
                if _same_book(e.get("title", ""), [a.get("name", "") for a in e.get("authors", [])], title, author):
                    return "openlibrary"
        except Exception as ex:  # noqa: BLE001
            print(f"  ! openlibrary check failed: {ex}", file=sys.stderr)
        if books_key:
            try:
                d = _get_json(f"https://www.googleapis.com/books/v1/volumes?q={q}&maxResults=10&key={books_key}")
                for it in d.get("items", []):
                    v = it.get("volumeInfo", {})
                    if _same_book(v.get("title", ""), v.get("authors", []), title, author):
                        return "googlebooks"
            except Exception as ex:  # noqa: BLE001
                print(f"  ! google books check failed: {ex}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- Gutenberg classics

def gutenberg_paragraphs(gid: int) -> list[str]:
    req = urllib.request.Request(f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt", headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore").replace("\r\n", "\n")
    start = raw.find("*** START OF")
    end = raw.find("*** END OF")
    body = raw[raw.find("\n", start) + 1 if start != -1 else 0: end if end != -1 else len(raw)]
    out = []
    for block in re.split(r"\n\s*\n", body):
        p = re.sub(r"\s+", " ", block).strip().replace("_", "")
        if 350 <= len(p) <= 1000 and not p.isupper() and "gutenberg" not in p.lower() and p[0].isalpha():
            out.append(p)
    return out


def classic_passage(book: dict, used: list[str], key, gemini, prompt, rng: random.Random) -> dict | None:
    """An exact paragraph from a Gutenberg classic; Gemini only chooses which one."""
    try:
        paras = [p for p in gutenberg_paragraphs(book["gutenberg"]) if p[:60] not in used]
    except Exception as ex:  # noqa: BLE001
        print(f"  ! gutenberg fetch failed for {book['title']}: {ex}", file=sys.stderr)
        return None
    if not paras:
        return None
    sample = rng.sample(paras, min(40, len(paras)))
    choice, context = sample[0], ""
    if key:
        listing = "\n\n".join(f"[{i}] {p}" for i, p in enumerate(sample))
        got = gemini(prompt("passage_classic", book=book["title"], author=book["author"], listing=listing), key)
        if isinstance(got, dict) and isinstance(got.get("ref"), int) and 0 <= got["ref"] < len(sample):
            choice = sample[got["ref"]]
            context = str(got.get("context", "")).strip()
    return {"text": choice, "book": book["title"], "author": book["author"], "context": context, "source": "gutenberg"}


# ---------------------------------------------------------------- paragraph of the day

def _pick_book(key, gemini, prompt, st: dict, classics: list[dict], rng: random.Random) -> dict | None:
    done = {_norm(t) for t in st.get("done", [])}
    free_classics = [c for c in classics if _norm(c["title"]) not in done]
    # Every third new book is a free classic (exact text); otherwise Gemini picks anything worth reading.
    if free_classics and (not key or len(st.get("done", [])) % 3 == 2):
        c = rng.choice(free_classics)
        return {**c, "days_planned": c.get("days", 3), "days_used": 0, "used": []}
    if key:
        got = gemini(prompt("bookpick", done="; ".join(st.get("done", [])[-200:]) or "(none)",
                            classics="; ".join(c["title"] for c in free_classics)), key)
        if isinstance(got, dict) and isinstance(got.get("title"), str) and isinstance(got.get("author"), str):
            days = got.get("great_passages", 2)
            days = max(1, min(4, days if isinstance(days, int) else 2))
            match = next((c for c in classics if _norm(c["title"]) == _norm(got["title"])), None)
            book = {**(match or {}), "title": got["title"].strip(), "author": got["author"].strip()}
            return {**book, "days_planned": days, "days_used": 0, "used": []}
    if free_classics:
        c = rng.choice(free_classics)
        return {**c, "days_planned": c.get("days", 3), "days_used": 0, "used": []}
    return None


def _modern_passage(book: dict, key, gemini, prompt, books_key) -> dict | None:
    got = gemini(prompt("passage", book=book["title"], author=book["author"],
                        used="; ".join(book["used"]) or "(none)"), key)
    for c in got if isinstance(got, list) else []:
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or len(c["text"]) < 120:
            continue
        if c["text"][:60] in book["used"]:
            continue
        how = verify_in_book(c["text"], book["title"], book["author"], books_key)
        if how:
            print(f"  read: passage verified via {how}")
            return {"text": c["text"].strip(), "book": book["title"], "author": book["author"],
                    "context": str(c.get("context", "")).strip(), "source": how}
        print(f"  read: candidate not found in '{book['title']}', skipped")
    return None


def build_passage(key, gemini, prompt, hist: dict, classics: list[dict], rng: random.Random, books_key) -> dict | None:
    st = hist.setdefault("books", {"current": None, "done": []})
    for _ in range(3):
        cur = st.get("current")
        if not cur or cur["days_used"] >= cur["days_planned"]:
            if cur:
                st["done"].append(cur["title"])
            cur = _pick_book(key, gemini, prompt, st, classics, rng)
            st["current"] = cur
            if not cur:
                return None
            print(f"  read: new book '{cur['title']}' by {cur['author']} for {cur['days_planned']} day(s)")
        if cur.get("gutenberg"):
            p = classic_passage(cur, cur["used"], key, gemini, prompt, rng)
        elif key:
            p = _modern_passage(cur, key, gemini, prompt, books_key)
        else:
            p = None
        if p:
            cur["days_used"] += 1
            cur["used"].append(p["text"][:60])
            return {**p, "day": cur["days_used"], "days": cur["days_planned"]}
        # Couldn't verify anything from this book: close it and try another.
        print(f"  read: no verifiable passage from '{cur['title']}', moving on")
        st["done"].append(cur["title"])
        st["current"] = None
    return None


# ---------------------------------------------------------------- quote of the day

def _similar(a: str, b: str) -> bool:
    wa, wb = set(_norm(a).split()), set(_norm(b).split())
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) > 0.6


def build_quote(key, gemini, prompt, hist: dict, classics: list[dict], rng: random.Random, books_key) -> dict | None:
    past = hist.setdefault("quotes", [])
    if key:
        got = gemini(prompt("quote", avoid=" | ".join(past[-150:]) or "(none)"), key)
        for q in got if isinstance(got, list) else []:
            if not all(isinstance(q.get(k), str) and q[k].strip() for k in ("text", "author", "book")):
                continue
            if len(q["text"].split()) > 45 or any(_similar(q["text"], p) for p in past):
                continue
            how = verify_in_book(q["text"], q["book"], q["author"], books_key)
            if how:
                print(f"  read: quote verified via {how} ({q['book']})")
                past.append(q["text"][:120])
                return {"text": q["text"].strip(), "author": q["author"].strip(), "book": q["book"].strip()}
    # Fallback: a short, exact line from a classic.
    for c in rng.sample(classics, min(4, len(classics))):
        try:
            text = " ".join(gutenberg_paragraphs(c["gutenberg"]))
        except Exception:  # noqa: BLE001
            continue
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 60 <= len(s.strip()) <= 200]
        sentences = [s for s in sentences if not any(_similar(s, p) for p in past)]
        if not sentences:
            continue
        pick = rng.choice(sentences)
        if key:
            listing = "\n".join(f"[{i}] {s}" for i, s in enumerate(rng.sample(sentences, min(60, len(sentences)))))
            got = gemini(prompt("quote_classic", book=c["title"], author=c["author"], listing=listing), key)
            lines = listing.splitlines()
            if isinstance(got, dict) and isinstance(got.get("ref"), int) and 0 <= got["ref"] < len(lines):
                pick = lines[got["ref"]].split("] ", 1)[1]
        past.append(pick[:120])
        print(f"  read: quote from classic ({c['title']})")
        return {"text": pick, "author": c["author"], "book": c["title"]}
    return None


# ---------------------------------------------------------------- book of the week

def _cover(title: str, author: str) -> str:
    try:
        q = urllib.parse.urlencode({"title": title, "author": author, "limit": 1, "fields": "cover_i"})
        docs = _get_json(f"https://openlibrary.org/search.json?{q}").get("docs", [])
        if docs and docs[0].get("cover_i"):
            return f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-M.jpg"
    except Exception as ex:  # noqa: BLE001
        print(f"  ! cover lookup failed: {ex}", file=sys.stderr)
    return ""


def build_week_book(key, gemini, prompt, hist: dict, day: dt.date) -> dict | None:
    wb = hist.get("weekbook")
    start = dt.date.fromisoformat(wb["start"]) if wb else None
    if not wb or (day - start).days >= 7 or (day.weekday() == 0 and day != start):
        if not key:
            return None
        done = hist.setdefault("weekbooks_done", [])
        got = gemini(prompt("weekbook", done="; ".join(done[-100:]) or "(none)"), key)
        if not (isinstance(got, dict) and isinstance(got.get("ideas"), list) and len(got["ideas"]) >= 7
                and isinstance(got.get("title"), str) and isinstance(got.get("author"), str)):
            return None
        ideas = [{"title": str(i.get("title", "")).strip(), "text": str(i.get("text", "")).strip()}
                 for i in got["ideas"][:7] if isinstance(i, dict)]
        if len(ideas) < 7 or not all(i["title"] and i["text"] for i in ideas):
            return None
        wb = {"title": got["title"].strip(), "author": got["author"].strip(), "start": day.isoformat(),
              "cover": _cover(got["title"], got["author"]), "ideas": ideas}
        hist["weekbook"] = wb
        done.append(wb["title"])
        print(f"  read: book of the week '{wb['title']}'")
    n = min(7, (day - dt.date.fromisoformat(wb["start"])).days + 1)
    return {"title": wb["title"], "author": wb["author"], "cover": wb["cover"], "day": n, "ideas": wb["ideas"]}


def build_read(key, gemini: Callable, prompt: Callable, hist: dict, classics: list[dict], day: dt.date,
               books_key: str | None) -> dict:
    rng = random.Random(f"read-{day.isoformat()}")
    out: dict = {}
    for name, fn in (("quote", lambda: build_quote(key, gemini, prompt, hist, classics, rng, books_key)),
                     ("passage", lambda: build_passage(key, gemini, prompt, hist, classics, rng, books_key)),
                     ("weekBook", lambda: build_week_book(key, gemini, prompt, hist, day))):
        try:
            v = fn()
            if v:
                out[name] = v
        except Exception as ex:  # noqa: BLE001 - one part failing must not block the day
            print(f"  ! read {name} failed: {ex}", file=sys.stderr)
    return out
