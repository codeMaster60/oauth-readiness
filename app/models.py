"""Guide schema + the deterministic difficulty rubric.

Every guide (hand-curated or agent-generated) uses the same schema, so the UI,
the Markdown export and the JSON API never special-case where a guide came from.

Difficulty is NOT chosen by the LLM. The agent extracts concrete facts
("review required", "security assessment", "max wait 60 days") into `Flags`,
and `score()` turns those into a 1-5 score with a fixed, documented rubric.
That keeps scores comparable across apps and explainable to the reader.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# How do we know a claim is true?
#   tested      - a human or agent actually did it in the provider's console
#   probed      - the agent hit a live public endpoint and observed this
#   documented  - stated in the provider's official docs (quote verified on page)
#   third_party - stated by a non-official source (blog, vendor docs, forum)
#   uncertain   - inferred, conflicting, or the quote could not be found on the page
Evidence = Literal["tested", "probed", "documented", "third_party", "uncertain"]
Who = Literal["you", "agent", "either"]


class Source(BaseModel):
    id: str
    title: str
    url: str
    kind: Literal["official", "third_party", "probe", "composio", "test"] = "official"
    accessed: Optional[str] = None


class Backed(BaseModel):
    evidence: Evidence = "uncertain"
    sources: list[str] = Field(default_factory=list)


class Note(Backed):
    text: str


class Step(Backed):
    title: str
    detail: str = ""
    who: Who = "you"
    minutes: Optional[int] = None


class Stage(BaseModel):
    id: str
    name: str
    summary: str = ""
    limits: list[Note] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)


class Requirement(Backed):
    item: str
    when: Literal["always", "production", "listing"] = "always"
    detail: str = ""


class Gotcha(Backed):
    title: str
    detail: str = ""
    severity: Literal["blocker", "high", "medium", "low"] = "medium"


class Blocker(Backed):
    symptom: str
    cause: str = ""
    fix: str = ""


class PathOption(Backed):
    """Same provider, different use case -> different approval path."""

    name: str
    when: str
    difficulty: int = Field(ge=1, le=5)
    wait: str


class Flags(BaseModel):
    """Rubric inputs. Facts, not opinions."""

    review_for_production: bool = False
    review_wait_days_max: Optional[int] = None
    security_assessment: bool = False
    business_verification: bool = False
    partner_gated: bool = False
    paid_requirement: bool = False
    install_cap_before_review: Optional[int] = None
    dev_mode_user_cap: Optional[int] = None


class Verdict(BaseModel):
    difficulty: int = Field(ge=1, le=5)
    label: str
    summary: str
    active_effort: str
    wait: str
    gating_step: str


class ComposioInfo(BaseModel):
    managed: Literal["yes", "no", "unknown"] = "unknown"
    managed_note: str = ""
    when_own_app: list[str] = Field(default_factory=list)
    redirect_uri: str = "https://backend.composio.dev/api/v3/toolkits/auth/callback"


class AgentSplit(BaseModel):
    agent: list[str] = Field(default_factory=list)
    you: list[str] = Field(default_factory=list)


class Probe(BaseModel):
    """What the agent observed by hitting public endpoints (no account needed)."""

    url: str
    method: str = "GET"
    status: Optional[int] = None
    observed: str
    ok: bool = True


class Stats(BaseModel):
    sources_read: int = 0
    facts_extracted: int = 0
    quotes_verified: int = 0
    probes_run: int = 0
    seconds: Optional[float] = None


class Guide(BaseModel):
    slug: str
    name: str
    website: str = ""
    category: str = ""
    use_case: str = ""
    origin: Literal["curated", "generated"] = "generated"
    updated: str = ""
    model: str = ""
    verdict: Verdict
    flags: Flags = Field(default_factory=Flags)
    concept: str = ""
    paths: list[PathOption] = Field(default_factory=list)
    composio: ComposioInfo = Field(default_factory=ComposioInfo)
    stages: list[Stage] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    gotchas: list[Gotcha] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    agent_split: AgentSplit = Field(default_factory=AgentSplit)
    uncertainties: list[str] = Field(default_factory=list)
    probes: list[Probe] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    stats: Stats = Field(default_factory=Stats)

    def source(self, sid: str) -> Optional[Source]:
        return next((s for s in self.sources if s.id == sid), None)

    def evidence_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in ("tested", "probed", "documented", "third_party", "uncertain")}
        for item in self.iter_claims():
            counts[item.evidence] += 1
        return counts

    def iter_claims(self):
        for st in self.stages:
            yield from st.limits
            yield from st.steps
        yield from self.requirements
        yield from self.gotchas
        yield from self.blockers
        yield from self.paths


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------

WORDS = {1: "Self-serve", 2: "Easy", 3: "Review needed", 4: "Hard", 5: "Gated"}
# Kept for the evidence page: what each number means in words.
LABELS = {1: "Self-serve", 2: "Easy", 3: "Review needed", 4: "Hard", 5: "Gated"}


def label(f: "Flags", d: int) -> str:
    """Difficulty word + how long you actually wait, so the headline can't contradict the wait line."""
    w = f.review_wait_days_max or 0
    if f.partner_gated:
        when = "approval not guaranteed"
    elif f.review_for_production and w >= 14:
        when = "weeks"
    elif f.review_for_production:
        when = "days"
    else:
        when = "minutes" if d == 1 else "same day"
    return f"{WORDS[d]} · {when}"


def score(f: Flags) -> tuple[int, list[str]]:
    """Return (difficulty 1-5, human-readable reasons). Pure function, unit tested."""
    pts = 1.0
    why: list[str] = ["Base: creating the app itself (1)"]
    if f.review_for_production:
        pts += 1
        why.append("Provider review before real users / scale (+1)")
    w = f.review_wait_days_max
    if w is not None and w >= 10:
        pts += 1
        why.append(f"Review can take {w}+ days (+1)")
        if w >= 30:
            pts += 0.5
            why.append("…a month or more (+0.5)")
    if f.security_assessment:
        pts += 1
        why.append("Third-party security assessment (+1)")
    if f.business_verification:
        pts += 0.5
        why.append("Business / domain / page verification (+0.5)")
    if f.partner_gated:
        pts += 2
        why.append("Access is partner-gated or discretionary (+2)")
    if f.paid_requirement:
        pts += 1
        why.append("Paid plan or fee required (+1)")
    if f.install_cap_before_review:
        pts += 0.5
        why.append(f"Capped at {f.install_cap_before_review} installs until reviewed (+0.5)")
    d = max(1, min(5, int(pts + 0.5)))
    return d, why
