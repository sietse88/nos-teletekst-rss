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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

USER_AGENT = "nos-teletekst-rss/1.0 (github.com/sietse88/nos-teletekst-rss)"
INDEX_PAGE = "101"
BASE_URL = "https://teletekst-data.nos.nl/json/"
OUTPUT = Path("docs/feed.xml")
SEEN_FILE = Path("seen.json")
REQUEST_PAUSE = 0.4

# Adres waar de feed (en het pictogram) gehost worden via GitHub Pages.
# Het pictogram (de rode NOS-O) toont de RSS-reader naast elk artikel.
SITE_BASE_URL = "https://sietse88.github.io/nos-teletekst-rss"
FEED_ICON_URL = f"{SITE_BASE_URL}/icon.png"

# Hoelang we onthouden dat een artikel al langs is gekomen. Zo blijft een
# artikel dat tijdelijk van pagina 101 verdwijnt en terugkeert hetzelfde item
# (en verschijnt het niet opnieuw als "nieuw" in de RSS-reader).
SEEN_RETENTION_DAYS = 14

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

# Losse subpagina-aanduiding zoals "1/3" of "3/3". Nodig voor subpagina's
# zonder titelbalk (bv. statistiekpagina's), waar de masthead niet wordt
# weggeknipt.
SUBPAGE_INDICATOR_RE = re.compile(r"^\d+\s*/\s*\d+\s*$")

# Maximaal aantal subpagina's dat we volgen (veiligheidslimiet tegen lussen).
MAX_SUBPAGES = 8

# Cross-reference voetregels zoals "speelschema groep met Oranje op 819"
# of "stand van zaken groep A2 op 847". Eindigen op " op <paginanummer>".
CROSSREF_RE = re.compile(r"\bop\s+\d{3,4}\s*$")

# Patronen die géén extra spatie mogen krijgen bij het spatie-herstel.
# We beschermen het patroon zélf (in plaats van "nooit na een cijfer"), zodat
# "Ligue 1,heeft" wél een spatie krijgt maar "22.000" heel blijft.
# 'de' en 'net' staan bewust niet in de domeinlijst: dat zijn Nederlandse
# woorden die na een punt aan een nieuwe zin kunnen beginnen.
PROTECT_RE = re.compile(
    r"\d+(?:[.,:]\d+)+"                                       # 22.000  1,5  15.00  15:45  1:0
    r"|\b(?:[a-z0-9-]+\.)+(?:nl|com|org|eu|be|uk|info|io)\b"   # nos.nl
    r"|\b(?:[a-zA-Z]\.){2,}",                                  # o.a.  a.s.  d.w.z.
    re.IGNORECASE,
)

# Een leesteken dat direct wordt gevolgd door tekst, een cijfer of een citaat.
# Alles wat heel moet blijven is hierboven al even weggezet.
PUNCT_SPACE_RE = re.compile(r"""([.,;:!?])(["'](?=\w)|[A-Za-zÀ-ÿ0-9])""")

# Sectienamen uit de teletekst-navigatie. Regels die alleen uit deze woorden
# bestaan, zijn de navigatiebalk onderaan de pagina.
NAV_WORDS = {
    "nieuws", "binnenland", "buitenland", "sport", "voetbal", "verkeer",
    "weer", "economie", "uitleg", "regio", "cultuur", "media", "tv",
    "wetenschap", "tech", "gezondheid", "opinie",
}


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
    """Voeg ontbrekende spaties na leestekens toe (teletekst spaart ruimte uit).

    Teletekst heeft maar 40 tekens per regel en laat daarom de spatie na een
    leesteken vaak weg: "asielzoekers.31 mensen", "drugsonderzoek:55" en
    'SGP."Het zit muurvast"'. Die spatie zetten we terug.

    Niet aangeraakt blijven: getallen met een scheidingsteken (22.000, 1,5),
    tijden (15.00, 15:45), scores (1:0), puntjes/ellips (...), domeinnamen
    (nos.nl) en afkortingen met punten (o.a., a.s.).
    """
    # Posities van stukken die heel moeten blijven (getallen, domeinen,
    # afkortingen). We slaan leestekens binnen die stukken over, in plaats van
    # ze weg te maskeren: zo blijft "augustus,20.00" wél splitsbaar op de komma
    # terwijl de punt binnen "20.00" met rust wordt gelaten.
    beschermd = [(m.start(), m.end()) for m in PROTECT_RE.finditer(s)]

    def _vervang(m: "re.Match") -> str:
        pos = m.start(1)
        if any(start <= pos < eind for start, eind in beschermd):
            return m.group(0)
        return f"{m.group(1)} {m.group(2)}"

    return PUNCT_SPACE_RE.sub(_vervang, s)


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


def find_title_match(raw: str) -> "re.Match | None":
    """Vind de titelbalk (eerste gekleurde balk met substantiële tekst)."""
    for m in TITLE_RE.finditer(raw):
        candidate = clean(m.group(1)).rstrip(".").strip()
        if len(candidate) >= 3:
            return m
    return None


def extract_title(raw: str) -> str:
    """Pak de titel uit de gekleurde balk (bg-blue/bg-red) van een (sub)pagina."""
    m = find_title_match(raw)
    return clean(m.group(1)).rstrip(".").strip() if m else ""


