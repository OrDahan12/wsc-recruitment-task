"""
scoring.py — Transparent, documented candidate scoring against a job opening.

The scoring is intentionally rule-based and explainable (not a black box):
every number a recruiter sees can be traced back to a concrete signal.

Five sub-scores are computed per candidate, each normalized to 0..1:

  1. skill_match     — overlap of candidate skills with the job's required /
                       nice-to-have skills.
  2. domain_relevance— does this person actually work in the job's domain, or
                       are they conference "noise" (e.g. a finance/IT/sales
                       attendee at a sports-tech event)? This is the core
                       signal-to-noise filter.
  3. seniority_fit   — years of experience vs. the seniority the role expects.
  4. referral_strength— warm-intro potential. Ranked by *relationship quality*,
                       not just count: a WSC employee who actually WORKED WITH
                       the candidate (overlapping years at the same company)
                       outweighs a random mutual connection. A same-department
                       referrer also counts for more.
  5. stability       — retention signal: penalizes job-hoppers (frequent short
                       stints) and factors "movability" — someone who just
                       started a new role is less likely to move than someone
                       ~2-3 years in (the typical sweet spot).

These combine into a weighted match_score (0..100). domain_relevance also acts
as a *gate*: a candidate far outside the domain is labeled NOISE regardless of
an incidental keyword match, because that is exactly the pain we are solving.

Note on scope: connection DEGREE (1st/2nd/3rd), open-to-work status, GitHub /
speaking presence, and culture/soft-skill fit are all valuable signals but need
data sources not present in the provided CSVs — see DESIGN.md § "Additional
signals" for how each would plug in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loader import Candidate, Employee, Job, Tenure

# --- Tunable configuration ---------------------------------------------------

WEIGHTS = {
    "skill_match": 0.35,
    "domain_relevance": 0.25,
    "seniority_fit": 0.10,
    "referral_strength": 0.20,
    "stability": 0.10,
}

# A candidate below this domain relevance is treated as conference noise.
DOMAIN_NOISE_GATE = 0.25

# Expected years-of-experience band per seniority label.
SENIORITY_BANDS = {
    "junior": (0, 3),
    "mid": (2, 6),
    "mid-senior": (4, 9),
    "senior": (6, 14),
    "lead": (9, 20),
    "principal": (10, 25),
}

# Tier thresholds on the final 0..100 score.
TIER_STRONG = 70
TIER_POTENTIAL = 50
TIER_LOW = 30

# Relationship types for a warm intro, strongest first, with their base weight.
# "recommendation" = an EXTERNAL person wrote a public LinkedIn recommendation
# AND is connected to a WSC employee (a bridge): explicit positive sentiment +
# a warm path in — the strongest signal of all.
# "worked_together" = a WSC employee overlapped in time at the same org — they
# can speak first-hand, but it is a reference OPPORTUNITY, not a guaranteed
# endorsement. "same_org" = same employer/school but no time overlap.
RELATION_STRENGTH = {
    "recommendation": 1.0,
    "worked_together": 0.9,
    "mutual_same_dept": 0.75,
    "same_org": 0.55,
    "mutual": 0.45,
}
RELATION_LABEL = {
    "recommendation": "Recommended (bridge to WSC)",
    "worked_together": "Worked together",
    "mutual_same_dept": "Mutual (same team)",
    "same_org": "Shared employer/school",
    "mutual": "Mutual connection",
}

# Generic org-name words that must be ignored when deciding "same company":
# e.g. "Intel" should match "Intel Computer Vision Lab", but "Sports" alone
# should not link "Nielsen Sports" to "Walla Sports".
_ORG_STOPWORDS = {
    "technologies", "technology", "software", "sports", "sport", "lab", "labs",
    "research", "group", "inc", "ltd", "the", "of", "and", "unit", "data",
    "communications", "digital", "online", "systems", "solutions", "media",
    "studio", "freelance", "startup", "self", "employed", "assistant",
    "intelligence", "university", "college", "school",
}


@dataclass
class Referral:
    employee_name: str
    employee_title: str
    department: str
    same_department: bool
    relation: str = "mutual"          # key into RELATION_STRENGTH
    shared_org: str = ""              # the company/school, when relevant
    is_external: bool = False         # True for an external recommender (bridge)
    bridge_employee: str = ""         # WSC employee the external recommender knows
    note: str = ""                    # e.g. the recommendation text

    @property
    def relation_label(self) -> str:
        return RELATION_LABEL.get(self.relation, "Mutual connection")


@dataclass
class ScoredCandidate:
    candidate: Candidate
    match_score: float                     # 0..100
    tier: str                              # Strong / Potential / Low / Noise
    skill_match: float                     # 0..1
    domain_relevance: float                # 0..1
    seniority_fit: float                   # 0..1
    referral_strength: float               # 0..1
    stability: float = 0.5                 # 0..1
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    referrals: List[Referral] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    recommended_action: str = ""


# --- Helpers -----------------------------------------------------------------

def _lc(items: List[str]) -> List[str]:
    return [i.lower() for i in items]


def _tokenize(*texts: str) -> set:
    """Break free text / phrases into a set of lowercase word tokens."""
    tokens = set()
    for text in texts:
        if not text:
            continue
        for chunk in text.replace("/", " ").replace(";", " ").replace(",", " ").split():
            cleaned = chunk.strip().lower()
            if len(cleaned) > 1:
                tokens.add(cleaned)
    return tokens


def _skill_tokens(skills: List[str]) -> set:
    """A skill like 'Object Detection' yields both the phrase and its words."""
    tokens = set()
    for skill in skills:
        s = skill.strip().lower()
        if not s:
            continue
        tokens.add(s)
        for word in s.replace("/", " ").split():
            if len(word) > 1:
                tokens.add(word)
    return tokens


# --- Sub-scores --------------------------------------------------------------

def score_skills(cand: Candidate, job: Job):
    """
    Fraction of required skills matched (weighted 0.8) plus nice-to-have
    coverage (weighted 0.2). Matching is done on both exact skill phrases and
    on individual tokens so 'Computer Vision' matches 'Computer Vision Engineer'.
    """
    cand_tokens = _skill_tokens(cand.top_skills) | _tokenize(
        cand.best_title, cand.industry, " ".join(cand.past_titles), cand.notes
    )

    matched, missing = [], []
    for skill in job.required_skills:
        st = _skill_tokens([skill])
        if st & cand_tokens:
            matched.append(skill)
        else:
            missing.append(skill)

    nice_matched = [s for s in job.nice_to_have if _skill_tokens([s]) & cand_tokens]

    required_ratio = len(matched) / len(job.required_skills) if job.required_skills else 0.0
    nice_ratio = len(nice_matched) / len(job.nice_to_have) if job.nice_to_have else 0.0
    score = 0.8 * required_ratio + 0.2 * nice_ratio
    return min(score, 1.0), matched, missing


def score_domain_relevance(cand: Candidate, job: Job, skill_match: float) -> float:
    """
    Signal-to-noise: is the person genuinely in the job's domain?

    Combines three cheap, explainable signals:
      - skill overlap with the role (already computed),
      - key-domain keyword hits in title / industry / skills,
      - conference-domain alignment with the role's domain.

    A finance engineer at a sports-tech conference with none of the domain
    keywords lands near zero and is filtered as noise.
    """
    domain_tokens = _tokenize(" ".join(job.key_domains), job.title, job.department)
    cand_text_tokens = _tokenize(
        cand.best_title, cand.industry, cand.conference_domain,
        " ".join(cand.top_skills), " ".join(cand.past_titles), cand.notes,
    )
    hits = domain_tokens & cand_text_tokens
    keyword_signal = min(len(hits) / max(len(domain_tokens), 1) * 2.0, 1.0)

    # Conference-domain alignment: a weak prior only. It nudges, never carries —
    # otherwise everyone at the sports conference would look "relevant" and we'd
    # lose exactly the signal-to-noise separation we are trying to create.
    conf_align = 0.1 if domain_tokens & _tokenize(cand.conference_domain) else 0.0

    # Blend: actual skills are the strongest evidence of true domain fit.
    score = 0.6 * skill_match + 0.3 * keyword_signal + conf_align
    return min(score, 1.0)


def score_seniority(cand: Candidate, job: Job) -> float:
    if cand.years_experience is None:
        return 0.5  # unknown — neutral, don't over-reward or over-punish
    band = SENIORITY_BANDS.get(job.seniority.strip().lower())
    if band is None:
        return 0.5
    low, high = band
    yrs = cand.years_experience
    if low <= yrs <= high:
        return 1.0
    # Graceful decay outside the band (1 year off ~ -0.15).
    distance = (low - yrs) if yrs < low else (yrs - high)
    return max(0.0, 1.0 - 0.15 * distance)


def _org_tokens(name: str) -> set:
    """Significant tokens of an org name (generic words removed) for matching."""
    tokens = set()
    for word in name.replace("/", " ").replace(".", " ").split():
        w = word.strip().lower()
        if len(w) > 1 and w not in _ORG_STOPWORDS:
            tokens.add(w)
    return tokens


def _same_org(a: str, b: str) -> bool:
    """True if two org names share a significant token (Intel ~ Intel CV Lab)."""
    return bool(_org_tokens(a) & _org_tokens(b))


def _overlaps(a: Tenure, b: Tenure) -> bool:
    """Do two employment periods overlap in time?"""
    return a.start <= b.end and b.start <= a.end


def _shared_history(cand: Candidate, emp: Employee):
    """
    Find the strongest shared-workplace relation between a candidate and an
    employee. Returns (relation_key, org) or (None, "").

    - Same org AND overlapping years  -> "worked_together" (they were colleagues).
    - Same org, no time overlap        -> "same_org" (shared alumni/network).
    """
    best = (None, "")
    for cr in cand.past_roles:
        for er in emp.work_history:
            if _same_org(cr.org, er.org):
                if _overlaps(cr, er):
                    return "worked_together", cr.org  # strongest, stop early
                best = ("same_org", cr.org)
    return best


def score_referral(cand: Candidate, job: Job, employees: Dict[str, Employee]):
    """
    Warm-intro potential, ranked by *relationship quality* rather than raw count.

    WSC especially values a candidate who arrives with a reference from someone
    who actually worked with them, so relationship type drives the score:

        worked_together  > mutual (same team) > shared employer/school > mutual

    We scan two independent sources and keep the strongest relation per employee:
      1. explicit LinkedIn mutual connections (wsc_mutual_connections), and
      2. shared work history (candidate past roles vs. employee work history),
         including whether the two overlapped in time (= true colleagues).

    Multiple connections still help (1 vs 3 differ) but never outweigh a single
    high-quality relationship: strongest relation leads, extras add a small bonus.
    """
    by_emp: Dict[str, Referral] = {}

    def _consider(emp: Employee, relation: str, org: str = ""):
        same = emp.department.strip().lower() == job.department.strip().lower()
        # A same-department mutual connection is upgraded to "mutual_same_dept".
        if relation == "mutual" and same:
            relation = "mutual_same_dept"
        existing = by_emp.get(emp.employee_id)
        # Keep only the strongest relation we found for this employee.
        if existing and RELATION_STRENGTH[existing.relation] >= RELATION_STRENGTH[relation]:
            return
        by_emp[emp.employee_id] = Referral(
            employee_name=emp.full_name, employee_title=emp.title,
            department=emp.department, same_department=same,
            relation=relation, shared_org=org,
        )

    # Source 1: explicit mutual connections.
    for emp_id in cand.mutual_connection_ids:
        emp = employees.get(emp_id)
        if emp:
            _consider(emp, "mutual")

    # Source 2: shared work history across the whole employee roster.
    for emp in employees.values():
        relation, org = _shared_history(cand, emp)
        if relation:
            _consider(emp, relation, org)

    referrals = list(by_emp.values())

    # Source 3: external LinkedIn recommendations that bridge to a WSC employee.
    # These are separate people (not employees), so they are appended directly.
    for rec in cand.recommendations:
        if rec.get("is_wsc_employee"):
            continue  # an employee's own recommendation is handled via mutuals
        bridge_id = rec.get("wsc_bridge_employee_id", "")
        bridge_emp = employees.get(bridge_id)
        bridge_name = bridge_emp.full_name if bridge_emp else bridge_id
        referrals.append(Referral(
            employee_name=rec.get("recommender_name", ""),
            employee_title=f"{rec.get('recommender_title', '')} @ {rec.get('recommender_company', '')}".strip(" @"),
            department="External",
            same_department=False,
            relation="recommendation",
            is_external=True,
            bridge_employee=bridge_name,
            note=rec.get("text", ""),
        ))

    if not referrals:
        return 0.0, referrals

    strengths = sorted((RELATION_STRENGTH[r.relation] for r in referrals), reverse=True)
    # Strongest relationship leads; each additional connection adds a little.
    score = min(strengths[0] + 0.08 * (len(strengths) - 1), 1.0)

    # Order for display: strongest relation first, then same-department.
    referrals.sort(key=lambda r: (-RELATION_STRENGTH[r.relation], not r.same_department, r.employee_name))
    return score, referrals


def score_stability(cand: Candidate):
    """
    Retention signal, combining two ideas a seasoned recruiter weighs:

      - Tenure stability: someone who changes jobs every 6-12 months is a
        higher retention risk than someone who stays ~2-3 years.
      - Movability: someone who *just* started a role is less likely to move;
        the ~2-4 year mark is the typical sweet spot to be open to a change.

    Returns (score 0..1, list_of_flags). Missing data -> neutral, never a crash.
    """
    flags: List[str] = []

    avg = cand.avg_past_tenure_years
    if avg is None:
        tenure_stability = 0.6  # unknown - mildly neutral
    elif avg >= 2.5:
        tenure_stability = 1.0
    elif avg >= 1.5:
        tenure_stability = 0.7
    else:
        tenure_stability = 0.35
        flags.append("JOB_HOPPER")

    cur = cand.current_tenure_years
    if cur is None:
        movability = 0.6
    elif cur < 1:
        movability = 0.45
        flags.append("RECENTLY_STARTED")
    elif cur <= 4:
        movability = 1.0
        flags.append("MOVABLE_SWEET_SPOT")
    elif cur <= 6:
        movability = 0.75
    else:
        movability = 0.6  # very tenured - comfortable, harder to move

    score = 0.5 * tenure_stability + 0.5 * movability
    return round(min(score, 1.0), 3), flags


# --- Orchestration -----------------------------------------------------------

def _tier(score: float, domain_relevance: float) -> str:
    if domain_relevance < DOMAIN_NOISE_GATE:
        return "Noise (off-domain)"
    if score >= TIER_STRONG:
        return "Strong Match"
    if score >= TIER_POTENTIAL:
        return "Potential"
    if score >= TIER_LOW:
        return "Low"
    return "Weak"


def _recommend(sc: "ScoredCandidate") -> str:
    c = sc.candidate
    if sc.tier == "Noise (off-domain)":
        return "Skip — attended the conference but works outside this role's domain."
    caution = " (note: frequent job changes — probe retention)" if "JOB_HOPPER" in sc.flags else ""
    if c.is_internal:
        yr = f" ({c.years_in_role} yrs in current role)" if c.years_in_role else ""
        lead = "Prioritise — internal mover" if sc.tier in ("Strong Match", "Potential") else "Consider — internal mover"
        return (f"{lead}{yr}: fastest, lowest-risk hire and a retention win. "
                f"Loop in their manager and HR before external outreach.")
    top_ref = sc.referrals[0] if sc.referrals else None
    worked = top_ref and top_ref.relation == "worked_together"
    strong_ref = top_ref and (top_ref.same_department or worked or len(sc.referrals) >= 2)

    if sc.tier == "Strong Match":
        if worked:
            return f"Reach out now — strong fit; {top_ref.employee_name} worked with them at {top_ref.shared_org} — a first-hand reference to check (verify it was a positive working relationship).{caution}"
        if strong_ref:
            return f"Reach out now — strong fit with a warm intro via {top_ref.employee_name}.{caution}"
        return f"Reach out now — strong fit; consider a cold but personalized outreach.{caution}"
    if sc.tier == "Potential":
        if top_ref:
            return f"Worth a conversation — partial fit; leverage intro via {top_ref.employee_name}.{caution}"
        return f"Worth a conversation — partial fit; verify the gaps before investing.{caution}"
    return "Keep in pool — weak fit for this role, may suit a future opening."


def _flags(sc: "ScoredCandidate") -> List[str]:
    flags = list(sc.flags)  # stability + ATS flags already collected
    if sc.candidate.is_internal:
        flags.append("INTERNAL_MOBILITY")
    if not sc.candidate.has_linkedin:
        flags.append("MISSING_LINKEDIN")
    if any(r.relation == "recommendation" for r in sc.referrals):
        flags.append("HAS_RECOMMENDATION")
    if not sc.referrals and not sc.candidate.is_internal:
        flags.append("NO_MUTUAL_CONNECTION")
    else:
        if any(r.relation == "worked_together" for r in sc.referrals):
            flags.append("WORKED_WITH_EMPLOYEE")
        if any(r.same_department for r in sc.referrals):
            flags.append("STRONG_REFERRAL")
    if sc.missing_skills and sc.matched_skills:
        flags.append("PARTIAL_SKILLS")
    if sc.tier == "Noise (off-domain)":
        flags.append("OFF_DOMAIN")
    return flags


def _ats_flags(cand: Candidate, job: Job) -> List[str]:
    """Flag candidates already in the ATS for this role (esp. prior rejections)."""
    flags = []
    for app in cand.ats_applications:
        if app.get("job_id") != job.job_id:
            continue
        status = app.get("ats_status", "").lower()
        if status == "rejected":
            flags.append("PREVIOUSLY_REJECTED")
        else:
            flags.append("ALREADY_APPLIED")
    return flags


def score_candidate(cand: Candidate, job: Job, employees: Dict[str, Employee]) -> ScoredCandidate:
    skill_match, matched, missing = score_skills(cand, job)
    domain_relevance = score_domain_relevance(cand, job, skill_match)
    seniority_fit = score_seniority(cand, job)
    referral_strength, referrals = score_referral(cand, job, employees)
    stability, stability_flags = score_stability(cand)

    raw = (
        WEIGHTS["skill_match"] * skill_match
        + WEIGHTS["domain_relevance"] * domain_relevance
        + WEIGHTS["seniority_fit"] * seniority_fit
        + WEIGHTS["referral_strength"] * referral_strength
        + WEIGHTS["stability"] * stability
    )
    match_score = round(raw * 100, 1)

    sc = ScoredCandidate(
        candidate=cand,
        match_score=match_score,
        tier=_tier(match_score, domain_relevance),
        skill_match=round(skill_match, 3),
        domain_relevance=round(domain_relevance, 3),
        seniority_fit=round(seniority_fit, 3),
        referral_strength=round(referral_strength, 3),
        stability=stability,
        matched_skills=matched,
        missing_skills=missing,
        referrals=referrals,
        flags=stability_flags + _ats_flags(cand, job),
    )
    sc.flags = _flags(sc)
    sc.recommended_action = _recommend(sc)
    return sc


def score_all(candidates: List[Candidate], job: Job, employees: Dict[str, Employee]) -> List[ScoredCandidate]:
    scored = [score_candidate(c, job, employees) for c in candidates]
    # Rank: highest match first; break ties by referral then skills.
    scored.sort(
        key=lambda s: (s.match_score, s.referral_strength, s.skill_match),
        reverse=True,
    )
    return scored
