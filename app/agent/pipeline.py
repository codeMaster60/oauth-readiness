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
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..models import (LABELS, AgentSplit, label, Blocker, ComposioInfo, Flags, Gotcha, Guide, Note, PathOption,
                      Probe, Requirement, Source, Stage, Stats, Step, Verdict, score)
from . import probe as probes
from .knowledge import known_pages, needs_google_restricted
from .llm import Groq, LLMError
from .prompts import EXTRACT_SYSTEM, EXTRACT_USER, SYNTH_SYSTEM, SYNTH_USER
from .web import (Page, domain_of, primary_domain, fetch, guess_official_domains, keep_url, pick_snippets, quote_on_page,
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
    on_fail: Optional[object] = None

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
TASKS: set = set()  # strong refs so asyncio doesn't garbage-collect a running job
_SEM = asyncio.Semaphore(2)  # free-tier quota: at most 2 generations at once
MAX_JOBS = 300


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "", q.lower().removeprefix("https://").removeprefix("http://").removeprefix("www."))


UMBRELLAS = {
    "google": ["Gmail", "Google Calendar", "Google Drive", "Google Sheets", "Google Docs", "YouTube"],
    "google workspace": ["Gmail", "Google Calendar", "Google Drive", "Google Sheets", "Google Docs"],
    "gsuite": ["Gmail", "Google Calendar", "Google Drive", "Google Sheets"],
    "microsoft": ["Outlook", "Microsoft Teams", "OneDrive", "SharePoint", "Microsoft Entra ID"],
    "microsoft 365": ["Outlook", "Microsoft Teams", "OneDrive", "SharePoint"],
    "office 365": ["Outlook", "Microsoft Teams", "OneDrive", "SharePoint"],
    "atlassian": ["Jira", "Confluence", "Bitbucket", "Trello"],
    "meta": ["Facebook Pages", "Instagram", "WhatsApp Business"],
    "facebook": ["Facebook Pages", "Instagram"],
    "zoho": ["Zoho CRM", "Zoho Mail", "Zoho Books"],
    "amazon": ["Amazon Seller Central", "Login with Amazon"],
}


def umbrella_choices(query: str) -> Optional[list[str]]:
    """'Google' covers products with very different review paths. Ask which one instead of guessing."""
    return UMBRELLAS.get(re.sub(r"\s+", " ", query.strip().lower()))


def running_job_for(query: str) -> Optional[Job]:
    """If someone is already researching this app, watch that run instead of starting another."""
    key = _norm(query)
    return next((j for j in JOBS.values() if not j.done and _norm(j.query) == key), None)


def new_job(query: str) -> Job:
    now = time.time()
    for jid in [j.id for j in JOBS.values() if j.done and now - j.started > 3600]:
        JOBS.pop(jid, None)
    while len(JOBS) >= MAX_JOBS:
        JOBS.pop(next(iter(JOBS)))
    job = Job(id=uuid.uuid4().hex[:10], query=query)
    JOBS[job.id] = job
    return job


def start(job: Job, llm, save, on_fail=None) -> None:
    job.on_fail = on_fail
    task = asyncio.create_task(run(job, llm, save))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


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
    return (q[:1].upper() + q[1:] if q.islower() else q), ""


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
        devish = any(k in low for k in ("developer", "docs", "/api", "api.", "oauth", "dev.", "/apps",
                                         "integration", "platform", "help.", "/help", "support."))
        s = ((10 if devish else 4) if off else 0) + sum(2 for h in hints if h in low)
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


SECURITY_RE = re.compile(r"\bCASA\b|security assessment|third[- ]party (?:security )?(?:assessment|audit)|"
                         r"penetration test|pen[- ]?test|SOC ?2|letter of (?:validation|assessment)", re.I)
JUNK_UNCERTAIN = re.compile(r"not provided|website url|url for|exact url|link to the", re.I)


def wait_days(text: str) -> Optional[int]:
    """Longest wait mentioned in free text, in calendar days ('3-5 business days' -> 7, '4-6 weeks' -> 42)."""
    best = None
    for m in re.finditer(r"(\d+)(?:\s*[-–to]+\s*(\d+))?\s*(business\s+days?|days?|weeks?|months?)", text or "", re.I):
        n = int(m.group(2) or m.group(1))
        unit = m.group(3).lower()
        days = n * 30 if unit.startswith("month") else n * 7 if unit.startswith("week") else \
            round(n * 1.4) if unit.startswith("business") else n
        best = max(best or 0, days)
    return best


