from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .agent import pipeline
from .agent.llm import Groq
from .export import EV, WHO, to_markdown
from .models import LABELS, score
from .store import ROOT, Store

BASE = Path(__file__).resolve().parent
app = FastAPI(title="OAuth Readiness")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(EV=EV, WHO=WHO, LABELS=LABELS)

store = Store()
llm = Groq()
VERIFICATION = json.loads((ROOT / "data" / "verification.json").read_text())


def catalog_rows():
    rows = []
    for c in store.catalog:
        g = store.find(c["name"]) or store.get(c.get("slug", ""))
        rows.append({**c, "guide": g})
    return rows


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
        raise HTTPException(404)
    return PlainTextResponse(to_markdown(g, str(request.base_url).rstrip("/")),
                             media_type="text/markdown; charset=utf-8")


@app.get("/api/guides/{slug}")
def guide_json(slug: str):
    g = store.get(slug)
    if not g:
        raise HTTPException(404)
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


@app.post("/api/generate")
async def generate(req: GenReq):
    q = req.query.strip()
    if not q or len(q) > 200:
        raise HTTPException(400, "Enter an app name or website")
    if not req.force:
        g = store.find(q)
        if g:
            return {"existing": f"/guides/{g.slug}"}
    job = pipeline.new_job(q)
    asyncio.create_task(pipeline.run(job, llm, store.save))
    return {"job": job.id, "watch": f"/generate/{job.id}"}


@app.get("/generate/{job_id}", response_class=HTMLResponse)
def watch(request: Request, job_id: str):
    job = pipeline.JOBS.get(job_id)
    if not job:
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "generate.html", {"job": job})


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    job = pipeline.JOBS.get(job_id)
    if not job:
        raise HTTPException(404)

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


@app.get("/healthz")
def health():
    return {"ok": True, "live_generation": llm.configured, "curated": len(store.curated),
            "search": "tavily" if os.getenv("TAVILY_API_KEY") else "duckduckgo"}