def _is_junk(line: str, nav_words: set) -> bool:
    """True voor regels die geen artikelinhoud zijn (restkop, indicator, navigatie).

    De volledige masthead (logo, sectienaam, banner) wordt al weggeknipt door
    alles vóór de titelbalk te negeren; deze functie is een vangnet voor de
    voettekst en voor pagina's zonder titelbalk (bv. statistiek-subpagina's).
    """
    if not line:
        return False  # lege regels behouden voor alinea-detectie
    if re.match(r"^NOS Teletekst\s+\d+\s*$", line):
        return True
    if SUBPAGE_INDICATOR_RE.match(line):
        return True
    if CROSSREF_RE.search(line):
        return True
    words = line.lower().split()
    if words and all(w in nav_words for w in words):
        return True
    return False


def extract_body_lines(raw: str, nav_words: set) -> list[str]:
    """Geef de schone tekstregels van één (sub)pagina (lege regels behouden).

    Alles vóór de titelbalk is masthead (kop, logo, sectienaam, banner) en
    wordt weggeknipt. Zo verdwijnen afgekapte sectienamen als 'LGEMEEN' of
    'OETBAL' structureel, zonder per geval een filter te hoeven toevoegen.
    """
    title_match = find_title_match(raw)
    body_raw = raw[title_match.end():] if title_match else raw

    text = TAG_RE.sub("", body_raw)
    text = html.unescape(text)
    text = PUA_RE.sub("", text)

    lines = [WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    lines = [l for l in lines if not _is_junk(l, nav_words)]

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

    # Navigatiewoorden onderaan de pagina komen uit de JSON zelf (fastTextLinks),
    # aangevuld met een vaste lijst. Zo herkennen we de voettekst ook als er een
    # ongebruikelijke sectienaam tussen staat.
    nav_words = set(NAV_WORDS)
    for data in pages:
        for link in data.get("fastTextLinks", []):
            title_text = (link.get("title") or "").strip().lower()
            if title_text:
                nav_words.add(title_text)

    combined: list[str] = []
    prev_continues = False
    n = len(pages)
    for i, data in enumerate(pages):
        raw = data["content"]
        lines = extract_body_lines(raw, nav_words)

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
    """Lees het 'gezien'-geheugen. Formaat: {guid: {"first": rfc, "last": rfc}}.

    Migreert het oude formaat {guid: rfc-datum} automatisch.
    """
    if not SEEN_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    seen: dict = {}
    for guid, value in raw.items():
        if isinstance(value, str):
            seen[guid] = {"first": value, "last": value}
        elif isinstance(value, dict) and "first" in value:
            seen[guid] = {"first": value["first"], "last": value.get("last", value["first"])}
    return seen


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(
        json.dumps(seen, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def is_recent(rfc_date: str, cutoff: datetime) -> bool:
    """True als rfc_date op of na cutoff valt. Onparseerbaar -> behouden."""
    try:
        dt = parsedate_to_datetime(rfc_date)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def normalize_headline(headline: str) -> str:
    """Maak een kop vergelijkbaar: kleine letters, alleen letters/cijfers."""
    return re.sub(r"[^a-z0-9]+", "", headline.lower())


def guid_for(headline: str) -> str:
    """Stabiel ID op basis van de kop op pagina 101.

    Bewust niet op de bodytekst gebaseerd: NOS past lopende berichten vaak
    licht aan, en dan moet het hetzelfde item blijven (geen duplicaat).
    """
    digest = hashlib.sha1(normalize_headline(headline).encode("utf-8")).hexdigest()[:16]
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
        "  <image>\n"
        f"    <url>{escape(FEED_ICON_URL)}</url>\n"
        "    <title>NOS Teletekst 101</title>\n"
        "    <link>https://nos.nl/teletekst#101</link>\n"
        "  </image>\n"
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
    seen_guids: set[str] = set()
    for page, headline in headlines:
        # De titel is de kop zoals die op pagina 101 staat (niet de titel van
        # de doelpagina, die kan afwijken — bv. "Kort nieuws binnenland").
        guid = guid_for(headline)
        if guid in seen_guids:
            continue  # zelfde kop twee keer op 101: niet dupliceren
        seen_guids.add(guid)

        # Titel = de 101-kop, met hetzelfde spatie-herstel als de bodytekst.
        # (Het ID blijft stabiel: guid_for negeert leestekens en spaties.)
        title = normalize_punctuation(headline)

        time.sleep(REQUEST_PAUSE)
        try:
            _, body = build_article(page)
        except urllib.error.URLError as e:
            print(f"  Pagina {page} overgeslagen ({e})", file=sys.stderr)
            continue
        if not body:
            body = [title]

        entry = seen.get(guid)
        first_seen = entry["first"] if entry else now_str
        seen[guid] = {"first": first_seen, "last": now_str}
        items.append({
            "title": title,
            "page": page,
            "guid": guid,
            "pubDate": first_seen,
            "body": body,
        })
        print(f"  {page}: {title}")

    # Verlopen entries opruimen: artikelen die we al 14+ dagen niet zagen.
    cutoff = now - timedelta(days=SEEN_RETENTION_DAYS)
    seen = {g: v for g, v in seen.items() if is_recent(v["last"], cutoff)}
    save_seen(seen)

    OUTPUT.write_text(build_rss(items, now), encoding="utf-8")
    print(f"Geschreven: {OUTPUT} ({len(items)} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
