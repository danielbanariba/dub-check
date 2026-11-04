#!/usr/bin/env python3
"""Does a Latino Spanish dub exist for a given film?

Answers by querying doblaje.fandom.com (Doblaje Wiki), a community-run
MediaWiki site that catalogs Spanish-language dubs and credits each dub to
the country that produced it.

THE CENTRAL RULE, enforced throughout this module: a wiki search hit is
never proof by itself. doblaje.fandom.com's search is fuzzy and always
returns *something*, even for a film it has never heard of (verified live:
a search for "Murder Death Koreatown", a film with no Latino dub, returns
several completely unrelated pages -- two unrelated Korean films and a
voice actor's biography page among them). Every candidate page returned by
a search must be opened and its infobox checked against the film's own
original title and year before it counts as evidence. See
evaluate_candidate() / evaluate_film().

See README.md for the full verification rule and worked examples.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Dub release years routinely differ from the film's canonical release year
# (the wiki's own "año" field for "Pengabdi Setan" lists BOTH 2017, the
# original release, and 2018, the Latino dub's release: "2017<br />2018
# <small>(México)</small>"). extract_years() already pulls out every year
# embedded in that field; this tolerance is an additional small safety
# margin on top of that, not a substitute for it.
DEFAULT_YEAR_TOLERANCE = 1

# doblaje.fandom.com writes the dub's country in Spanish, inside a
# "{{Bandera|<Country>}}" flag template. These are matched after accent- and
# case-normalization (see normalize_title), so e.g. "México" and "Mexico"
# are treated the same.
LATIN_AMERICAN_COUNTRIES = frozenset({
    "mexico", "argentina", "venezuela", "chile", "colombia", "peru",
    "puerto rico", "ecuador", "uruguay", "bolivia", "paraguay",
    "guatemala", "honduras", "el salvador", "nicaragua", "costa rica",
    "panama", "cuba", "republica dominicana",
})
SPAIN_NAMES = frozenset({"espana", "spain"})


# ---------------------------------------------------------------------------
# Title normalization
# ---------------------------------------------------------------------------

_LEADING_ARTICLES = ("the", "el", "la", "los", "las", "un", "una", "unos", "unas")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_title(text: str, strip_article: bool = False) -> str:
    """Fold a title down to a form comparable across wiki free-text quirks:
    accents stripped, lowercased, punctuation turned to spaces (not removed
    outright, so "Spider-Man" becomes "spider man" rather than the
    unrelated-looking "spiderman"), whitespace collapsed.

    strip_article additionally drops exactly one leading article ("the",
    "el", "la", ...), because the wiki is not consistent about whether a
    dub's infobox keeps a film's leading article. It is opt-in and applied
    on BOTH sides of a comparison by titles_match(), never on its own --
    an exact match should never depend on it.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = without_accents.lower()
    spaced = _PUNCTUATION_RE.sub(" ", lowered)
    collapsed = _WHITESPACE_RE.sub(" ", spaced).strip()
    if strip_article:
        for article in _LEADING_ARTICLES:
            prefix = article + " "
            if collapsed.startswith(prefix):
                return collapsed[len(prefix):]
    return collapsed


# ---------------------------------------------------------------------------
# Year extraction
# ---------------------------------------------------------------------------

# Matches a bare 4-digit year in [1800, 2099], but only when it is not part
# of a longer digit run (so a pageid or timestamp embedded in nearby text
# cannot be mistaken for a film year).
_YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")


def extract_years(raw: str) -> set[int]:
    """Pull every plausible film year out of a messy infobox "año" value,
    e.g. "2017<br />2018 <small>(México)</small>" -> {2017, 2018}."""
    if not raw:
        return set()
    return {int(y) for y in _YEAR_RE.findall(raw)}


