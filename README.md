# OAuth Readiness

Creating an OAuth app takes ten minutes. Getting it approved can take ten weeks. And nobody tells you which one you're in for until you're already in it.

This is a small tool for that. You type the app your users need to connect (Gmail, Slack, Zoom, whatever) and you get a guide that starts with the thing you actually want to know: how long is this going to take, and what's going to block me.

**Live:** https://oauth-readiness.onrender.com
It's on Render's free plan, so if nobody's opened it in a while the first load takes about 50 seconds. It wakes up.

## What's in here

- **Six guides I checked by hand.** GitHub, Notion, LinkedIn, Slack, HubSpot, Gmail. Picked on purpose so they cover the whole range, from "done in ten minutes" to "weeks, plus a paid audit every year".
- **A live agent for everything else.** Type any app, watch it read the provider's docs, poke at its real login endpoints, and write a guide in a minute or two.
- **Every claim says how I know it:** `tested`, `probed live`, `from docs`, `third-party`, or `not verified`.
- **Every guide is shareable.** A link, plus Markdown and JSON so your own coding agent can read it.
- **An evidence page (`/evidence`)** with what I tested, what I corrected, and what I couldn't confirm.

## Why the verdict comes first

When I started, I assumed the hard part was the setup. It isn't. I created apps on GitHub, Slack and Google Cloud while building this. The longest one took about 20 minutes.

The hard part is what comes after, and it's different for every provider:

| App | Making it | What actually holds up launch |
|---|---|---|
| GitHub | ~3 min (I timed it) | Your customer's GitHub org blocks OAuth apps by default until an owner approves yours |
| Notion | minutes | Nothing, but one setting you pick at creation can never be changed |
| LinkedIn | minutes | Tokens die after 60 days. Company pages need a separate, vetted application |
| Slack | ~15 min | Unlisted apps get history APIs throttled to 1 request a minute. Listing needs 10 active workspaces and up to ~12 weeks of review |
| HubSpot | about an hour | Hard stop at 25 installs until your marketplace listing is approved |
| Gmail | ~20 min | Reading mail means restricted-scope review plus a paid security audit, every year |

So every guide opens with four things: difficulty out of 5, hands-on time, waiting time, and the one step most likely to hold you up. Steps come after.

The other thing I didn't expect: sometimes the scope you pick matters more than the provider. Gmail send-only goes through a few days of review. Ask to read mail and it's restricted, weeks plus an audit. I watched Google sort the two into different buckets in the console. So guides have a "pick your path" bit before any steps.

## The Composio bit

You don't always need your own OAuth app. Composio has managed apps for most providers, which is fine while you're building. You'll want your own once your users should see your name instead of "Composio", or you need extra scopes, your own rate limits, or faster polling.

The part that catches people: connections stick to the app they were made with. Move your users to your own app later and every single one of them reconnects. So every guide brings this up first, before you're in too deep.

## How the live agent works

1. Checks Composio's toolkit page for a managed app.
2. Runs four searches and puts the provider's own docs first. Forums and blogs count as third-party.
3. Opens the pages and keeps only the paragraphs about OAuth, review and limits. No model involved in that part, which keeps it inside the free tier.
4. Pokes the real endpoints without logging in: the OpenID discovery file, and the actual login URL with a fake client ID, to see what error you'd hit.
5. A small model pulls facts out of each page. Each fact needs a quote from that page.
6. Code checks every quote is actually on the page. If it isn't, the claim gets marked `not verified`.
7. If I've already verified rules for this provider (Google, for now), those facts get added and win any conflict. A live "Google Calendar" guide shouldn't re-guess Google's review times from a blog.
8. A bigger model writes the guide from those facts only.
9. The score comes from fixed rules, not the model. A security audit only counts if a source actually names one.

Two of those I care about more than the rest.

**The model doesn't get to make up quotes.** Models will invent a convincing line and cite it. So every quote gets checked against the page it supposedly came from. On the first real run (Zoom), 32 of 36 checked out. The other four show up as not verified instead of being quietly trusted.

