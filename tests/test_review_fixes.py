"""Regression tests for issues found in the outside review of the live site."""

import asyncio

import httpx
from fastapi.testclient import TestClient

from app.agent import pipeline, probe
from app.agent.knowledge import known_pages
from app.agent.pipeline import build_guide, wait_days
from app.export import a_an
from app.models import Stats


def _guide(draft, facts=()):
    return build_guide("Google Calendar", "", draft, list(facts), [], [], ("yes", "", None), "m", Stats())


def test_no_audit_point_without_an_audit_in_the_sources():
    facts = [{"id": "F1", "topic": "review", "claim": "Apps go through Google's review.", "quote": "x",
              "url": "u", "official": True, "verified": True}]
    g = _guide({"flags": {"review_for_production": True, "security_assessment": True}, "verdict": {}}, facts)
    assert g.flags.security_assessment is False


def test_hand_checked_timeline_beats_third_party_weeks():
    known = [{"id": "F1", "topic": "timeline", "claim": "Sensitive scope verification typically takes 3-5 business days.",
              "quote": "q", "url": "u", "official": True, "verified": True, "known": True}]
    g = _guide({"flags": {"review_for_production": True}, "verdict": {"wait": "4-6 weeks"}}, known)
    assert "7 days" in g.verdict.wait and g.flags.review_wait_days_max == 7


def test_wait_text_feeds_score_when_model_left_days_empty():
    g = _guide({"flags": {"review_for_production": True}, "verdict": {"wait": "up to 10 weeks"}})
    assert g.flags.review_wait_days_max == 70 and g.verdict.difficulty >= 3


def test_wait_parser():
    assert wait_days("3-5 business days") == 7
    assert wait_days("4–6 weeks") == 42
    assert wait_days("unknown") is None


def test_google_products_get_hand_checked_facts():
    pages = known_pages({"google.com", "cloud.google.com"}, "Google Calendar")
    claims = [c for p in pages for _, c, _ in p.known]
    assert any("3-5 business days" in c for c in claims)
    assert known_pages({"asana.com"}, "Asana") == []


def test_build_stage_always_has_the_basics_and_waiting_is_not_agent_work():
    draft = {"flags": {}, "verdict": {}, "stages": [
        {"id": "build", "name": "Build", "steps": [{"title": "Add redirect URL", "who": "you", "refs": []}]},
        {"id": "launch", "name": "Launch", "steps": [{"title": "Wait for review", "who": "agent", "refs": []}]}]}
    g = _guide(draft)
    titles = " | ".join(s.title.lower() for s in g.stages[0].steps)
    assert "create the oauth app" in titles and "composio" in titles and "test" in titles
    assert g.stages[1].steps[0].who == "you"


def test_unused_sources_are_dropped():
    from app.agent.web import Page
    pages = [Page(url="https://a.com/used", title="A", text="t", official=True),
             Page(url="https://a.com/unused", title="B", text="t", official=True)]
    facts = [{"id": "F1", "topic": "x", "claim": "c", "quote": "q", "url": "https://a.com/used",
              "official": True, "verified": True}]
    draft = {"flags": {}, "verdict": {}, "gotchas": [{"title": "g", "refs": ["F1"]}]}
    g = build_guide("A", "", draft, facts, [], pages, ("yes", "", None), "m", Stats())
    urls = [s.url for s in g.sources]
    assert "https://a.com/used" in urls and "https://a.com/unused" not in urls


def test_discovery_prefers_main_domain_over_cloud_workforce():
    docs = {
        "https://accounts.cloud.google.com/.well-known/openid-configuration":
            {"issuer": "https://accounts.cloud.google.com", "authorization_endpoint": "https://accounts.cloud.google.com/authorize",
             "token_endpoint": "https://sts.googleapis.com/v1/token"},
        "https://accounts.google.com/.well-known/openid-configuration":
            {"issuer": "https://accounts.google.com", "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
             "token_endpoint": "https://oauth2.googleapis.com/token"},
    }

    def handler(req):
        d = docs.get(str(req.url))
        return httpx.Response(200, json=d) if d else httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await probe.discovery({"cloud.google.com", "google.com"}, c, "google.com")

    probes, doc = asyncio.run(go())
    assert doc["issuer"] == "https://accounts.google.com"


def test_probe_output_has_no_css():
    html = "<html><style>*{margin:0;padding:0}html,code{font:15px}</style><body>Error 401: invalid_client</body></html>"

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text=html))) as c:
            return await probe.authorize_probe("https://accounts.example.com/authorize", c)

    p = asyncio.run(go())
    assert "margin" not in p.observed and "invalid_client" in p.observed


def test_grammar_and_aliases():
    assert a_an("Asana") == "an Asana" and a_an("GitHub") == "a GitHub"
    from app.main import store
    assert store.find("google") is None and store.find("google workspace") is None
    assert store.find("gmail").slug == "google-gmail"


def test_web_routes_404_rate_limit_and_dedup(monkeypatch):
    from app import main
    c = TestClient(main.app)
    r = c.get("/guides/does-not-exist")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "Generate it" in r.text
    assert c.get("/robots.txt").status_code == 200
    assert c.get("/favicon.ico", follow_redirects=False).status_code == 301
    assert "too long" in c.post("/api/generate", json={"query": "x" * 201}).json()["detail"]

    started = []
    monkeypatch.setattr(main.pipeline, "start", lambda job, llm, save: started.append(job))
    monkeypatch.setattr(main, "PER_IP_LIMIT", 2)
    main._ip_runs.clear()
    a = c.post("/api/generate", json={"query": "Acme One"}).json()
    b = c.post("/api/generate", json={"query": "acme one"}).json()          # same app, still running -> same job
    assert a["job"] == b["job"]
    c.post("/api/generate", json={"query": "Acme Two"})
    r = c.post("/api/generate", json={"query": "Acme Three"})
    assert r.status_code == 429 and "10 minutes" in r.json()["detail"]
    for j in started:
        j.done = True


def test_known_casa_fact_does_not_unlock_the_audit_point():
    known = [{"id": "F1", "topic": "review", "claim": "Only restricted scopes need the CASA security assessment.",
              "quote": "q", "url": "u", "official": True, "verified": True, "known": True}]
    g = _guide({"flags": {"review_for_production": True, "security_assessment": True}, "verdict": {}}, known)
    assert g.flags.security_assessment is False
