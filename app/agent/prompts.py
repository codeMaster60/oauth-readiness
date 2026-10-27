EXTRACT_SYSTEM = """You extract facts about getting an OAuth app working and APPROVED for a third-party provider.
You read excerpts of ONE web page. Output JSON only.

Return: {"facts": [{"topic": T, "claim": C, "quote": Q}]}
- T is one of: create_app, redirect, scopes, dev_limits, review, timeline, requirement, tokens, rate_limits, cost, gotcha
- C: one short, specific, self-contained sentence in your own words (include numbers, limits, durations).
- Q: an EXACT sentence or clause copied verbatim from the excerpt that supports C (max 30 words). Never invent text.
- Only facts about the provider named by the user. Max 10 facts. Prefer: review/approval steps, wait times,
  install/user caps before approval, required documents (privacy policy, demo video, company verification),
  security assessments, sensitive scopes, token expiry, redirect URI rules, rate limits for unapproved apps.
- If the excerpt has nothing relevant, return {"facts": []}."""

EXTRACT_USER = """Provider: {name}
Page: {title}
URL: {url}

Excerpts:
{snippets}"""


SYNTH_SYSTEM = """You write a practical guide for a developer who must create an OAuth app with a provider and get it
approved so their AI agent's users can connect their accounts. Use ONLY the numbered facts given.
Cite facts with refs like ["F3","F7"]. A claim with no supporting fact must have refs [] (it will be marked uncertain).
Be concrete and honest: if facts don't establish something (e.g. review time), say it is unknown.
Write for a busy developer: short sentences, no marketing. Output JSON only with exactly these keys:

{
 "name": str, "category": str (e.g. "CRM", "Email", "Dev tools"),
 "use_case": str (the typical agent use case this guide assumes, e.g. "read and send email"),
 "concept": str (2-3 sentences: what the provider calls its OAuth app, how it relates to the developer's integration
            and to each end-user's connection),
 "flags": {"review_for_production": bool, "review_wait_days_max": int|null, "security_assessment": bool,
           "business_verification": bool, "partner_gated": bool, "paid_requirement": bool,
           "install_cap_before_review": int|null, "dev_mode_user_cap": int|null},
 "verdict": {"summary": str (1-2 sentences, the bottom line), "active_effort": str (hands-on time, e.g. "30-60 min"),
             "wait": str (waiting for the provider, e.g. "none" or "2-10 weeks" or "unknown"),
             "gating_step": str (the single step most likely to block launch)},
 "paths": [{"name": str, "when": str, "difficulty": 1-5, "wait": str, "refs": []}]  (only if use case changes the path; else []),
 "stages": [
   {"id": "build", "name": "Build & test (works today)", "summary": str,
    "limits": [{"text": str, "refs": []}],
    "steps": [{"title": str, "detail": str, "who": "you"|"agent"|"either", "minutes": int|null, "refs": []}]},
   {"id": "launch", "name": "Go to production", "summary": str, "limits": [], "steps": [...]}
 ],
 "requirements": [{"item": str, "when": "always"|"production"|"listing", "detail": str, "refs": []}],
 "gotchas": [{"title": str, "detail": str, "severity": "blocker"|"high"|"medium"|"low", "refs": []}],
 "blockers": [{"symptom": str, "cause": str, "fix": str, "refs": []}],
 "agent_split": {"agent": [str], "you": [str]},
 "uncertainties": [str]
}
Rules: 4-7 build steps, 2-6 launch steps. The redirect/callback URL step must tell them to paste the Composio
callback URL {redirect}. "agent" means an AI agent can do it (research, drafting justifications/privacy policy/
demo script, filling config); "you" means a human must (sign up, accept terms, verify identity/business, pay,
record video, click submit). Flags must follow the facts; unknown -> false/null."""

SYNTH_USER = """Provider: {name}
Website: {website}

Facts (id | topic | source | claim):
{facts}

Live probe observations (cite as P1, P2…):
{probes}

Write the guide JSON now."""
