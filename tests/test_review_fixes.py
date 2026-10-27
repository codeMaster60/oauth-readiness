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
    assert "3-5 business days (about a week)" in g.verdict.wait and g.flags.review_wait_days_max == 7


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
    monkeypatch.setattr(main.pipeline, "start", lambda job, llm, save, on_fail=None: started.append(job))
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


def test_label_matches_wait_and_human_tasks_stay_human():
    g = _guide({"flags": {"review_for_production": True, "review_wait_days_max": 7}, "verdict": {},
                "agent_split": {"agent": ["draft privacy policy", "record demo video", "submit verification"],
                                "you": ["create OAuth client"]}})
    assert g.verdict.label.endswith("days") and "same day" not in g.verdict.label
    assert g.agent_split.agent == ["draft privacy policy"]
    assert "record demo video" in g.agent_split.you and "submit verification" in g.agent_split.you



# ---- second review ----------------------------------------------------------------------------------------------

def _known_google():
    return [{"id": "F1", "topic": "timeline", "claim": "Sensitive scope verification typically takes 3-5 business days.",
             "quote": "q", "url": "https://developers.google.com/s", "official": True, "verified": True, "known": True},
            {"id": "F2", "topic": "review", "claim": "Only restricted scopes need the annual third-party CASA security assessment.",
             "quote": "q", "url": "https://support.google.com/cloud/answer/13465431", "official": True, "verified": True,
             "known": True}]


def test_drive_read_and_list_is_restricted_not_a_week():
    from app.agent.web import Page
    pages = [Page(url=f["url"], title="t", text="q", official=True) for f in _known_google()]
    draft = {"use_case": "read and list files", "flags": {"review_for_production": True},
             "verdict": {"wait": "about 7 days"}, "stages": [{"id": "build", "steps": [
                 {"title": "Add the drive.file scope", "who": "you", "refs": []}]}]}
    g = build_guide("Google Drive", "", draft, _known_google(), [], pages, ("yes", "", None), "m", Stats())
    assert g.flags.security_assessment and "weeks" in g.verdict.wait and g.verdict.difficulty >= 4
    assert "audit" in g.verdict.label
    assert [p.name for p in g.paths] == ["Sensitive scopes only", "Restricted scopes (what this use case needs)"]


def test_calendar_stays_sensitive():
    draft = {"use_case": "read and write calendar events", "flags": {"review_for_production": True}, "verdict": {}}
    g = build_guide("Google Calendar", "", draft, _known_google()[:1], [], [], ("yes", "", None), "m", Stats())
    assert not g.flags.security_assessment and "about a week" in g.verdict.wait