# ---------------------------------------------------------------------------
# Infobox parsing
#
# WHY a hand-rolled scanner instead of a line-anchored regex: real pages on
# this wiki format infobox fields two different ways. Some put one field per
# line (e.g. "Videodrome: Cuerpos invadidos"). Others cram every field onto
# a single line separated by "|", with no surrounding spaces and even a
# leading "||" (verified live: "Reporte de un asesino" is
# "{{Infobox||titulo=Reporte de un asesino|img=|titulo_orig=...|año=2025|...}}").
# A regex anchored on line boundaries has no line break to stop at on that
# second shape, so it silently glues every later field's text onto the
# first field's captured value. Field values also routinely contain their
# OWN pipes -- {{Bandera|México}}, or [[Archivo:x.png|55px|link=...]] -- so
# a naive split on every "|" breaks just as badly. This scanner tracks
# {{ }} / [[ ]] nesting depth so only a "|" directly inside the outermost
# template is treated as a field separator.
# ---------------------------------------------------------------------------

def _find_matching_close(text: str, open_pos: int) -> int:
    """Return the index just past the '}}' that closes the '{{' template
    opened at open_pos, accounting for nested {{ }} templates."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return n  # unterminated template (malformed wikitext) -- take the rest


def _split_top_level(segment: str) -> list[str]:
    """Split on '|' characters that are not nested inside {{ }} or [[ ]]."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    n = len(segment)
    while i < n:
        two = segment[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            current.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            depth = max(0, depth - 1)
            current.append(two)
            i += 2
            continue
        if segment[i] == "|" and depth == 0:
            parts.append("".join(current))
            current = []
            i += 1
            continue
        current.append(segment[i])
        i += 1
    parts.append("".join(current))
    return parts


def parse_infobox_fields(wikitext: str) -> dict[str, str]:
    """Extract every top-level "|key = value" pair from the first {{...}}
    template in the wikitext (the film's infobox) into a dict. A page whose
    first template is not a film infobox (e.g. a voice actor's bio page)
    simply yields a dict without "titulo_orig"/"año" keys -- callers must
    treat those as absent, not crash."""
    start = wikitext.find("{{")
    if start == -1:
        return {}
    end = _find_matching_close(wikitext, start)
    # _find_matching_close returns an index just past a real "}}" when the
    # template is well-formed, but falls back to len(wikitext) for
    # malformed/truncated wikitext with no closing "}}" anywhere -- which
    # can coincidentally still equal a position "past a }}". Check the
    # actual characters rather than compare indices, so that fallback case
    # takes the whole rest of the string instead of silently losing its
    # last two characters to a "}}" that was never really there.
    if wikitext[end - 2:end] == "}}":
        inner = wikitext[start + 2:end - 2]
    else:
        inner = wikitext[start + 2:end]
    segments = _split_top_level(inner)[1:]  # drop the template name itself
    fields: dict[str, str] = {}
    for segment in segments:
        if "=" not in segment:
            continue  # a bare positional/flag parameter -- not a named field
        key, value = segment.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


# ---------------------------------------------------------------------------
# Country extraction / classification
#
# pais is the DUB's country -- the Latino-vs-Castellano signal this whole
# tool exists to read. origen is the FILM's production country and must
# never be used here; a Canadian-produced film with a Mexican dub has
# origen=Canadá and pais=México, and only pais answers Daniel's question.
# ---------------------------------------------------------------------------

_BANDERA_RE = re.compile(r"\{\{\s*[Bb]andera\s*\|([^{}|]+)(?:\|[^{}]*)?\}\}")


def extract_countries(pais_raw: str) -> list[str]:
    """Return the normalized country name(s) credited in a pais field."""
    if not pais_raw:
        return []
    matches = _BANDERA_RE.findall(pais_raw)
    if matches:
        return [normalize_title(m) for m in matches]
    # Fallback for a pais field with no {{Bandera|..}} template (not seen in
    # the pages checked while building this tool, but cheap to handle
    # defensively). Deliberately conservative: stripped markup either
    # yields a plain country name or an empty string -- it never invents a
    # country name that would push classify_countries() into a false
    # LATINO or CASTELLANO.
    text = re.sub(r"\{\{.*?\}\}", " ", pais_raw)
    text = re.sub(r"\[\[.*?\]\]", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    normalized = normalize_title(text)
    return [normalized] if normalized else []


def classify_countries(countries: list[str]) -> str:
    """"latino" if any credited country is Latin American (a Latino dub
    exists, even if a Spain dub is also credited alongside it -- the
    question is whether a Latino dub exists, not whether it is the only
    one). "castellano" only when every credited country is Spain.
    "unknown" when nothing recognizable was found -- never guessed."""
    if any(c in LATIN_AMERICAN_COUNTRIES for c in countries):
        return "latino"
    if countries and all(c in SPAIN_NAMES for c in countries):
        return "castellano"
    return "unknown"


# ---------------------------------------------------------------------------
# Wiki markup cleanup and alternate-title extraction
#
# Discovered live while verifying this tool against real pages: some films
# have no plain "titulo_orig" field at all. A foreign-language film credited
# with several alternate international release titles instead gets numbered
# variants -- "titulo_orig1", "titulo_orig2", ... (verified on the real
# "Pareja dispareja" page: titulo_orig1 is the Korean title, titulo_orig2 an
# English international title). Those field values also routinely carry
# wiki markup that a plain titulo_orig almost never does -- a flag
# template, a <small> romanization note, a decorative image embed.
# ---------------------------------------------------------------------------

_SMALL_SPAN_RE = re.compile(r"<small>.*?</small>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WIKILINK_RE = re.compile(r"\[\[([^\]]*)\]\]")
_FILE_NAMESPACE_PREFIXES = ("archivo:", "file:", "imagen:", "image:")


def _replace_wikilink(match: re.Match) -> str:
    inner = match.group(1)
    parts = inner.split("|")
    if parts[0].strip().lower().startswith(_FILE_NAMESPACE_PREFIXES):
        return ""  # a decorative icon/image embed (e.g. a flag graphic), not title text
    return parts[-1]  # MediaWiki convention: the last "|"-segment is the display text


def strip_wiki_markup(text: str) -> str:
    """Clean MediaWiki syntax out of a field value so what is left is just
    human-readable text. This is evidence-driven, not a general MediaWiki
    parser:

    - <small>...</small> spans are removed WHOLE (tag AND content), because
      every real example seen while building this tool uses <small> for a
      SECONDARY annotation, never the title itself -- a romanization note
      ("(gupilsuneun eobsda)") or a version label ("(doblaje original)").
    - {{templates}} (flag icons) are removed entirely.
    - [[Archivo:...|options|caption]] image embeds are removed entirely
      (verified live on "Pareja dispareja"'s titulo_orig2), but a genuine
      [[Target|Display]] wikilink keeps its display text.
    - Remaining HTML tags and wiki bold/italic markers are stripped.
    """
    if not text:
        return text
    without_small = _SMALL_SPAN_RE.sub(" ", text)

    # Strip {{templates}}, tracking nesting depth the same way infobox
    # parsing does (a flag template is never nested here in practice, but
    # depth-tracking costs nothing and stays correct if that ever changes).
    kept = []
    depth = 0
    i = 0
    n = len(without_small)
    while i < n:
        two = without_small[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth = max(0, depth - 1)
            i += 2
            continue
        if depth == 0:
            kept.append(without_small[i])
        i += 1
    without_templates = "".join(kept)

    without_links = _WIKILINK_RE.sub(_replace_wikilink, without_templates)
    without_tags = _HTML_TAG_RE.sub(" ", without_links)
    without_formatting = without_tags.replace("'''", "").replace("''", "")
    return _WHITESPACE_RE.sub(" ", without_formatting).strip()


def extract_original_titles(fields: dict[str, str]) -> list[str]:
    """Collect every known spelling of a film's original title from an
    infobox's fields: the plain "titulo_orig" field when present, plus any
    numbered "titulo_orig1", "titulo_orig2", ... variants, each cleaned of
    wiki markup. Returns an empty list when the page has neither (e.g. a
    non-film infobox such as a voice actor's bio page)."""
    raw_values = []
    plain = fields.get("titulo_orig")
    if plain:
        raw_values.append(plain)
    for n in range(1, 11):  # observed real pages use at most 4; generous headroom
        value = fields.get(f"titulo_orig{n}")
        if value:
            raw_values.append(value)
    return [strip_wiki_markup(v) for v in raw_values]


# ---------------------------------------------------------------------------
# Candidate verification -- the trap defense
# ---------------------------------------------------------------------------

def titles_match(candidate_titulo_orig: str | None, title: str | None, original_title: str | None) -> bool:
    """True only on an EXACT match (after normalization) against the film's
    title or original title. Deliberately NOT fuzzy/similarity-based: the
    wiki's search already introduces all the fuzziness this tool can
    tolerate (a real search for "Murder Death Koreatown" returns, among
    others, "Reporte de un asesino" and "El complejo de apartamentos" --
    unrelated Korean films with no textual similarity to the query at all).
    Adding a second layer of fuzzy matching here would only make the trap
    worse, not better."""
    if not candidate_titulo_orig:
        return False
    targets = [t for t in (original_title, title) if t]
    for target in targets:
        if normalize_title(candidate_titulo_orig) == normalize_title(target):
            return True
        if normalize_title(candidate_titulo_orig, strip_article=True) == normalize_title(target, strip_article=True):
            return True
    return False


def year_matches(candidate_ano_raw: str | None, target_year: int | None, tolerance: int) -> bool | None:
    """True/False once verified; None means "not checked" because no target
    year was supplied (single-film CLI mode allows omitting --year, at the
    cost of a weaker verdict -- see evaluate_candidate)."""
    if target_year is None:
        return None
    if not candidate_ano_raw:
        return False
    years = extract_years(candidate_ano_raw)
    if not years:
        return False
    return any(abs(year - target_year) <= tolerance for year in years)


@dataclass
class CandidateResult:
    page_title: str
    verified: bool
    country_class: str | None  # "latino" | "castellano" | "unknown", only when verified
    note: str  # human-readable explanation, always populated (this IS the evidence)


def evaluate_candidate(
    page_title: str,
    fields: dict[str, str],
    title: str | None,
    original_title: str | None,
    year: int | None,
    year_tolerance: int = DEFAULT_YEAR_TOLERANCE,
) -> CandidateResult:
    """Verify a single candidate page against the target film. BOTH title
    and year (when a year was given) must match -- a match on only one axis
    is exactly the kind of coincidence that produces a false positive (two
    unrelated films sharing a release year; a remake sharing a title).

    Title matching checks every known spelling of the candidate's original
    title (plain "titulo_orig", or numbered "titulo_orig1"/"titulo_orig2"/...
    variants for a film with several alternate international titles -- see
    extract_original_titles) -- a match against ANY one of them counts."""
    candidate_titles = extract_original_titles(fields)
    candidate_ano_raw = fields.get("año")
    candidate_pais_raw = fields.get("pais")

    matched_title = next(
        (t for t in candidate_titles if titles_match(t, title, original_title)), None
    )
    if matched_title is None:
        shown = ", ".join(repr(t) for t in candidate_titles) if candidate_titles else repr(None)
        return CandidateResult(
            page_title, False, None,
            f'"{page_title}": titulo_orig ({shown}) does not match -> rejected',
        )

    year_ok = year_matches(candidate_ano_raw, year, year_tolerance)
    if year_ok is False:
        return CandidateResult(
            page_title, False, None,
            f'"{page_title}": titulo_orig matches ({matched_title!r}), but año ({candidate_ano_raw!r}) '
            f"does not contain {year} (+/-{year_tolerance}) -> rejected",
        )

    countries = extract_countries(candidate_pais_raw or "")
    country_class = classify_countries(countries)
    year_note = "año not checked (no target year given)" if year_ok is None else f"año matches ({candidate_ano_raw!r})"
    return CandidateResult(
        page_title, True, country_class,
        f'"{page_title}": VERIFIED (titulo_orig={matched_title!r}, {year_note}) '
        f"-> pais={countries or '?'} -> {country_class}",
    )


@dataclass
class Verdict:
    status: str  # "LATINO" | "CASTELLANO" | "NO_ENCONTRADO"
    page_title: str | None
    notes: list[str] = field(default_factory=list)


def _unique_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def evaluate_film(
    title: str | None,
    original_title: str | None,
    year: int | None,
    search_fn,
    fetch_fn,
    search_limit: int = 8,
    year_tolerance: int = DEFAULT_YEAR_TOLERANCE,
) -> Verdict:
    """Orchestrate the whole verification rule: search, then open and verify
    every candidate page before trusting it. A search hit alone NEVER
    produces a verdict -- see the module docstring and README.md.

    search_fn(term, limit) -> list[page_title]
    fetch_fn(page_title) -> wikitext string, or None if the page does not
        exist / could not be fetched.

    Both are injected so this function -- the one the trap-defense tests
    exercise -- never touches the network or a cache directly.
    """
    search_terms = _unique_preserve_order([t for t in (original_title, title) if t])
    if not search_terms:
        raise ValueError("evaluate_film requires at least a title or an original_title")

    seen_pages: set[str] = set()
    notes: list[str] = []
    best: CandidateResult | None = None

    for term in search_terms:
        for page_title in search_fn(term, search_limit):
            if page_title in seen_pages:
                continue
            seen_pages.add(page_title)

            wikitext = fetch_fn(page_title)
            if wikitext is None:
                notes.append(f'"{page_title}": page fetch failed or page does not exist -> skipped')
                continue

            fields = parse_infobox_fields(wikitext)
            result = evaluate_candidate(page_title, fields, title, original_title, year, year_tolerance)
            notes.append(result.note)

            if result.verified and result.country_class == "latino":
                # Best possible outcome -- stop spending requests immediately.
                return Verdict("LATINO", page_title, notes)
            if result.verified and result.country_class == "castellano" and best is None:
                best = result

        # Deliberately NOT breaking here even once a Castellano match is
        # found: CASTELLANO is not the answer Daniel wants, and the next
        # search term might still turn up a genuine Latino dub. Only a
        # confirmed LATINO match (handled above via an immediate return) is
        # good enough to stop early.

    if best is not None:
        return Verdict("CASTELLANO", best.page_title, notes)
    return Verdict("NO_ENCONTRADO", None, notes)


# ---------------------------------------------------------------------------
# Disk cache -- what makes batch mode resumable and re-runs cheap
# ---------------------------------------------------------------------------

API_BASE = "https://doblaje.fandom.com/es/api.php"

# No personal contact info belongs in a header sent to a third-party
# service, so this deliberately carries no email or name -- just enough to
# identify the tool and its behavior to a wiki operator glancing at logs.
DEFAULT_USER_AGENT = (
    "dub-check/1.0 (personal, low-volume research tool for checking Latino "
    "Spanish dub availability; runs manually, respects rate limiting)"
)
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.5


class _MissType:
    """Sentinel type distinguishing 'not cached yet' from 'cached as None'
    -- a confirmed-missing wiki page is a legitimate value to cache as
    None, and must not be re-requested on every run as if it were new."""

    def __repr__(self) -> str:
        return "CACHE_MISS"


CACHE_MISS = _MissType()


class DiskCache:
    """A small JSON-file cache, one file per key, under a namespace
    subdirectory ("search" / "page"). This is what lets a batch run over
    ~100 films be killed and resumed without re-asking the wiki anything it
    already answered, and lets a single-film re-run return instantly.
    Entries never expire on their own -- a film's identity is not going to
    change; to force a refresh, delete the relevant file(s) (or the whole
    cache directory) by hand.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, namespace: str, key: str) -> Path:
        namespace_dir = self.root / namespace
        namespace_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return namespace_dir / f"{digest}.json"

    def get(self, namespace: str, key: str):
        path = self._path(namespace, key)
        if not path.exists():
            return CACHE_MISS
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload["value"]

    def set(self, namespace: str, key: str, value) -> None:
        path = self._path(namespace, key)
        with path.open("w", encoding="utf-8") as fh:
            json.dump({"key": key, "value": value}, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Wiki HTTP client
# ---------------------------------------------------------------------------

class WikiResponseError(Exception):
    """The wiki answered, but not with a usable result (an API "error"
    object such as "ratelimited", or a body of the wrong shape). Never
    cached: caching it would turn a transient failure into a permanent
    answer."""


class WikiClient:
    """Talks to the doblaje.fandom.com MediaWiki API. Three layers, each
    independently testable:

      _http_get_json_uncached -- the actual network call. No throttling, no
        caching. Its own method so tests can check the exact request
        MediaWiki will receive, or exercise the throttle logic below
        without touching the network.
      _http_get_json -- throttles, then delegates to the layer above.
      search() / get_wikitext() -- check the disk cache first; only on a
        miss do they call _http_get_json, then persist the result (even a
        "page does not exist" result -- see get_wikitext).
    """

    def __init__(
        self,
        cache: DiskCache,
        min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        base_url: str = API_BASE,
        timeout: float = 20.0,
    ):
        self.cache = cache
        self.min_interval = min_interval
        self.user_agent = user_agent
        self.base_url = base_url
        self.timeout = timeout
        self._last_request_at: float | None = None

    def _throttle(self) -> None:
        """Be gentle with a free community resource: never issue two real
        requests less than min_interval apart. Only affects real requests --
        callers only reach this method on a cache miss."""
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = self.min_interval - (now - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _http_get_json_uncached(self, params: dict) -> dict:
        url = self.base_url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = response.read()
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            raise WikiResponseError(f"wiki returned a non-JSON body ({len(body)} bytes)") from exc

    def _http_get_json(self, params: dict) -> dict:
        self._throttle()
        return self._http_get_json_uncached(params)

    def search(self, term: str, limit: int = 8) -> list[str]:
        """Return candidate page titles for a search term. NEVER treat this
        return value as evidence of anything -- it is the fuzzy, unverified
        input to evaluate_film()/evaluate_candidate(), not an answer."""
        cache_key = f"{term}::{limit}"
        cached = self.cache.get("search", cache_key)
        if cached is not CACHE_MISS:
            return cached
        data = self._http_get_json({
            "action": "query",
            "list": "search",
            "srsearch": term,
            "format": "json",
            "srlimit": limit,
        })
        # An API-level error (rate limit, maxlag, ...) comes back as HTTP 200
        # with an "error" object and no search list. That is not "zero hits"
        # and must never be cached as such.
        search_results = data.get("query", {}).get("search") if isinstance(data, dict) else None
        if not isinstance(search_results, list) or "error" in data:
            raise WikiResponseError(f"unusable search response for {term!r}: {str(data)[:200]}")
        titles = [item["title"] for item in search_results]
        self.cache.set("search", cache_key, titles)
        return titles

    def get_wikitext(self, page_title: str) -> str | None:
        """Return a page's raw wikitext, or None if it does not exist. A
        None result is cached too, so a candidate page this tool has
        already confirmed is not real is never re-fetched on a later run."""
        cached = self.cache.get("page", page_title)
        if cached is not CACHE_MISS:
            return cached
        data = self._http_get_json({
            "action": "parse",
            "page": page_title,
            "prop": "wikitext",
            "format": "json",
        })
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict) and error.get("code") == "missingtitle":
            wikitext = None  # the only error that really means "no such page"
        else:
            wikitext = None
            if isinstance(data, dict) and error is None:
                wikitext = data.get("parse", {}).get("wikitext", {}).get("*")
            if not isinstance(wikitext, str):
                raise WikiResponseError(f"unusable parse response for {page_title!r}: {str(data)[:200]}")
        self.cache.set("page", page_title, wikitext)
        return wikitext


# ---------------------------------------------------------------------------
# Radarr client -- READ-ONLY.
#
# This tool reports on Radarr's wanted list; it never monitors, searches,
# blocklists, or otherwise edits anything in Radarr. Every function below
# issues GET requests only. Do not add a POST/PUT/DELETE call here.
# ---------------------------------------------------------------------------

_API_KEY_RE = re.compile(r"<ApiKey>([^<]+)</ApiKey>")


def read_radarr_api_key(config_path: Path) -> str:
    """Read Radarr's API key straight out of its own config.xml at runtime.
    Never hardcode a key into this file -- a sibling tool in this stack
    once committed a live key into its source and README, which is exactly
    the mistake this function exists to avoid."""
    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8")
    match = _API_KEY_RE.search(text)
    if not match:
        raise ValueError(f"No <ApiKey> element found in {config_path}")
    return match.group(1)


def fetch_wanted_missing(base_url: str, api_key: str, page_size: int = 200, max_pages: int = 50) -> list[dict]:
    """GET every record from Radarr's /wanted/missing list (paginating past
    a single page_size only if there are genuinely more records than that).
    Read-only: issues nothing but GET requests, carries no request body, and
    never calls any Radarr endpoint other than this one."""
    records: list[dict] = []
    page = 1
    while page <= max_pages:
        query = urllib.parse.urlencode({"pageSize": page_size, "page": page})
        url = f"{base_url.rstrip('/')}/api/v3/wanted/missing?{query}"
        request = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        batch = data.get("records", [])
        records.extend(batch)
        total = data.get("totalRecords", len(records))
        if len(batch) < page_size or len(records) >= total:
            break
        page += 1
    return records


# ---------------------------------------------------------------------------
# Human-readable verdict formatting
# ---------------------------------------------------------------------------

def format_verdict(title: str, year: int | None, verdict: Verdict) -> str:
    header = f"Film: {title}" + (f" ({year})" if year is not None else "")
    lines = [header, f"Verdict: {verdict.status}"]

    if verdict.status == "LATINO":
        lines.append(
            f'Evidence: confirmed Latin American Spanish dub on doblaje.fandom.com, page "{verdict.page_title}".'
        )
    elif verdict.status == "CASTELLANO":
        lines.append(
            f'Evidence: a dub was confirmed on doblaje.fandom.com, page "{verdict.page_title}", but it is '
            "credited to Spain (Castellano), not Latin America -- this is NOT the dub that was requested."
        )
    else:
        lines.append(
            "Evidence: no page on doblaje.fandom.com could be verified as this film. This means NOT FOUND -- "
            "it is not proof that a Latino dub does not exist, only that this tool could not confirm one "
            "today. The wiki may add it later, or a dub may exist undocumented there."
        )

    if verdict.notes:
        lines.append("Candidates considered:")
        for note in verdict.notes:
            lines.append(f"  - {note}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_RADARR_URL = "http://localhost:7878"
# Computed relative to this file rather than hardcoded to a specific user's
# home directory -- it resolves to the same "servarr/radarr/config.xml"
# sibling path either way, since dub-check/ lives directly inside servarr/.
DEFAULT_RADARR_CONFIG = Path(__file__).resolve().parent.parent / "radarr" / "config.xml"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache"
DEFAULT_RESULTS_PATH = Path(__file__).resolve().parent / "results.json"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dub_check.py",
        description="Does a Latino Spanish dub exist for a film? Checks doblaje.fandom.com.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    single = subparsers.add_parser("single", help="Check one film")
    single.add_argument("title", help="Film title as known locally (e.g. in Radarr)")
    single.add_argument("--original-title", default=None, help="The film's original-language title, if different")
    single.add_argument("--year", type=int, default=None,
                         help="Release year. Recommended: without it, only the title is verified.")
    single.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    single.add_argument("--min-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
                         help="Minimum seconds between real requests to the wiki")

    batch = subparsers.add_parser("batch", help="Check every film in Radarr's wanted/missing list")
    batch.add_argument("--radarr-url", default=DEFAULT_RADARR_URL)
    batch.add_argument("--radarr-config", type=Path, default=DEFAULT_RADARR_CONFIG)
    batch.add_argument("--out", type=Path, default=DEFAULT_RESULTS_PATH, help="Where to write results (JSON)")
    batch.add_argument("--limit", type=int, default=None, help="Only process the first N movies (useful for testing)")
    batch.add_argument("--force", action="store_true", help="Re-check movies already present in --out")
    batch.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    batch.add_argument("--min-interval", type=float, default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS)

    return parser


def cmd_single(args: argparse.Namespace) -> int:
    cache = DiskCache(args.cache_dir)
    client = WikiClient(cache, min_interval=args.min_interval)
    verdict = evaluate_film(args.title, args.original_title, args.year, client.search, client.get_wikitext)
    print(format_verdict(args.title, args.year, verdict))
    return 0


def _load_results(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_results(path: Path, results: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, sort_keys=True)


def _print_batch_summary(results: dict) -> None:
    counts = {"LATINO": 0, "CASTELLANO": 0, "NO_ENCONTRADO": 0}
    for entry in results.values():
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    print()
    print("=== Summary ===")
    print(f"LATINO       (confirmed Latino dub -- worth hunting):        {counts['LATINO']}")
    print(f"CASTELLANO   (Spain-only dub confirmed -- not what Daniel wants): {counts['CASTELLANO']}")
    print(f"NO_ENCONTRADO (no confirmed page today -- NOT proof one can never exist): {counts['NO_ENCONTRADO']}")


def cmd_batch(args: argparse.Namespace) -> int:
    api_key = read_radarr_api_key(args.radarr_config)
    movies = fetch_wanted_missing(args.radarr_url, api_key)  # READ-ONLY: GET only, see fetch_wanted_missing
    if args.limit is not None:
        movies = movies[: args.limit]

    results = _load_results(args.out)
    cache = DiskCache(args.cache_dir)
    client = WikiClient(cache, min_interval=args.min_interval)

    for movie in movies:
        movie_id = str(movie.get("id"))
        if movie_id in results and not args.force:
            continue  # already resolved in a previous run -- this is the resumability contract

        title = movie.get("title")
        original_title = movie.get("originalTitle")
        year = movie.get("year")
        try:
            verdict = evaluate_film(title, original_title, year, client.search, client.get_wikitext)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError,
                http.client.HTTPException, WikiResponseError) as exc:
            # TimeoutError / ConnectionError / HTTPException are listed
            # because urllib only wraps connect-phase failures in URLError;
            # a read timeout or a dropped connection arrives unwrapped.
            # Left out of `results` on purpose: a transient network failure
            # is not a verdict, and omitting the entry means the next run
            # retries this movie instead of silently treating it as done.
            print(f"WARNING: network error checking {title!r}: {exc} -- left for a future run", file=sys.stderr)
            continue

        results[movie_id] = {
            "title": title,
            "year": year,
            "status": verdict.status,
            "page_title": verdict.page_title,
            "notes": verdict.notes,
        }
        _save_results(args.out, results)  # flushed after every movie so a killed run loses no progress
        print(f"{title} ({year}): {verdict.status}")

    _print_batch_summary(results)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.mode == "single":
        return cmd_single(args)
    if args.mode == "batch":
        return cmd_batch(args)
    parser.error("unknown mode")  # argparse's required=True makes this unreachable in practice
    return 2


if __name__ == "__main__":
    sys.exit(main())
