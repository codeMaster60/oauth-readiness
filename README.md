# OAuth Readiness

**Will this OAuth app take an hour, or block your launch for weeks?**

A developer building an AI agent types the app their users need to connect (e.g. "Gmail", "zoom.us"). They get:

- a **verdict** (difficulty 1–5, hands-on effort, wait time, and the single step that gates production);
- **ordered steps**, split into what works today and what production needs;
- **requirements, gotchas and blocker fixes**;
- **what an agent can do vs. where a human must take over**.

Every claim is tagged with how we know it: ✅ tested · 🛰️ probed live · 📄 official docs · 🔗 third-party · ⚠️ uncertain.

If the app isn't in the library, an agent researches it **live** (about 1–3 min) and you watch it work: searching, reading the official docs, probing the provider's real endpoints, and writing a guide with the same structure. Every guide has a shareable link, plus Markdown and JSON versions so a developer's own coding agent can consume it.

- **Live:** https://oauth-readiness.onrender.com (free Render plan: the first load after idle can take ~50s to wake up)
- **Evidence & method:** `/evidence` lists the setup attempts, claims checked against sources, what we corrected, and what we couldn't verify.

---

## The problem, and the scope I chose

Creating an OAuth app takes minutes with every provider. What developers can't easily see is **what gates production**, and that's different for each one:

| Provider | Creating the app | What actually gates launch |
|---|---|---|
| GitHub | minutes | Customer orgs block OAuth apps by default until an owner approves |
| Notion | minutes | Nothing, but one permanent setting at creation rules out the Marketplace |
| LinkedIn | minutes | 60-day tokens with no refresh (unless partner); company Pages are vetted |
| Slack | minutes | Unlisted apps get 1 req/min history APIs; listing needs 10 installs + up to ~12 weeks |
| HubSpot | ~1 hour (CLI) | Hard cap of 25 installs until the Marketplace listing is approved |
| Gmail | ~1 hour | Restricted-scope review + paid annual CASA security audit (weeks) |

So the product is focused on **the path to production approval**, not on re-documenting console clicks. Each guide leads with the gate. It also shows how the scope you choose changes the path (Gmail send-only takes days; Gmail read takes weeks plus an audit), so developers decide before writing code.

**How the provider OAuth app relates to integrations and users.** You register one OAuth app per provider. In Composio, it becomes an auth config; until you have one, Composio's managed app stands in. Each user who clicks "Allow" creates a connection tied to *that* app. Switching apps later forces every user to reconnect. Each guide therefore opens with **"Do you need your own app?"**: when Composio's managed app is enough, and when to bring your own (branding, custom scopes, dedicated rate limits, faster polling).

## What's in this slice

- **6 hand-checked guides** chosen to span the range: GitHub (1/5), Notion (1/5), LinkedIn (2/5), Slack (4/5), HubSpot (4/5), Gmail (5/5).
- **The 100-app catalog**: the target library. Ready guides open instantly; any other app is one click from a live agent run.
- **Live agent generation** for any app name or URL, streamed to the browser.
- **The `/evidence` page**: setup attempts, 14 sampled claims (confirmed / corrected / conflict / unverified), lessons learned, the rubric, and limitations.

## How the live agent works

```
name or URL
  → Composio public toolkit page: is a managed OAuth app available?
  → 4 targeted web searches → rank: provider's own domains first (≤2 third-party pages)
  → fetch + clean pages → keep only OAuth/review/limits paragraphs (keyword scoring, no LLM)
  → live probes, no account: /.well-known/openid-configuration, and the real authorize endpoint
     with a fake client_id (shows the exact error a developer will hit)
  → per-page fact extraction (small model); each fact must carry a verbatim quote
  → code checks every quote is actually on the page; if not, the claim is downgraded to ⚠️ uncertain
  → guide written by a larger model from numbered facts only, citing fact IDs
  → difficulty computed by a fixed rubric from extracted flags (never the model's opinion)
```

## Key decisions

1. **Lead with the verdict, not the steps.** The assignment's core question is "an hour or weeks?", so that's the first thing on every page.
2. **Deterministic scoring.** The LLM extracts facts (e.g. "security assessment required", "max wait 60 days"), and a published rubric turns them into 1–5. Scores are comparable across apps and explainable ("Why 4/5?" on every guide).
3. **Evidence is enforced in code, not requested in a prompt.** Model-quoted text is matched against the fetched page. Unmatched claims are shown as uncertain rather than hidden.
4. **"Attempt", not just "read".** Without accounts, the agent still probes providers' real OAuth endpoints. Console steps that need a human login were tried by a person and logged on `/evidence`.
5. **One schema for curated and generated guides.** The same page, Markdown export and JSON API serve both, and a test enforces that curated scores equal the rubric.
6. **Free-tier friendly.** Groq's free tier allows about 8K tokens/min *per model*, so calls are spread across a model pool with a sliding-window token budget and `retry-after` handling. Pages are pre-filtered so only relevant paragraphs reach the model.
7. **Composio-aware, provider-agnostic.** Guides work for anyone registering an OAuth app, with Composio specifics (managed-app status, callback URL, migration trap) where they matter.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add GROQ_API_KEY (free: https://console.groq.com/keys)
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q                       # 20 tests, no network needed
```

Without `GROQ_API_KEY`, the curated library works and live generation reports that it's off.

## Deploy (Render, free)

1. New → **Blueprint** → select this repo (uses `render.yaml`).
2. Set `GROQ_API_KEY`. Optionally set `TAVILY_API_KEY` for more reliable search from cloud IPs.
3. Open the URL and run a live generation (e.g. "Zoom").

## Limitations

- Generated guides are a strong first draft, limited by what search surfaces. Login-walled docs produce thin guides; pasting the provider's docs URL helps.
- Live runs take about 1–3 min on the free tier; at most 2 run at once.
- DuckDuckGo may throttle cloud servers. `TAVILY_API_KEY` fixes this.
- Render's free disk is ephemeral, so generated guides are lost on redeploy (curated guides ship in the repo).
- No app was submitted for real provider review. "Tested" covers only the console steps a person actually performed.
- The scoring rubric is intentionally simple. Two 4/5 providers can still feel different, so read the gating step.

## Repo layout

```
app/main.py            routes: library, guide pages, live run (SSE), Markdown/JSON, evidence
app/models.py          guide schema + difficulty rubric
app/agent/pipeline.py  the live agent (steps above) and job/event streaming
app/agent/web.py       search, fetch, paragraph selection, quote verification
app/agent/probe.py     account-free live probes of OAuth endpoints
app/agent/llm.py       Groq client: model pool, token budgeting, retries
app/agent/prompts.py   extraction and synthesis prompts
data/guides/*.json     the 6 hand-checked guides
data/catalog.json      the 100-app catalog
data/verification.json setup attempts, claim checks, lessons, limitations
tests/                 rubric, evidence rules, quote checks, end-to-end agent run with fakes
```

No credentials are stored in this repo. Keys are read from environment variables only.