def test_waiting_for_capacity_does_not_use_up_attempts(monkeypatch):
    from app.agent import llm as L
    g = L.Groq(api_key="k")
    calls = {"pick": 0}
    real_pick = g.synth_pool.pick

    def pick(need):  # busy 20 times, then free
        calls["pick"] += 1
        return (None, 0.0) if calls["pick"] <= 20 else real_pick(need)
    monkeypatch.setattr(g.synth_pool, "pick", pick)

    async def fake_sleep(_):
        return None
    monkeypatch.setattr(L.asyncio, "sleep", fake_sleep)

    class R:
        status_code, headers, text = 200, {}, ""
        def json(self):
            return {"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {"total_tokens": 10}}

    async def post(*a, **k):
        return R()
    monkeypatch.setattr(g.client, "post", post)
    data, _ = asyncio.run(g.json_call("s", "u", pool="synth"))
    assert data == {"ok": 1} and calls["pick"] == 21


def test_failed_run_is_friendly_and_refunded():
    class Broken:
        configured = True
        async def json_call(self, *a, **k):
            from app.agent.llm import LLMError
            raise LLMError("busy")
    refunded = []
    job = pipeline.new_job("Acme")
    job.on_fail = lambda: refunded.append(1)

    async def fail_early(*a, **k):
        from app.agent.llm import LLMError
        raise LLMError("busy")
    import app.agent.pipeline as P
    orig = P._run
    P._run = fail_early
    try:
        asyncio.run(pipeline.run(job, Broken(), lambda g: None))
    finally:
        P._run = orig
    err = next(e for e in job.events if e["type"] == "error")
    assert "busy" not in err["message"].split()[0] and "try again" in err["message"].lower() and refunded == [1]


def test_umbrella_ip_and_case(monkeypatch):
    from app import main
    c = TestClient(main.app)
    assert "Gmail" in c.post("/api/generate", json={"query": "Google"}).json()["choose"]
    assert "choose" in c.post("/api/generate", json={"query": "google workspace"}).json()

    class Req:
        def __init__(self, h): self.headers, self.client = h, None
    assert main._client_ip(Req({"x-forwarded-for": "6.6.6.6, 1.2.3.4"})) == "1.2.3.4"
    assert main._client_ip(Req({"cf-connecting-ip": "9.9.9.9", "x-forwarded-for": "6.6.6.6"})) == "9.9.9.9"
    r = c.get("/guides/GitHub", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/guides/github"


def test_people_do_the_uploading_and_waiting():
    draft = {"flags": {}, "verdict": {}, "agent_split": {"agent": ["upload demo video", "draft scope justification"]},
             "stages": [{"id": "launch", "steps": [{"title": "Wait for Google review", "who": "either", "refs": []}]}]}
    g = _guide(draft)
    assert g.agent_split.agent == ["draft scope justification"]
    launch = next(st for st in g.stages if st.id == "launch")
    assert launch.steps[0].who == "you"


def test_source_ids_are_contiguous():
    from app.agent.web import Page
    pages = [Page(url=f"https://a.com/{i}", title=str(i), text="t", official=True) for i in range(4)]
    facts = [{"id": f"F{i}", "topic": "x", "claim": "c", "quote": "q", "url": f"https://a.com/{i}",
              "official": True, "verified": True} for i in (1, 3)]
    draft = {"flags": {}, "verdict": {}, "gotchas": [{"title": "g", "refs": ["F1", "F3"]}]}
    g = build_guide("A", "", draft, facts, [], pages, ("yes", "", None), "m", Stats())
    page_ids = [s.id for s in g.sources if s.id.startswith("s")]
    assert page_ids == ["s1", "s2"] and g.gotchas[0].sources == ["s1", "s2"]


def test_durations_inside_the_guide_agree_with_the_headline():
    from app.agent.web import Page
    pages = [Page(url=f["url"], title="t", text="q", official=True) for f in _known_google()]
    draft = {"use_case": "read and list files", "flags": {"review_for_production": True}, "verdict": {},
             "stages": [{"id": "build", "steps": [
                 {"title": "Configure consent screen", "detail": "Mark the app as Internal for testing.", "refs": []}]},
                 {"id": "launch", "steps": [
                 {"title": "Wait for verification", "detail": "Google reviews the request (24-72 hours).", "refs": []}]}]}
    g = build_guide("Google Drive", "", draft, _known_google(), [], pages, ("yes", "", None), "m", Stats())
    launch = next(st for st in g.stages if st.id == "launch")
    wait = next(x for x in launch.steps if x.title.startswith("Wait"))
    assert "72 hours" not in wait.detail and "several weeks" in wait.detail.lower()
    assert "CASA" in g.verdict.gating_step
    build = next(st for st in g.stages if st.id == "build")
    assert any("Workspace org" in x.detail for x in build.steps)


def test_restricted_guide_does_not_sell_the_narrow_path_as_the_answer():
    from app.agent.web import Page
    pages = [Page(url=f["url"], title="t", text="q", official=True) for f in _known_google()]
    draft = {"use_case": "read and write files", "flags": {"review_for_production": True},
             "verdict": {"summary": "You must pass verification for the drive.file scope."},
             "uncertainties": ["Exact verification time may vary beyond the 3\u20115 day estimate"],
             "requirements": [{"item": "OAuth verification", "detail": "Required because the app uses a sensitive Drive scope",
                               "refs": []}],
             "stages": [{"id": "build", "steps": [
                 {"title": "Declare scopes", "detail": "Request the drive.file scope.", "refs": []}]},
                 {"id": "launch", "steps": [
                 {"title": "Wait for verification outcome", "detail": "Google responds within 24\u201172 hours.", "refs": []}]}]}
    g = build_guide("Google Drive", "", draft, _known_google(), [], pages, ("yes", "", None), "m", Stats())
    assert "restricted" in g.verdict.summary and "CASA" in g.verdict.summary
    build = next(st for st in g.stages if st.id == "build")
    assert any("drive.readonly" in x.detail for x in build.steps if x.title == "Declare scopes")
    launch = next(st for st in g.stages if st.id == "launch")
    titles = [x.title for x in launch.steps]
    assert titles.index("Complete the CASA security assessment") < titles.index("Wait for verification outcome")
    assert all("hours" not in x.detail and "24" not in x.detail for x in launch.steps)
    assert not any("3" in u and "5 day" in u for u in g.uncertainties)
    assert "restricted scope" in g.requirements[0].detail
    casa = next(x for x in launch.steps if "CASA" in x.title)
    assert casa.sources and casa.sources[0] in {s.id for s in g.sources}


def test_non_breaking_hyphen_durations_are_rewritten():
    draft = {"flags": {"review_for_production": True, "review_wait_days_max": 30}, "verdict": {},
             "stages": [{"id": "build", "steps": []}, {"id": "launch", "steps": [
                 {"title": "Wait for review", "detail": "Review takes 24\u201172 hours.", "refs": []}]}]}
    g = build_guide("Acme", "", draft, [], [], [], ("yes", "", None), "m", Stats())
    d = next(st for st in g.stages if st.id == "launch").steps[0].detail
    assert d == "Review takes about 30 days."
