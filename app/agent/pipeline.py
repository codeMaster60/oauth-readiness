"""The live guide-generation agent.

    input (name or URL)
      -> check Composio managed-auth status (public toolkit page)
      -> search (4 targeted queries)            -> rank: official docs first
      -> fetch + clean pages                     -> keep OAuth/approval paragraphs only
      -> probe live endpoints (OIDC discovery, authorize endpoint with fake client_id)
      -> per-page fact extraction (LLM)          -> verify every quote is really on the page
      -> synthesis (LLM, facts only, cites refs) -> deterministic difficulty score
      -> Guide (schema shared with curated guides)

Every step emits events so the UI can show the agent working in real time.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import httpx

from ..models import (LABELS, AgentSplit, Blocker, ComposioInfo, Flags, Gotcha, Guide, Note, PathOption,
                      Probe, Requirement, Source, Stage, Stats, Step, Verdict, score)
from . import probe as probes
from .llm import Groq, LLMError
from .prompts import EXTRACT_SYSTEM, EXTRACT_USER, SYNTH_SYSTEM, SYNTH_USER
from .web import (Page, domain_of, fetch, guess_official_domains, keep_url, pick_snippets, quote_on_page,
                  search)

COMPOSIO_CALLBACK = "https://backend.composio.dev/api/v3/toolkits/auth/callback"
COMPOSIO_WHEN_OWN = [
    "Production: users should see your app name on the consent screen, not “Composio”.",
    "You need scopes beyond Composio's defaults.",
    "You need your own rate-limit quota (managed apps share quota across Composio users).",
    "You need faster polling triggers than the managed 15-minute minimum.",
    "Self-hosted or regional instances of the provider.",
]


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "app"


# ---------------------------------------------------------------------------
# Job / event plumbing
# ---------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    query: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    slug: Optional[str] = None
    error: Optional[str] = None
    started: float = field(default_factory=time.time)
    _cond: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def emit(self, type_: str, **data):
        async with self._cond:
            self.events.append({"type": type_, "t": round(time.time() - self.started, 1), **data})
            self._cond.notify_all()

    async def wait_for(self, n: int, timeout: float = 15):
        async with self._cond:
            if len(self.events) > n or self.done:
                return
            try:
                await asyncio.wait_for(self._cond.wait(), timeout)
            except asyncio.TimeoutError:
                pass


JOBS: dict[str, Job] = {}
_SEM = asyncio.Semaphore(2)  # free-tier quota: at most 2 generations at once


def new_job(query: str) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], query=query)
    JOBS[job.id] = job
    return job


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def parse_input(q: str) -> tuple[str, str]:
    q = q.strip()
    if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", q):
        url = q if q.startswith("http") else f"https://{q}"
        host = re.sub(r"^www\.", "", url.split("/")[2])
        labels = host.split(".")
        core = labels[-2] if len(labels) >= 2 else labels[0]
        if core in {"co", "com"} and len(labels) >= 3:
            core = labels[-3]
        return core.replace("-", " ").title(), url
    return q, ""


async def composio_status(name: str, client: httpx.AsyncClient) -> tuple[str, str, Optional[str]]:
    """Read Composio's public toolkit page. Returns (managed yes/no/unknown, note, url)."""
    for slug in dict.fromkeys([slugify(name).replace("-", ""), slugify(name).replace("-", "_"), slugify(name)]):
        url = f"https://docs.composio.dev/toolkits/{slug}"
        try:
            r = await client.get(url, timeout=10)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        t = r.text
        if re.search(r"Composio-managed OAuth available", t, re.I):
            return "yes", "Composio's toolkit page lists a Composio-managed OAuth app.", url
        if re.search(r"Bring your own OAuth|Requires your own|your own credentials", t, re.I):
            return "no", "Composio's toolkit page says you must bring your own OAuth app.", url
        if re.search(r"API key|Bearer|Basic auth", t, re.I) and not re.search(r"OAuth", t):
            return "no", "Composio's toolkit page lists only non-OAuth auth (e.g. API key).", url
        return "unknown", "Composio has a toolkit page, but managed-auth status was not stated.", url
    return "unknown", "No Composio toolkit page found under this name.", None


COMMUNITY_HOSTS = ("forum", "community", "discuss", "support.", "answers.", "stackoverflow", "devforum")
OFF_TOPIC = ("server-to-server", "s2s", "internal-app", "client-credential", "service-account", "personal-access")


def is_official(url: str, official: set[str]) -> bool:
    """Provider's own docs. Forums and community sites on the same domain are user posts, not official."""
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return domain_of(url) in official and not any(c in host for c in COMMUNITY_HOSTS)


