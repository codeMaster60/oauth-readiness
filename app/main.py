from __future__ import annotations

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
        "live": llm.configured})


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(request: Request, slug: str):
    if slug.endswith(".md"):
        return guide_md(request, slug[:-3])
    g = store.get(slug)
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
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


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
    running = pipeline.running_job_for(q)
    if running:  # someone is already researching this one, watch that run
        return {"job": running.id, "watch": f"/generate/{running.id}"}
    limited = _rate_limited(_client_ip(request))
    if limited:
        raise HTTPException(429, limited)
    job = pipeline.new_job(q)
    pipeline.start(job, llm, store.save)
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
    return {"ok": True, "live_generation": llm.configured, "curated": len(store.curated),
            "search": "tavily" if os.getenv("TAVILY_API_KEY") else "duckduckgo"}
