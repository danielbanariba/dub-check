# dub-check

Answers one question for a film: **does a Latino Spanish dub exist?**

It exists because Radarr's wanted list accumulates movies that can never be
found in Latino Spanish, and chasing those forever wastes effort. This tool
checks [doblaje.fandom.com](https://doblaje.fandom.com) (Doblaje Wiki) so
that question doesn't have to be answered by hand, one film at a time, in a
chat window.

Python 3, standard library only. Read-only towards Radarr.

## Quick path

```bash
# One film
python3 dub_check.py single "Videodrome" --original-title "Videodrome" --year 1983

# Every film in Radarr's wanted/missing list
python3 dub_check.py batch
```

Single-film output:

```
Film: Videodrome (1983)
Verdict: LATINO
Evidence: confirmed Latin American Spanish dub on doblaje.fandom.com, page "Videodrome: Cuerpos invadidos".
Candidates considered:
  - "Videodrome: Cuerpos invadidos": VERIFIED (titulo_orig='Videodrome', año matches ('1983')) -> pais=['mexico'] -> latino
```

## The rule this whole tool exists to enforce

**A wiki search hit is never proof of a dub. It is only a candidate.**

doblaje.fandom.com's search is fuzzy, and it always returns *something* —
even for a film it has never heard of. Searching `Murder Death Koreatown`
(a film with no Latino dub) returns eight pages. None of them is the film:

| Search result | What it actually is |
|---|---|
| "Reporte de un asesino" | A different Korean film (titulo_orig: 살인자 리포트) |
| "Pareja dispareja" | A different Korean film (titulo_orig: 구필수는 없다) |
| "Pablo Moreno" | A voice actor's biography page — not a film at all |
| ...5 more | All different, unrelated pages |

A tool that treats "the search returned rows" as an answer would report a
Latino dub that does not exist, and send Daniel hunting for a phantom
release. **So every candidate the search returns gets opened, and its
infobox gets checked before it counts as evidence:**

1. The page's `titulo_orig` field (or, for a foreign film with several
   alternate international titles, one of its numbered `titulo_orig1`,
   `titulo_orig2`, ... variants) must equal the film's title.
2. The page's `año` field must contain the film's year, within a small
   tolerance (a dub's release year often differs from the film's — the
   wiki's own entry for *Pengabdi Setan* lists `año = 2017<br />2018
   <small>(México)</small>`: 2017 is the original release, 2018 is the dub).
3. Only if **both** match is the page trusted. A match on just one axis
   (same year, different film; same title, different year — a remake) is
   exactly the kind of coincidence a fuzzy search produces, so it does not
   count either.
4. Matching is **exact** (after normalizing case, accents, punctuation, and
   a leading article), never fuzzy/similarity-based. The search already
   contributes all the fuzziness this tool can tolerate — a second layer of
   "close enough" matching on top of it would make the trap worse, not
   better.

Once a page is trusted, its `pais` field — the **dub's** country, not the
film's own production country (`origen`) — decides the verdict:

| Verdict | Means | Example |
|---|---|---|
| `LATINO` | A verified page credits a Latin American country | México, Argentina, Chile, ... |
| `CASTELLANO` | A verified page exists, but only credits Spain | Not what was requested |
| `NO_ENCONTRADO` | No candidate page could be verified as this film | **Not** proof a dub doesn't exist |

**`NO_ENCONTRADO` is a statement about today's wiki, not a statement about
reality.** The tool says so in its own output, every time: the wiki may add
the page later, or a dub may already exist and simply not be documented
there yet. Treat it as "nothing to hunt for right now," never as "give up
on this film forever."

## Usage

### Single film

```bash
python3 dub_check.py single "<title>" [--original-title "<original>"] [--year YYYY]
```

`--year` is optional but strongly recommended — without it, only the title
is verified, which is a materially weaker guarantee (see the rule above:
title-only matches are exactly the coincidence this tool guards against).

### Batch (Radarr's wanted list)

```bash
python3 dub_check.py batch [--out results.json] [--limit N] [--force]
```

| Flag | Default | Purpose |
|---|---|---|
| `--radarr-url` | `http://localhost:7878` | Radarr base URL |
| `--radarr-config` | `../radarr/config.xml` | Where to read the API key from (see below) |
| `--out` | `dub-check/results.json` | Where results are written |
| `--limit N` | none | Only process the first N movies (useful for a quick test) |
| `--force` | off | Re-check movies already present in `--out` |
| `--cache-dir` | `dub-check/.cache` | Wiki response cache location |
| `--min-interval` | `1.5` seconds | Minimum spacing between real wiki requests |

Batch mode prints a running log, writes `results.json` after **every**
movie (not just at the end), and finishes with a LATINO / CASTELLANO /
NO_ENCONTRADO summary count.

**Resumable by default.** A movie already present in the results file is
skipped on the next run — kill the process partway through ~112 movies and
re-running picks up where it left off, at no cost to the wiki. Pass
`--force` to re-check everything anyway.

A movie whose year Radarr does not know yet (it reports `0`) is checked
title-only and printed, but **not saved**, so a later run retries it once
Radarr has the year.

## Radarr access

Read-only: the only Radarr call this tool makes is
`GET /api/v3/wanted/missing`. It never monitors, searches, blocklists, or
edits anything.

The API key is read out of Radarr's own `config.xml` **at runtime**, every
run — never hardcoded into this repository. (A sibling tool in this stack
once committed a live key into its source and README; this is exactly the
mistake that must not repeat.)

## Being a good citizen of the wiki

doblaje.fandom.com is a free, community-run resource, not an API meant for
bulk scripted access. This tool is built to stay a good guest:

- **Rate-limited**: real requests are spaced at least `--min-interval`
  seconds apart (default 1.5s), enforced in-process — never issued in
  parallel.
- **Cached on disk** (`.cache/`, one JSON file per query/page, keyed by a
  hash of the request): a re-run — single-film or batch — never re-asks the
  wiki anything it already answered. Even a confirmed-missing page is
  cached, so a film the wiki has never heard of isn't re-queried on every
  run. Entries never expire on their own; delete files under `.cache/` by
  hand to force a refresh.
- **A descriptive `User-Agent`**, and no personal data (name, email) is
  ever sent to the wiki in a header, URL, or request body.

## Development

```bash
python3 -m unittest test_dub_check -v
```

Built with strict TDD: every function in `dub_check.py` has tests written
and confirmed failing before it existed. The most important tests are the
trap-defense ones in `test_dub_check.py` — they feed `evaluate_film()` a
fake search that returns unrelated pages (mirroring real, verified
doblaje.fandom.com behavior) and assert the verdict is `NO_ENCONTRADO`, not
a false `LATINO`.

Several test fixtures are not invented: they are excerpts pulled live from
the real API while building this tool, because reality turned out messier
than the spec's own examples — one real page crams its entire infobox onto
a single line with no spaces (`{{Infobox||titulo=...|titulo_orig=...}}`),
and another has no plain `titulo_orig` field at all, only numbered
`titulo_orig1`/`titulo_orig2`/... variants for its several alternate
international titles. A synthetic-only test suite would never have caught
either.