HUMAN_ONLY = re.compile(r"\brecord|\bsubmit|sign up|\bsign in|\bpay\b|\bpurchase|verify (?:your|the|company|business|identity)|"
                        r"accept (?:the )?terms|\bupload\b.*\b(?:video|id)\b|\bclick\b", re.I)


def split_work(raw: dict) -> AgentSplit:
    """An agent can draft a demo script; it can't record the video or click submit. Move those to 'you'."""
    agent = [str(v) for v in raw.get("agent", [])]
    you = [str(v) for v in raw.get("you", [])]
    moved = [a for a in agent if HUMAN_ONLY.search(a)]
    return AgentSplit(agent=[a for a in agent if a not in moved], you=you + [m for m in moved if m not in you])


def build_guide(name: str, website: str, draft: dict[str, Any], facts: list[dict], probe_list: list[Probe],
                pages: list[Page], composio: tuple[str, str, Optional[str]], model: str, stats: Stats) -> Guide:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources: list[Source] = []
    page_sid: dict[str, str] = {}
    for p in pages:
        sid = f"s{len(sources) + 1}"
        page_sid[p.url] = sid
        sources.append(Source(id=sid, title=p.title[:120], url=p.url,
                              kind="official" if p.official else "third_party", accessed=stamp))
    probe_sid = {}
    for i, pr in enumerate(probe_list, 1):
        sid = f"p{i}"
        probe_sid[f"P{i}"] = sid
        sources.append(Source(id=sid, title=f"Live probe: {pr.url}", url=pr.url, kind="probe", accessed=stamp))
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
    vt = dict(draft.get("verdict") or {})
    extra_uncertain: list[str] = []
    # 1. The audit point needs evidence: a model saying "security review" isn't a third-party assessment.
    # (Hand-checked facts don't count here: Google's one mentions CASA precisely to say Calendar doesn't need it.)
    if flags.security_assessment and not any(SECURITY_RE.search(f"{f['claim']} {f['quote']}")
                                             for f in facts if not f.get("known")):
        flags.security_assessment = False
    # 1b. A backed "blocker" gotcha about an audit also counts (the model sometimes flags it there, not in flags).
    for gx in draft.get("gotchas") or []:
        if isinstance(gx, dict) and gx.get("severity") == "blocker" and gx.get("refs") and \
                SECURITY_RE.search(f"{gx.get('title', '')} {gx.get('detail', '')}"):
            flags.security_assessment = True
    # 2. Google family: does this use case need a restricted scope? Then it's weeks plus a yearly CASA audit,
    #    whatever a sensitive-scope estimate says. (Drive "read and list files" can't work on drive.file alone.)
    is_google = any(f.get("known") for f in facts) or name.lower().startswith(("google", "gmail", "youtube"))
    step_text = [f"{x.get('title', '')} {x.get('detail', '')}" for st in draft.get("stages") or [] if isinstance(st, dict)
                 for x in st.get("steps") or [] if isinstance(x, dict)]
    step_text += [f"{x.get('item', '')} {x.get('detail', '')}" for x in draft.get("requirements") or [] if isinstance(x, dict)]
    restricted = is_google and needs_google_restricted(str(draft.get("use_case") or ""), step_text)
    known_days = [wait_days(f["claim"]) for f in facts if f.get("known") and f["topic"] == "timeline"]
    known_days = [x for x in known_days if x]
    wait = str(vt.get("wait") or "unknown")
    if restricted:
        flags.review_for_production = True
        flags.security_assessment = True
        flags.review_wait_days_max = max(flags.review_wait_days_max or 0, 42)
        wait = "Several weeks: restricted-scope review plus a yearly CASA audit (Google publishes no fixed time)"
        extra_uncertain.append("Google publishes no timeline for restricted scopes. The 42 days used for the score is "
                               "my estimate, not Google's.")
    elif known_days and flags.review_for_production and not flags.security_assessment:
        flags.review_wait_days_max = max(known_days)
        span = None
        for f in facts:
            if f.get("known") and f["topic"] == "timeline" and wait_days(f["claim"]) == max(known_days):
                m = re.search(r"\d+\s*[-–]\s*\d+\s*business days", f["claim"])
                span = m.group(0) if m else None
        wait = (f"{span} (about a week), going by the provider's own published estimates" if span
                else f"About {max(known_days)} days, going by the provider's own published estimates")
    # 3. Wait text and score must agree.
    text_days = wait_days(wait)
    if not flags.review_for_production and not flags.partner_gated:
        if not wait.lower().startswith("none"):
            wait = "None to connect users" + ("" if wait.lower() in ("unknown", "") else f" (optional listing review: {wait})")
    elif text_days and flags.review_wait_days_max is None:
        flags.review_wait_days_max = text_days
    elif text_days and flags.review_wait_days_max and text_days > 2 * flags.review_wait_days_max:
        extra_uncertain.append(f"Sources disagree on the wait: the docs suggest about {flags.review_wait_days_max} days, "
                               f"but another source says {wait}.")
        wait = f"About {flags.review_wait_days_max} days (sources disagree, see below)"
    vt["wait"] = wait
    d, _ = score(flags)
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
    build = next((st for st in stages if st.id == "build"), stages[0] if stages else None)
    if build is None:
        build = Stage(id="build", name="Build & test (works today)")
        stages.insert(0, build)
    def has(pattern):
        return any(re.search(pattern, f"{x.title} {x.detail}", re.I) for x in build.steps)

    if not has(r"\bcreate\b|\bregister\b|new (?:oauth )?app|new client"):
        build.steps.insert(0, Step(
            title="Create the OAuth app",
            detail=f"In {name}'s developer console, register a new user-authorized (authorization code) OAuth app.",
            who="you", minutes=10, evidence="uncertain"))
    if not has(r"redirect|callback"):
        build.steps.insert(1, Step(
            title="Add the redirect (callback) URL",
            detail=f"In the app's OAuth settings, add Composio's callback URL: {COMPOSIO_CALLBACK}. It has to match exactly.",
            who="you", minutes=2, evidence="documented", sources=["c2"]))
    if not has(r"composio|client id|client secret|credentials"):
        build.steps.append(Step(
            title="Put the client ID and secret into a Composio auth config",
            detail="Create Auth Config, pick the toolkit, turn on your own developer credentials, paste both values.",
            who="you", minutes=3, evidence="documented", sources=["c2"]))
    if not has(r"\btest\b|connect (?:a|your)"):
        build.steps.append(Step(
            title="Connect a test account end to end",
            detail="Run one real connection through your app and make a read-only call to check the token and scopes.",
            who="either", minutes=5, evidence="documented", sources=["c2"]))
    for st in stages:
        for x in st.steps:
            if x.who in ("agent", "either") and re.search(r"\bwait|submit|sign up|pay|verify (?:your|the) (?:identity|business)|record", x.title, re.I):
                x.who = "you"
    managed, mnote, murl = composio
    if murl:
        sources.append(Source(id="c1", title="Composio toolkit page", url=murl, kind="composio", accessed=stamp))
    sources.append(Source(id="c2", title="Composio: managed vs custom auth",
                          url="https://docs.composio.dev/docs/authentication/custom-app-vs-managed-app",
                          kind="composio"))
    # Durations elsewhere in the guide must agree with the headline (models drift: "24-72 hours" under a weeks-long
    # review). Rewrite any review/wait step or gotcha whose own duration is way off.
    target = flags.review_wait_days_max
    if target:
        short = "several weeks" if restricted else ("about a week" if target <= 10 else f"about {target} days")
        dash = r"[-‐-―]"  # models love non-breaking hyphens ("24‑72 hours")
        unit = r"(?:business\s+)?(?:hours?|days?|weeks?)"
        dur = re.compile(rf"\b\d+\s*(?:{dash}|to)\s*(?:\d+|several|a few)\s*{unit}|\b\d+\s*{unit}", re.I)

        def fix(text):
            if not text:
                return text
            text = re.sub(dash, "-", text)
            days = wait_days(text) or (1 if re.search(r"\d+\s*(?:-|to)?\s*\d*\s*hours?", text, re.I) else None)
            if days and (days * 2 < target or days > target * 2) and re.search(r"review|verif|approv|wait", text, re.I):
                return dur.sub(short, text, count=1)
            return text
        for st in stages:
            for x in st.steps:
                if restricted and re.search(r"\bwait\b", x.title, re.I):
                    # Sub-estimates ("brand 2-3 days, sensitive 3-5 days") only confuse a weeks-long restricted review.
                    x.detail = ("Expect several weeks. Google publishes no fixed time for restricted scopes, and the CASA "
                                "assessment runs on the assessor's schedule. Watch for follow-up emails from Google and "
                                "answer them quickly.")
                elif re.search(r"wait|review|verif", x.title, re.I):
                    x.detail = fix(x.detail)
        for gx in draft.get("gotchas") or []:
            if isinstance(gx, dict):
                gx["detail"] = fix(gx.get("detail", ""))
    if restricted:
        vt["gating_step"] = "Restricted-scope verification plus the yearly CASA security assessment"
        if not re.search(r"restricted|casa|security assessment", str(vt.get("summary") or ""), re.I):
            vt["summary"] = (f"Doable, but slow. Reading a user's existing {name} data needs a restricted scope, which "
                             "means Google's restricted-scope verification and a CASA security assessment every year. "
                             "If your agent only touches files it creates or the user picks (drive.file), or only sends "
                             "mail (gmail.send), it's a 3-5 business day review instead.")
        narrow_scope = re.compile(r"drive\.file|gmail\.send|narrow", re.I)
        for st in stages:
            for x in st.steps:
                if re.search(r"scope", x.title, re.I) and narrow_scope.search(x.detail or ""):
                    x.detail += (" That only covers the narrow path. To read or list the user's existing data you need a "
                                 "restricted scope (e.g. drive.readonly, gmail.readonly), see 'Pick your path'.")
        launch = next((st for st in stages if st.id != "build"), None)
        casa_sid = next((page_sid[f["url"]] for f in facts
                         if f.get("known") and f["url"] in page_sid and SECURITY_RE.search(f["claim"])), None)
        if launch and not any(re.search(r"casa|security assessment", x.title, re.I) for x in launch.steps):
            at = next((i for i, x in enumerate(launch.steps) if re.search(r"\bwait\b", x.title, re.I)), len(launch.steps))
            launch.steps.insert(at, Step(
                title="Complete the CASA security assessment",
                detail="Restricted scopes need a CASA security assessment with one of Google's authorized labs. "
                       "For most apps that means running a security scan and having the lab review the results. "
                       "It repeats every year.",
                who="you", evidence="documented" if casa_sid else "uncertain",
                sources=[casa_sid] if casa_sid else []))
    # Google: "Internal" only works inside a Workspace org (seen first-hand: greyed out on a personal account).
    if is_google:
        for st in stages:
            for x in st.steps:
                if re.search(r"\binternal\b", x.detail or "", re.I) and "Workspace" not in (x.detail or ""):
                    x.detail += " (Internal only works if every user is in your own Google Workspace org; otherwise pick External.)"
    extra_paths = []
    if restricted and not draft.get("paths"):
        casa = [page_sid[f["url"]] for f in facts if f.get("known") and SECURITY_RE.search(f["claim"])]
        narrow = flags.model_copy(update={"security_assessment": False, "review_wait_days_max": 7})
        extra_paths = [
            PathOption(name="Sensitive scopes only", difficulty=score(narrow)[0], wait="3-5 business days (about a week)",
                       when="If you can live without reading the user's existing data. For Drive that's drive.file, which only "
                            "sees files your app created or the user picks. For Gmail, gmail.send.",
                       evidence="documented" if casa else "uncertain", sources=casa[:1]),
            PathOption(name="Restricted scopes (what this use case needs)", difficulty=score(flags)[0],
                       wait="several weeks, plus a CASA audit every year",
                       when="Reading, listing or searching the user's mail or files.",
                       evidence="documented" if casa else "uncertain", sources=casa[:1]),
        ]
    requirements = items(draft.get("requirements"), Requirement, ["item", "when", "detail"])
    gotchas = items(draft.get("gotchas"), Gotcha, ["title", "detail", "severity"])
    blockers = items(draft.get("blockers"), Blocker, ["symptom", "cause", "fix"])
    paths = items(draft.get("paths"), PathOption, ["name", "when", "difficulty", "wait"]) + extra_paths
    used = {sid for coll in (requirements, gotchas, blockers, paths) for x in coll for sid in x.sources}
    used |= {sid for st in stages for x in [*st.limits, *st.steps] for sid in x.sources}
    # Only list pages that something on the guide actually cites. Probes and Composio pages always stay.
    sources = [s for s in sources if s.kind in ("probe", "composio") or s.id in used]
    # Renumber page sources s1..sN so the list doesn't read s2, s5, s10 after unused ones are dropped.
    remap, n = {}, 0
    for src in sources:
        if src.id.startswith("s"):
            n += 1
            remap[src.id] = f"s{n}"
            src.id = remap[src.id]
    for coll in (requirements, gotchas, blockers, paths, *[[*st.limits, *st.steps] for st in stages]):
        for x in coll:
            x.sources = [remap.get(sid, sid) for sid in x.sources]
    uncertainties = [str(u) for u in draft.get("uncertainties") or [] if not JUNK_UNCERTAIN.search(str(u))]
    if restricted:  # leftovers from the narrow-path reading ("may vary beyond the 3-5 day estimate")
        uncertainties = [u for u in uncertainties if not re.search(r"\b3\s*[-\u2010-\u2015]\s*5\b", u)]
        for r in requirements:
            r.detail = re.sub(r"\bsensitive (Drive |Gmail )?scope", "restricted scope", r.detail or "", flags=re.I)
    uncertainties += extra_uncertain
    return Guide(
        # The name comes from the user's input, never the model (it once echoed prompt wording into the title).
        slug=slugify(name),
        name=name,
        website=website,
        category=draft.get("category") or "",
        use_case=draft.get("use_case") or "",
        origin="generated",
        updated=stamp,
        model=model,
        verdict=Verdict(
            difficulty=d, label=label(flags, d),
            summary=vt.get("summary") or "", active_effort=vt.get("active_effort") or "unknown",
            wait=vt.get("wait") or "unknown", gating_step=vt.get("gating_step") or "unknown",
        ),
        flags=flags,
        concept=draft.get("concept") or "",
        paths=paths,
        composio=ComposioInfo(managed=managed, managed_note=mnote, when_own_app=COMPOSIO_WHEN_OWN,
                              redirect_uri=COMPOSIO_CALLBACK),
        stages=stages,
        requirements=requirements,
        gotchas=gotchas,
        blockers=blockers,
        agent_split=split_work(draft.get("agent_split") or {}),
        uncertainties=uncertainties,
        probes=probe_list,
        sources=sources,
        stats=stats,
    )


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if "GROQ_API_KEY" in msg:
        return "The live agent is switched off on this server (no model key). The hand-checked guides still work."
    if msg == "busy" or "capacity" in msg:
        return "The free model tier stayed busy for too long, usually because other runs were going at the same time. Give it a minute and try again."
    if isinstance(e, LLMError):
        return "The model kept erroring out, so I stopped rather than write a half-baked guide. Try again in a minute."
    return msg  # our own RuntimeErrors are already written for people


