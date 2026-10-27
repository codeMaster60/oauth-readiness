import json
from pathlib import Path

import pytest

from app.agent.llm import ModelPool, parse_json
from app.agent.pipeline import parse_input, rank_urls, slugify
from app.agent.web import guess_official_domains, pick_snippets, quote_on_page, registrable
from app.models import Flags, Guide, score

ROOT = Path(__file__).resolve().parent.parent


def test_rubric_extremes():
    assert score(Flags())[0] == 1
    assert score(Flags(review_for_production=True, review_wait_days_max=60, security_assessment=True,
                       business_verification=True, paid_requirement=True))[0] == 5
    assert score(Flags(partner_gated=True))[0] == 3


@pytest.mark.parametrize("f", sorted((ROOT / "data" / "guides").glob("*.json")), ids=lambda p: p.stem)
def test_curated_guides_are_consistent(f):
    g = Guide.model_validate_json(f.read_text())
    # Stored difficulty must equal the rubric: no hand-tuned scores.
    assert g.verdict.difficulty == score(g.flags)[0]
    ids = {s.id for s in g.sources}
    for claim in g.iter_claims():
        for sid in claim.sources:
            assert sid in ids, f"{g.slug}: dangling source {sid}"
        if claim.evidence in ("documented", "third_party"):
            assert claim.sources, f"{g.slug}: '{claim}' says {claim.evidence} but cites nothing"
        if claim.evidence == "tested":
            assert any(g.source(s).kind == "test" for s in claim.sources), f"{g.slug}: tested without a test log"


def test_catalog_has_100_unique_apps():
    rows = json.loads((ROOT / "data" / "catalog.json").read_text())
    assert len(rows) == 100 and len({r["name"] for r in rows}) == 100


def test_quote_verification():
    page = "Projects configured with a publishing status of Testing are limited to up to 100 test users."
    assert quote_on_page("limited to up to 100 test users", page)
    assert quote_on_page("Projects configured with a publishing status of Testing are limited", page)
    assert not quote_on_page("limited to up to 500 test users forever and ever", page)
    assert not quote_on_page("short", page)


def test_snippets_prefer_oauth_paragraphs():
    text = "\n".join([
        "Welcome to our blog about cooking pasta and other delicious recipes for the whole family.",
        "To create an OAuth app, set the redirect URI and copy the client ID and client secret.",
        "App review typically takes 10 business days before your app is approved for the marketplace.",
    ])
    snips = pick_snippets(text)
    assert any("redirect URI" in s for s in snips) and any("10 business days" in s for s in snips)
    assert not any("pasta" in s for s in snips)


def test_domains():
    assert registrable("docs.slack.dev") == "slack.dev"
    assert registrable("www.example.co.uk") == "example.co.uk"
    doms = guess_official_domains("Zoom", "", ["https://developers.zoom.us/docs/x", "https://blog.other.com/zoom"])
    assert "zoom.us" in doms and "other.com" not in doms


def test_parse_input():
    assert parse_input("Zoom") == ("Zoom", "")
    name, url = parse_input("asana.com")
    assert name == "Asana" and url == "https://asana.com"
    assert slugify("X (Twitter)") == "x-twitter"


def test_rank_urls_official_first():
    results = [{"url": "https://someblog.com/zoom-oauth", "title": "blog"},
               {"url": "https://developers.zoom.us/docs/integrations/oauth/", "title": "Zoom OAuth"},
               {"url": "https://www.youtube.com/watch?v=1", "title": "video"}]
    ranked = rank_urls(results, {"zoom.us"}, None)
    assert ranked[0][0].startswith("https://developers.zoom.us") and ranked[0][2] is True
    assert all("youtube" not in u for u, _, _ in ranked)


def test_parse_json_lenient():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} hope that helps') == {"a": 2}


def test_model_pool_spreads_load():
    p = ModelPool(["m1", "m2"], tpm=8000)
    a, _ = p.pick(5000); p.record(a, 5000)
    b, _ = p.pick(5000)
    assert a != b
    p.record(b, 5000)
    none, wait = p.pick(5000)
    assert none is None and wait > 0


def test_fact_ids_never_reach_readers():
    from app.agent.pipeline import clean, strip_refs
    assert strip_refs("Tokens expire after one hour (F1,F7)") == "Tokens expire after one hour"
    assert strip_refs("Use a wildcard per F26.") == "Use a wildcard."
    assert strip_refs("Cited twice (F1 and F2)") == "Cited twice"
    assert strip_refs("Office 365 and P2P stay") == "Office 365 and P2P stay"
    assert clean({"text": "x (F3)", "refs": ["F3"]}) == {"text": "x", "refs": ["F3"]}


def test_forums_are_not_official():
    from app.agent.pipeline import is_official, rank_urls
    assert is_official("https://developers.zoom.us/docs/integrations/oauth", {"zoom.us"})
    assert not is_official("https://devforum.zoom.us/t/123", {"zoom.us"})
    ranked = rank_urls([{"url": "https://developers.zoom.us/docs/internal-apps/s2s-oauth", "title": ""},
                        {"url": "https://developers.zoom.us/docs/integrations/oauth", "title": ""}], {"zoom.us"}, None)
    assert ranked[0][0].endswith("/integrations/oauth")  # server-to-server docs ranked below user OAuth


def test_off_topic_pages_dropped_when_enough_on_topic():
    from app.agent.pipeline import rank_urls
    urls = [f"https://developers.zoom.us/docs/integrations/{x}" for x in ("oauth", "create", "scopes")]
    urls.append("https://developers.zoom.us/docs/internal-apps/s2s-oauth")
    ranked = rank_urls([{"url": u, "title": ""} for u in urls], {"zoom.us"}, None)
    assert all("s2s" not in u for u, _, _ in ranked)
