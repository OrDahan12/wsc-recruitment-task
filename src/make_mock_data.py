"""
make_mock_data.py — Generate simulated supplementary data files.

The task ships four "end-state" CSVs. To demonstrate the recruiter dashboard we
need two more layers that, in production, come from real systems but aren't in
the provided export:

  internal_applications.csv  — who already applied to each role via the internal
                               ATS (Comeet). Lets HR compare active applicants
                               against passive talent-pool candidates, and lets
                               us flag "already applied / previously rejected".

  linkedin_recommendations.csv — public LinkedIn recommendations for a candidate,
                               authored by a REAL WSC employee (from
                               wsc_employees.csv) who is one of the candidate's
                               mutual connections. Explicit positive endorsement
                               + a warm path in — fully grounded, no invented
                               outside people.

  salary_expectations.csv    — expected salary, populated ONLY for candidates a
                               recruiter has actually screened (it is not known
                               for a passive conference lead). job_budgets.csv
                               holds the per-role ceiling for the budget check.

  internal_candidates.csv    — existing employees who opted to move to an open
                               role (internal mobility).

This generator is deterministic (fixed seed) so results are reproducible. It is
clearly separated from the real data and only run to (re)build the mock files.
Nothing here fabricates real personal data — all names are synthetic.

Name provenance (every person is real / from the provided dataset):
  * Conference candidates come straight from the provided conference_attendees.csv
    (we only add ATS status / recommendations / salary layers on top of them).
  * Internal-mobility candidates are real employees from the provided
    wsc_employees.csv.
  * Recommendation authors are real employees from wsc_employees.csv.
  No names are invented — the assignment dataset is the single source of truth
  for who exists.
"""

from __future__ import annotations

import csv
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")

RNG = random.Random(42)

JOBS = ["JOB001", "JOB002", "JOB003", "JOB004"]
ATS_STATUS = ["New", "In Review", "Phone Screen", "Rejected", "On Hold"]

# Annual budget ceiling per open role (USD). The recruiter compares candidate
# salary expectations against this. It is a *business constraint*, kept separate
# from the merit-based match score (a great candidate slightly over budget is a
# negotiation, not a disqualification).
JOB_BUDGETS = {
    "JOB001": {"budget_min": 150000, "budget_max": 190000, "currency": "USD"},
    "JOB002": {"budget_min": 120000, "budget_max": 155000, "currency": "USD"},
    "JOB003": {"budget_min": 140000, "budget_max": 180000, "currency": "USD"},
    "JOB004": {"budget_min": 115000, "budget_max": 150000, "currency": "USD"},
}

# Internal employees who are open to moving into a newly opened role. Modeled
# explicitly (in production this comes from an internal-mobility / HR system or
# an employee opting in). Internal movers are the cheapest, fastest, lowest-risk
# hire and a retention win, so they are surfaced as a distinct candidate source.
INTERNAL_CANDIDATES = [
    {"employee_id": "WSC013", "full_name": "Itai Nahum", "current_title": "ML Research Engineer",
     "department": "AI/ML", "years_in_role": 4, "years_experience": 9, "target_job_id": "JOB001",
     "skills": "Python;PyTorch;Computer Vision;Deep Learning;Object Detection;AWS",
     "expected_salary": 175000,
     "note": "Strong internal fit; wants to move from research into applied ML product work."},
    {"employee_id": "WSC004", "full_name": "Avi Goldberg", "current_title": "Data Engineer",
     "department": "Data", "years_in_role": 3, "years_experience": 7, "target_job_id": "JOB002",
     "skills": "Python;AWS;Kafka;SQL;Airflow;Microservices",
     "expected_salary": 140000,
     "note": "Data engineer looking to move into backend; solid platform overlap."},
    {"employee_id": "WSC008", "full_name": "Liron Katz", "current_title": "Sports Data Analyst",
     "department": "Data", "years_in_role": 3, "years_experience": 8, "target_job_id": "JOB004",
     "skills": "Python;SQL;Spark;Sports Analytics;dbt;Airflow",
     "expected_salary": 130000,
     "note": "Sports-domain analyst upskilling into data engineering; deep domain knowledge."},
    {"employee_id": "WSC010", "full_name": "Hila Peled", "current_title": "UX Designer",
     "department": "Product", "years_in_role": 3, "years_experience": 6, "target_job_id": "JOB003",
     "skills": "Product Management;Stakeholder Management;UX;Sports Analytics",
     "expected_salary": 150000,
     "note": "Designer pivoting to product; strong stakeholder and user-research background."},
    {"employee_id": "WSC006", "full_name": "Tal Mizrahi", "current_title": "Backend Engineer",
     "department": "Engineering", "years_in_role": 5, "years_experience": 9, "target_job_id": "JOB004",
     "skills": "Python;AWS;Kafka;SQL;Microservices;REST APIs",
     "expected_salary": 145000,
     "note": "Tenured backend engineer seeking a data-platform move; strong retention win."},
]

