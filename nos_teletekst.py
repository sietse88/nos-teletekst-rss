#!/usr/bin/env python3
"""Bouwt een RSS-feed uit NOS Teletekst pagina 101.

Haalt de overzichtspagina op, leest elke gelinkte sub-pagina uit, en
schrijft het resultaat naar docs/feed.xml. Eerste-keer-gezien tijdstippen
worden onthouden in seen.json zodat publicatiedatums stabiel blijven.
"""

import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

USER_AGENT = "nos-teletekst-rss/1.0 (github.com/your-username/nos-teletekst-rss)"
INDEX_PAGE = "101"
BASE_URL = "https://teletekst-data.nos.nl/json/"
OUTPUT = Path("docs/feed.xml")
SEEN_FILE = Path("seen.json")
REQUEST_PAUSE = 0.4

# Teletekst gebruikt het Unicode Private Use Area voor blokgrafiek-tekens.
PUA_RE = re.compile(r"[-]")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

INDEX_ITEM_RE = re.compile(
    r'<span class="cyan\s*"[^>]*>([^<]*?)</span>'
    r'\s*<span class="yellow\s*"[^>]*>\s*'
    r'<a[^>]*href="#(\d+)"[^>]*>\d+</a>',
    re.DOTALL,
)

# De titel van een artikel staat in een gekleurde balk (bg-blue voor nieuws,
# bg-red voor sport). Tekstkleur varieert: yellow voor de meeste pagina's,
# geen kleur (= wit) voor speciale lay-outs zoals WK-pagina's. We matchen
# elke span met die achtergrond en filteren later lege/PUA-only resultaten.
TITLE_RE = re.compile(
    r'<span class="[^"]*bg-(?:blue|red)[^"]*"[^>]*>([^<]+)</span>',
    re.DOTALL,
)

# Banner-/indicatorregels die geen inhoud zijn:
#  - "2026"        : jaartal uit de WK-banner
#  - "2026 1/3"    : jaartal + subpagina-aanduiding
#  - "1/3" / "3/3" : losse subpagina-aanduiding
BANNER_RE = re.compile(r"^\d{4}(\s+\d+\s*/\s*\d+)?\s*$")
SUBPAGE_INDICATOR_RE = re.compile(r"^\d+\s*/\s*\d+\s*$")

# Maximaal aantal subpagina's dat we volgen (veiligheidslimiet tegen lussen).
MAX_SUBPAGES = 8

# Cross-reference voetregels zoals "speelschema groep met Oranje op 819"
# of "stand van zaken groep A2 op 847". Eindigen op " op <paginanummer>".
CROSSREF_RE = re.compile(r"\bop\s+\d{3,4}\s*$")

# Sectienamen uit de teletekst-navigatie. Regels die alleen uit deze woorden
# bestaan, zijn de navigatiebalk onderaan de pagina.
NAV_WORDS = {
    "nieuws", "binnenland", "buitenland", "sport", "voetbal", "verkeer",
    "weer", "economie", "uitleg", "regio", "cultuur", "media", "tv",
    "wetenschap", "tech", "gezondheid", "opinie",
}

# Sectie-koptekst-patroon: "OETBAL 1/3" of "INNENLAND 2/4" (eerste letter is
# door teletekst als PUA-blokgrafiek weergegeven en wordt eruit gestript).
SECTION_HEADER_RE = re.compile(r"^[A-Z]+\s+\d+/\d+\s*$")


