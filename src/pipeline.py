"""
pipeline.py — End-to-end talent-pool matching pipeline (CLI entry point).

Given a job_id, it:
  1. Loads conference attendees + LinkedIn profiles + employees + jobs.
  2. Joins and enriches each attendee.
  3. Scores every attendee against the selected role (skills, domain relevance,
     seniority, referral strength) — see scoring.py for the transparent logic.
  4. Writes a ranked, recruiter-ready CSV.
  5. Writes an HTML shortlist view (bonus).

Usage:
    python src/pipeline.py --job JOB001
    python src/pipeline.py --job JOB001 --data data --out output --top 15
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

from loader import load_candidates, load_employees, load_jobs
from report import render_html
from scoring import ScoredCandidate, score_all

OUTPUT_COLUMNS = [
    "rank",
    "match_score",
    "tier",
    "hubspot_id",
    "full_name",
    "current_title",
    "current_company",
    "location",
    "years_experience",
    "conference_name",
    "conference_date",
    "skill_match_pct",
    "domain_relevance_pct",
    "seniority_fit_pct",
    "referral_strength_pct",
    "stability_pct",
    "matched_skills",
    "missing_skills",
    "top_referral_type",
    "mutual_connections",
    "worked_with_employee",
    "has_same_dept_referral",
    "current_tenure_years",
    "avg_past_tenure_years",
    "recommended_action",
    "flags",
    "email",
    "linkedin_url",
]


def _row(rank: int, sc: ScoredCandidate) -> dict:
    c = sc.candidate
    referrals = "; ".join(
        f"{r.employee_name} ({r.relation_label}" + (f" @ {r.shared_org}" if r.shared_org else "") + ")"
        for r in sc.referrals
    ) or ""
    top_rel = sc.referrals[0].relation_label if sc.referrals else ""
    avg_tenure = c.avg_past_tenure_years
    return {
        "rank": rank,
        "match_score": sc.match_score,
        "tier": sc.tier,
        "hubspot_id": c.hubspot_id,
        "full_name": c.full_name,
        "current_title": sc.candidate.best_title,
        "current_company": c.current_company or c.company,
        "location": c.location,
        "years_experience": c.years_experience if c.years_experience is not None else "",
        "conference_name": c.conference_name,
        "conference_date": c.conference_date,
        "skill_match_pct": round(sc.skill_match * 100),
        "domain_relevance_pct": round(sc.domain_relevance * 100),
        "seniority_fit_pct": round(sc.seniority_fit * 100),
        "referral_strength_pct": round(sc.referral_strength * 100),
        "stability_pct": round(sc.stability * 100),
        "matched_skills": "; ".join(sc.matched_skills),
        "missing_skills": "; ".join(sc.missing_skills),
        "top_referral_type": top_rel,
        "mutual_connections": referrals,
        "worked_with_employee": "yes" if "WORKED_WITH_EMPLOYEE" in sc.flags else "no",
        "has_same_dept_referral": "yes" if any(r.same_department for r in sc.referrals) else "no",
        "current_tenure_years": c.current_tenure_years if c.current_tenure_years is not None else "",
        "avg_past_tenure_years": round(avg_tenure, 1) if avg_tenure is not None else "",
        "recommended_action": sc.recommended_action,
        "flags": "; ".join(sc.flags),
        "email": c.email,
        "linkedin_url": c.linkedin_url,
    }


def write_csv(path: str, scored, include_noise: bool) -> int:
    rows = scored if include_noise else [s for s in scored if s.tier != "Noise (off-domain)"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for i, sc in enumerate(rows, start=1):
            writer.writerow(_row(i, sc))
    return len(rows)


def run(job_id: str, data_dir: str, out_dir: str, top: int, include_noise: bool) -> None:
    jobs = load_jobs(data_dir)
    if job_id not in jobs:
        available = ", ".join(sorted(jobs))
        sys.exit(f"[error] Unknown job_id '{job_id}'. Available: {available}")
    job = jobs[job_id]

    employees = load_employees(data_dir)
    candidates = load_candidates(data_dir)
    scored = score_all(candidates, job, employees)

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"{job_id}_candidates.csv")
    html_path = os.path.join(out_dir, f"{job_id}_report.html")

    n_rows = write_csv(csv_path, scored, include_noise)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(job, scored, shown=top))

    # Console summary — the quick "what did we find" for the recruiter.
    # "Relevant" = worth acting on now (Strong or Potential). Low/Weak stay in
    # the pool for future roles; Noise is filtered out.
    relevant = [s for s in scored if s.tier in ("Strong Match", "Potential")]
    strong = [s for s in scored if s.tier == "Strong Match"]
    noise = [s for s in scored if s.tier == "Noise (off-domain)"]
    with_ref = [s for s in relevant if s.referrals]
    missing_li = [s for s in candidates if not s.has_linkedin]

    print(f"\n  Job: {job.title} ({job.job_id}) — {job.department}, {job.seniority}")
    print(f"  Attendees screened : {len(candidates)}")
    print(f"  Relevant candidates: {len(relevant)}")
    print(f"  Strong matches     : {len(strong)}")
    print(f"  With warm intro    : {len(with_ref)}")
    print(f"  Filtered as noise  : {len(noise)}")
    print(f"  Missing LinkedIn   : {len(missing_li)}")
    print(f"\n  CSV : {csv_path} ({n_rows} rows)")
    print(f"  HTML: {html_path}")

    print("\n  Top 5:")
    for i, sc in enumerate(scored[:5], start=1):
        ref = f" | intro via {sc.referrals[0].employee_name}" if sc.referrals else ""
        print(f"   {i}. {sc.candidate.full_name:22s} {sc.match_score:5.1f}  {sc.tier}{ref}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Talent-pool candidate matching pipeline.")
    parser.add_argument("--job", required=True, help="job_id to match against, e.g. JOB001")
    parser.add_argument("--data", default=None, help="path to the data/ folder (default: ../data next to src/)")
    parser.add_argument("--out", default=None, help="output folder (default: ../output next to src/)")
    parser.add_argument("--top", type=int, default=15, help="how many candidates to show in the HTML view")
    parser.add_argument("--include-noise", action="store_true",
                        help="also write off-domain (noise) rows to the CSV")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    data_dir = args.data or os.path.join(root, "data")
    out_dir = args.out or os.path.join(root, "output")

    run(args.job, data_dir, out_dir, args.top, args.include_noise)


if __name__ == "__main__":
    main()
