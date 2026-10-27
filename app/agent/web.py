"""Search, fetch, clean and pick the relevant parts of provider docs.

Everything here is deterministic (no LLM) so we spend scarce free-tier tokens
only on reading the paragraphs that actually talk about OAuth and approval.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; OAuthReadinessBot/1.0; +https://github.com/codeMaster60/oauth-readiness)"

KEYWORDS = {
    # weight, terms
    3: ["client id", "client secret", "redirect uri", "redirect url", "callback url", "oauth app",
        "app review", "verification", "security review", "security assessment", "marketplace",
        "approved", "approval", "submit for review", "business days", "weeks", "publish"],
    2: ["oauth", "scope", "scopes", "consent screen", "test users", "rate limit", "install",
        "distribution", "partner", "privacy policy", "terms of service", "refresh token",
        "expire", "expires", "production", "sandbox", "developer account", "verified"],
    1: ["authorize", "authorization", "token", "app", "api", "permission", "domain", "listing", "review"],
}

SKIP_DOMAINS = {"youtube.com", "facebook.com", "twitter.com", "x.com", "reddit.com", "quora.com",
                "linkedin.com/pulse", "medium.com", "pinterest.com", "tiktok.com", "instagram.com"}


@dataclass
class Page:
    url: str
    title: str
    text: str
    official: bool = False
    snippets: list[str] = field(default_factory=list)


def registrable(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_of(url: str) -> str:
    return registrable(urlparse(url if "://" in url else f"https://{url}").netloc)


def guess_official_domains(name: str, website: str, urls: list[str]) -> set[str]:
    """The app's own domain(s). Website wins; otherwise domains whose name contains the app token."""
    doms: set[str] = set()
    if website:
        doms.add(domain_of(website))
    token = re.sub(r"[^a-z0-9]", "", name.lower())
    for u in urls:
        d = domain_of(u)
        base = re.sub(r"[^a-z0-9]", "", d.split(".")[0])
        if token and len(token) >= 3 and (token in base or base in token) and len(base) >= 3:
            doms.add(d)
    # Well-known doc hosts that belong to a provider family.
    aliases = {"google": {"google.com", "googleapis.com", "cloud.google.com"},
               "linkedin": {"microsoft.com"}, "slack": {"slack.dev", "slack.com"},
               "github": {"github.com", "github.blog"}, "atlassian": {"atlassian.com"},
               "microsoft": {"microsoft.com"}, "x": {"x.com", "twitter.com"}}
    for k, v in aliases.items():
        if k == token or token.startswith(k):
            doms |= v
    return doms


async def search(query: str, n: int = 6) -> list[dict]:
    """Tavily if a key is configured (reliable from servers), else DuckDuckGo (free, no key)."""
    key = os.getenv("TAVILY_API_KEY")
    if key:
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post("https://api.tavily.com/search",
                                 json={"api_key": key, "query": query, "max_results": n})
                r.raise_for_status()
                return [{"title": x.get("title", ""), "url": x["url"], "snippet": x.get("content", "")}
                        for x in r.json().get("results", [])]
        except Exception:
            pass  # fall through to DDG
    def _ddg():
        from ddgs import DDGS
        return [{"title": x.get("title", ""), "url": x.get("href", ""), "snippet": x.get("body", "")}
                for x in DDGS().text(query, max_results=n)]
    try:
        return await asyncio.to_thread(_ddg)
    except Exception:
        return []


def clean_html(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    blocks = []
    for el in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "pre", "blockquote", "dt", "dd"]):
        t = " ".join(el.get_text(" ", strip=True).split())
        if len(t) >= 3:
            blocks.append(("## " + t) if el.name in ("h1", "h2", "h3", "h4") else t)
    text = "\n".join(dict.fromkeys(blocks))  # dedupe, keep order
    if len(text) < 400:  # fallback for div-soup pages
        text = " ".join(main.get_text(" ", strip=True).split())
    return title, text


async def fetch(url: str, client: httpx.AsyncClient) -> Optional[Page]:
    try:
        r = await client.get(url, headers={"User-Agent": UA, "Accept": "text/html,text/markdown,*/*"})
        if r.status_code >= 400:
            return None
        ctype = r.headers.get("content-type", "")
        body = r.text[:1_500_000]
        if "html" in ctype:
            title, text = clean_html(body)
        elif "text" in ctype or "markdown" in ctype or "json" in ctype:
            title, text = url, body
        else:
            return None
        if len(text) < 200:
            return None
        return Page(url=str(r.url), title=title or str(r.url), text=text)
    except Exception:
        return None


def pick_snippets(text: str, budget_chars: int = 6000) -> list[str]:
    """Score paragraphs by OAuth/approval keywords, keep the best within budget, original order."""
    paras = [p.strip() for p in text.split("\n") if len(p.strip()) > 30]
    scored = []
    for i, p in enumerate(paras):
        low = p.lower()
        s = sum(w * low.count(k) for w, ks in KEYWORDS.items() for k in ks)
        if re.search(r"\b\d+\s*(business\s+)?(days?|weeks?|months?|hours?)\b", low):
            s += 4
        if re.search(r"\b\d+\s*(users|installs|installations|workspaces|requests)\b", low):
            s += 3
        if p.startswith("## "):
            s = s * 0.5
        scored.append((s, i, p))
    keep, used = [], 0
    for s, i, p in sorted(scored, key=lambda x: -x[0]):
        if s <= 1:
            break
        p = p[:900]
        if used + len(p) > budget_chars:
            continue
        keep.append((i, p))
        used += len(p)
    return [p for _, p in sorted(keep)]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quote_on_page(quote: str, page_text: str) -> bool:
    """Was the model's 'verbatim' quote really on the page? (normalised substring, or 85% word overlap)."""
    q, t = norm(quote), norm(page_text)
    if len(q) < 12:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < 5:
        return False
    # sliding window: does some window of the page contain most of the quote's words in order-ish?
    tw = t.split()
    qs = set(words)
    n = len(words)
    for i in range(0, max(1, len(tw) - n + 1), max(1, n // 3)):
        window = set(tw[i : i + n + 4])
        if len(qs & window) / len(qs) >= 0.85:
            return True
    return False


def keep_url(url: str) -> bool:
    if not url.startswith("http"):
        return False
    host = urlparse(url).netloc.lower()
    return not any(s.split("/")[0] in host for s in SKIP_DOMAINS) and not url.lower().endswith(".pdf")