async def run(job: Job, llm: Groq, save) -> None:
    if _SEM.locked():
        await job.emit("step", id="queue", status="running",
                       label="Waiting for a free slot. The free tier runs two at a time, so this can take a few minutes.")
    async with _SEM:
        if any(e.get("id") == "queue" for e in job.events):
            await job.emit("step", id="queue", status="done", label="Got a slot, starting now")
        try:
            await _run(job, llm, save)
        except Exception as e:  # surface every failure to the UI, in plain words
            job.error = str(e)
            await job.emit("error", message=friendly_error(e), retry=True)
            if getattr(job, "on_fail", None):
                job.on_fail()  # a failed run shouldn't count against the person's run limit
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
            f"{name} developer publish app review OAuth app listing requirements",
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
            p.snippets = await asyncio.to_thread(pick_snippets, p.text)
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
        probe_list, disc = await probes.discovery(official, client, primary_domain(name, website, official))
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
        raw = [f for f in (data.get("facts") or [])[:10] if isinstance(f, dict) and f.get("claim")]
        # Fuzzy quote matching is CPU work; keep it off the event loop so the site stays responsive.
        oks = await asyncio.to_thread(lambda: [quote_on_page(str(f.get("quote", "")), p.text) for f in raw])
        out = [{"topic": f.get("topic", "gotcha"), "claim": str(f["claim"])[:300],
                "quote": str(f.get("quote", ""))[:300], "url": p.url, "official": p.official,
                "verified": ok, "known": False} for f, ok in zip(raw, oks)]
        await job.emit("log", text=f"{len(out)} facts from {p.title[:60]} ({sum(x['verified'] for x in out)} quotes verified)")
        return out

    sem = asyncio.Semaphore(3)

    async def guarded(p):
        async with sem:
            return await extract(p)

    for chunk in await asyncio.gather(*(guarded(p) for p in pages)):
        facts.extend(chunk)
    kp = known_pages(official, name)
    if kp:
        for p in kp:
            pages.append(p)
            for topic, claim, quote in p.known:
                facts.append({"topic": topic, "claim": claim, "quote": quote, "url": p.url, "official": True,
                              "verified": True, "known": True})
        await job.emit("log", text=f"Added {sum(len(p.known) for p in kp)} hand-checked facts I already verified for this provider")
    # Prefer verified + official facts if we must trim to fit the synthesis budget.
    facts.sort(key=lambda f: (not f.get("known"), not f["verified"], not f["official"]))
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
        f"{f['id']} | {f['topic']} | {'HAND-CHECKED' if f.get('known') else 'official' if f['official'] else '3rd-party'} s{src_idx[f['url']]}"
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
