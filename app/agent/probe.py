"""Live, account-free probes of the provider's OAuth surface.

The assignment asks agents to *attempt* setup where practical. Without an
account we can still observe real behaviour:
  1. OpenID discovery documents (`/.well-known/openid-configuration`) - the
     provider's own machine-readable statement of endpoints and scopes.
  2. The real authorization endpoint, called with a deliberately fake
     client_id. How it fails tells the developer what a wrong client ID or
     redirect URI will look like - the most common first-run blocker.
All requests are harmless anonymous GETs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from ..models import Probe
from .web import UA, domain_of

FAKE_CLIENT = "oauth-readiness-probe-invalid"
AUTH_URL_RE = re.compile(r"https://[\w.-]+/[\w/.\-]*(?:oauth2?|authorize|auth)[\w/.\-]*", re.I)


async def discovery(domains: set[str], client: httpx.AsyncClient, primary: str = "") -> tuple[list[Probe], Optional[dict]]:
    """Try the provider's main domain first. Alphabetical order once picked cloud.google.com (a workforce
    sign-in) over google.com, so order matters, and we also prefer a document whose issuer is that domain."""
    ordered = ([primary] if primary else []) + [d for d in sorted(domains) if d != primary]
    cands = []
    for d in ordered[:3]:
        for sub in ("accounts.", "", "login.", "auth.", "oauth.", "id.", "app.", "api."):
            cands.append(f"https://{sub}{d}/.well-known/openid-configuration")

    async def one(u):
        try:
            r = await client.get(u, headers={"User-Agent": UA}, timeout=6)
            if r.status_code == 200 and "authorization_endpoint" in r.text:
                return u, r.json()
        except Exception:
            return None
        return None

    results = [x for x in await asyncio.gather(*(one(u) for u in cands)) if x]
    if not results:
        return [], None

    def trust(item):
        u, doc = item
        issuer = urlparse(str(doc.get("issuer", ""))).netloc
        token_ep = str(doc.get("token_endpoint", ""))
        bad = any(x in issuer for x in ("cloud.", "workforce", "sts.")) or "sts." in token_ep
        return (0 if primary and domain_of(issuer) == primary else 1, 1 if bad else 0, cands.index(u))

    url, doc = sorted(results, key=trust)[0]
    scopes = doc.get("scopes_supported") or []
    obs = (
        f"Discovery document is live. Authorization endpoint: {doc.get('authorization_endpoint')}; "
        f"token endpoint: {doc.get('token_endpoint')}."
    )
    if scopes:
        obs += f" Advertises {len(scopes)} scopes (e.g. {', '.join(map(str, scopes[:6]))})."
    if doc.get("code_challenge_methods_supported"):
        obs += f" PKCE methods: {', '.join(doc['code_challenge_methods_supported'])}."
    return [Probe(url=url, status=200, observed=obs)], doc


DOCS_PATH = re.compile(r"/(?:docs?|developers?|guides?|reference|api-reference|help|blog|changelog)(?:/|$)", re.I)


def looks_like_authorize(u: str) -> bool:
    """A real authorize endpoint ends in /authorize (or /authorization, /auth). A docs page *about* authorization
    (linear.app/developers/oauth-actor-authorization) is not one."""
    host, _, path = re.sub(r"^https?://", "", u.split("?")[0]).partition("/")
    if re.match(r"(?:docs|developers?|dev|help|support|blog)\.", host, re.I) or DOCS_PATH.search("/" + path):
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return last in ("authorize", "authorization", "auth", "authorize.php") or last.endswith("authorize")


def find_authorize_urls(texts: list[str], official: set[str]) -> list[str]:
    seen = []
    for t in texts:
        for m in AUTH_URL_RE.findall(t):
            u = m.rstrip(".,)")
            if domain_of(u) in official and looks_like_authorize(u) and u not in seen:
                seen.append(u)
    return seen[:2]


async def authorize_probe(url: str, client: httpx.AsyncClient) -> Probe:
    params = {
        "response_type": "code",
        "client_id": FAKE_CLIENT,
        "redirect_uri": "https://example.com/callback",
        "state": "probe",
    }
    full = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    try:
        r = await client.get(full, headers={"User-Agent": UA}, timeout=10, follow_redirects=False)
    except Exception as e:
        return Probe(url=url, observed=f"Endpoint did not respond ({type(e).__name__}).", ok=False)
    loc = r.headers.get("location", "")
    status = r.status_code
    if 300 <= status < 400 and loc:
        q = parse_qs(urlparse(loc).query)
        err = (q.get("error") or [""])[0]
        host = urlparse(loc).netloc or urlparse(url).netloc
        if err:
            obs = f"Redirects with error '{err}' for an unknown client_id."
        elif "login" in loc.lower() or "signin" in loc.lower():
            obs = (f"Redirects to a sign-in page on {host} before validating the client_id - "
                   "a wrong client ID or redirect URI will only surface after the user logs in.")
        else:
            obs = f"Redirects to {host}{urlparse(loc).path} for an unknown client_id."
    else:
        soup = BeautifulSoup(r.text or "", "html.parser")
        for tag in soup(["style", "script", "noscript", "svg"]):
            tag.decompose()
        body = " ".join(soup.get_text(" ").split())
        m = re.search(r"(invalid[_ ]\w+|unauthori[sz]ed[_ ]client|client[_ ]?id[^.]{0,80}|error[^.]{0,100})",
                      body, re.I)
        snippet = m.group(0)[:140] if m else body[:120]
        obs = f"Returns HTTP {status} for an unknown client_id: “{snippet}”."
    return Probe(url=url, status=status, observed=obs)
