"""End-to-end agent run with the network and LLM faked out.

Checks the parts that matter for trust: quotes not on the page become
'uncertain', probes become 'probed', difficulty comes from the rubric,
and the UI receives a complete event stream.
"""

import asyncio

from app.agent import pipeline
from app.agent.web import Page
from app.models import Probe

DOC = ("To create an OAuth app, go to the Acme Developer Console and set the redirect URI.\n"
       "Apps must pass Acme app review before more than 50 users can install them.\n"
       "Review typically takes 15 business days.\n")


class FakeLLM:
    configured = True

    async def json_call(self, system, user, *, pool="extract", max_tokens=0, notify=None):
        if pool == "extract":
            return {"facts": [
                {"topic": "review", "claim": "Review needed past 50 users.",
                 "quote": "Apps must pass Acme app review before more than 50 users can install them."},
                {"topic": "timeline", "claim": "Review takes 15 business days.",
                 "quote": "Review typically takes 15 business days."},
                {"topic": "gotcha", "claim": "Invented claim.", "quote": "Acme guarantees approval within one hour always."},
            ]}, "fake-small"
        return {
            "name": "Acme", "category": "CRM", "use_case": "sync contacts",
            "flags": {"review_for_production": True, "review_wait_days_max": 21, "install_cap_before_review": 50},
            "verdict": {"summary": "Needs review.", "active_effort": "1h", "wait": "3 weeks", "gating_step": "App review"},
            "stages": [{"id": "build", "name": "Build", "steps": [
                {"title": "Create app", "who": "you", "refs": ["F1"]},
                {"title": "Check endpoint", "who": "agent", "refs": ["P1"]},
                {"title": "Fast approval", "who": "you", "refs": ["F3"]},
            ]}],
            "gotchas": [{"title": "Cap", "detail": "50 users", "severity": "high", "refs": ["F2"]}],
            "agent_split": {"agent": ["draft"], "you": ["submit"]},
        }, "fake-big"


def test_full_run(monkeypatch):
    async def fake_search(q, n=6):
        return [{"url": "https://docs.acme.com/oauth", "title": "Acme OAuth"}]

    async def fake_fetch(url, client):
        return Page(url=url, title="Acme OAuth docs", text=DOC)

    async def fake_composio(name, client):
        return "unknown", "No Composio toolkit page found under this name.", None

    async def fake_discovery(domains, client):
        return [Probe(url="https://acme.com/.well-known/openid-configuration", status=200,
                      observed="Discovery document is live.")], {}

    monkeypatch.setattr(pipeline, "search", fake_search)
    monkeypatch.setattr(pipeline, "fetch", fake_fetch)
    monkeypatch.setattr(pipeline, "composio_status", fake_composio)
    monkeypatch.setattr(pipeline.probes, "discovery", fake_discovery)

    saved = []
    job = pipeline.new_job("Acme")
    asyncio.run(pipeline.run(job, FakeLLM(), saved.append))

    assert job.error is None, job.error
    g = saved[0]
    steps = {s.title: s for s in g.stages[0].steps}
    assert steps["Create app"].evidence == "documented"   # official page, quote verified
    assert steps["Check endpoint"].evidence == "probed"    # live probe
    assert steps["Fast approval"].evidence == "uncertain"  # hallucinated quote
    assert g.name == "Acme" and g.slug == "acme"
    assert g.verdict.difficulty == 4                       # rubric: 1 +1 review +1 wait>=10 +0.5 cap = 3.5 -> 4
    assert g.stats.quotes_verified == 2
    types = [e["type"] for e in job.events]
    assert types[-2:] == ["done", "end"] and "source" in types and "probe" in types


def test_run_without_key_reports_error():
    class NoKey:
        configured = False
    job = pipeline.new_job("Zoom")
    asyncio.run(pipeline.run(job, NoKey(), lambda g: None))
    assert job.error and "GROQ_API_KEY" in job.error
    assert job.events[-1]["type"] == "end"