# Sentiment lines reused across the generated recommendations.
SENTIMENTS = [
    "Outstanding engineer - shipped our real-time tracking pipeline end to end.",
    "One of the strongest hires I've made; deep expertise and a great teammate.",
    "Exceptional problem-solver, calm under pressure, mentors others generously.",
    "Delivered complex video AI work reliably; I'd work with them again anytime.",
    "Rare mix of technical depth and product sense; highly recommended.",
    "Took ownership from day one and raised the bar for the whole team.",
]


def load_attendees():
    with open(os.path.join(DATA, "conference_attendees.csv"), encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_profiles():
    with open(os.path.join(DATA, "linkedin_profiles.csv"), encoding="utf-8-sig") as fh:
        return {r["linkedin_url"].strip().lower(): r for r in csv.DictReader(fh)}


def load_employees():
    with open(os.path.join(DATA, "wsc_employees.csv"), encoding="utf-8-sig") as fh:
        return {r["employee_id"]: r for r in csv.DictReader(fh)}


def make_internal_applications(attendees):
    """
    Mark a realistic subset of attendees as having already applied via the ATS.
    ~30% of the pool applied to at least one job. This intentionally overlaps
    with the talent pool so HR can see "this passive candidate also applied /
    was previously rejected".
    """
    rows = []
    applicant_id = 1000
    for att in attendees:
        if RNG.random() > 0.30:
            continue
        job = RNG.choice(JOBS)
        status = RNG.choices(ATS_STATUS, weights=[30, 20, 15, 25, 10])[0]
        year = RNG.choice(["2024", "2025"])
        month = RNG.randint(1, 12)
        rows.append({
            "application_id": f"APP{applicant_id}",
            "job_id": job,
            "hubspot_id": att["hubspot_id"],
            "candidate_name": att["full_name"],
            "source": "Internal ATS (Comeet)",
            "applied_date": f"{year}-{month:02d}-{RNG.randint(1, 28):02d}",
            "ats_status": status,
        })
        applicant_id += 1

    # NOTE: every applicant above is a real person from the provided
    # conference_attendees.csv (joined by hubspot_id). We deliberately do NOT
    # invent any ATS-only applicants — the assignment dataset is the single
    # source of truth for who exists.
    path = os.path.join(DATA, "internal_applications.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "application_id", "job_id", "hubspot_id", "candidate_name",
            "source", "applied_date", "ats_status",
        ])
        w.writeheader()
        w.writerows(rows)
    return len(rows), path, rows


def make_recommendations(attendees, profiles, employees):
    """
    Create a public LinkedIn recommendation for a subset of candidates, authored
    by a REAL WSC employee who is one of the candidate's mutual connections
    (from wsc_employees.csv). This keeps the endorsement/network signal fully
    grounded in the provided data — no invented outside people. It models the
    realistic case where a current employee publicly vouched for a former
    colleague they are connected to.
    """
    rows = []
    rec_id = 500
    for att in attendees:
        url = att.get("linkedin_url", "").strip().lower()
        prof = profiles.get(url)
        if not prof:
            continue
        mutuals = [m.strip() for m in prof.get("wsc_mutual_connections", "").split(";") if m.strip()]
        if not mutuals:
            continue
        # ~55% of connected candidates have such a bridge recommendation.
        if RNG.random() > 0.55:
            continue
        valid = [m for m in mutuals if m in employees]
        if not valid:
            continue
        emp = employees[RNG.choice(valid)]
        rows.append({
            "recommendation_id": f"REC{rec_id}",
            "candidate_name": att["full_name"],
            "candidate_linkedin_url": att["linkedin_url"],
            "recommender_name": emp["full_name"],
            "recommender_title": emp["title"],
            "recommender_company": "WSC Sports",
            "is_wsc_employee": "yes",
            "wsc_bridge_employee_id": emp["employee_id"],
            "sentiment": "positive",
            "text": RNG.choice(SENTIMENTS),
        })
        rec_id += 1

    path = os.path.join(DATA, "linkedin_recommendations.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "recommendation_id", "candidate_name", "candidate_linkedin_url",
            "recommender_name", "recommender_title", "recommender_company",
            "is_wsc_employee", "wsc_bridge_employee_id", "sentiment", "text",
        ])
        w.writeheader()
        w.writerows(rows)
    return len(rows), path