_REF = r"[FP]\d{1,2}"
_LIST = _REF + r"(?:\s*(?:,|;|/|&|\band\b)\s*" + _REF + r")*"
PAREN_RE = re.compile(r"\s*\(\s*(?:see|per|from)?\s*" + _LIST + r"\s*\)")
LEAD_RE = re.compile(r"\s*\b(?:per|see|from)\s+" + _LIST + r"\b")
BARE_RE = re.compile(r"\b" + _LIST + r"\b")


def strip_refs(text):
    """Fact IDs are for us (citations), not for readers: remove '(F3, F7)', 'per F26' and stray 'F3'."""
    if not isinstance(text, str):
        return text
    out = PAREN_RE.sub("", text)
    out = LEAD_RE.sub("", out)
    out = BARE_RE.sub("", out)
    out = re.sub(r"\s+([,.;:)])", r"\1", out)      # space before punctuation
    out = re.sub(r"([,;])\s*([,;.])", r"\2", out)   # doubled punctuation
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"^\s*[,;]\s*", "", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def clean(obj):
    if isinstance(obj, dict):
        return {k: (v if k == "refs" else clean(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return strip_refs(obj)


def rank_urls(results: list[dict], official: set[str], first: Optional[str]) -> list[tuple[str, str, bool]]:
    hints = ("oauth", "auth", "app", "review", "marketplace", "publish", "verif", "scope", "distribut",
             "partner", "developer", "listing", "consent", "rate-limit", "ratelimit")
    seen, scored = set(), []
    for r in results:
        u = r["url"].split("#")[0]
        if u in seen or not keep_url(u):
            continue
        seen.add(u)
        off = is_official(u, official)
        low = u.lower()
        s = (10 if off else 0) + sum(2 for h in hints if h in low)
        # Internal / server-to-server app types don't serve an agent's end users.
        s -= sum(6 for h in OFF_TOPIC if h in low)
        scored.append((s, u, r.get("title", ""), off))
    scored.sort(key=lambda x: -x[0])
    # Drop server-to-server / internal-app pages entirely when there are enough user-OAuth pages to read.
    on_topic = [x for x in scored if not any(h in x[1].lower() for h in OFF_TOPIC)]
    if sum(1 for x in on_topic if x[3]) >= 3:
        scored = on_topic
    out, n_off, n_3p = [], 0, 0
    if first:
        out.append((first, "", True))
    for s, u, t, off in scored:
        if u == first:
            continue
        if off and n_off < 6:
            out.append((u, t, True)); n_off += 1
        elif not off and n_3p < 2:
            out.append((u, t, False)); n_3p += 1
        if len(out) >= 8:
            break
    return out


def build_guide(name: str, website: str, draft: dict[str, Any], facts: list[dict], probe_list: list[Probe],
                pages: list[Page], composio: tuple[str, str, Optional[str]], model: str, stats: Stats) -> Guide:
    sources: list[Source] = []
    page_sid: dict[str, str] = {}
    for p in pages:
        sid = f"s{len(sources) + 1}"
        page_sid[p.url] = sid
        sources.append(Source(id=sid, title=p.title[:120], url=p.url,
                              kind="official" if p.official else "third_party", accessed=str(date.today())))
    probe_sid = {}
    for i, pr in enumerate(probe_list, 1):
        sid = f"p{i}"
        probe_sid[f"P{i}"] = sid
        sources.append(Source(id=sid, title=f"Live probe: {pr.url}", url=pr.url, kind="probe",
                              accessed=str(date.today())))
    fact_by_id = {f["id"]: f for f in facts}

    def backing(refs) -> dict:
        refs = [str(r).strip().upper() for r in (refs or [])]
        sids, ev = [], "uncertain"
        rank = {"uncertain": 0, "third_party": 1, "documented": 2, "probed": 3}
        for r in refs:
            if r in probe_sid:
                sids.append(probe_sid[r])
                if rank["probed"] > rank[ev]:
                    ev = "probed"
            elif r in fact_by_id:
                f = fact_by_id[r]
                sids.append(page_sid[f["url"]])
                if f["verified"]:
                    cand = "documented" if f["official"] else "third_party"
                    if rank[cand] > rank[ev]:
                        ev = cand
        return {"evidence": ev, "sources": list(dict.fromkeys(sids))}

    def items(lst, cls, keys):
        out = []
        for x in lst or []:
            if not isinstance(x, dict):
                continue
            data = {k: x.get(k) for k in keys if x.get(k) is not None}
            try:
                out.append(cls(**data, **backing(x.get("refs"))))
            except Exception:
                continue
        return out

    flags = Flags(**{k: v for k, v in (draft.get("flags") or {}).items() if k in Flags.model_fields})
    d, _ = score(flags)
    vt = dict(draft.get("verdict") or {})
    # Keep the headline consistent with the rubric: if no review gates real users, the wait can't read "2-10 weeks".
    wait = str(vt.get("wait") or "unknown")
    if not flags.review_for_production and not flags.partner_gated and not wait.lower().startswith("none"):
        vt["wait"] = "None to connect users" + ("" if wait.lower() in ("unknown", "") else f" (optional listing review: {wait})")
    stages = []
    for st in draft.get("stages") or []:
        if not isinstance(st, dict):
            continue
        stages.append(Stage(
            id=str(st.get("id") or f"stage{len(stages) + 1}"),
            name=st.get("name") or "Stage",
            summary=st.get("summary") or "",
            limits=items(st.get("limits"), Note, ["text"]),
            steps=items(st.get("steps"), Step, ["title", "detail", "who", "minutes"]),
        ))
    managed, mnote, murl = composio
    if murl:
        sources.append(Source(id="c1", title="Composio toolkit page", url=murl, kind="composio",
                              accessed=str(date.today())))
    sources.append(Source(id="c2", title="Composio: managed vs custom auth",
                          url="https://docs.composio.dev/docs/authentication/custom-app-vs-managed-app",
                          kind="composio"))
    return Guide(
        # The name comes from the user's input, never the model (it once echoed prompt wording into the title).
        slug=slugify(name),
        name=name,
        website=website,
        category=draft.get("category") or "",
        use_case=draft.get("use_case") or "",
        origin="generated",
        updated=str(date.today()),
        model=model,
        verdict=Verdict(
            difficulty=d, label=LABELS[d],
            summary=vt.get("summary") or "", active_effort=vt.get("active_effort") or "unknown",
            wait=vt.get("wait") or "unknown", gating_step=vt.get("gating_step") or "unknown",
        ),
        flags=flags,
        concept=draft.get("concept") or "",
        paths=items(draft.get("paths"), PathOption, ["name", "when", "difficulty", "wait"]),
        composio=ComposioInfo(managed=managed, managed_note=mnote, when_own_app=COMPOSIO_WHEN_OWN,
                              redirect_uri=COMPOSIO_CALLBACK),
        stages=stages,
        requirements=items(draft.get("requirements"), Requirement, ["item", "when", "detail"]),
        gotchas=items(draft.get("gotchas"), Gotcha, ["title", "detail", "severity"]),
        blockers=items(draft.get("blockers"), Blocker, ["symptom", "cause", "fix"]),
        agent_split=AgentSplit(**{k: [str(v) for v in (draft.get("agent_split") or {}).get(k, [])]
                                  for k in ("agent", "you")}),
        uncertainties=[str(u) for u in draft.get("uncertainties") or []],
        probes=probe_list,
        sources=sources,
        stats=stats,
    )


async def run(job: Job, llm: Groq, save) -> None:
    async with _SEM:
        try:
            await _run(job, llm, save)
        except Exception as e:  # surface every failure to the UI
            job.error = str(e)
            await job.emit("error", message=str(e))
        finally:
            job.done = True
            await job.emit("end")


async def _run(job: Job, llm: Groq, save) -> None:
    t0 = time.time()
    name, website = parse_input(job.query)
    stats = Stats()
    notify = lambda msg: job.emit("log", text=msg)  # noqa: E731
    if not llm.configured:
        raise LLMError("Server has no GROQ_API_KEY configured, so live generation is off.")

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # 1. Composio
        await job.emit("step", id="composio", label="Checking Composio's managed-auth directory", status="running")
        composio = await composio_status(name, client)
        await job.emit("step", id="composio", label=composio[1], status="done")

        # 2. Search
        await job.emit("step", id="search", label=f"Searching for {name}'s OAuth & app-review docs", status="running")
        queries = [
            f"{name} developer OAuth app create client id redirect URI",
            f"{name} API OAuth 2.0 authorization documentation scopes",
            f"{name} app review approval marketplace publish requirements",
            f"{name} OAuth app verification rate limits unverified app",
        ]
        if website:
            queries.insert(0, f"site:{domain_of(website)} OAuth app")
        results = [r for rs in await asyncio.gather(*(search(q) for q in queries)) for r in rs]
        official = guess_official_domains(name, website, [r["url"] for r in results])
        ranked = rank_urls(results, official, website or None)
        if not ranked:
            raise RuntimeError("Search returned nothing usable. Try pasting the provider's developer-docs URL.")
        await job.emit("step", id="search", status="done",
                       label=f"Found {len(results)} results · official domain(s): {', '.join(sorted(official)) or 'unknown'}")

        # 3. Fetch
        await job.emit("step", id="fetch", label=f"Reading {len(ranked)} pages", status="running")
        fetched = await asyncio.gather(*(fetch(u, client) for u, _, _ in ranked))
        pages: list[Page] = []
        for (u, t, off), p in zip(ranked, fetched):
            if not p:
                await job.emit("log", text=f"Could not read {u}")
                continue
            p.official = is_official(p.url, official) if p.url != (website or None) else off
            p.snippets = pick_snippets(p.text)
            if p.snippets:
                pages.append(p)
                await job.emit("source", url=p.url, title=p.title[:100], official=p.official)
        if not pages:
            raise RuntimeError("None of the pages could be read. Try pasting the provider's developer-docs URL.")
        stats.sources_read = len(pages)
        await job.emit("step", id="fetch", status="done",
                       label=f"Read {len(pages)} pages ({sum(p.official for p in pages)} official)")

        # 4. Probes
        await job.emit("step", id="probe", label="Probing live OAuth endpoints (no account needed)", status="running")
        probe_list, disc = await probes.discovery(official, client)
        auth_urls = probes.find_authorize_urls([p.text for p in pages if p.official], official)
        if disc and disc.get("authorization_endpoint"):
            auth_urls.insert(0, disc["authorization_endpoint"])
        for u in list(dict.fromkeys(auth_urls))[:2]:
            probe_list.append(await probes.authorize_probe(u, client))
        stats.probes_run = len(probe_list)
        for pr in probe_list:
            await job.emit("probe", url=pr.url, observed=pr.observed)
        await job.emit("step", id="probe", status="done",
                       label=f"{len(probe_list)} live probe(s)" if probe_list else "No public OAuth endpoint found to probe")

    # 5. Extract
    await job.emit("step", id="extract", label="Extracting facts from each page", status="running")
    facts: list[dict] = []
    models_used = set()

    async def extract(p: Page):
        user = EXTRACT_USER.format(name=name, title=p.title, url=p.url, snippets="\n\n".join(p.snippets))
        try:
            data, model = await llm.json_call(EXTRACT_SYSTEM, user, pool="extract", max_tokens=1100, notify=notify)
        except LLMError as e:
            await job.emit("log", text=f"Skipped {p.url}: {e}")
            return []
        models_used.add(model)
        out = []
        for f in (data.get("facts") or [])[:10]:
            if not isinstance(f, dict) or not f.get("claim"):
                continue
            ok = quote_on_page(f.get("quote", ""), p.text)
            out.append({"topic": f.get("topic", "gotcha"), "claim": str(f["claim"])[:300],
                        "quote": str(f.get("quote", ""))[:300], "url": p.url, "official": p.official,
                        "verified": ok})
        await job.emit("log", text=f"{len(out)} facts from {p.title[:60]} ({sum(x['verified'] for x in out)} quotes verified)")
        return out

    sem = asyncio.Semaphore(3)

    async def guarded(p):
        async with sem:
            return await extract(p)

    for chunk in await asyncio.gather(*(guarded(p) for p in pages)):
        facts.extend(chunk)
    # Prefer verified + official facts if we must trim to fit the synthesis budget.
    facts.sort(key=lambda f: (not f["verified"], not f["official"]))
    facts = facts[:45]
    for i, f in enumerate(facts, 1):
        f["id"] = f"F{i}"
    stats.facts_extracted = len(facts)
    stats.quotes_verified = sum(f["verified"] for f in facts)
    if not facts:
        raise RuntimeError("The pages didn't contain usable OAuth facts. Try pasting the provider's OAuth docs URL.")
    await job.emit("step", id="extract", status="done",
                   label=f"{len(facts)} facts · {stats.quotes_verified} quotes verified word-for-word on the source page")

    # 6. Synthesize
    await job.emit("step", id="write", label="Writing the guide from verified facts", status="running")
    src_idx = {p.url: i for i, p in enumerate(pages, 1)}
    fact_lines = "\n".join(
        f"{f['id']} | {f['topic']} | {'official' if f['official'] else '3rd-party'} s{src_idx[f['url']]}"
        f"{'' if f['verified'] else ' (unverified)'} | {f['claim']}" for f in facts)
    probe_lines = "\n".join(f"P{i} | {p.url} | {p.observed}" for i, p in enumerate(probe_list, 1)) or "(none)"
    draft, model = await llm.json_call(
        SYNTH_SYSTEM.replace("{redirect}", COMPOSIO_CALLBACK),
        SYNTH_USER.format(name=name, website=website or "unknown", facts=fact_lines, probes=probe_lines),
        pool="synth", max_tokens=3000, notify=notify)
    models_used.add(model)
    draft = clean(draft)
    stats.seconds = round(time.time() - t0, 1)
    guide = build_guide(name, website, draft, facts, probe_list, pages, composio,
                        ", ".join(sorted(models_used)), stats)
    save(guide)
    job.slug = guide.slug
    await job.emit("step", id="write", status="done",
                   label=f"Guide ready · difficulty {guide.verdict.difficulty}/5 ({guide.verdict.label})")
    await job.emit("done", slug=guide.slug)