**The model doesn't pick the score either.** It pulls facts out ("security audit required", "review takes up to 60 days") and a fixed set of rules turns those into 1 to 5. Every guide has a "Why 4/5?" that shows the math.

The probes turned out more useful than I expected. For Zoom, the agent found the sign-in page loads before Zoom checks your client ID, so a wrong ID only shows up after the user has logged in. I couldn't find that written down anywhere. Small thing, but exactly the kind of thing that eats an afternoon.

## What actually trying it changed

Honestly, the most useful part of this whole thing was doing instead of reading.

- GitHub's docs tell you to fill in "Authorization callback URL". That field doesn't exist anymore. The form says "Redirect URIs" now.
- GitHub tokens expire by default since August 2026. Plenty of guides and libraries still say they never expire.
- Slack's "From scratch" option is gone. It's "Blank app" now, followed by a four-step wizard that also installs the app.
- Slack's own docs say 10 active workspaces before you can submit to the marketplace. A blog from this year says 5. I went with Slack.
- Running my own agent on Zoom and Asana turned up five bugs in it: fact IDs leaking into sentences, a forum labelled official, server-to-server docs mixed into a user-login guide, a weird title, and a wait time that contradicted the score. All fixed, all covered by tests now.
- Then I had the whole site reviewed end to end by a pass that only hunted for problems. The worst ones: the server could stall while parsing big pages, the Google Calendar guide probed the wrong Google endpoint, and it billed Calendar for an audit that only applies to Gmail-style scopes. Fixed, and each one has a regression test.
- A second review of the fixed site caught two more: two live runs at once could make the agent give up while it was only waiting its turn, and the Google Drive guide said "about a week" when listing someone's files needs a restricted scope and a yearly paid audit. Both fixed and tested too.

The full list, with sources, is on `/evidence`.

## Running it yourself

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # put your GROQ_API_KEY in (free: https://console.groq.com/keys)
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload   # http://localhost:8000
pytest -q                       # 45 tests, no network needed
```

No Groq key? The six hand-checked guides still work, and the live agent just tells you it's off.

## Deploying (Render, free)

1. New → Blueprint → pick this repo. It reads `render.yaml`.
2. Add `GROQ_API_KEY`. Add `TAVILY_API_KEY` too (free) because search from cloud servers is flaky without it.
3. Open the URL and try an app.

## Where it falls short

- Live guides are a good first draft, not gospel. They're only as good as what search turns up, and docs behind a login come out thin.
- Free-tier models are slow-ish, so a live run is a minute or two and only two run at once. A third person sees "waiting for a free slot" instead of a blank page.
- To keep the free quota alive, each visitor gets 5 live runs per 10 minutes and the site caps at 80 a day.
- Live scores can wobble between runs when a provider's docs are vague. Asana went 3/5, then 2/5. It's written up on `/evidence` rather than hidden.
- Render's free plan forgets generated guides on redeploy. The hand-checked ones live in the repo.
- "Tested" means I clicked through it. I didn't submit anything for real review, so review timelines come from docs, not experience.
- The scoring is simple on purpose. Two apps that both score 4 can still feel very different, so read the line about what blocks launch.

## Where things live

```
app/main.py            routes: library, guides, live runs, markdown/json, evidence
app/models.py          the guide format and the scoring rules
app/agent/pipeline.py  the live agent
app/agent/web.py       search, fetching, picking paragraphs, checking quotes
app/agent/probe.py     poking real OAuth endpoints without logging in
app/agent/llm.py       Groq client: spreads load across models to stay in the free tier
app/agent/prompts.py   what the models are told
data/guides/*.json     the six hand-checked guides
data/catalog.json      the 100-app list
data/verification.json setup notes, checked claims, lessons, limitations
tests/                 scoring, evidence rules, quote checks, a full fake agent run
```

No keys in here. Everything reads from environment variables.
