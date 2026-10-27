from __future__ import annotations

import ipaddress
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .agent import pipeline
from .agent.llm import Groq
from .export import EV, WHO, a_an, to_markdown
from .models import LABELS, score
from .store import ROOT, Store

BASE = Path(__file__).resolve().parent
app = FastAPI(title="OAuth Readiness")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(EV=EV, WHO=WHO, LABELS=LABELS)
templates.env.filters["a_an"] = a_an

store = Store()
llm = Groq()
VERIFICATION = json.loads((ROOT / "data" / "verification.json").read_text())

# Free-tier quota protection: each live run spends Groq + Tavily credits.
PER_IP_LIMIT, PER_IP_WINDOW = int(os.getenv("RUNS_PER_IP", "5")), 600
DAILY_LIMIT = int(os.getenv("RUNS_PER_DAY", "80"))
_ip_runs: dict[str, deque] = defaultdict(deque)
_day_runs: deque = deque()


def catalog_rows():
    rows = []
    for c in store.catalog:
        g = store.find(c["name"]) or store.get(c.get("slug", ""))
        search = " ".join(filter(None, [c["name"], c.get("category", ""), g.name if g else ""])).lower()
        rows.append({**c, "guide": g, "search": search})
    return rows


@app.exception_handler(StarletteHTTPException)
async def not_found(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and not request.url.path.startswith("/api/"):
        slug = request.url.path.rstrip("/").split("/")[-1]
        guess = slug.removesuffix(".md").replace("-", " ").strip() if request.url.path.startswith("/guides/") else ""
        return templates.TemplateResponse(request, "404.html", {"guess": guess}, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    featured = sorted(store.curated.values(), key=lambda g: g.verdict.difficulty)
    return templates.TemplateResponse(request, "index.html", {
        "featured": featured, "catalog": catalog_rows(), "generated": store.generated()[:12],
        "live": llm.configured, "out_of_quota": llm.out_of_quota})


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(request: Request, slug: str):
    if slug.lower().endswith(".md"):
        base = slug[:-3]
        if not store.get(base) and store.get(base.lower()):
            return RedirectResponse(f"/guides/{base.lower()}.md", status_code=301)
        return guide_md(request, base)
    g = store.get(slug)
    if not g and slug.lower() != slug and store.get(slug.lower()):
        return RedirectResponse(f"/guides/{slug.lower()}", status_code=301)
    if not g:
        raise HTTPException(404, "No guide with that name yet")
    _, reasons = score(g.flags)
    return templates.TemplateResponse(request, "guide.html", {"g": g, "reasons": reasons,
                                                              "counts": g.evidence_counts()})


def guide_md(request: Request, slug: str):
    g = store.get(slug)
    if not g:
        raise HTTPException(404, "No guide with that name yet")
    return PlainTextResponse(to_markdown(g, str(request.base_url).rstrip("/")),
                             media_type="text/markdown; charset=utf-8")


@app.get("/api/guides/{slug}")
def guide_json(slug: str):
    g = store.get(slug)
    if not g:
        raise HTTPException(404, "No guide with that name yet")
    return JSONResponse(g.model_dump())


@app.get("/api/catalog")
def api_catalog():
    return [{"name": r["name"], "category": r.get("category"),
             "guide": f"/guides/{r['guide'].slug}" if r["guide"] else None} for r in catalog_rows()]


@app.get("/evidence", response_class=HTMLResponse)
def evidence(request: Request):
    return templates.TemplateResponse(request, "evidence.html", {"v": VERIFICATION, "labels": LABELS})


class GenReq(BaseModel):
    query: str
    force: bool = False


def _client_ip(request: Request) -> str:
    """Who is this, for the rate limit. Checked on the live Render deploy (Oct 2026): Cloudflare sits in front and
    sets CF-Connecting-IP and True-Client-IP to the real client. A client-sent True-Client-IP is overwritten and a
    client-sent CF-Connecting-IP is refused with a 403, so these can't be spoofed. X-Forwarded-For can: the client's
    own value is kept at the front (and uvicorn's request.client trusts it), so never use its first entry."""
    for h in ("cf-connecting-ip", "true-client-ip"):
        if request.headers.get(h):
            return request.headers[h].strip()
    # Without the edge headers (local runs, another host): the rightmost public hop. Render's own hops are 10.x.
    fwd = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    for hop in reversed(fwd):
        try:
            if not ipaddress.ip_address(hop).is_private:
                return hop
        except ValueError:
            continue
    return request.client.host if request.client else "unknown"


def _refund(ip: str):
    """A run that failed on our side shouldn't count against the person's limit."""
    def go():
        if _ip_runs[ip]:
            _ip_runs[ip].pop()
        if _day_runs:
            _day_runs.pop()
    return go


def _rate_limited(ip: str) -> str | None:
    now = time.time()
    q = _ip_runs[ip]
    while q and now - q[0] > PER_IP_WINDOW:
        q.popleft()
    while _day_runs and now - _day_runs[0] > 86400:
        _day_runs.popleft()
    if len(q) >= PER_IP_LIMIT:
        return f"You've started {PER_IP_LIMIT} live runs in the last 10 minutes. Give it a few minutes and try again."
    if len(_day_runs) >= DAILY_LIMIT:
        return "The live agent has hit today's free-tier limit. The hand-checked guides still work."
    q.append(now)
    _day_runs.append(now)
    return None


@app.post("/api/generate")
async def generate(req: GenReq, request: Request):
    q = req.query.strip()
    if not q:
        raise HTTPException(400, "Type an app name or website.")
    if len(q) > 200:
        raise HTTPException(400, "That's too long. Just the app's name or website is enough.")
    if not req.force:
        g = store.find(q)
        if g:
            return {"existing": f"/guides/{g.slug}"}
    if llm.out_of_quota:  # every run would fail; say so now instead of after ten minutes
        raise HTTPException(503, pipeline.friendly_error(pipeline.LLMError("daily")))
    choices = pipeline.umbrella_choices(q)
    if choices:  # "Google" is five different review paths; ask instead of guessing
        return {"choose": choices}
    running = pipeline.running_job_for(q)
    if running:  # someone is already researching this one, watch that run
        return {"job": running.id, "watch": f"/generate/{running.id}"}
    ip = _client_ip(request)
    limited = _rate_limited(ip)
    if limited:
        raise HTTPException(429, limited)
    job = pipeline.new_job(q)
    pipeline.start(job, llm, store.save, on_fail=_refund(ip))
    return {"job": job.id, "watch": f"/generate/{job.id}"}


@app.get("/generate/{job_id}", response_class=HTMLResponse)
def watch(request: Request, job_id: str):
    job = pipeline.JOBS.get(job_id)
    if not job:
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "generate.html", {"job": job, "started_ms": int(job.started * 1000)})


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    job = pipeline.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such run")

    async def stream():
        i = 0
        while True:
            while i < len(job.events):
                yield f"data: {json.dumps({**job.events[i], 'i': i})}\n\n"
                i += 1
            if job.done and i >= len(job.events):
                break
            await job.wait_for(i)
            yield ": keep-alive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=301)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /api/\nDisallow: /generate/\n")


@app.get("/healthz")
def health():
    return {"ok": True, "live_generation": llm.configured, "out_of_quota": llm.out_of_quota, "curated": len(store.curated),
            "search": "tavily" if os.getenv("TAVILY_API_KEY") else "duckduckgo"}