def make_salary_expectations(attendees, profiles, applications):
    """
    Assign an annual salary expectation (USD) ONLY to candidates a recruiter has
    actually spoken to. In reality this is NOT known for a passive pool lead met
    at a conference — it surfaces only once a recruiter screens them. So we
    populate it exclusively for applicants who reached a real conversation stage
    in the ATS ("Phone Screen", "In Review" or "On Hold"). Everyone else is left
    blank on purpose, and the dashboard shows "not yet known" for them.
    Derived from years of experience with noise; some exceed a role's budget so
    the OVER_BUDGET signal is demonstrable where the data legitimately exists.
    """
    KNOWN_STAGES = {"Phone Screen", "In Review", "On Hold"}
    screened_ids = {
        a["hubspot_id"] for a in applications
        if a.get("hubspot_id") and a.get("ats_status") in KNOWN_STAGES
    }
    prof_by_id = {}
    for att in attendees:
        url = att.get("linkedin_url", "").strip().lower()
        prof_by_id[att["hubspot_id"]] = profiles.get(url)

    rows = []
    for att in attendees:
        hid = att["hubspot_id"]
        if hid not in screened_ids:
            continue
        prof = prof_by_id.get(hid)
        try:
            years = int(float((prof or {}).get("years_experience", "") or 0))
        except ValueError:
            years = 0
        base = 70000 + years * 8500
        expected = int(round((base + RNG.randint(-15000, 35000)) / 1000.0)) * 1000
        expected = max(60000, min(expected, 260000))
        rows.append({
            "hubspot_id": hid,
            "candidate_name": att["full_name"],
            "expected_salary": expected,
            "currency": "USD",
            "source": "Disclosed during recruiter phone screen",
        })
    path = os.path.join(DATA, "salary_expectations.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "hubspot_id", "candidate_name", "expected_salary", "currency", "source",
        ])
        w.writeheader()
        w.writerows(rows)
    return len(rows), path


def make_job_budgets():
    """Write the per-role budget ceilings (simulated; from Finance/HR in production)."""
    path = os.path.join(DATA, "job_budgets.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["job_id", "budget_min", "budget_max", "currency"])
        w.writeheader()
        for job_id, b in JOB_BUDGETS.items():
            w.writerow({"job_id": job_id, **b})
    return len(JOB_BUDGETS), path


def make_internal_candidates():
    """Write internal employees who opted in to move to an open role."""
    path = os.path.join(DATA, "internal_candidates.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "employee_id", "full_name", "current_title", "department",
            "years_in_role", "years_experience", "target_job_id", "skills",
            "expected_salary", "note",
        ])
        w.writeheader()
        w.writerows(INTERNAL_CANDIDATES)
    return len(INTERNAL_CANDIDATES), path


def main():
    attendees = load_attendees()
    profiles = load_profiles()    employees = load_employees()    n_apps, p_apps, apps = make_internal_applications(attendees)
    n_recs, p_recs = make_recommendations(attendees, profiles)
    n_sal, p_sal = make_salary_expectations(attendees, profiles, apps)
    n_bud, p_bud = make_job_budgets()
    n_int, p_int = make_internal_candidates()
    print(f"Wrote {n_apps} internal applications -> {p_apps}")
    print(f"Wrote {n_recs} linkedin recommendations -> {p_recs}")
    print(f"Wrote {n_sal} salary expectations -> {p_sal}")
    print(f"Wrote {n_bud} job budgets -> {p_bud}")
    print(f"Wrote {n_int} internal candidates -> {p_int}")


if __name__ == "__main__":
    main()
