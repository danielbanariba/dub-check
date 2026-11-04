"""Tests for dub_check.py.

Strict TDD: this file is written and run RED before dub_check.py contains any
real implementation. Run with:

    python3 -m unittest test_dub_check -v

Several fixtures below are not invented -- they are verbatim (or lightly
trimmed) excerpts pulled live from https://doblaje.fandom.com/es/api.php
while designing this tool, because the wiki turned out to format its infobox
two different ways (see FakeWikiWikitextShapes) and a synthetic-only fixture
would have hidden that.
"""
import datetime
import email.message
import email.utils
import http.client
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dub_check import (
    normalize_title,
    extract_years,
    parse_infobox_fields,
    extract_countries,
    classify_countries,
    titles_match,
    year_matches,
    evaluate_candidate,
    evaluate_film,
    Verdict,
    CACHE_MISS,
    DiskCache,
    WikiClient,
    read_radarr_api_key,
    fetch_wanted_missing,
    format_verdict,
    build_arg_parser,
    main,
    DEFAULT_RADARR_CONFIG,
    strip_wiki_markup,
    extract_original_titles,
    WikiResponseError,
    _load_results,
    _save_results,
    API_BASE,
    RATE_LIMIT_INITIAL_BACKOFF_SECONDS,
    RATE_LIMIT_MAX_BACKOFF_SECONDS,
)