def fetch_page(page: str) -> dict:
    req = urllib.request.Request(
        BASE_URL + page,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def clean(s: str) -> str:
    s = html.unescape(s)
    s = PUA_RE.sub("", s)
    return WS_RE.sub(" ", s).strip()


def normalize_punctuation(s: str) -> str:
    """Voeg ontbrekende spaties na leestekens toe (teletekst spaart ruimte uit)."""
    return re.sub(r"([.,;!?])(?=[A-Za-zÀ-ÿ])", r"\1 ", s)


def parse_index(data: dict) -> list[tuple[str, str]]:
    """Geeft een lijst (sub_page, headline) terug uit pagina 101."""
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in INDEX_ITEM_RE.finditer(data["content"]):
        page = m.group(2)
        if page in seen:
            continue
        seen.add(page)
        headline = clean(m.group(1)).rstrip(".").strip()
        if headline:
            items.append((page, headline))
    return items


def extract_title(raw: str) -> str:
    """Pak de titel uit de gekleurde balk (bg-blue/bg-red) van een (sub)pagina."""
    for m in TITLE_RE.finditer(raw):
        candidate = clean(m.group(1)).rstrip(".").strip()
        if len(candidate) >= 3:
            return candidate
    return ""


def _is_junk(line: str) -> bool:
    """True voor regels die geen artikelinhoud zijn (kop, banner, navigatie)."""
    if not line:
        return False  # lege regels behouden voor alinea-detectie
    if re.match(r"^NOS Teletekst\s+\d+\s*$", line):
        return True
    if SECTION_HEADER_RE.match(line):
        return True
    if BANNER_RE.match(line):
        return True
    if SUBPAGE_INDICATOR_RE.match(line):
        return True
    if CROSSREF_RE.search(line):
        return True
    words = line.lower().split()
    if words and all(w in NAV_WORDS for w in words):
        return True
    return False


def extract_body_lines(raw: str, page_title: str) -> list[str]:
    """Geef de schone tekstregels van één (sub)pagina (lege regels behouden)."""
    text = TAG_RE.sub("", raw)
    text = html.unescape(text)
    text = PUA_RE.sub("", text)

    lines = [WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    lines = [l for l in lines if not _is_junk(l)]
    if page_title:
        lines = [l for l in lines if l != page_title and not l.startswith(page_title)]

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _lines_to_paragraphs(lines: list[str]) -> list[str]:
    """Groepeer regels in alinea's; lege regels markeren een alineagrens."""
    paragraphs: list[str] = []
    current: list[str] = []
    for l in lines:
        if l:
            current.append(l)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return [normalize_punctuation(p).strip() for p in paragraphs if p.strip()]


def fetch_subpages(page: str) -> list[dict]:
    """Volg de nextSubPage-keten en geef de inhoud van alle subpagina's terug."""
    pages: list[dict] = []
    current = page
    visited: set[str] = set()
    for _ in range(MAX_SUBPAGES):
        if not current or current in visited:
            break
        visited.add(current)
        if pages:  # niet pauzeren vóór de allereerste request van dit artikel
            time.sleep(REQUEST_PAUSE)
        data = fetch_page(current)
        pages.append(data)
        current = (data.get("nextSubPage") or "").strip()
    return pages


def build_article(page: str) -> tuple[str, list[str]]:
    """Haal een artikel op (incl. alle subpagina's) en geef (titel, alinea's).

    Subpagina's worden achter elkaar geplakt. Teletekst markeert tekst die op
    de volgende pagina doorloopt met '...'; die markering gebruiken we om te
    bepalen of een alinea doorloopt of dat er een nieuwe alinea begint.
    """
    pages = fetch_subpages(page)
    if not pages:
        return "", []

    title = extract_title(pages[0]["content"])

    combined: list[str] = []
    prev_continues = False
    n = len(pages)
    for i, data in enumerate(pages):
        raw = data["content"]
        page_title = extract_title(raw)
        lines = extract_body_lines(raw, page_title)

        # '...' aan begin/eind markeert doorlopende tekst tussen subpagina's.
        starts_cont = False
        if i > 0 and lines:
            stripped = re.sub(r"^\.{2,}\s*", "", lines[0]).strip()
            starts_cont = stripped != lines[0]
            lines[0] = stripped
            if not lines[0]:
                lines.pop(0)

        ends_cont = False
        if i < n - 1 and lines:
            stripped = re.sub(r"\s*\.{2,}$", "", lines[-1]).strip()
            ends_cont = stripped != lines[-1]
            lines[-1] = stripped
            if not lines[-1]:
                lines.pop()

        if not lines:
            prev_continues = prev_continues or ends_cont
            continue

        if combined:
            if prev_continues or starts_cont:
                pass  # zelfde alinea voortzetten: geen lege regel ertussen
            else:
                combined.append("")  # nieuwe alinea
        combined.extend(lines)
        prev_continues = ends_cont

    paragraphs = _lines_to_paragraphs(combined)
    return normalize_punctuation(title), paragraphs


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def guid_for(title: str, body: list[str]) -> str:
    fingerprint = title + "\n" + "\n".join(body)
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"nos-tt-{digest}"


def build_rss(items: list[dict], now: datetime) -> str:
    rss_items = []
    for it in items:
        body_html = "<p>" + "</p><p>".join(it["body"]) + "</p>"
        rss_items.append(
            "  <item>\n"
            f"    <title>{escape(it['title'])}</title>\n"
            f"    <link>https://nos.nl/teletekst#{it['page']}</link>\n"
            f"    <guid isPermaLink=\"false\">{escape(it['guid'])}</guid>\n"
            f"    <pubDate>{it['pubDate']}</pubDate>\n"
            f"    <description>{escape(body_html)}</description>\n"
            "  </item>"
        )
    body = "\n".join(rss_items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "<channel>\n"
        "  <title>NOS Teletekst 101</title>\n"
        "  <link>https://nos.nl/teletekst#101</link>\n"
        "  <description>Het nieuws van NOS Teletekst pagina 101, "
        "automatisch omgezet naar RSS.</description>\n"
        "  <language>nl-NL</language>\n"
        f"  <lastBuildDate>{format_datetime(now)}</lastBuildDate>\n"
        f"{body}\n"
        "</channel>\n"
        "</rss>\n"
    )


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen()
    now = datetime.now(timezone.utc)
    now_str = format_datetime(now)

    try:
        index = fetch_page(INDEX_PAGE)
    except urllib.error.URLError as e:
        print(f"Kon overzichtspagina niet ophalen: {e}", file=sys.stderr)
        return 1

    headlines = parse_index(index)
    print(f"Gevonden: {len(headlines)} koppen op pagina 101")
    if not headlines:
        print("Geen koppen gevonden — pagina-indeling mogelijk gewijzigd.",
              file=sys.stderr)
        return 1

    items: list[dict] = []
    for page, headline_fallback in headlines:
        time.sleep(REQUEST_PAUSE)
        try:
            title, body = build_article(page)
        except urllib.error.URLError as e:
            print(f"  Pagina {page} overgeslagen ({e})", file=sys.stderr)
            continue
        if not title:
            title = headline_fallback
        if not body:
            body = [headline_fallback]
        guid = guid_for(title, body)
        first_seen = seen.get(guid) or now_str
        seen[guid] = first_seen
        items.append({
            "title": title,
            "page": page,
            "guid": guid,
            "pubDate": first_seen,
            "body": body,
        })
        print(f"  {page}: {title}")

    # Houd seen.json schoon: gooi entries weg die niet meer op 101 staan.
    current_guids = {it["guid"] for it in items}
    seen = {g: t for g, t in seen.items() if g in current_guids}
    save_seen(seen)

    OUTPUT.write_text(build_rss(items, now), encoding="utf-8")
    print(f"Geschreven: {OUTPUT} ({len(items)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
