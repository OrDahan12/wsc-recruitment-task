"""
loader.py — Load and join the raw CSV exports into enriched candidate records.

In production these CSVs are replaced by live integrations (see DESIGN.md):
  conference_attendees.csv -> HubSpot contacts API (source: conference lists / badge scans)
  linkedin_profiles.csv    -> LinkedIn enrichment API (or a data provider e.g. Proxycurl)
  wsc_employees.csv        -> HR system / LinkedIn company connections
  job_openings.csv         -> Comeet ATS open requisitions

This module is deliberately dependency-free (Python stdlib only) so the pipeline
runs anywhere with `python` and no `pip install` step.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Reference "current year" used to estimate current-role tenure when a role is
# open-ended. The provided conferences run Nov 2024 - Apr 2025, so 2025 is a
# sensible "now" for this dataset. In production this is simply date.today().
REFERENCE_YEAR = 2025

# A tenure entry looks like "Company Name (2016-2020)" or
# "Title at Company (2019-present)". This captures the org label and the years.
_TENURE_RE = re.compile(r"^(?P<label>.*?)\s*\((?P<start>\d{4})\s*[-\u2013]\s*(?P<end>\d{4}|present|now|current)\)\s*$", re.IGNORECASE)


@dataclass
class Tenure:
    """One position in a work history: which org, and the years there."""
    org: str
    start: int
    end: int  # REFERENCE_YEAR when the role is still current (present/now)

    @property
    def years(self) -> int:
        return max(0, self.end - self.start)


def _split_list(value: str, sep: str = ";") -> List[str]:
    """Split a delimited cell into a clean list, dropping blanks/whitespace."""
    if not value:
        return []
    return [item.strip() for item in value.split(sep) if item.strip()]


def _norm(value: Optional[str]) -> str:
    return (value or "").strip()


def _parse_tenures(value: str, strip_title: bool = False) -> List[Tenure]:
    """
    Parse a ";"-delimited work-history string into Tenure records.

    Handles both employee format  "Akamai Technologies (2016-2020)"
    and candidate format          "ML Engineer at Opta Sports (2019-2022)".
    Entries that don't match the expected shape are skipped gracefully.
    """
    tenures: List[Tenure] = []
    for entry in _split_list(value):
        m = _TENURE_RE.match(entry)
        if not m:
            continue
        label = m.group("label").strip()
        if strip_title and " at " in label:
            label = label.split(" at ", 1)[1].strip()
        end_raw = m.group("end").lower()
        end = REFERENCE_YEAR if end_raw in ("present", "now", "current") else int(end_raw)
        tenures.append(Tenure(org=label, start=int(m.group("start")), end=end))
    return tenures


@dataclass
class Job:
    job_id: str
    title: str
    department: str
    seniority: str
    key_domains: List[str]
    required_skills: List[str]
    nice_to_have: List[str]


@dataclass
class Employee:
    employee_id: str
    full_name: str
    title: str
    department: str
    linkedin_id: str
    work_history: List[Tenure] = field(default_factory=list)


@dataclass
class Candidate:
    """A conference attendee joined with their LinkedIn profile (when available)."""

    # From conference_attendees.csv (always present — this is the lead source of truth)
    hubspot_id: str
    full_name: str
    email: str
    company: str
    conference_title: str          # title as captured at the conference
    conference_name: str
    conference_domain: str
    conference_date: str
    source: str
    notes: str
    linkedin_url: str

    # From linkedin_profiles.csv (may be missing — see has_linkedin)
    has_linkedin: bool = False
    current_company: str = ""
    current_title: str = ""
    location: str = ""
    years_experience: Optional[int] = None
    top_skills: List[str] = field(default_factory=list)
    industry: str = ""
    past_companies: List[str] = field(default_factory=list)
    past_titles: List[str] = field(default_factory=list)
    past_roles: List[Tenure] = field(default_factory=list)
    mutual_connection_ids: List[str] = field(default_factory=list)

    # From linkedin_recommendations.csv (bridge endorsements) and
    # internal_applications.csv (ATS) - both optional, joined post-load.
    recommendations: List[dict] = field(default_factory=list)
    ats_applications: List[dict] = field(default_factory=list)

    # From salary_expectations.csv (optional). A business constraint weighed
    # alongside merit - never folded into the match score.
    expected_salary: Optional[int] = None
    salary_currency: str = ""

    # Internal-mobility candidates (existing employees who opted to move to an
    # open role). Sourced from internal_candidates.csv, not a conference.
    is_internal: bool = False
    target_job_id: str = ""
    years_in_role: Optional[int] = None

    @property
    def best_title(self) -> str:
        """Prefer the enriched LinkedIn title; fall back to the conference title."""
        return self.current_title or self.conference_title

    @property
    def current_tenure_years(self) -> Optional[int]:
        """
        Estimated years in the *current* role. Inferred as the gap between the
        latest past role's end year and now (the candidate joined their current
        job roughly when the previous one ended). None when we can't tell.
        """
        if not self.past_roles:
            return None
        latest_end = max(t.end for t in self.past_roles)
        return max(0, REFERENCE_YEAR - latest_end)

    @property
    def avg_past_tenure_years(self) -> Optional[float]:
        """Average length of previous roles — the job-hopping signal."""
        if not self.past_roles:
            return None
        spans = [t.years for t in self.past_roles if t.years > 0]
        return (sum(spans) / len(spans)) if spans else None


def load_jobs(data_dir: str) -> Dict[str, Job]:
    jobs: Dict[str, Job] = {}
    with open(os.path.join(data_dir, "job_openings.csv"), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            job = Job(
                job_id=_norm(row.get("job_id")),
                title=_norm(row.get("title")),
                department=_norm(row.get("department")),
                seniority=_norm(row.get("seniority")),
                key_domains=_split_list(row.get("key_domains", "")),
                required_skills=_split_list(row.get("required_skills", "")),
                nice_to_have=_split_list(row.get("nice_to_have", "")),
            )
            jobs[job.job_id] = job
    return jobs


def load_employees(data_dir: str) -> Dict[str, Employee]:
    """Return employees keyed by employee_id (e.g. WSC002) for referral lookups."""
    employees: Dict[str, Employee] = {}
    with open(os.path.join(data_dir, "wsc_employees.csv"), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            emp = Employee(
                employee_id=_norm(row.get("employee_id")),
                full_name=_norm(row.get("full_name")),
                title=_norm(row.get("title")),
                department=_norm(row.get("department")),
                linkedin_id=_norm(row.get("linkedin_id")),
                work_history=_parse_tenures(row.get("work_history", "")),
            )
            employees[emp.employee_id] = emp
    return employees


def _load_linkedin(data_dir: str) -> Dict[str, dict]:
    """Index LinkedIn profiles by normalized linkedin_url for joining."""
    profiles: Dict[str, dict] = {}
    with open(os.path.join(data_dir, "linkedin_profiles.csv"), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            url = _norm(row.get("linkedin_url")).lower()
            if url:
                profiles[url] = row
    return profiles


def _parse_years(value: str) -> Optional[int]:
    value = _norm(value)
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def load_candidates(data_dir: str) -> List[Candidate]:
    """
    Load conference attendees and left-join them with LinkedIn profiles.

    Edge case: an attendee with no linkedin_url, or a URL with no matching
    profile row, is kept with has_linkedin=False rather than being dropped.
    Downstream scoring degrades gracefully instead of crashing.
    """
    profiles = _load_linkedin(data_dir)
    candidates: List[Candidate] = []

    with open(os.path.join(data_dir, "conference_attendees.csv"), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            url = _norm(row.get("linkedin_url"))
            profile = profiles.get(url.lower()) if url else None

            cand = Candidate(
                hubspot_id=_norm(row.get("hubspot_id")),
                full_name=_norm(row.get("full_name")),
                email=_norm(row.get("email")),
                company=_norm(row.get("company")),
                conference_title=_norm(row.get("title")),
                conference_name=_norm(row.get("conference_name")),
                conference_domain=_norm(row.get("conference_domain")),
                conference_date=_norm(row.get("conference_date")),
                source=_norm(row.get("source")),
                notes=_norm(row.get("notes")),
                linkedin_url=url,
            )

            if profile is not None:
                cand.has_linkedin = True
                cand.current_company = _norm(profile.get("current_company"))
                cand.current_title = _norm(profile.get("current_title"))
                cand.location = _norm(profile.get("location"))
                cand.years_experience = _parse_years(profile.get("years_experience", ""))
                cand.top_skills = _split_list(profile.get("top_skills", ""))
                cand.industry = _norm(profile.get("industry"))
                cand.past_companies = _split_list(profile.get("past_companies", ""))
                cand.past_titles = _split_list(profile.get("past_titles", ""))
                cand.past_roles = _parse_tenures(profile.get("past_titles", ""), strip_title=True)
                cand.mutual_connection_ids = _split_list(profile.get("wsc_mutual_connections", ""))

            candidates.append(cand)

    _join_recommendations(candidates, data_dir)
    _join_applications(candidates, data_dir)
    _join_salary(candidates, data_dir)
    return candidates


def _join_recommendations(candidates: List[Candidate], data_dir: str) -> None:
    """Attach LinkedIn bridge recommendations (optional file, joined by URL)."""
    path = os.path.join(data_dir, "linkedin_recommendations.csv")
    if not os.path.exists(path):
        return
    by_url: Dict[str, List[dict]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            url = _norm(row.get("candidate_linkedin_url")).lower()
            by_url.setdefault(url, []).append({
                "recommender_name": _norm(row.get("recommender_name")),
                "recommender_title": _norm(row.get("recommender_title")),
                "recommender_company": _norm(row.get("recommender_company")),
                "is_wsc_employee": _norm(row.get("is_wsc_employee")).lower() == "yes",
                "wsc_bridge_employee_id": _norm(row.get("wsc_bridge_employee_id")),
                "sentiment": _norm(row.get("sentiment")),
                "text": _norm(row.get("text")),
            })
    for cand in candidates:
        cand.recommendations = by_url.get(cand.linkedin_url.lower(), [])


def _join_applications(candidates: List[Candidate], data_dir: str) -> None:
    """Attach internal ATS applications (optional file, joined by hubspot_id)."""
    path = os.path.join(data_dir, "internal_applications.csv")
    if not os.path.exists(path):
        return
    by_id: Dict[str, List[dict]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            hid = _norm(row.get("hubspot_id"))
            if not hid:
                continue
            by_id.setdefault(hid, []).append({
                "job_id": _norm(row.get("job_id")),
                "ats_status": _norm(row.get("ats_status")),
                "applied_date": _norm(row.get("applied_date")),
            })
    for cand in candidates:
        cand.ats_applications = by_id.get(cand.hubspot_id, [])


def load_internal_applications(data_dir: str) -> List[dict]:
    """All ATS applications as raw rows (for the Open Jobs tab counts)."""
    path = os.path.join(data_dir, "internal_applications.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{k: _norm(v) for k, v in row.items()} for row in csv.DictReader(fh)]


def _join_salary(candidates: List[Candidate], data_dir: str) -> None:
    """Attach salary expectations (optional file, joined by hubspot_id)."""
    path = os.path.join(data_dir, "salary_expectations.csv")
    if not os.path.exists(path):
        return
    by_id: Dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            hid = _norm(row.get("hubspot_id"))
            if hid:
                by_id[hid] = row
    for cand in candidates:
        row = by_id.get(cand.hubspot_id)
        if row:
            cand.expected_salary = _parse_years(row.get("expected_salary", ""))
            cand.salary_currency = _norm(row.get("currency"))


def load_job_budgets(data_dir: str) -> Dict[str, dict]:
    """Per-role budget ceilings keyed by job_id (optional file)."""
    path = os.path.join(data_dir, "job_budgets.csv")
    budgets: Dict[str, dict] = {}
    if not os.path.exists(path):
        return budgets
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            jid = _norm(row.get("job_id"))
            if jid:
                budgets[jid] = {
                    "budget_min": _parse_years(row.get("budget_min", "")),
                    "budget_max": _parse_years(row.get("budget_max", "")),
                    "currency": _norm(row.get("currency")),
                }
    return budgets


def load_internal_candidates(data_dir: str) -> List[Candidate]:
    """
    Load internal-mobility candidates (existing employees who opted to move) as
    Candidate records so they flow through the same scoring. They carry no
    conference/LinkedIn join; instead their skills and tenure come from the
    internal_candidates.csv file, and they are flagged is_internal.
    """
    path = os.path.join(data_dir, "internal_candidates.csv")
    if not os.path.exists(path):
        return []
    out: List[Candidate] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            cand = Candidate(
                hubspot_id=_norm(row.get("employee_id")),
                full_name=_norm(row.get("full_name")),
                email="",
                company="WSC Sports",
                conference_title=_norm(row.get("current_title")),
                conference_name="",
                conference_domain="",
                conference_date="",
                source="Internal Mobility",
                notes=_norm(row.get("note")),
                linkedin_url="",
            )
            cand.has_linkedin = True  # we have structured data on them
            cand.current_company = "WSC Sports"
            cand.current_title = _norm(row.get("current_title"))
            cand.industry = _norm(row.get("department"))
            cand.years_experience = _parse_years(row.get("years_experience", ""))
            cand.top_skills = _split_list(row.get("skills", ""))
            cand.is_internal = True
            cand.target_job_id = _norm(row.get("target_job_id"))
            cand.years_in_role = _parse_years(row.get("years_in_role", ""))
            cand.expected_salary = _parse_years(row.get("expected_salary", ""))
            cand.salary_currency = "USD"
            out.append(cand)
    return out