def fake_wiki_backend(search_results: dict, pages: dict):
    """Build a fake replacement for WikiClient._http_get_json that answers
    both the search and parse API actions from plain Python dicts, so CLI
    integration tests never touch the network."""

    def _fake(self, params):
        if params["action"] == "query":
            term = params["srsearch"]
            return {"query": {"search": [{"title": t} for t in search_results.get(term, [])]}}
        if params["action"] == "parse":
            title = params["page"]
            wikitext = pages.get(title)
            if wikitext is None:
                return {"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}
            return {"parse": {"wikitext": {"*": wikitext}}}
        raise AssertionError(f"unexpected API params in test: {params}")

    return _fake


class NormalizeTitleShouldMakeTwoSpellingsOfTheSameTitleComparable(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(normalize_title("Videodrome"), "videodrome")

    def test_strips_accents(self):
        # "México" vs "Mexico" must compare equal -- the wiki is not
        # consistent about typing accents in free text.
        self.assertEqual(normalize_title("México"), normalize_title("Mexico"))

    def test_strips_punctuation_without_merging_words(self):
        # Punctuation becomes a space, not nothing, so "Spider-Man" does not
        # silently collapse into "spiderman" and start matching unrelated
        # concatenations.
        self.assertEqual(normalize_title("Spider-Man"), "spider man")

    def test_collapses_repeated_whitespace(self):
        self.assertEqual(normalize_title("Videodrome:   Cuerpos  invadidos"), "videodrome cuerpos invadidos")

    def test_leaves_leading_article_by_default(self):
        # Article-stripping is opt-in (see titles_match): an exact match
        # should never depend on it.
        self.assertEqual(normalize_title("The Evil Dead"), "the evil dead")

    def test_strips_exactly_one_leading_article_when_requested(self):
        self.assertEqual(normalize_title("The Evil Dead", strip_article=True), "evil dead")
        self.assertEqual(normalize_title("La Casa del Diablo", strip_article=True), "casa del diablo")

    def test_strips_a_leading_english_a_or_an_when_requested(self):
        self.assertEqual(normalize_title("A Quiet Place", strip_article=True), "quiet place")
        self.assertEqual(normalize_title("An American Werewolf in London", strip_article=True),
                         "american werewolf in london")
        self.assertTrue(titles_match("Quiet Place", "A Quiet Place", None))
        self.assertTrue(titles_match("An American Werewolf in London", "American Werewolf in London", None))

    def test_decodes_html_entities_the_wiki_may_leave_in_a_field(self):
        self.assertEqual(normalize_title("Schindler&#39;s List"), normalize_title("Schindler's List"))
        self.assertEqual(normalize_title("Tom &amp; Jerry"), normalize_title("Tom & Jerry"))
        self.assertEqual(normalize_title("Amores&nbsp;perros"), "amores perros")

    def test_an_ampersand_compares_equal_to_and(self):
        self.assertTrue(titles_match("Tom &amp; Jerry", "Tom and Jerry", None))
        self.assertTrue(titles_match("Fast & Furious", "Fast and Furious", None))
        self.assertTrue(titles_match("Romeo+Juliet & Co", "Romeo+Juliet & Co", None))

    def test_a_word_merely_starting_with_a_or_an_is_not_an_article(self):
        self.assertEqual(normalize_title("Annie Hall", strip_article=True), "annie hall")
        self.assertEqual(normalize_title("Aliens", strip_article=True), "aliens")

    def test_does_not_strip_an_article_that_is_not_at_the_very_start(self):
        self.assertEqual(normalize_title("Return of the Living Dead", strip_article=True), "return of the living dead")


class ExtractYearsShouldFindEveryPlausibleFilmYearInMessyText(unittest.TestCase):
    def test_finds_a_single_plain_year(self):
        self.assertEqual(extract_years("1983"), {1983})

    def test_finds_both_years_in_the_real_pengabdi_setan_ano_field(self):
        # Verbatim field value from doblaje.fandom.com's "Los huérfanos" page:
        # the original release year and the Latino dub's release year are
        # both packed into one field, separated by an HTML line break.
        raw = "2017<br />2018 <small>(México)</small>"
        self.assertEqual(extract_years(raw), {2017, 2018})

    def test_ignores_numbers_that_are_not_four_digits(self):
        self.assertEqual(extract_years("episode 20, season 5"), set())

    def test_ignores_a_longer_digit_run_that_merely_contains_a_year_looking_substring(self):
        # A pageid, timestamp, or phone-number-shaped string must not be
        # mistaken for an embedded year.
        self.assertEqual(extract_years("id 20171234567"), set())

    def test_ignores_years_inside_html_comments(self):
        self.assertEqual(extract_years("2011 <!-- precuela de la película de 1982 -->"), {2011})
        self.assertEqual(extract_years("2011 <!-- unterminated 1982"), {2011})

    def test_ignores_years_inside_ref_footnotes(self):
        self.assertEqual(extract_years("2011<ref>Estreno de la versión de 1982 en México</ref>"), {2011})
        self.assertEqual(extract_years('2011<ref name="imdb1982">IMDb</ref>'), {2011})
        self.assertEqual(extract_years('2011<ref name="imdb1982" />'), {2011})

    def test_returns_an_empty_set_for_text_with_no_year(self):
        self.assertEqual(extract_years(""), set())
        self.assertEqual(extract_years("sin fecha"), set())


# Real wikitext shapes pulled from the live API. Keeping both as named
# constants (rather than only ad-hoc strings per test) documents that the
# parser must handle BOTH, not just whichever one a test author reached for.
class FakeWikiWikitextShapes:
    # https://doblaje.fandom.com/es/api.php?action=parse&page=Videodrome:%20Cuerpos%20invadidos&prop=wikitext&format=json
    # One field per line. This is the shape the task spec's own examples use.
    VIDEODROME = """{{Infobox
|titulo = Videodrome:<br>Cuerpos invadidos
|img = Videodrome_poster.jpg
|titulo_orig = Videodrome
|version_español = [[Archivo:Universal_1963_Logo.png|85px]]<br /><small>(doblaje original)</small><br />[[Archivo:Universal_1990_Logo.png|85px]]<br /><small>(redoblaje)</small>
|pais = {{Bandera|México}} México
|origen = {{Bandera|Canadá}} Canadá
|año = 1983
|idioma_orig = {{Bandera|Canadá}} {{Bandera|Estados Unidos}} Inglés
}}
'''''Videodrome: Cuerpos invadidos''''' es una película de 1983 dirigida por [[David Cronenberg]].
"""

    # https://doblaje.fandom.com/es/api.php?action=parse&page=Reporte%20de%20un%20asesino&prop=wikitext&format=json
    # Everything crammed onto a single line, no spaces around "=", and a
    # double "||" right after the template name. A line-anchored regex would
    # glue every later field onto titulo_orig's value on a page like this.
    REPORTE_DE_UN_ASESINO = (
        "{{Infobox||titulo=Reporte de un asesino|img=|titulo_orig=살인자 리포트"
        "|idioma_orig={{Bandera|Corea del Sur}} Coreano"
        "|estudio_doblaje=[[Crystaldub]]"
        "|direccion_doblaje=[[Sebastián Pinardi]]"
        "|direc_creativo=[[Santiago Díaz Graham|Santiago Rodrigo Díaz Graham]]"
        "|version_español=[[Archivo:Sony Pictures.png|55px|link=http://www.sonypictures.com.mx/]]"
        "|pais={{Bandera|Argentina}} Argentina"
        "|origen={{Bandera|Corea del Sur}} Corea del Sur"
        "|año=2025"
        "|predecesor=}}"
        "'''Reporte de un asesino''' es una película surcoreana de 2025."
    )

    # https://doblaje.fandom.com/es/api.php?action=parse&page=Pablo%20Moreno&prop=wikitext&format=json
    # A completely different infobox (a voice actor's bio page, one of the
    # real unrelated hits the wiki's fuzzy search mixes into results) that
    # has no titulo_orig or año at all.
    UNRELATED_PERSON_PAGE = """{{Infoactor
|nombre = Pablo Moreno
|nacionalidad = {{Bandera|México}} Mexicano
|ocupacion = Actor
}}
"""

    # Spain-only dub, built from the field values given verbatim in the task
    # spec (pais is the dub's country, not the film's -- origen is Canada,
    # the film's own production country, and must be ignored for the verdict).
    SPAIN_ONLY_DUB = """{{Infobox
|titulo_orig = Some Spanish-Only Film
|pais = {{Bandera|España}} España
|origen = {{Bandera|Canadá}} Canadá
|año = 1999
}}
"""

    # https://doblaje.fandom.com/es/api.php?action=parse&page=Pareja%20dispareja&prop=wikitext&format=json
    # A real "Murder Death Koreatown" trap-case candidate: titulo, titulo_orig1
    # and titulo_orig2 below are verbatim from the live page. This page has
    # NO plain "titulo_orig" field at all -- a foreign film with several
    # alternate international titles gets numbered variants instead
    # (titulo_orig1, titulo_orig2, ...). titulo_orig1 also demonstrates the
    # wiki's <small>...</small> convention for a secondary annotation (here,
    # a romanization note) riding along inside the same field value.
    # pais/origen/año were NOT part of the truncated recon fetch and are
    # filled in here with plausible values, only to exercise the matching
    # logic end to end.
    PAREJA_DISPAREJA = (
        "{{Infobox \n"
        "|titulo = Never Give Up\n"
        "|img = Never Give Up - poster.png\n"
        "|titulo_orig1 = {{Bandera|Corea del Sur}} 구필수는 없다 <br><small>''(gupilsuneun eobsda)''</small> \n"
        "|titulo_orig2 = [[Archivo:Internacional.png|15px|Internacional]]  Goo Pil Soo is Not There\n"
        "|pais = {{Bandera|Argentina}} Argentina\n"
        "|origen = {{Bandera|Corea del Sur}} Corea del Sur\n"
        "|año = 2024\n"
        "}}\n"
    )


class ParseInfoboxFieldsShouldSurviveBothRealWikiFormattingStyles(unittest.TestCase):
    def test_extracts_fields_from_the_one_field_per_line_format(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.VIDEODROME)
        self.assertEqual(fields["titulo_orig"], "Videodrome")
        self.assertEqual(fields["pais"], "{{Bandera|México}} México")
        self.assertEqual(fields["origen"], "{{Bandera|Canadá}} Canadá")
        self.assertEqual(fields["año"], "1983")

    def test_does_not_swallow_later_fields_into_an_earlier_ones_value_on_a_wikilink_with_a_pipe(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.VIDEODROME)
        # version_español's value contains "[[Archivo:...|85px]]" -- the pipe
        # inside the wikilink must not be read as a field separator, and
        # must not bleed into the next field (pais).
        self.assertNotIn("pais", fields["version_español"])
        self.assertEqual(fields["pais"], "{{Bandera|México}} México")

    def test_extracts_fields_from_the_single_line_crammed_format(self):
        # This is the exact bug real reconnaissance caught: a line-anchored
        # regex has no line break to stop at here, so it would capture the
        # rest of the entire infobox as titulo_orig's "value".
        fields = parse_infobox_fields(FakeWikiWikitextShapes.REPORTE_DE_UN_ASESINO)
        self.assertEqual(fields["titulo_orig"], "살인자 리포트")
        self.assertEqual(fields["pais"], "{{Bandera|Argentina}} Argentina")
        self.assertEqual(fields["año"], "2025")

    def test_a_pipe_inside_a_nested_wikilink_display_text_is_not_a_field_separator(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.REPORTE_DE_UN_ASESINO)
        # direc_creativo = [[Santiago Díaz Graham|Santiago Rodrigo Díaz Graham]]
        self.assertIn("Santiago Rodrigo", fields["direc_creativo"])

    def test_a_page_with_a_different_infobox_template_yields_no_titulo_orig_or_ano(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE)
        self.assertNotIn("titulo_orig", fields)
        self.assertNotIn("año", fields)

    def test_a_pipe_inside_a_ref_footnote_is_not_a_field_separator(self):
        # MediaWiki does not split a template field on a "|" inside <ref>.
        wikitext = ("{{Infobox|titulo_orig = Videodrome<ref>Título en Canadá | según IMDb | lang=en</ref>"
                    "|pais = {{Bandera|México}} México|año = 1983<ref name=\"a\" />}}")
        fields = parse_infobox_fields(wikitext)
        self.assertEqual(fields["titulo_orig"], "Videodrome<ref>Título en Canadá | según IMDb | lang=en</ref>")
        self.assertNotIn("lang", fields)
        self.assertEqual(fields["pais"], "{{Bandera|México}} México")
        self.assertEqual(fields["año"], '1983<ref name="a" />')

    def test_a_commented_out_field_does_not_override_the_real_one(self):
        # MediaWiki drops <!-- comments --> before reading template fields;
        # a commented-out "|año = ..." must not become the field's value.
        fields = parse_infobox_fields("{{Infobox|titulo_orig = The Thing|año = 2011 <!-- |año = 1982 -->}}")
        self.assertEqual(fields["año"], "2011")

    def test_finds_the_film_infobox_when_another_template_comes_first(self):
        # A hatnote / maintenance template above the infobox must not make
        # the page unverifiable.
        wikitext = (
            "{{Otros usos|Videodrome (desambiguación)}}\n"
            "{{Esbozo|fecha=2020}}\n"
            + FakeWikiWikitextShapes.VIDEODROME
        )
        fields = parse_infobox_fields(wikitext)
        self.assertEqual(fields["titulo_orig"], "Videodrome")
        self.assertEqual(fields["año"], "1983")

    def test_finds_an_infobox_with_only_numbered_titulo_orig_variants_after_another_template(self):
        fields = parse_infobox_fields("{{Esbozo}}\n" + FakeWikiWikitextShapes.PAREJA_DISPAREJA)
        self.assertIn("titulo_orig1", fields)

    def test_a_film_template_in_the_article_body_is_not_taken_as_the_pages_infobox(self):
        # Only templates at the head of the page can be its infobox. A voice
        # actor's page that embeds a film template further down, after the
        # prose, is still not that film's page.
        wikitext = (
            FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE
            + "'''Pablo Moreno''' es un actor de doblaje mexicano.\n\n== Filmografía ==\n"
            + "{{Infobox|titulo_orig = Murder Death Koreatown|pais = {{Bandera|México}} México|año = 2020}}\n"
        )
        self.assertEqual(parse_infobox_fields(wikitext)["nombre"], "Pablo Moreno")
        verdict = evaluate_film("Murder Death Koreatown", "Murder Death Koreatown", 2020,
                                lambda term, limit: ["Pablo Moreno"], {"Pablo Moreno": wikitext}.get)
        self.assertEqual(verdict.status, "NO_ENCONTRADO")

    def test_a_page_with_no_film_infobox_still_yields_its_first_templates_fields(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE + "{{Esbozo|fecha=2020}}")
        self.assertEqual(fields["nombre"], "Pablo Moreno")

    def test_returns_an_empty_dict_for_text_with_no_template_at_all(self):
        self.assertEqual(parse_infobox_fields("just some prose, no braces here"), {})

    def test_unterminated_wikitext_does_not_silently_lose_the_last_two_characters_of_a_value(self):
        # Malformed/truncated wikitext (no closing "}}" anywhere) must fall
        # back to "take the rest of the string", not chop two real
        # characters off whatever value happens to be last.
        wikitext = "{{Infobox|titulo_orig = Broken Page No Closing Braces"
        fields = parse_infobox_fields(wikitext)
        self.assertEqual(fields["titulo_orig"], "Broken Page No Closing Braces")

    def test_numbered_titulo_orig_variants_are_extracted_as_distinct_fields(self):
        # Real page "Pareja dispareja" has no plain titulo_orig at all --
        # only titulo_orig1, titulo_orig2, ... for its several alternate
        # international titles.
        fields = parse_infobox_fields(FakeWikiWikitextShapes.PAREJA_DISPAREJA)
        self.assertNotIn("titulo_orig", fields)
        self.assertIn("titulo_orig1", fields)
        self.assertIn("titulo_orig2", fields)
        self.assertIn("구필수는 없다", fields["titulo_orig1"])


class StripWikiMarkupShouldLeaveOnlyHumanReadableText(unittest.TestCase):
    def test_leaves_plain_text_with_no_markup_unchanged(self):
        self.assertEqual(strip_wiki_markup("Videodrome"), "Videodrome")

    def test_removes_a_bandera_template_entirely(self):
        self.assertEqual(strip_wiki_markup("{{Bandera|México}} México"), "México")

    def test_removes_a_small_annotation_span_including_its_content(self):
        # Every real example seen while building this tool uses <small> for
        # a SECONDARY annotation, never the title itself: a romanization
        # note, or a "(doblaje original)" / "(redoblaje)" version label.
        self.assertEqual(
            strip_wiki_markup("Videodrome <small>(doblaje original)</small>"),
            "Videodrome",
        )

    def test_removes_remaining_html_tags(self):
        self.assertEqual(strip_wiki_markup("Cuerpos<br>invadidos"), "Cuerpos invadidos")

    def test_removes_bold_and_italic_markers(self):
        self.assertEqual(strip_wiki_markup("'''Videodrome'''"), "Videodrome")

    def test_a_plain_wikilink_keeps_its_display_text(self):
        self.assertEqual(strip_wiki_markup("[[Some Target|Display Text]]"), "Display Text")
        self.assertEqual(strip_wiki_markup("[[Bare Target]]"), "Bare Target")

    def test_a_file_or_image_embed_is_removed_entirely_not_kept_as_text(self):
        # [[Archivo:...|options|caption]] is a decorative icon, not title
        # text -- verified live on "Pareja dispareja"'s titulo_orig2.
        self.assertEqual(
            strip_wiki_markup("[[Archivo:Internacional.png|15px|Internacional]] Goo Pil Soo is Not There"),
            "Goo Pil Soo is Not There",
        )

    def test_cleans_the_real_pareja_dispareja_titulo_orig1_field_down_to_the_korean_title(self):
        raw = "{{Bandera|Corea del Sur}} 구필수는 없다 <br><small>''(gupilsuneun eobsda)''</small>"
        self.assertEqual(strip_wiki_markup(raw), "구필수는 없다")

    def test_an_empty_value_stays_empty(self):
        self.assertEqual(strip_wiki_markup(""), "")


class ExtractOriginalTitlesShouldCollectEveryPlainOrNumberedVariant(unittest.TestCase):
    def test_returns_the_plain_titulo_orig_when_present(self):
        fields = {"titulo_orig": "Videodrome"}
        self.assertEqual(extract_original_titles(fields), ["Videodrome"])

    def test_returns_every_numbered_variant_cleaned_of_markup_when_there_is_no_plain_field(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.PAREJA_DISPAREJA)
        titles = extract_original_titles(fields)
        self.assertIn("구필수는 없다", titles)
        self.assertIn("Goo Pil Soo is Not There", titles)

    def test_several_titles_in_one_field_separated_by_br_are_each_a_candidate(self):
        fields = {"titulo_orig": "Pengabdi Setan<br />Satan's Slaves <small>(internacional)</small>"}
        titles = extract_original_titles(fields)
        self.assertIn("Pengabdi Setan", titles)
        self.assertIn("Satan's Slaves", titles)

    def test_a_br_separated_alternate_title_verifies_the_page(self):
        fields = parse_infobox_fields(
            "{{Infobox|titulo_orig = Shingeki no Kyojin<br>進撃の巨人|pais = {{Bandera|México}} México|año = 2015}}"
        )
        result = evaluate_candidate("Ataque a los titanes", fields, "Attack on Titan", "進撃の巨人", 2015, 1)
        self.assertTrue(result.verified)

    def test_a_title_written_across_a_br_still_matches_as_a_whole(self):
        # "<br>" is also used as a plain line break inside one long title.
        fields = {"titulo_orig": "Videodrome:<br>Cuerpos invadidos"}
        self.assertTrue(titles_match(extract_original_titles(fields)[0], "Videodrome: Cuerpos invadidos", None))

    def test_returns_an_empty_list_when_no_variant_is_present_at_all(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE)
        self.assertEqual(extract_original_titles(fields), [])


class ExtractAndClassifyCountriesShouldReadTheDubsCountryNotTheFilmsCountry(unittest.TestCase):
    def test_extracts_a_single_bandera_country(self):
        self.assertEqual(extract_countries("{{Bandera|México}} México"), ["mexico"])

    def test_classifies_a_latin_american_country_as_latino(self):
        self.assertEqual(classify_countries(["mexico"]), "latino")
        self.assertEqual(classify_countries(["argentina"]), "latino")
        self.assertEqual(classify_countries(["peru"]), "latino")

    def test_classifies_spain_as_castellano(self):
        self.assertEqual(extract_countries("{{Bandera|España}} España"), ["espana"])
        self.assertEqual(classify_countries(["espana"]), "castellano")

    def test_a_mix_of_latin_american_and_spain_is_latino_because_a_latino_dub_does_exist(self):
        # The question this whole tool answers is "does a Latino dub exist",
        # so a page jointly crediting México and España must not lose the
        # Latino signal just because España is also present.
        self.assertEqual(classify_countries(["mexico", "espana"]), "latino")

    def test_a_dub_credited_to_estados_unidos_is_latino(self):
        # Latin American Spanish dubs recorded in Miami / Los Angeles are
        # credited to Estados Unidos on the wiki.
        countries = extract_countries("{{Bandera|Estados Unidos}} Estados Unidos")
        self.assertEqual(countries, ["estados unidos"])
        self.assertEqual(classify_countries(countries), "latino")

    def test_a_dub_credited_to_latinoamerica_is_latino(self):
        self.assertEqual(classify_countries(extract_countries("{{Bandera|Latinoamérica}} Latinoamérica")), "latino")
        self.assertEqual(classify_countries(extract_countries("Latinoamérica")), "latino")

    def test_a_film_produced_in_the_us_but_dubbed_in_spain_is_still_castellano(self):
        # origen is never read: only the dub's own country (pais) counts.
        fields = parse_infobox_fields("{{Infobox|titulo_orig = X|pais = {{Bandera|España}} España"
                                      "|origen = {{Bandera|Estados Unidos}} Estados Unidos|año = 2000}}")
        self.assertEqual(evaluate_candidate("X", fields, "X", "X", 2000, 1).country_class, "castellano")

    def test_an_unrecognized_country_alone_is_unknown_not_silently_latino_or_castellano(self):
        self.assertEqual(classify_countries(["narnia"]), "unknown")

    def test_an_empty_country_list_is_unknown(self):
        self.assertEqual(classify_countries([]), "unknown")


class TitlesMatchShouldRequireAnExactNormalizedMatchNeverFuzzySimilarity(unittest.TestCase):
    def test_matches_the_identical_title(self):
        self.assertTrue(titles_match("Videodrome", "Videodrome", "Videodrome"))

    def test_matches_case_and_accent_insensitively(self):
        self.assertTrue(titles_match("videodrome", "Videodrome", None))

    def test_matches_against_original_title_when_the_radarr_title_differs(self):
        self.assertTrue(titles_match("Pengabdi Setan", "Some Localized Title", "Pengabdi Setan"))

    def test_matches_when_only_a_leading_article_differs(self):
        self.assertTrue(titles_match("Evil Dead", "The Evil Dead", None))

    def test_does_not_match_a_similar_but_different_title(self):
        # This is the crux of the whole tool: "close" is not a match.
        # "Death Race" is not "Murder Death Koreatown", no matter how much
        # fuzzy search wants them to be related.
        self.assertFalse(titles_match("Death Race", "Murder Death Koreatown", "Murder Death Koreatown"))

    def test_does_not_match_a_title_that_shares_only_one_word(self):
        self.assertFalse(titles_match("Koreatown Ghost Story", "Murder Death Koreatown", "Murder Death Koreatown"))

    def test_a_missing_candidate_title_never_matches(self):
        self.assertFalse(titles_match(None, "Videodrome", "Videodrome"))
        self.assertFalse(titles_match("", "Videodrome", "Videodrome"))

    def test_two_different_punctuation_only_titles_do_not_match(self):
        # Both normalize to "" -- that is the absence of a title, not an
        # exact match. A placeholder titulo_orig ("—", "-", "?") must not
        # verify against a film whose title is itself only punctuation.
        self.assertFalse(titles_match("—", "?", "?"))
        self.assertFalse(titles_match("-", "...", None))

    def test_a_placeholder_titulo_orig_does_not_produce_a_false_latino(self):
        pages = {"Otra película": "{{Infobox|titulo_orig = —|pais = {{Bandera|México}} México|año = 2011}}"}
        verdict = evaluate_film("?", "?", 2011, lambda term, limit: ["Otra película"], pages.get)
        self.assertEqual(verdict.status, "NO_ENCONTRADO")


class YearMatchesShouldTolerateTheDubYearDifferingFromTheReleaseYear(unittest.TestCase):
    def test_matches_an_exact_year(self):
        self.assertTrue(year_matches("1983", 1983, tolerance=1))

    def test_matches_when_the_target_year_is_one_of_several_embedded_years(self):
        # Real Pengabdi Setan field: original release 2017, Latino dub 2018.
        self.assertTrue(year_matches("2017<br />2018 <small>(México)</small>", 2017, tolerance=1))

    def test_matches_within_tolerance_even_if_not_exact(self):
        self.assertTrue(year_matches("1984", 1983, tolerance=1))

    def test_rejects_a_year_outside_tolerance(self):
        self.assertFalse(year_matches("1990", 1983, tolerance=1))

    def test_returns_none_meaning_not_checked_when_no_target_year_was_given(self):
        self.assertIsNone(year_matches("1983", None, tolerance=1))

    def test_rejects_when_the_field_has_no_parseable_year(self):
        self.assertFalse(year_matches("sin fecha", 1983, tolerance=1))

    def test_rejects_when_the_field_is_missing_entirely(self):
        self.assertFalse(year_matches(None, 1983, tolerance=1))


class EvaluateCandidateShouldRequireBothTitleAndYearBeforeTrustingACountry(unittest.TestCase):
    def test_verified_latino_candidate(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.VIDEODROME)
        result = evaluate_candidate("Videodrome: Cuerpos invadidos", fields, "Videodrome", "Videodrome", 1983, 1)
        self.assertTrue(result.verified)
        self.assertEqual(result.country_class, "latino")

    def test_verified_castellano_candidate(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.SPAIN_ONLY_DUB)
        result = evaluate_candidate("Spain Page", fields, "Some Spanish-Only Film", "Some Spanish-Only Film", 1999, 1)
        self.assertTrue(result.verified)
        self.assertEqual(result.country_class, "castellano")

    def test_rejects_when_title_does_not_match_even_if_year_does(self):
        # Coincidence guard: sharing a year is not evidence of identity.
        fields = parse_infobox_fields(FakeWikiWikitextShapes.REPORTE_DE_UN_ASESINO)  # año=2025
        result = evaluate_candidate("Reporte de un asesino", fields, "Some Other Film From 2025", "Some Other Film From 2025", 2025, 1)
        self.assertFalse(result.verified)

    def test_rejects_when_year_does_not_match_even_if_title_does(self):
        # Coincidence guard the other way: a title match alone is not enough
        # either -- guards against a remake/reboot sharing a title.
        fields = parse_infobox_fields(FakeWikiWikitextShapes.VIDEODROME)  # titulo_orig=Videodrome, año=1983
        result = evaluate_candidate("Videodrome: Cuerpos invadidos", fields, "Videodrome", "Videodrome", 2023, 1)
        self.assertFalse(result.verified)

    def test_rejects_a_page_with_no_film_infobox_at_all(self):
        fields = parse_infobox_fields(FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE)
        result = evaluate_candidate("Pablo Moreno", fields, "Pablo Moreno", "Pablo Moreno", 1983, 1)
        self.assertFalse(result.verified)

    def test_verifies_via_a_numbered_titulo_orig_variant_when_there_is_no_plain_field(self):
        # Real page "Pareja dispareja" has no plain titulo_orig -- only
        # titulo_orig1 (Korean) and titulo_orig2 (an English alt-title).
        # Matching against the Korean original title must still work.
        fields = parse_infobox_fields(FakeWikiWikitextShapes.PAREJA_DISPAREJA)
        result = evaluate_candidate("Pareja dispareja", fields, "Never Give Up", "구필수는 없다", 2024, 1)
        self.assertTrue(result.verified)
        self.assertEqual(result.country_class, "latino")

    def test_the_real_pareja_dispareja_page_is_correctly_rejected_as_a_murder_death_koreatown_candidate(self):
        # Regression for the actual trap case: this real candidate page
        # must NOT verify against an unrelated film, now that it is being
        # matched through its numbered titulo_orig variants too.
        fields = parse_infobox_fields(FakeWikiWikitextShapes.PAREJA_DISPAREJA)
        result = evaluate_candidate("Pareja dispareja", fields, "Murder Death Koreatown", "Murder Death Koreatown", 2020, 1)
        self.assertFalse(result.verified)


class EvaluateFilmShouldNeverTrustASearchHitWithoutOpeningAndVerifyingThePage(unittest.TestCase):
    """The trap, end to end. A fake search_fn always returns *something* --
    exactly like the real doblaje.fandom.com search does -- and evaluate_film
    must still say NO_ENCONTRADO unless a returned candidate page actually
    verifies as the requested film."""

    def test_murder_death_koreatown_is_no_encontrado_even_though_search_returns_five_hits(self):
        # These are the REAL page titles doblaje.fandom.com's search returns
        # for "Murder Death Koreatown" (a film with no Latino dub). None of
        # them is the film in question.
        real_unrelated_hits = [
            "Reporte de un asesino",
            "Pareja dispareja",
            "El complejo de apartamentos",
            "Negocio letal",
            "Pablo Moreno",
        ]

        def fake_search(term, limit):
            return real_unrelated_hits

        pages = {
            "Reporte de un asesino": FakeWikiWikitextShapes.REPORTE_DE_UN_ASESINO,
            "Pareja dispareja": "{{Infobox|titulo_orig = Some Unrelated Comedy|pais = {{Bandera|México}} México|año = 2018}}",
            "El complejo de apartamentos": "{{Infobox|titulo_orig = The Apartment Complex|pais = {{Bandera|Colombia}} Colombia|año = 2022}}",
            "Negocio letal": "{{Infobox|titulo_orig = Lethal Business|pais = {{Bandera|Venezuela}} Venezuela|año = 2015}}",
            "Pablo Moreno": FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE,
        }

        def fake_fetch(page_title):
            return pages.get(page_title)

        verdict = evaluate_film("Murder Death Koreatown", "Murder Death Koreatown", 2020, fake_search, fake_fetch)

        self.assertEqual(verdict.status, "NO_ENCONTRADO")
        self.assertIsNone(verdict.page_title)

    def test_a_year_mentioned_only_in_a_comment_does_not_verify_a_same_name_remake(self):
        # The 2011 "The Thing" page must not verify as the 1982 film just
        # because an editor's comment in its año field mentions 1982.
        pages = {"La cosa (2011)": "{{Infobox|titulo_orig = The Thing|pais = {{Bandera|México}} México"
                                   "|año = 2011 <!-- precuela de la película de 1982 -->}}"}
        verdict = evaluate_film("The Thing", "The Thing", 1982, lambda term, limit: ["La cosa (2011)"], pages.get)
        self.assertEqual(verdict.status, "NO_ENCONTRADO")

    def test_a_search_returning_zero_hits_is_also_no_encontrado(self):
        verdict = evaluate_film("Some Obscure Film", "Some Obscure Film", 2020, lambda term, limit: [], lambda title: None)
        self.assertEqual(verdict.status, "NO_ENCONTRADO")

    def test_finds_latino_when_the_wiki_page_title_is_the_dub_title_not_the_original_title(self):
        # Mirrors the real Pengabdi Setan case: the page is titled after the
        # Latino dub ("Los huérfanos"), not the original title, so matching
        # must key off titulo_orig, never off the page title string itself.
        def fake_search(term, limit):
            return ["Some Unrelated Film", "Los huérfanos"]

        pages = {
            "Some Unrelated Film": "{{Infobox|titulo_orig = Some Unrelated Film|pais = {{Bandera|España}} España|año = 2010}}",
            "Los huérfanos": "{{Infobox|titulo_orig = Pengabdi Setan|pais = {{Bandera|México}} México|año = 2017<br />2018 <small>(México)</small>}}",
        }

        verdict = evaluate_film("Pengabdi Setan", "Pengabdi Setan", 2017, fake_search, pages.get)

        self.assertEqual(verdict.status, "LATINO")
        self.assertEqual(verdict.page_title, "Los huérfanos")

    def test_reports_castellano_when_only_a_spain_dub_verifies(self):
        def fake_search(term, limit):
            return ["Spain Page"]

        pages = {"Spain Page": FakeWikiWikitextShapes.SPAIN_ONLY_DUB}

        verdict = evaluate_film("Some Spanish-Only Film", "Some Spanish-Only Film", 1999, fake_search, pages.get)

        self.assertEqual(verdict.status, "CASTELLANO")

    def test_prefers_latino_over_castellano_when_both_a_latino_and_a_spain_dub_verify(self):
        def fake_search(term, limit):
            return ["Spain Page", "Latino Page"]

        pages = {
            "Spain Page": FakeWikiWikitextShapes.SPAIN_ONLY_DUB,
            "Latino Page": "{{Infobox|titulo_orig = Some Spanish-Only Film|pais = {{Bandera|Argentina}} Argentina|año = 1999}}",
        }

        verdict = evaluate_film("Some Spanish-Only Film", "Some Spanish-Only Film", 1999, fake_search, pages.get)

        self.assertEqual(verdict.status, "LATINO")
        self.assertEqual(verdict.page_title, "Latino Page")

    def test_a_page_fetch_failure_for_one_candidate_does_not_crash_and_other_candidates_are_still_checked(self):
        def fake_search(term, limit):
            return ["Vanished Page", "Los huérfanos"]

        pages = {
            "Los huérfanos": "{{Infobox|titulo_orig = Pengabdi Setan|pais = {{Bandera|México}} México|año = 2017}}",
        }

        verdict = evaluate_film("Pengabdi Setan", "Pengabdi Setan", 2017, fake_search, pages.get)

        self.assertEqual(verdict.status, "LATINO")

    def test_does_not_query_the_second_search_term_once_a_latino_match_is_already_confirmed(self):
        # Gentleness: once the best possible answer is found from the first
        # search term, there is no reason to spend another request on the
        # wiki for the same film.
        calls = []

        def fake_search(term, limit):
            calls.append(term)
            return ["Los huérfanos"]

        pages = {
            "Los huérfanos": "{{Infobox|titulo_orig = Pengabdi Setan|pais = {{Bandera|México}} México|año = 2017}}",
        }

        evaluate_film("Pengabdi Setan (English Title)", "Pengabdi Setan", 2017, fake_search, pages.get)

        self.assertEqual(calls, ["Pengabdi Setan"])

    def test_still_tries_the_second_search_term_after_a_castellano_only_match_to_look_for_a_better_latino_answer(self):
        # CASTELLANO is not the answer Daniel wants. Finding a Spain-only
        # dub under the first search term must not stop the search early --
        # the second term might still turn up a genuine Latino dub, which
        # is strictly better information and the whole point of the tool.
        calls = []

        def fake_search(term, limit):
            calls.append(term)
            if term == "Original Title":
                return ["Spain Page"]
            return ["Latino Page"]

        pages = {
            "Spain Page": "{{Infobox|titulo_orig = Original Title|pais = {{Bandera|España}} España|año = 2020}}",
            "Latino Page": "{{Infobox|titulo_orig = Original Title|pais = {{Bandera|Chile}} Chile|año = 2020}}",
        }

        verdict = evaluate_film("English Title", "Original Title", 2020, fake_search, pages.get)

        self.assertEqual(calls, ["Original Title", "English Title"])
        self.assertEqual(verdict.status, "LATINO")
        self.assertEqual(verdict.page_title, "Latino Page")

    def test_tries_the_second_search_term_when_the_first_term_finds_nothing(self):
        calls = []

        def fake_search(term, limit):
            calls.append(term)
            if term == "Original Title":
                return []
            return ["Match Page"]

        pages = {
            "Match Page": "{{Infobox|titulo_orig = Original Title|pais = {{Bandera|Chile}} Chile|año = 2020}}",
        }

        verdict = evaluate_film("English Title", "Original Title", 2020, fake_search, pages.get)

        self.assertEqual(calls, ["Original Title", "English Title"])
        self.assertEqual(verdict.status, "LATINO")


class DiskCacheShouldPersistResultsAcrossRunsSoAReRunDoesNotReAsk(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_a_value_that_was_set_can_be_read_back(self):
        cache = DiskCache(self.root)
        cache.set("search", "videodrome::8", ["Videodrome: Cuerpos invadidos"])
        self.assertEqual(cache.get("search", "videodrome::8"), ["Videodrome: Cuerpos invadidos"])

    def test_an_unset_key_reports_a_distinct_miss_not_none(self):
        # None is a legitimate cached value (a confirmed-missing wiki page),
        # so "not cached yet" must be distinguishable from "cached as None".
        cache = DiskCache(self.root)
        self.assertIs(cache.get("page", "Nonexistent Page"), CACHE_MISS)

    def test_none_round_trips_as_a_real_cached_value_distinct_from_a_miss(self):
        cache = DiskCache(self.root)
        cache.set("page", "Some Page With No Wiki Entry", None)
        self.assertIsNone(cache.get("page", "Some Page With No Wiki Entry"))

    def test_the_same_key_in_two_different_namespaces_does_not_collide(self):
        cache = DiskCache(self.root)
        cache.set("search", "X", "search-value")
        cache.set("page", "X", "page-value")
        self.assertEqual(cache.get("search", "X"), "search-value")
        self.assertEqual(cache.get("page", "X"), "page-value")

    def test_a_value_survives_into_a_fresh_diskcache_instance_pointed_at_the_same_directory(self):
        # This is what makes batch mode resumable across separate process runs.
        DiskCache(self.root).set("page", "Videodrome: Cuerpos invadidos", "{{Infobox|...}}")
        reopened = DiskCache(self.root)
        self.assertEqual(reopened.get("page", "Videodrome: Cuerpos invadidos"), "{{Infobox|...}}")

    def test_a_write_interrupted_partway_leaves_a_miss_not_a_corrupt_entry(self):
        # README: "kill the process partway through ... re-running picks up
        # where it left off". A Ctrl-C landing mid-write must not leave a
        # truncated JSON file that makes every later run crash on this key.
        cache = DiskCache(self.root)

        def interrupted_dump(obj, fh, **kwargs):
            fh.write('{"key": "Videodrome::8", "val')
            raise KeyboardInterrupt

        with patch("dub_check.json.dump", side_effect=interrupted_dump):
            with self.assertRaises(KeyboardInterrupt):
                cache.set("search", "Videodrome::8", ["Videodrome: Cuerpos invadidos"])

        self.assertIs(DiskCache(self.root).get("search", "Videodrome::8"), CACHE_MISS)


class WikiClientSearchShouldCacheSoARerunNeverRepeatsAnIdenticalRequest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DiskCache(Path(self._tmp.name))

    def test_search_returns_the_page_titles_from_a_well_formed_response(self):
        client = WikiClient(self.cache)
        fake_response = {"query": {"search": [{"title": "Videodrome: Cuerpos invadidos"}, {"title": "David Cronenberg"}]}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response) as mocked:
            titles = client.search("Videodrome", limit=3)
        self.assertEqual(titles, ["Videodrome: Cuerpos invadidos", "David Cronenberg"])
        mocked.assert_called_once()

    def test_a_repeated_search_is_served_from_cache_and_never_calls_the_network_layer_again(self):
        client = WikiClient(self.cache)
        fake_response = {"query": {"search": [{"title": "Videodrome: Cuerpos invadidos"}]}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response) as mocked:
            client.search("Videodrome", limit=3)
            client.search("Videodrome", limit=3)
        mocked.assert_called_once()

    def test_a_different_search_limit_is_treated_as_a_different_cache_entry(self):
        client = WikiClient(self.cache)
        fake_response = {"query": {"search": []}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response) as mocked:
            client.search("Videodrome", limit=3)
            client.search("Videodrome", limit=8)
        self.assertEqual(mocked.call_count, 2)

    def test_search_syntax_characters_in_a_title_are_not_sent_as_search_operators(self):
        # CirrusSearch reads a leading "!" or "-" as NOT, '"' as a phrase,
        # "*"/"?" as wildcards, "~" as fuzzy/force-search, "word:" as a
        # keyword filter and uppercase AND/OR/NOT as boolean operators. A
        # film title containing them must be searched as plain words.
        cases = [
            ("!Women Art Revolution", "Women Art Revolution"),
            ("-1 Day", "1 Day"),
            ("Mission: Impossible", "Mission Impossible"),
            ('"Weird" Science', "Weird Science"),
            ("Alien?", "Alien"),
            ("Se7en*", "Se7en"),
            ("~Haunted", "Haunted"),
            ("intitle:Alien", "intitle Alien"),
            ("Love AND Monsters", "Love and Monsters"),
            ("Videodrome", "Videodrome"),
            ("Don't Look Up", "Don't Look Up"),
        ]
        client = WikiClient(self.cache)
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.object(WikiClient, "_http_get_json", return_value={"query": {"search": []}}) as mocked:
                    client.search(raw, limit=3)
                self.assertEqual(mocked.call_args.args[0]["srsearch"], expected)

    def test_a_title_with_nothing_searchable_left_returns_no_candidates_without_a_request(self):
        client = WikiClient(self.cache)
        with patch.object(WikiClient, "_http_get_json") as mocked:
            self.assertEqual(client.search("?", limit=3), [])
        mocked.assert_not_called()

    def test_an_api_error_response_raises_and_is_not_cached_as_an_empty_result(self):
        # MediaWiki reports rate limiting / maxlag as an "error" object in an
        # HTTP 200 body, with no "query" key. Treating that as "the search
        # found nothing" and caching it would make the film NO_ENCONTRADO
        # forever, on every future run.
        client = WikiClient(self.cache)
        rate_limited = {"error": {"code": "ratelimited", "info": "You've exceeded your rate limit."}}
        good = {"query": {"search": [{"title": "Videodrome: Cuerpos invadidos"}]}}
        with patch.object(WikiClient, "_http_get_json", side_effect=[rate_limited, good]) as mocked:
            with self.assertRaises(WikiResponseError):
                client.search("Videodrome", limit=3)
            titles = client.search("Videodrome", limit=3)
        self.assertEqual(titles, ["Videodrome: Cuerpos invadidos"])
        self.assertEqual(mocked.call_count, 2)

    def test_a_response_with_no_search_list_raises_instead_of_meaning_zero_hits(self):
        client = WikiClient(self.cache)
        with patch.object(WikiClient, "_http_get_json", return_value={"batchcomplete": ""}):
            with self.assertRaises(WikiResponseError):
                client.search("Videodrome", limit=3)
        self.assertIs(self.cache.get("search", "Videodrome::3"), CACHE_MISS)


class WikiClientGetWikitextShouldCacheAConfirmedMissingPageToo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DiskCache(Path(self._tmp.name))

    def test_returns_the_wikitext_string_from_a_well_formed_response(self):
        client = WikiClient(self.cache)
        fake_response = {"parse": {"title": "Videodrome: Cuerpos invadidos", "wikitext": {"*": "{{Infobox|titulo_orig = Videodrome}}"}}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response):
            wikitext = client.get_wikitext("Videodrome: Cuerpos invadidos")
        self.assertEqual(wikitext, "{{Infobox|titulo_orig = Videodrome}}")

    def test_a_missingtitle_error_response_yields_none_not_a_crash(self):
        client = WikiClient(self.cache)
        fake_response = {"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response):
            wikitext = client.get_wikitext("Some Page That Does Not Exist")
        self.assertIsNone(wikitext)

    def test_a_confirmed_missing_page_is_cached_so_it_is_never_requested_again(self):
        client = WikiClient(self.cache)
        fake_response = {"error": {"code": "missingtitle", "info": "..."}}
        with patch.object(WikiClient, "_http_get_json", return_value=fake_response) as mocked:
            client.get_wikitext("Some Page That Does Not Exist")
            client.get_wikitext("Some Page That Does Not Exist")
        mocked.assert_called_once()

    def test_a_rate_limit_error_is_not_cached_as_a_missing_page(self):
        # Only "missingtitle" means the page does not exist. Any other API
        # error is transient; caching it as None would make a real dub page
        # look nonexistent on every future run.
        client = WikiClient(self.cache)
        rate_limited = {"error": {"code": "ratelimited", "info": "You've exceeded your rate limit."}}
        good = {"parse": {"wikitext": {"*": "{{Infobox|titulo_orig = Videodrome}}"}}}
        with patch.object(WikiClient, "_http_get_json", side_effect=[rate_limited, good]):
            with self.assertRaises(WikiResponseError):
                client.get_wikitext("Videodrome: Cuerpos invadidos")
            wikitext = client.get_wikitext("Videodrome: Cuerpos invadidos")
        self.assertEqual(wikitext, "{{Infobox|titulo_orig = Videodrome}}")

    def test_a_response_with_no_wikitext_raises_instead_of_meaning_a_missing_page(self):
        client = WikiClient(self.cache)
        with patch.object(WikiClient, "_http_get_json", return_value={"batchcomplete": ""}):
            with self.assertRaises(WikiResponseError):
                client.get_wikitext("Videodrome: Cuerpos invadidos")
        self.assertIs(self.cache.get("page", "Videodrome: Cuerpos invadidos"), CACHE_MISS)


class WikiClientShouldThrottleRealRequestsButNeverThrottleCacheHits(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DiskCache(Path(self._tmp.name))

    def test_waits_between_two_consecutive_real_requests(self):
        client = WikiClient(self.cache, min_interval=1.5)
        fake_response = {"query": {"search": []}}
        fake_clock = [1000.0]

        def fake_monotonic():
            return fake_clock[0]

        sleep_calls = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            fake_clock[0] += seconds

        with patch("dub_check.time.monotonic", side_effect=fake_monotonic), \
             patch("dub_check.time.sleep", side_effect=fake_sleep), \
             patch.object(WikiClient, "_http_get_json_uncached", return_value=fake_response):
            client.search("First Term", limit=3)
            client.search("Second Term", limit=3)

        self.assertEqual(len(sleep_calls), 1)
        self.assertAlmostEqual(sleep_calls[0], 1.5)

    def test_does_not_sleep_at_all_when_every_request_is_served_from_cache(self):
        client = WikiClient(self.cache, min_interval=1.5)
        fake_response = {"query": {"search": []}}
        with patch.object(WikiClient, "_http_get_json_uncached", return_value=fake_response) as mocked, \
             patch("dub_check.time.sleep") as sleep_mock:
            client.search("Repeated Term", limit=3)
            client.search("Repeated Term", limit=3)
            client.search("Repeated Term", limit=3)
        mocked.assert_called_once()
        sleep_mock.assert_not_called()


class WikiClientHttpLayerShouldBuildARequestMediaWikiWillAcceptAndBeAGoodCitizen(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DiskCache(Path(self._tmp.name))

    def test_sends_a_descriptive_user_agent_and_no_personal_data(self):
        # A descriptive User-Agent is asked for by the task spec as a way to
        # be a good citizen of a free community resource. It must not carry
        # the operator's personal email or any other PII to a third party.
        client = WikiClient(self.cache)
        self.assertTrue(len(client.user_agent) > 10)
        self.assertNotIn("@", client.user_agent)

    def test_http_get_json_issues_a_plain_get_request_to_the_configured_api_base(self):
        client = WikiClient(self.cache)

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode("utf-8")

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client._http_get_json_uncached({"action": "query", "srsearch": "Videodrome"})

        self.assertEqual(result, {"ok": True})
        request = captured["request"]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.startswith(client.base_url))
        self.assertIn("srsearch=Videodrome", request.full_url)
        self.assertEqual(request.get_header("User-agent"), client.user_agent)

    def test_a_non_json_or_empty_body_raises_wikiresponseerror(self):
        # An HTML error/challenge page served with HTTP 200, an empty body,
        # or bytes that are not UTF-8 must surface as a wiki-response
        # failure batch mode can skip, not as a raw decode crash.
        client = WikiClient(self.cache)
        for body in (b"<!DOCTYPE html><html><body>Just a moment...</body></html>", b"", b"\xff\xfe{"):
            with self.subTest(body=body):
                class FakeResponse:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                    def read(self, body=body):
                        return body

                with patch("dub_check.urllib.request.urlopen", return_value=FakeResponse()):
                    with self.assertRaises(WikiResponseError):
                        client._http_get_json_uncached({"action": "query", "srsearch": "Videodrome"})


class ReadRadarrApiKeyShouldReadItFromConfigXmlAtRuntimeNeverHardcoded(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write_config(self, contents: str) -> Path:
        path = Path(self._tmp.name) / "config.xml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_extracts_the_key_from_a_realistic_radarr_config_xml(self):
        path = self._write_config(
            "<Config>\n"
            "  <Port>7878</Port>\n"
            "  <ApiKey>abcd1234efgh5678ijkl9012mnop3456</ApiKey>\n"
            "  <AuthenticationMethod>Forms</AuthenticationMethod>\n"
            "</Config>\n"
        )
        self.assertEqual(read_radarr_api_key(path), "abcd1234efgh5678ijkl9012mnop3456")

    def test_raises_a_clear_error_when_no_apikey_element_is_present(self):
        path = self._write_config("<Config>\n  <Port>7878</Port>\n</Config>\n")
        with self.assertRaises(ValueError):
            read_radarr_api_key(path)


class FetchWantedMissingShouldBeStrictlyReadOnly(unittest.TestCase):
    """Radarr access must never be anything but a GET -- this tool reports,
    it never monitors, searches, blocklists, or edits."""

    def _fake_response(self, payload: dict):
        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read(self_inner):
                return json.dumps(payload).encode("utf-8")

        return FakeResponse()

    def test_issues_only_get_requests(self):
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return self._fake_response({"page": 1, "pageSize": 200, "totalRecords": 1, "records": [{"id": 1, "title": "X", "originalTitle": "X", "year": 2020}]})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_wanted_missing("http://localhost:7878", "fake-key", page_size=200)

        self.assertTrue(len(captured) >= 1)
        for request in captured:
            self.assertEqual(request.get_method(), "GET")

    def test_sends_the_api_key_as_a_header_not_hardcoded_anywhere(self):
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return self._fake_response({"page": 1, "pageSize": 200, "totalRecords": 0, "records": []})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_wanted_missing("http://localhost:7878", "the-real-key", page_size=200)

        self.assertEqual(captured[0].get_header("X-api-key"), "the-real-key")

    def test_requests_the_wanted_missing_endpoint_with_the_requested_page_size(self):
        captured = []

        def fake_urlopen(request, timeout=None):
            captured.append(request)
            return self._fake_response({"page": 1, "pageSize": 50, "totalRecords": 0, "records": []})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_wanted_missing("http://localhost:7878", "fake-key", page_size=50)

        self.assertIn("/api/v3/wanted/missing", captured[0].full_url)
        self.assertIn("pageSize=50", captured[0].full_url)

    def test_returns_every_record_from_a_single_page_response(self):
        movies = [{"id": i, "title": f"Film {i}", "originalTitle": f"Film {i}", "year": 2000 + i} for i in range(3)]

        def fake_urlopen(request, timeout=None):
            return self._fake_response({"page": 1, "pageSize": 200, "totalRecords": 3, "records": movies})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            records = fetch_wanted_missing("http://localhost:7878", "fake-key", page_size=200)

        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["title"], "Film 0")

    def test_follows_pagination_when_more_records_exist_than_one_page_size(self):
        page1 = [{"id": i, "title": f"Film {i}"} for i in range(2)]
        page2 = [{"id": i, "title": f"Film {i}"} for i in range(2, 3)]
        calls = []

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if "page=2" in request.full_url:
                return self._fake_response({"page": 2, "pageSize": 2, "totalRecords": 3, "records": page2})
            return self._fake_response({"page": 1, "pageSize": 2, "totalRecords": 3, "records": page1})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            records = fetch_wanted_missing("http://localhost:7878", "fake-key", page_size=2)

        self.assertEqual(len(records), 3)
        self.assertEqual(len(calls), 2)

    def test_stops_paginating_once_a_short_page_is_returned_even_if_totalrecords_looks_larger(self):
        # Defensive: an empty/short final page must not spin the loop forever.
        def fake_urlopen(request, timeout=None):
            return self._fake_response({"page": 1, "pageSize": 200, "totalRecords": 999, "records": []})

        with patch("dub_check.urllib.request.urlopen", side_effect=fake_urlopen):
            records = fetch_wanted_missing("http://localhost:7878", "fake-key", page_size=200)

        self.assertEqual(records, [])


class FormatVerdictShouldStateHonestlyWhatWasAndWasNotConfirmed(unittest.TestCase):
    def test_latino_verdict_names_the_confirming_page(self):
        verdict = Verdict("LATINO", "Videodrome: Cuerpos invadidos", ['"Videodrome: Cuerpos invadidos": VERIFIED -> latino'])
        text = format_verdict("Videodrome", 1983, verdict)
        self.assertIn("LATINO", text)
        self.assertIn("Videodrome: Cuerpos invadidos", text)

    def test_castellano_verdict_flags_that_this_is_not_the_requested_dub(self):
        verdict = Verdict("CASTELLANO", "Spain Page", [])
        text = format_verdict("Some Film", 1999, verdict)
        self.assertIn("CASTELLANO", text)
        self.assertIn("Spain Page", text)
        self.assertIn("not", text.lower())

    def test_no_encontrado_verdict_explicitly_says_not_found_is_not_proof_of_absence(self):
        # Directly enforces the task's own wording requirement: "'Not
        # found' is not proof of absence -- say so in the output wording."
        verdict = Verdict("NO_ENCONTRADO", None, ['"Death Race": titulo_orig does not match -> rejected'])
        text = format_verdict("Murder Death Koreatown", 2020, verdict)
        self.assertIn("NO_ENCONTRADO", text)
        self.assertIn("not proof", text.lower())
        self.assertIn("Death Race", text)  # rejected candidates are shown as evidence, not hidden


class BuildArgParserShouldExposeSensibleDefaults(unittest.TestCase):
    def test_single_mode_requires_only_a_title(self):
        parser = build_arg_parser()
        args = parser.parse_args(["single", "Videodrome"])
        self.assertEqual(args.title, "Videodrome")
        self.assertIsNone(args.original_title)
        self.assertIsNone(args.year)

    def test_single_mode_accepts_original_title_and_an_integer_year(self):
        parser = build_arg_parser()
        args = parser.parse_args(["single", "Videodrome", "--original-title", "Videodrome", "--year", "1983"])
        self.assertEqual(args.original_title, "Videodrome")
        self.assertEqual(args.year, 1983)

    def test_batch_mode_defaults_to_the_documented_radarr_base_url_and_is_not_forced(self):
        parser = build_arg_parser()
        args = parser.parse_args(["batch"])
        self.assertEqual(args.radarr_url, "http://localhost:7878")
        self.assertFalse(args.force)

    def test_default_radarr_config_points_at_the_sibling_radarr_directory(self):
        self.assertTrue(str(DEFAULT_RADARR_CONFIG).endswith("radarr/config.xml"))


class MainSingleModeShouldPrintAHumanReadableVerdictWithoutTouchingRadarr(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "cache"

    def test_prints_latino_for_a_verified_film(self):
        backend = fake_wiki_backend(
            search_results={"Videodrome": ["Videodrome: Cuerpos invadidos"]},
            pages={"Videodrome: Cuerpos invadidos": "{{Infobox|titulo_orig = Videodrome|pais = {{Bandera|México}} México|año = 1983}}"},
        )
        argv = ["single", "Videodrome", "--original-title", "Videodrome", "--year", "1983",
                "--cache-dir", str(self.cache_dir), "--min-interval", "0"]
        out = io.StringIO()
        with patch.object(WikiClient, "_http_get_json", backend):
            with redirect_stdout(out):
                exit_code = main(argv)
        self.assertEqual(exit_code, 0)
        self.assertIn("LATINO", out.getvalue())

    def test_prints_no_encontrado_for_the_trap_case_and_never_raises(self):
        backend = fake_wiki_backend(
            search_results={"Murder Death Koreatown": ["Reporte de un asesino", "Pablo Moreno"]},
            pages={
                "Reporte de un asesino": FakeWikiWikitextShapes.REPORTE_DE_UN_ASESINO,
                "Pablo Moreno": FakeWikiWikitextShapes.UNRELATED_PERSON_PAGE,
            },
        )
        argv = ["single", "Murder Death Koreatown", "--year", "2020",
                "--cache-dir", str(self.cache_dir), "--min-interval", "0"]
        out = io.StringIO()
        with patch.object(WikiClient, "_http_get_json", backend):
            with redirect_stdout(out):
                exit_code = main(argv)
        self.assertEqual(exit_code, 0)
        self.assertIn("NO_ENCONTRADO", out.getvalue())

    def test_a_network_failure_prints_a_clear_message_and_no_verdict_instead_of_a_traceback(self):
        failures = [
            urllib.error.URLError("[Errno -3] Temporary failure in name resolution"),
            TimeoutError("timed out"),
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            WikiResponseError("unusable search response", "ratelimited"),
        ]
        for exc in failures:
            with self.subTest(exc=type(exc).__name__):
                def failing_backend(self, params, exc=exc):
                    raise exc

                argv = ["single", "Videodrome", "--year", "1983",
                        "--cache-dir", str(self.cache_dir), "--min-interval", "0"]
                out, err = io.StringIO(), io.StringIO()
                with patch.object(WikiClient, "_http_get_json", failing_backend):
                    with redirect_stdout(out), redirect_stderr(err):
                        exit_code = main(argv)  # must not raise
                self.assertEqual(exit_code, 1)
                self.assertNotIn("Verdict", out.getvalue())  # no answer is better than a wrong one
                self.assertIn("could not check", err.getvalue())
                self.assertIn("doblaje.fandom.com", err.getvalue())
                self.assertNotIn("Traceback", err.getvalue())


class MainBatchModeShouldBeResumableAndReadOnlyTowardRadarr(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.cache_dir = self.base / "cache"
        self.out_path = self.base / "results.json"
        self.config_path = self.base / "config.xml"
        self.config_path.write_text("<Config><ApiKey>fake-key-xyz</ApiKey></Config>", encoding="utf-8")
        self.videodrome_backend = fake_wiki_backend(
            search_results={"Videodrome": ["Videodrome: Cuerpos invadidos"]},
            pages={"Videodrome: Cuerpos invadidos": "{{Infobox|titulo_orig = Videodrome|pais = {{Bandera|México}} México|año = 1983}}"},
        )

    def _movies(self):
        return [
            {"id": 1, "title": "Videodrome", "originalTitle": "Videodrome", "year": 1983},
            {"id": 2, "title": "Murder Death Koreatown", "originalTitle": "Murder Death Koreatown", "year": 2020},
        ]

    def _argv(self, extra_tokens=()):
        return ["batch", "--radarr-config", str(self.config_path), "--out", str(self.out_path),
                "--cache-dir", str(self.cache_dir), "--min-interval", "0", *extra_tokens]

    def _run_main(self, argv):
        # Batch mode prints progress as it goes; swallow it here so a test
        # run's output stays readable and only shows real failures.
        with redirect_stdout(io.StringIO()):
            return main(argv)

    def test_writes_a_verdict_for_every_movie_and_the_results_file_is_valid_json(self):
        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", self.videodrome_backend):
            self._run_main(self._argv())

        results = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(results["1"]["status"], "LATINO")
        self.assertEqual(results["2"]["status"], "NO_ENCONTRADO")

    def test_calls_fetch_wanted_missing_exactly_once_and_nothing_else_reads_radarr(self):
        with patch("dub_check.fetch_wanted_missing", return_value=[]) as fake_fetch:
            self._run_main(self._argv())
        fake_fetch.assert_called_once()

    def test_a_second_run_skips_movies_already_present_in_the_results_file(self):
        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", self.videodrome_backend):
            self._run_main(self._argv())

        def explode(self, params):
            raise AssertionError("must not query the wiki again for an already-resolved movie")

        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", explode):
            self._run_main(self._argv())  # must complete without raising

    def test_force_reprocesses_a_movie_already_present_in_the_results_file(self):
        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", self.videodrome_backend):
            self._run_main(self._argv())

        # --force controls the results-file-level skip specifically. Point
        # the second run at a *fresh, empty* wiki cache directory too, so a
        # warm disk cache from the first run cannot itself explain why the
        # wiki gets queried again -- only --force can.
        calls = []
        inner_backend = fake_wiki_backend(
            search_results={"Videodrome": ["Videodrome: Cuerpos invadidos"]},
            pages={"Videodrome: Cuerpos invadidos": "{{Infobox|titulo_orig = Videodrome|pais = {{Bandera|México}} México|año = 1983}}"},
        )

        def counting_backend(self, params):
            calls.append(params)
            return inner_backend(self, params)

        fresh_cache_dir = self.base / "cache-2"
        argv = ["batch", "--radarr-config", str(self.config_path), "--out", str(self.out_path),
                "--cache-dir", str(fresh_cache_dir), "--min-interval", "0", "--force"]
        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", counting_backend):
            self._run_main(argv)

        self.assertTrue(len(calls) > 0, "with --force and a cold cache, the wiki must be queried again")

    def test_limit_restricts_how_many_movies_are_processed(self):
        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", self.videodrome_backend):
            self._run_main(self._argv(["--limit", "1"]))

        results = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(results), 1)
        self.assertIn("1", results)
        self.assertNotIn("2", results)

    def test_a_network_error_for_one_movie_does_not_abort_the_rest_of_the_batch(self):
        def flaky_backend(self, params):
            if params.get("srsearch") == "Videodrome":
                raise urllib.error.URLError("simulated network failure")
            return fake_wiki_backend(search_results={}, pages={})(self, params)

        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", flaky_backend):
            self._run_main(self._argv())

        results = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertNotIn("1", results)  # left in place for a future retry
        self.assertIn("2", results)
        self.assertEqual(results["2"]["status"], "NO_ENCONTRADO")

    def test_a_timeout_or_dropped_connection_for_one_movie_does_not_abort_the_rest_of_the_batch(self):
        # urllib only wraps connect-phase failures in URLError. A server that
        # accepts the connection but never answers raises a bare
        # TimeoutError; a dropped connection raises RemoteDisconnected /
        # ConnectionResetError; a cut-off body raises IncompleteRead.
        for exc in (TimeoutError("timed out"),
                    http.client.RemoteDisconnected("Remote end closed connection without response"),
                    ConnectionResetError(104, "Connection reset by peer"),
                    http.client.IncompleteRead(b"{")):
            with self.subTest(exc=type(exc).__name__):
                self.out_path.unlink(missing_ok=True)

                def flaky_backend(self, params, exc=exc):
                    if params.get("srsearch") == "Videodrome":
                        raise exc
                    return fake_wiki_backend(search_results={}, pages={})(self, params)

                with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
                     patch.object(WikiClient, "_http_get_json", flaky_backend), \
                     redirect_stderr(io.StringIO()):
                    self._run_main(self._argv())

                results = json.loads(self.out_path.read_text(encoding="utf-8"))
                self.assertNotIn("1", results)
                self.assertEqual(results["2"]["status"], "NO_ENCONTRADO")

    def test_a_movie_with_radarr_year_0_is_treated_as_unknown_and_left_for_a_future_run(self):
        # Radarr reports year 0 when it does not know a film's year yet.
        # Checking "año contains 0" can never match, so the film used to be
        # saved as NO_ENCONTRADO and, batch mode being resumable, never
        # looked at again even after Radarr learned the year.
        for unknown_year in (0, None):
            with self.subTest(year=unknown_year):
                self.out_path.unlink(missing_ok=True)
                movies = [{"id": 1, "title": "Videodrome", "originalTitle": "Videodrome", "year": unknown_year}]
                out = io.StringIO()
                with patch("dub_check.fetch_wanted_missing", return_value=movies), \
                     patch.object(WikiClient, "_http_get_json", self.videodrome_backend), \
                     redirect_stdout(out), redirect_stderr(io.StringIO()):
                    main(self._argv())

                results = json.loads(self.out_path.read_text(encoding="utf-8")) if self.out_path.exists() else {}
                self.assertNotIn("1", results)
                self.assertNotIn("year 0", out.getvalue())
                # Checked title-only (like single mode without --year), shown, not saved.
                self.assertIn("LATINO", out.getvalue())

    def test_a_results_write_interrupted_partway_keeps_the_previous_results_file_readable(self):
        # results.json is rewritten after every movie. A Ctrl-C mid-write
        # must not leave a truncated file that makes every later batch run
        # crash in _load_results (and lose all progress made so far).
        _save_results(self.out_path, {"1": {"title": "Videodrome", "status": "LATINO"}})

        def interrupted_dump(obj, fh, **kwargs):
            fh.write('{"1": {"title": "Vid')
            raise KeyboardInterrupt

        with patch("dub_check.json.dump", side_effect=interrupted_dump):
            with self.assertRaises(KeyboardInterrupt):
                _save_results(self.out_path, {"1": {"title": "Videodrome", "status": "LATINO"}, "2": {}})

        self.assertEqual(_load_results(self.out_path), {"1": {"title": "Videodrome", "status": "LATINO"}})

    def test_a_rate_limited_search_is_not_recorded_as_no_encontrado(self):
        # A rate-limit error must leave the movie out of results.json (so a
        # later run retries it), not record a false NO_ENCONTRADO that the
        # resumable batch would then never revisit.
        def rate_limited_backend(self, params):
            if params.get("srsearch") == "Videodrome":
                return {"error": {"code": "ratelimited", "info": "You've exceeded your rate limit."}}
            return fake_wiki_backend(search_results={}, pages={})(self, params)

        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", rate_limited_backend), \
             patch("dub_check.time.sleep"), \
             redirect_stderr(io.StringIO()):
            self._run_main(self._argv())

        results = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertNotIn("1", results)
        self.assertIn("2", results)

        with patch("dub_check.fetch_wanted_missing", return_value=self._movies()), \
             patch.object(WikiClient, "_http_get_json", self.videodrome_backend):
            self._run_main(self._argv())

        results = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(results["1"]["status"], "LATINO")


class MainBatchModeShouldBackOffWhenTheWikiRateLimits(unittest.TestCase):
    """After a rate limit, batch mode must slow down (honoring Retry-After
    when the wiki sends one) instead of firing the next film's requests at
    the normal pace and failing every remaining film the same way."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.out_path = base / "results.json"
        self.config_path = base / "config.xml"
        self.config_path.write_text("<Config><ApiKey>fake-key-xyz</ApiKey></Config>", encoding="utf-8")
        self.argv = ["batch", "--radarr-config", str(self.config_path), "--out", str(self.out_path),
                     "--cache-dir", str(base / "cache"), "--min-interval", "0"]

    @staticmethod
    def _movies(*titles):
        return [{"id": i, "title": t, "originalTitle": t, "year": 2020} for i, t in enumerate(titles, 1)]

    @staticmethod
    def _http_429(retry_after=None):
        headers = email.message.Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError(API_BASE, 429, "Too Many Requests", headers, None)

    def _run(self, movies, failures):
        """failures maps a search term to the exception (or API error body)
        its search produces; every other film searches cleanly."""
        def backend(client_self, params):
            failure = failures.get(params.get("srsearch"))
            if isinstance(failure, BaseException):
                raise failure
            if failure is not None:
                return failure
            return fake_wiki_backend(search_results={}, pages={})(client_self, params)

        sleeps = []
        stderr = io.StringIO()
        with patch("dub_check.fetch_wanted_missing", return_value=movies), \
             patch.object(WikiClient, "_http_get_json", backend), \
             patch("dub_check.time.sleep", side_effect=sleeps.append), \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            main(self.argv)
        results = json.loads(self.out_path.read_text(encoding="utf-8")) if self.out_path.exists() else {}
        return sleeps, results, stderr.getvalue()

    def test_honors_a_retry_after_in_seconds_then_carries_on_with_the_next_film(self):
        sleeps, results, _ = self._run(self._movies("A", "B"), {"A": self._http_429("7")})
        self.assertEqual(sleeps, [7.0])
        self.assertNotIn("1", results)
        self.assertIn("2", results)

    def test_honors_a_retry_after_given_as_an_http_date(self):
        when = email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=120), usegmt=True)
        sleeps, _, _ = self._run(self._movies("A", "B"), {"A": self._http_429(when)})
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 120, delta=3)

    def test_backs_off_exponentially_without_retry_after_and_resets_after_a_success(self):
        rate_limited = {"error": {"code": "ratelimited", "info": "You've exceeded your rate limit."}}
        sleeps, results, _ = self._run(
            self._movies("A", "B", "C", "D"),
            {"A": rate_limited, "B": self._http_429(), "D": rate_limited},
        )
        self.assertEqual(sleeps, [RATE_LIMIT_INITIAL_BACKOFF_SECONDS, 2 * RATE_LIMIT_INITIAL_BACKOFF_SECONDS,
                                  RATE_LIMIT_INITIAL_BACKOFF_SECONDS])
        self.assertEqual(sorted(results), ["3"])

    def test_stops_the_run_instead_of_hanging_when_retry_after_is_longer_than_the_cap(self):
        too_long = str(int(RATE_LIMIT_MAX_BACKOFF_SECONDS) + 1)
        sleeps, results, stderr = self._run(self._movies("A", "B"), {"A": self._http_429(too_long)})
        self.assertEqual(sleeps, [])
        self.assertEqual(results, {})  # B was not attempted: the wiki asked for a long pause
        self.assertIn(too_long, stderr)

    def test_does_not_sleep_after_a_failure_that_is_not_a_rate_limit(self):
        sleeps, results, _ = self._run(self._movies("A", "B"), {"A": urllib.error.URLError("no route to host")})
        self.assertEqual(sleeps, [])
        self.assertIn("2", results)


if __name__ == "__main__":
    unittest.main()
