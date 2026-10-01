#!/usr/bin/env python3
"""
AI Paper Digest
---------------
Fetches the latest AI/ML papers from arXiv, ranks them by keyword relevance,
summarizes the top picks with an OpenAI-compatible LLM endpoint, and writes:

  digests/YYYY/MM/YYYY-MM-DD.md   full daily digest
  data/papers.csv                 cumulative dataset of every paper included
  data/seen.json                  arXiv IDs already processed (deduplication)
  README.md                       "latest digest" section + running stats

If nothing new is found, no files change, so the workflow makes no commit.
Standard library only; no third-party dependencies.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

DATA_DIR = ROOT / "data"
DIGEST_DIR = ROOT / "digests"
SEEN_FILE = DATA_DIR / "seen.json"
CSV_FILE = DATA_DIR / "papers.csv"
README = ROOT / "README.md"

ARXIV_API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "ai-paper-digest/1.0 (+https://github.com/arielshakaramiro/ai-paper-digest)"

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL") or "openai/gpt-oss-20b"
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low").strip()

README_START = "<!-- LATEST_DIGEST_START -->"
README_END = "<!-- LATEST_DIGEST_END -->"
SEEN_LIMIT = 5000
CSV_FIELDS = ["date", "arxiv_id", "title", "authors", "categories", "published", "score", "url"]

# Output languages. The first entry in config "languages" is the primary digest
# (YYYY-MM-DD.md); every other language gets YYYY-MM-DD.<suffix>.md.
LANGS = {
    "en": {
        "name": "English",
        "suffix": "",
        "label": "English",
        "flag": "🇬🇧",
        "why": "Why it matters",
        "heading": "AI Paper Digest",
        "intro": "Top {n} of {total} new papers in {cats}, ranked by relevance.",
        "authors": "Authors",
        "categories": "Categories",
        "published": "Published",
        "relevance": "Relevance",
        "more": "more",
        "abstract": "Abstract",
        "full": "Read the full digest",
    },
    "id": {
        "name": "Indonesian (Bahasa Indonesia)",
        "suffix": ".id",
        "label": "Bahasa Indonesia",
        "flag": "🇮🇩",
        "why": "Kenapa penting",
        "heading": "Ringkasan Paper AI",
        "intro": "{n} teratas dari {total} paper baru di {cats}, diurutkan berdasarkan relevansi.",
        "authors": "Penulis",
        "categories": "Kategori",
        "published": "Terbit",
        "relevance": "Relevansi",
        "more": "lainnya",
        "abstract": "Abstrak",
        "full": "Baca digest lengkap",
    },
}


def configured_languages() -> list[str]:
    langs = CONFIG.get("languages")
    if not langs:  # backward compatible with the old single "language" setting
        langs = ["id" if CONFIG.get("language", "").lower().startswith(("indo", "bahasa")) else "en"]
    unknown = [code for code in langs if code not in LANGS]
    if unknown:
        raise SystemExit(f"Unknown language code(s) in config: {unknown}. Supported: {list(LANGS)}")
    return langs


def digest_path_for(date_str: str, lang: str) -> Path:
    y, m, _ = date_str.split("-")
    return DIGEST_DIR / y / m / f"{date_str}{LANGS[lang]['suffix']}.md"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_request(url: str, data: bytes | None = None, headers: dict | None = None,
                 timeout: int = 60, retries: int = 3) -> str:
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 4xx (except 429) will not fix itself on retry
            if 400 <= e.code < 500 and e.code != 429:
                body = e.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"HTTP {e.code}: {body}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        time.sleep(5 * attempt)
    raise RuntimeError(f"Request failed after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------- #
# arXiv
# --------------------------------------------------------------------------- #
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        id_url = entry.findtext(f"{ATOM}id", "").strip()
        if "/abs/" not in id_url:
            continue
        base_id = re.sub(r"v\d+$", "", id_url.rsplit("/abs/", 1)[-1])
        papers.append({
            "id": base_id,
            "title": clean(entry.findtext(f"{ATOM}title", "")),
            "abstract": clean(entry.findtext(f"{ATOM}summary", "")),
            "authors": [clean(a.findtext(f"{ATOM}name", "")) for a in entry.findall(f"{ATOM}author")],
            "categories": [c.get("term", "") for c in entry.findall(f"{ATOM}category")],
            "published": entry.findtext(f"{ATOM}published", "")[:10],
            "url": f"https://arxiv.org/abs/{base_id}",
        })
    return papers


def fetch_papers(categories: list[str], limit: int) -> list[dict]:
    query = " OR ".join(f"cat:{c}" for c in categories)
    params = urllib.parse.urlencode({
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": limit,
    })
    return parse_feed(http_request(f"{ARXIV_API}?{params}"))


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
def score_paper(paper: dict, keywords: dict[str, int]) -> int:
    text = f"{paper['title']} {paper['title']} {paper['abstract']}".lower()  # title counts double
    score = 0
    for kw, weight in keywords.items():
        if re.search(rf"\b{re.escape(kw.lower())}\b", text):
            score += weight
    return score


# --------------------------------------------------------------------------- #
# Summarization
# --------------------------------------------------------------------------- #
def fallback_summary(abstract: str, sentences: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", abstract)
    return " ".join(parts[:sentences])


def llm_summarize(paper: dict, lang: str) -> str | None:
    if not LLM_API_KEY:
        return None
    meta = LANGS[lang]
    extra = ""
    if lang == "id":
        extra = ("Write natural, formal-but-friendly Indonesian. Keep established technical terms "
                 "in English (e.g. fine-tuning, benchmark, retrieval, agent, LLM).\n")
    prompt = (
        f"Summarize this research paper for busy AI engineers. Write the answer in {meta['name']}.\n"
        f"{extra}"
        "Format exactly (keep the labels as written, nothing else):\n"
        "TL;DR: <2 sentences, plain language, no hype>\n"
        f"{meta['why']}: <1 sentence on the practical relevance>\n\n"
        f"Title: {paper['title']}\n\nAbstract: {paper['abstract']}"
    )
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1024,
    }
    if LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = LLM_REASONING_EFFORT

    def call(body: dict) -> str:
        raw = http_request(
            f"{LLM_BASE_URL}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            timeout=90,
        )
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

    try:
        return call(payload) or None
    except RuntimeError as e:
        # Some providers reject reasoning_effort; retry once without it.
        if "reasoning_effort" in payload and "HTTP 400" in str(e):
            payload.pop("reasoning_effort")
            try:
                return call(payload) or None
            except Exception as e2:  # noqa: BLE001
                print(f"  ! LLM failed for {paper['id']}: {e2}", file=sys.stderr)
                return None
        print(f"  ! LLM failed for {paper['id']}: {e}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  ! LLM failed for {paper['id']}: {e}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def load_seen() -> list[str]:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return []


def save_seen(seen: list[str]) -> None:
    SEEN_FILE.write_text(json.dumps(seen[-SEEN_LIMIT:], indent=0) + "\n", encoding="utf-8")


def append_csv(rows: list[dict]) -> int:
    new_file = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)
    with CSV_FILE.open(encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


def format_authors(authors: list[str], more: str = "more", limit: int = 4) -> str:
    if len(authors) <= limit:
        return ", ".join(authors)
    return ", ".join(authors[:limit]) + f" +{len(authors) - limit} {more}"


def format_summary(text: str) -> str:
    # Trim each line and keep line breaks when the Markdown is rendered.
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return "  \n".join(lines)


def write_digest(date_str: str, picks: list[dict], total_fetched: int, lang: str,
                 all_langs: list[str]) -> Path:
    t = LANGS[lang]
    path = digest_path_for(date_str, lang)
    path.parent.mkdir(parents=True, exist_ok=True)

    switch = " · ".join(
        f"{LANGS[c]['flag']} {LANGS[c]['label']}" if c == lang
        else f"[{LANGS[c]['flag']} {LANGS[c]['label']}]({digest_path_for(date_str, c).name})"
        for c in all_langs
    )
    lines = [f"# {t['heading']} — {date_str}", ""]
    if len(all_langs) > 1:
        lines += [switch, ""]
    lines += [
        t["intro"].format(n=len(picks), total=total_fetched, cats=", ".join(CONFIG["categories"])),
        "",
    ]
    for i, p in enumerate(picks, 1):
        lines += [
            f"## {i}. [{p['title']}]({p['url']})",
            "",
            f"**{t['authors']}:** {format_authors(p['authors'], t['more'])}  ",
            f"**{t['categories']}:** {', '.join(p['categories'][:4])} · **{t['published']}:** {p['published']} "
            f"· **{t['relevance']}:** {p['score']}",
            "",
            format_summary(p["summaries"][lang]),
            "",
            f"[{t['abstract']}]({p['url']}) · [PDF](https://arxiv.org/pdf/{p['id']})",
            "",
            "---",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def update_readme(date_str: str, digest_paths: dict[str, Path], picks: list[dict],
                  total_papers: int) -> None:
    if not README.exists():
        return
    text = README.read_text(encoding="utf-8")
    if README_START not in text or README_END not in text:
        return
    # one digest per day, counted once regardless of how many languages it has
    digest_count = len({f.name.split(".")[0] for f in DIGEST_DIR.rglob("*.md")})
    preview = picks[: CONFIG.get("readme_preview", 5)]
    links = " · ".join(
        f"{LANGS[c]['flag']} [{LANGS[c]['full']}]({path.relative_to(ROOT).as_posix()})"
        for c, path in digest_paths.items()
    )
    block = [
        README_START,
        f"### 📅 {date_str}",
        "",
        *[f"{i}. [{p['title']}]({p['url']})" for i, p in enumerate(preview, 1)],
        "",
        f"➡️ {links}",
        "",
        f"**Stats:** {digest_count} digests · {total_papers} papers archived · last updated {date_str}",
        README_END,
    ]
    pattern = re.compile(re.escape(README_START) + r".*?" + re.escape(README_END), re.S)
    README.write_text(pattern.sub(lambda _: "\n".join(block), text), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    tz = ZoneInfo(CONFIG.get("timezone", "UTC"))
    date_str = datetime.now(tz).strftime("%Y-%m-%d")
    langs = configured_languages()

    if digest_path_for(date_str, langs[0]).exists():
        print(f"Digest for {date_str} already exists. Nothing to do.")
        return 0

    print(f"Fetching arXiv: {CONFIG['categories']}")
    papers = fetch_papers(CONFIG["categories"], CONFIG["fetch_limit"])
    seen = load_seen()
    seen_set = set(seen)
    fresh = [p for p in papers if p["id"] not in seen_set]
    print(f"  {len(papers)} fetched, {len(fresh)} new")

    if not fresh:
        print("No new papers. Skipping (no commit will be made).")
        return 0

    for p in fresh:
        p["score"] = score_paper(p, CONFIG["keywords"])
    fresh.sort(key=lambda p: (p["score"], p["published"]), reverse=True)
    picks = fresh[: CONFIG["digest_size"]]

    mode = f"LLM ({LLM_MODEL})" if LLM_API_KEY else "abstract fallback (no LLM_API_KEY)"
    print(f"Summarizing {len(picks)} papers in {langs} via {mode}")
    for i, p in enumerate(picks, 1):
        p["summaries"] = {}
        for lang in langs:
            summary = llm_summarize(p, lang)
            # Without an LLM the abstract (English) is the only text available.
            p["summaries"][lang] = summary or f"TL;DR: {fallback_summary(p['abstract'])}"
            if LLM_API_KEY:
                time.sleep(CONFIG.get("llm_delay_seconds", 2))
        print(f"  [{i}/{len(picks)}] {p['title'][:70]}")

    digest_paths = {lang: write_digest(date_str, picks, len(fresh), lang, langs) for lang in langs}
    total = append_csv([{
        "date": date_str,
        "arxiv_id": p["id"],
        "title": p["title"],
        "authors": "; ".join(p["authors"]),
        "categories": " ".join(p["categories"]),
        "published": p["published"],
        "score": p["score"],
        "url": p["url"],
    } for p in picks])
    save_seen(seen + [p["id"] for p in fresh])
    update_readme(date_str, digest_paths, picks, total)

    for path in digest_paths.values():
        print(f"Wrote {path.relative_to(ROOT)}")
    print(f"{total} papers archived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
