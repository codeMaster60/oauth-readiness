"""Hand-checked provider facts that the live agent reuses.

The six curated guides cost real effort to verify, so the live agent shouldn't
re-guess the same provider rules from blog posts. When a live run is for a
product in one of these provider families (e.g. Google Calendar, Google Drive),
these facts are added to what the agent read, marked official and verified.
Each quote is a short line from the official page it links to.
"""

from __future__ import annotations

from .web import Page

KNOWN: dict[str, dict] = {
    "google.com": {
        "label": "Google (hand-checked)",
        "facts": [
            ("timeline", "Brand verification typically takes 2-3 business days.",
             "The brand verification process typically takes 2-3 business days after submission.",
             "https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification"),
            ("timeline", "Sensitive scope verification typically takes 3-5 business days.",
             "The sensitive scope verification process typically takes 3-5 business days to complete.",
             "https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification"),
            ("review", "Only apps using restricted scopes (such as reading Gmail or full Drive access) need the annual "
             "third-party CASA security assessment; sensitive scopes like Calendar do not.",
             "apps that request access to restricted scopes need to undergo an annual security assessment",
             "https://support.google.com/cloud/answer/13465431"),
            ("dev_limits", "In Testing mode the app is limited to 100 test users.",
             "Projects configured with a publishing status of Testing are limited to up to 100 test users",
             "https://support.google.com/cloud/answer/15549945"),
            ("dev_limits", "In Testing mode, test users' authorizations and refresh tokens expire after 7 days.",
             "Authorizations by a test user will expire seven days from the time of consent.",
             "https://support.google.com/cloud/answer/15549945"),
        ],
    },
}

TITLES = {
    "https://developers.google.com/identity/protocols/oauth2/production-readiness/brand-verification": "brand verification",
    "https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification": "sensitive scope verification",
    "https://support.google.com/cloud/answer/13465431": "security assessment (CASA)",
    "https://support.google.com/cloud/answer/15549945": "app audience, test users and caps",
}

FAMILY_ALIASES = {"googleapis.com": "google.com", "youtube.com": "google.com", "gmail.com": "google.com"}


def known_pages(official_domains: set[str], name: str) -> list[Page]:
    """Pages of hand-checked facts that apply to this provider (empty if none)."""
    keys = {FAMILY_ALIASES.get(d, d) for d in official_domains}
    if name.lower().startswith(("google", "gmail", "youtube")):
        keys.add("google.com")
    by_url: dict[str, Page] = {}
    for k in sorted(keys):
        entry = KNOWN.get(k)
        if not entry:
            continue
        for topic, claim, quote, url in entry["facts"]:
            p = by_url.get(url)
            if p is None:
                p = by_url[url] = Page(url=url, title=f'{entry["label"]}: {TITLES.get(url, url)}', text="", official=True)
                p.known = []  # type: ignore[attr-defined]
            p.text += quote + "\n"
            p.known.append((topic, claim, quote))  # type: ignore[attr-defined]
    return list(by_url.values())
