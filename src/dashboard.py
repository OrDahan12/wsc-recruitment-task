"""
dashboard.py — Build a single interactive HR dashboard (one self-contained HTML).

Unlike the per-job static report, this is the recruiter's cockpit. It embeds all
pre-computed data as JSON and uses vanilla JS (no build step, no dependencies) to:

  * Overview  — the living talent pool at a glance (conferences + pool + alerts).
  * Conferences — every event: name, date, domain, # attendees captured.
  * Open Jobs — all active roles, internal ATS applicants vs. talent-pool matches.
  * Match — pick a role, adjust the scoring weights live (sliders), click the KPI
            cards to filter, search the pool, and drill into any candidate.

Why weights live in the browser: with thousands of candidates, every role needs a
slightly different emphasis. HR gets a sensible default but stays in control — move
a slider and the whole shortlist re-scores and re-ranks instantly.

Run:  python src/dashboard.py         (writes output/dashboard.html)
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Dict, List

from loader import (
    Candidate, load_candidates, load_employees, load_internal_applications, load_jobs,
    load_internal_candidates, load_job_budgets,
)
from scoring import (
    WEIGHTS, DOMAIN_NOISE_GATE, SENIORITY_BANDS, RELATION_STRENGTH, RELATION_LABEL,
    TIER_STRONG, TIER_POTENTIAL, TIER_LOW, score_all,
)

# Reference "today" for the New/Hot marks. Anchored to the dataset timeframe
# (conferences ran Nov 2024 - Apr 2025); date.today() in a live deployment.
REFERENCE_TODAY = date(2025, 5, 1)
NEW_ROLE_DAYS = 30          # opened within this many days -> "New"
HOT_STRONG_MIN = 5          # this many strong pool matches waiting -> "Hot"
HOT_APPLICANTS_MAX = 2      # this few active applicants (hard to fill) -> "Hot"

# Which "department" a conference attendee really belongs to, inferred from their
# title / skills / industry. This is what powers the Conferences department tree
# and is itself a readout of the signal-vs-noise story: many attendees classify
# as "Other" (IT/sales/finance who happened to attend) rather than a real domain.
_DEPARTMENT_RULES = [
    ("AI/ML", ("machine learning", "ml engineer", "ml ", " ai", "ai ", "deep learning",
               "computer vision", "nlp", "data scientist", "research scientist", "mlops",
               "ml research", "artificial intelligence", "perception")),
    ("Data", ("data engineer", "data engineering", "analytics", "spark", "etl", "dbt",
              "data platform", "bi ", "business intelligence", "data pipeline", "airflow",
              "data analyst", "warehouse")),
    ("Engineering", ("backend", "software engineer", "devops", "sre", "site reliability",
                     "platform engineer", "infrastructure", "full stack", "fullstack",
                     "cloud engineer", "systems engineer", "microservices", "developer")),
    ("Product", ("product manager", "product owner", "ux", "ui/ux", "product design",
                 "product lead", "designer", "product marketing")),
]


def _classify_department(cand: Candidate) -> str:
    hay = " ".join([
        cand.best_title or "", cand.conference_title or "", cand.industry or "",
        " ".join(cand.top_skills or []),
    ]).lower()
    for dept, keywords in _DEPARTMENT_RULES:
        if any(k in hay for k in keywords):
            return dept
    return "Other / Noise"


def _conferences(candidates: List[Candidate]) -> List[dict]:
    agg: Dict[str, dict] = {}
    for c in candidates:
        key = c.conference_name
        if key not in agg:
            agg[key] = {
                "name": c.conference_name,
                "date": c.conference_date,
                "domain": c.conference_domain,
                "attendees": 0,
            }
        agg[key]["attendees"] += 1
    return sorted(agg.values(), key=lambda d: d["date"])


def _attendees(candidates: List[Candidate]) -> List[dict]:
    """Flat attendee list (conference leads only) for the department tree."""
    out = []
    for c in candidates:
        out.append({
            "name": c.full_name,
            "title": c.best_title,
            "company": c.current_company or c.company,
            "location": c.location,
            "years": c.years_experience,
            "conference": c.conference_name,
            "conference_date": c.conference_date,
            "department": _classify_department(c),
            "has_linkedin": c.has_linkedin,
            "skills": c.top_skills,
        })
    return out


def _scoring_meta() -> dict:
    """Human-readable explanations surfaced behind the '?' help icons."""
    return {
        "weights_help": {
            "skill_match": "How many of the role's required (and nice-to-have) skills the "
                           "candidate actually has. Required skills count 4x the nice-to-haves.",
            "domain_relevance": "Is this person genuinely in the role's domain, or conference "
                                "noise? Below 25% they are gated out entirely. This is the "
                                "signal-vs-noise filter that is the whole point of the tool.",
            "seniority_fit": "Years of experience vs. the band the role expects "
                             "(e.g. Senior = 6-14 yrs). Unknown experience scores a neutral 50%.",
            "referral_strength": "Warm-intro potential, ranked by relationship QUALITY not count: "
                                 "a recommendation or an ex-colleague inside WSC beats a random "
                                 "shared connection.",
            "stability": "Retention signal. Penalises job-hoppers and factors 'movability' - "
                         "someone ~2-4 yrs into a role is at the sweet spot to move.",
        },
        "formula": "match_score = 100 x ( 0.35xSkills + 0.25xDomain + 0.10xSeniority "
                   "+ 0.20xReferral + 0.10xStability )  -- weights are the defaults; "
                   "recruiters can retune them with the sliders.",
        "tiers": {"strong": TIER_STRONG, "potential": TIER_POTENTIAL, "low": TIER_LOW},
        "seniority_bands": {k: list(v) for k, v in SENIORITY_BANDS.items()},
        "relation_strength": RELATION_STRENGTH,
        "relation_label": RELATION_LABEL,
        "penalties": {
            "JOB_HOPPER": "Average past tenure under 1.5 yrs - a retention risk. Cuts the "
                          "Stability sub-score to ~35%.",
            "RECENTLY_STARTED": "Under a year into the current role - less likely to move now. "
                                "Lowers movability inside Stability.",
            "PARTIAL_SKILLS": "Has some but not all required skills - the missing ones are "
                              "listed explicitly, and Skills is scored on the fraction matched.",
            "MISSING_LINKEDIN": "No LinkedIn profile to enrich from - scored on conference data "
                                "alone, kept in the pool rather than dropped.",
            "NO_MUTUAL_CONNECTION": "No warm-intro path found - Referral scores 0. Still ranked "
                                    "on merit.",
            "PREVIOUSLY_REJECTED": "Already rejected for this role in the ATS - surfaced so you "
                                   "don't re-approach by mistake.",
            "OFF_DOMAIN": "Failed the domain gate (relevance < 25%) - conference noise, excluded "
                          "from the shortlist.",
        },
    }


def _referral_json(referrals) -> List[dict]:
    out = []
    for r in referrals:
        out.append({
            "name": r.employee_name,
            "title": r.employee_title,
            "dept": r.department,
            "relation": r.relation,
            "relation_label": r.relation_label,
            "org": r.shared_org,
            "is_external": r.is_external,
            "bridge": r.bridge_employee,
            "note": r.note,
        })
    return out


def _ats_status_for(cand: Candidate, job_id: str) -> str:
    for app in cand.ats_applications:
        if app.get("job_id") == job_id:
            return app.get("ats_status", "")
    return ""


def build_payload(data_dir: str) -> dict:
    jobs = load_jobs(data_dir)
    employees = load_employees(data_dir)
    candidates = load_candidates(data_dir)
    applications = load_internal_applications(data_dir)
    internal_candidates = load_internal_candidates(data_dir)
    budgets = load_job_budgets(data_dir)

    apps_by_job = defaultdict(list)
    for app in applications:
        apps_by_job[app.get("job_id", "")].append(app)

    candidates_by_job: Dict[str, List[dict]] = {}
    jobs_json: List[dict] = []

    for job_id, job in jobs.items():
        # Internal movers who opted into *this* role join the same scoring pool.
        pool = candidates + [ic for ic in internal_candidates if ic.target_job_id == job_id]
        scored = score_all(pool, job, employees)
        rows = []
        for sc in scored:
            c = sc.candidate
            rows.append({
                "hubspot_id": c.hubspot_id,
                "name": c.full_name,
                "title": c.best_title,
                "company": c.current_company or c.company,
                "location": c.location,
                "years": c.years_experience,
                "conference": c.conference_name,
                "conference_date": c.conference_date,
                "department": _classify_department(c),
                "source": "Internal Mobility" if c.is_internal else "Conference",
                "is_internal": c.is_internal,
                "domain_relevance": sc.domain_relevance,
                "subs": {
                    "skill_match": sc.skill_match,
                    "domain_relevance": sc.domain_relevance,
                    "seniority_fit": sc.seniority_fit,
                    "referral_strength": sc.referral_strength,
                    "stability": sc.stability,
                },
                "matched": sc.matched_skills,
                "missing": sc.missing_skills,
                "referrals": _referral_json(sc.referrals),
                "flags": list(sc.flags),
                "ats_status": _ats_status_for(c, job_id),
                "recommended": sc.recommended_action,
                "notes": c.notes,
                "current_tenure": c.current_tenure_years,
                "avg_tenure": round(c.avg_past_tenure_years, 1) if c.avg_past_tenure_years else None,
                "email": c.email,
                "linkedin_url": c.linkedin_url,
            })
        candidates_by_job[job_id] = rows

        # Pool readouts used for the Hot mark and the Open Jobs tab.
        strong_ct = sum(1 for sc in scored if sc.tier == "Strong Match")
        relevant_ct = sum(1 for sc in scored if sc.tier in ("Strong Match", "Potential"))

        job_apps = apps_by_job.get(job_id, [])
        status_counts = Counter(a.get("ats_status", "") for a in job_apps)
        budget = budgets.get(job_id) or {}
        opened = budget.get("opened_date", "")
        is_new = False
        days_open = None
        if opened:
            try:
                d = datetime.strptime(opened, "%Y-%m-%d").date()
                days_open = (REFERENCE_TODAY - d).days
                is_new = 0 <= days_open <= NEW_ROLE_DAYS
            except ValueError:
                pass
        # Hot = important/urgent: lots of strong talent already waiting in the pool,
        # or so few active applicants that the role is hard to fill.
        is_hot = strong_ct >= HOT_STRONG_MIN or len(job_apps) <= HOT_APPLICANTS_MAX
        jobs_json.append({
            "job_id": job.job_id,
            "title": job.title,
            "department": job.department,
            "seniority": job.seniority,
            "key_domains": job.key_domains,
            "required_skills": job.required_skills,
            "nice_to_have": job.nice_to_have,
            "internal_applicants": len(job_apps),
            "applicants_by_status": dict(status_counts),
            "internal_movers": sum(1 for ic in internal_candidates if ic.target_job_id == job_id),
            "budget_max": budget.get("budget_max"),
            "budget_currency": budget.get("currency", ""),
            "opened_date": opened,
            "days_open": days_open,
            "is_new": is_new,
            "is_hot": is_hot,
            "strong_matches": strong_ct,
            "relevant_matches": relevant_ct,
        })

    return {
        "weights": WEIGHTS,
        "conferences": _conferences(candidates),
        "attendees": _attendees(candidates),
        "employees": [
            {"id": e.employee_id, "name": e.full_name, "title": e.title, "department": e.department}
            for e in employees.values()
        ],
        "jobs": jobs_json,
        "candidates_by_job": candidates_by_job,
        "noise_gate": DOMAIN_NOISE_GATE,
        "new_role_days": NEW_ROLE_DAYS,
        "scoring_meta": _scoring_meta(),
    }


def render(payload: dict) -> str:
    data_js = json.dumps(payload, ensure_ascii=False)
    return _TEMPLATE.replace("/*__DATA__*/", data_js)


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    data_dir = os.path.join(root, "data")
    out_dir = os.path.join(root, "output")
    os.makedirs(out_dir, exist_ok=True)

    payload = build_payload(data_dir)
    html = render(payload)
    path = os.path.join(out_dir, "dashboard.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)

    total = sum(len(v) for v in payload["candidates_by_job"].values())
    print(f"Dashboard written -> {path}")
    print(f"  jobs={len(payload['jobs'])}  conferences={len(payload['conferences'])}"
          f"  candidate-rows={total}  employees={len(payload['employees'])}")


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>TalentOps — HR Talent Pool Dashboard</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#f1f5f9; color:#1e293b; }
  .topbar { background:linear-gradient(135deg,#0f3460,#16213e); color:#fff; padding:18px 28px;
            display:flex; align-items:center; justify-content:space-between; }
  .topbar h1 { font-size:19px; font-weight:700; }
  .topbar .live { font-size:12px; opacity:.8; display:flex; align-items:center; gap:7px; }
  .dot { width:8px; height:8px; border-radius:50%; background:#22c55e; box-shadow:0 0 0 0 rgba(34,197,94,.6);
         animation:pulse 2s infinite; }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(34,197,94,.6)} 70%{box-shadow:0 0 0 8px rgba(34,197,94,0)} 100%{box-shadow:0 0 0 0 rgba(34,197,94,0)} }
  .tabs { display:flex; gap:2px; background:#fff; border-bottom:1px solid #e2e8f0; padding:0 20px; }
  .tab { padding:14px 20px; font-size:14px; font-weight:600; color:#64748b; cursor:pointer;
         border-bottom:3px solid transparent; }
  .tab:hover { color:#0f3460; }
  .tab.active { color:#0f3460; border-bottom-color:#0f3460; }
  .page { display:none; max-width:1080px; margin:0 auto; padding:24px 20px 80px; }
  .page.active { display:block; }
  h2 { font-size:18px; color:#0f3460; margin-bottom:4px; }
  .sub { color:#64748b; font-size:13px; margin-bottom:16px; }
  .kpis { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:18px; }
  .kpi { background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:14px 18px; min-width:132px;
         cursor:pointer; transition:.15s; }
  .kpi:hover { border-color:#0f3460; transform:translateY(-1px); }
  .kpi.active { border-color:#0f3460; box-shadow:0 0 0 2px #0f346022; }
  .kpi .n { font-size:26px; font-weight:800; color:#0f3460; }
  .kpi .l { font-size:12px; color:#64748b; }
  .kpi.static { cursor:default; }
  .kpi.static:hover { transform:none; border-color:#e2e8f0; }
  table { width:100%; border-collapse:collapse; background:#fff; border:1px solid #e2e8f0;
          border-radius:12px; overflow:hidden; font-size:13px; }
  th { background:#f8fafc; text-align:left; padding:11px 14px; color:#0f3460; font-weight:700;
       border-bottom:1px solid #e2e8f0; cursor:pointer; user-select:none; white-space:nowrap; }
  th:hover { background:#eef2f7; }
  td { padding:10px 14px; border-bottom:1px solid #f1f5f9; vertical-align:middle; }
  tr:last-child td { border-bottom:none; }
  tr.clickable { cursor:pointer; }
  tr.clickable:hover td { background:#f8fafc; }
  .pill { font-size:11px; font-weight:700; border-radius:20px; padding:2px 10px; color:#fff; white-space:nowrap; }
  .chip { display:inline-block; font-size:11px; border-radius:5px; padding:2px 7px; margin:0 3px 3px 0; }
  .chip-ok { background:#dcfce7; color:#166534; }
  .chip-miss { background:#fee2e2; color:#991b1b; }
  .flag { font-size:10px; font-weight:700; border:1px solid var(--fc,#94a3b8); color:var(--fc,#94a3b8);
          border-radius:20px; padding:1px 7px; margin:0 3px 3px 0; display:inline-block; }
  .src-int { font-size:9px; font-weight:800; letter-spacing:.4px; text-transform:uppercase;
             background:#0d9488; color:#fff; border-radius:4px; padding:1px 5px; vertical-align:middle; }
  .controls { background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:16px 18px; margin-bottom:16px; }
  .controls .row { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
  select, input[type=text] { border:1px solid #cbd5e1; border-radius:8px; padding:8px 12px; font-size:14px; }
  input[type=text] { flex:1; min-width:200px; }
  .sliders { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-top:14px;
             border-top:1px solid #f1f5f9; padding-top:14px; }
  .slider label { font-size:12px; font-weight:600; color:#334155; display:flex; justify-content:space-between; }
  .slider input[type=range] { width:100%; margin-top:5px; accent-color:#0f3460; }
  .reset { font-size:12px; color:#0f3460; background:#eef2f7; border:1px solid #cbd5e1; border-radius:8px;
           padding:7px 12px; cursor:pointer; font-weight:600; }
  .barmini { background:#e2e8f0; border-radius:5px; height:6px; width:70px; display:inline-block; vertical-align:middle; }
  .barmini > div { background:#0f3460; height:6px; border-radius:5px; }
  .drawer-bg { display:none; position:fixed; inset:0; background:rgba(15,23,42,.45); z-index:40; }
  .drawer-bg.open { display:block; }
  .drawer { position:fixed; top:0; right:0; width:min(480px,92vw); height:100%; background:#fff; z-index:50;
            box-shadow:-4px 0 20px rgba(0,0,0,.15); transform:translateX(100%); transition:.25s; overflow-y:auto; }
  .drawer.open { transform:translateX(0); }
  .drawer .dh { background:linear-gradient(135deg,#0f3460,#16213e); color:#fff; padding:22px 24px; }
  .drawer .dh h3 { font-size:20px; }
  .drawer .dh p { opacity:.8; font-size:13px; }
  .drawer .body { padding:20px 24px; }
  .close { position:absolute; top:16px; right:18px; color:#fff; font-size:22px; cursor:pointer; opacity:.8; }
  .metric { margin:9px 0; }
  .metric .ml { display:flex; justify-content:space-between; font-size:12px; color:#475569; }
  .metric .mt { background:#e2e8f0; border-radius:6px; height:8px; margin-top:3px; }
  .metric .mf { background:#0f3460; height:8px; border-radius:6px; }
  .seclbl { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#94a3b8; font-weight:700;
            margin:16px 0 7px; }
  .refbox { border:1px solid #e2e8f0; border-radius:10px; padding:11px 13px; margin-bottom:8px; font-size:13px; }
  .refbox .rel { font-size:10px; font-weight:700; border-radius:20px; padding:1px 8px; color:#fff; margin-left:6px; }
  .refbox .note { color:#64748b; font-style:italic; margin-top:4px; font-size:12px; }
  .action { background:#eff6ff; border:1px solid #bfdbfe; color:#1e3a8a; border-radius:8px; padding:11px 14px;
            font-size:13px; font-weight:600; margin-top:14px; }
  .muted { color:#94a3b8; }
  .card { background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:16px 18px; margin-bottom:12px; }
  .note-info { background:#fffbeb; border-left:3px solid #f59e0b; padding:8px 12px; border-radius:6px;
               font-size:12px; color:#78350f; margin-top:6px; }
  .banner { background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; border-radius:10px; padding:12px 16px;
            font-size:13px; margin-bottom:16px; }

  /* --- help icons + popovers (scoring transparency) --- */
  .help { display:inline-flex; align-items:center; justify-content:center; width:15px; height:15px;
          border-radius:50%; background:#cbd5e1; color:#fff; font-size:10px; font-weight:800;
          cursor:help; position:relative; margin-left:5px; vertical-align:middle; flex-shrink:0; }
  .help:hover { background:#0f3460; }
  .help .tip { display:none; position:absolute; bottom:130%; left:50%; transform:translateX(-50%);
               width:250px; background:#0f172a; color:#e2e8f0; font-size:11px; font-weight:400;
               line-height:1.55; text-align:left; padding:9px 11px; border-radius:8px; z-index:60;
               box-shadow:0 8px 24px rgba(0,0,0,.25); }
  .help .tip::after { content:""; position:absolute; top:100%; left:50%; transform:translateX(-50%);
                      border:6px solid transparent; border-top-color:#0f172a; }
  .help:hover .tip { display:block; }
  .help.right .tip { left:auto; right:0; transform:none; }
  .help.right .tip::after { left:auto; right:6px; transform:none; }

  .scoring-help { background:#f8fafc; border:1px solid #e2e8f0; border-radius:12px; margin-bottom:16px; }
  .scoring-help summary { cursor:pointer; padding:13px 18px; font-weight:700; color:#0f3460; font-size:14px;
                          list-style:none; display:flex; align-items:center; gap:8px; }
  .scoring-help summary::-webkit-details-marker { display:none; }
  .scoring-help[open] summary { border-bottom:1px solid #e2e8f0; }
  .scoring-help .sh-body { padding:14px 18px; font-size:13px; color:#334155; line-height:1.7; }
  .formula { font-family:'Courier New',monospace; background:#0f172a; color:#a7f3d0; padding:11px 13px;
             border-radius:8px; font-size:12px; margin:8px 0 14px; overflow-x:auto; }
  .wexp { display:grid; grid-template-columns:130px 1fr; gap:6px 14px; align-items:start; margin-top:6px; }
  .wexp .wk { font-weight:700; color:#0f3460; }
  .wexp .wd { color:#475569; }

  /* --- New / Hot job marks --- */
  .mark { font-size:10px; font-weight:800; letter-spacing:.4px; text-transform:uppercase;
          border-radius:5px; padding:2px 7px; margin-left:6px; vertical-align:middle; display:inline-block; }
  .mark-new { background:#dbeafe; color:#1d4ed8; }
  .mark-hot { background:#fee2e2; color:#dc2626; }

  /* --- department tree --- */
  .tree { background:#fff; border:1px solid #e2e8f0; border-radius:12px; overflow:hidden; }
  .tnode { border-bottom:1px solid #f1f5f9; }
  .tnode:last-child { border-bottom:none; }
  .tdept { display:flex; align-items:center; gap:10px; padding:12px 16px; cursor:pointer; user-select:none; }
  .tdept:hover { background:#f8fafc; }
  .tdept .caret { transition:.15s; color:#94a3b8; font-size:12px; width:12px; }
  .tdept.open .caret { transform:rotate(90deg); }
  .tdept .dname { font-weight:700; color:#0f3460; font-size:14px; }
  .tdept .dcount { background:#eef2f7; color:#0f3460; font-size:11px; font-weight:700; border-radius:20px; padding:1px 9px; }
  .tdept .dbar { flex:1; height:7px; background:#eef2f7; border-radius:5px; overflow:hidden; max-width:220px; }
  .tdept .dbar > div { height:7px; border-radius:5px; }
  .tkids { display:none; padding:0 16px 12px 38px; }
  .tkids.open { display:block; }
  .tconf { font-size:12px; font-weight:700; color:#334155; margin:10px 0 5px; }
  .mini-table { width:100%; border-collapse:collapse; font-size:12px; }
  .mini-table th { background:#f8fafc; padding:6px 10px; font-size:11px; }
  .mini-table td { padding:6px 10px; border-bottom:1px solid #f1f5f9; }

  /* --- charts --- */
  .charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; margin-bottom:18px; }
  .chart { background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:14px 16px; }
  .chart h4 { font-size:13px; color:#0f3460; margin-bottom:12px; display:flex; align-items:center; }
  .bars { display:flex; flex-direction:column; gap:9px; }
  .barrow { display:grid; grid-template-columns:120px 1fr 34px; align-items:center; gap:10px; font-size:12px; }
  .barrow .bl { color:#475569; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .barrow .bt { background:#eef2f7; border-radius:5px; height:14px; overflow:hidden; }
  .barrow .bt > div { height:14px; border-radius:5px; background:#0f3460; }
  .barrow .bn { font-weight:700; color:#0f3460; text-align:right; }
  .hist { display:flex; align-items:flex-end; gap:6px; height:120px; padding-top:8px; }
  .hist .hb { flex:1; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:4px; height:100%; }
  .hist .hb .hbar { width:100%; background:#0f3460; border-radius:4px 4px 0 0; min-height:2px; }
  .hist .hb .hlbl { font-size:10px; color:#94a3b8; }

  /* --- score math + penalties in the drawer --- */
  .mathrow { display:grid; grid-template-columns:1fr auto auto; gap:8px; align-items:center;
             font-size:12px; padding:4px 0; border-bottom:1px solid #f8fafc; }
  .mathrow .mlbl { color:#475569; }
  .mathrow .mcalc { font-family:'Courier New',monospace; color:#94a3b8; font-size:11px; }
  .mathrow .mpts { font-weight:700; color:#0f3460; text-align:right; min-width:46px; }
  .mathtot { display:flex; justify-content:space-between; font-weight:800; color:#0f3460; font-size:14px;
             padding-top:8px; margin-top:4px; border-top:2px solid #e2e8f0; }
  .penbox { border:1px solid #fecaca; background:#fef2f2; border-radius:10px; padding:10px 13px; margin-top:8px; }
  .penrow { font-size:12px; color:#7f1d1d; padding:3px 0; }
  .penrow strong { color:#dc2626; }
  .penrow .pd { color:#9f5b5b; }
  .goodrow { font-size:12px; color:#166534; padding:2px 0; }
</style></head><body>

<div class="topbar">
  <h1>TalentOps · HR Talent Pool</h1>
  <div class="live"><span class="dot"></span> Living pool — auto-updates after every conference &amp; LinkedIn refresh</div>
</div>

<div class="tabs">
  <div class="tab active" data-page="overview">Overview</div>
  <div class="tab" data-page="conferences">Conferences</div>
  <div class="tab" data-page="jobs">Open Jobs</div>
  <div class="tab" data-page="match">Candidate Match</div>
</div>

<!-- OVERVIEW -->
<div class="page active" id="overview">
  <h2>The talent pool at a glance</h2>
  <p class="sub">Every conference attendee captured, enriched, and ready to match the moment a role opens.</p>
  <div class="kpis" id="ov-kpis"></div>
  <div class="banner" id="ov-banner"></div>
  <div class="charts" id="ov-charts"></div>
  <div class="card">
    <div class="seclbl">How a lead becomes a shortlisted candidate</div>
    <p style="font-size:13px;color:#334155;line-height:1.7;">
      Conference (badge scan) → captured in HubSpot → enriched with LinkedIn (skills, tenure,
      mutual connections, recommendations) → filtered for domain signal-vs-noise → scored against
      the open role → surfaced here with a warm-intro path. The pool is a living bank: it grows
      after each event and refreshes as profiles change.
    </p>
  </div>
</div>

<!-- CONFERENCES -->
<div class="page" id="conferences">
  <h2>Conferences &amp; talent domains</h2>
  <p class="sub">Where we met the talent, grouped by the domain each attendee really works in.
     Expand a department, then click any attendee to shortlist their conference. The big
     <em>Other / Noise</em> bucket is exactly the signal-vs-noise problem this tool solves.</p>
  <div class="kpis" id="cf-kpis"></div>
  <div class="charts" id="cf-charts"></div>
  <h4 style="font-size:14px;color:#0f3460;margin:6px 0 10px;">Attendees by department → conference</h4>
  <div class="tree" id="cf-tree"></div>
</div>

<!-- OPEN JOBS -->
<div class="page" id="jobs">
  <h2>Open positions</h2>
  <p class="sub">Active roles with internal ATS applicants vs. passive talent-pool matches. Click a role to shortlist.</p>
  <table id="jobs-table"><thead><tr>
    <th>Role</th><th>Dept</th><th>Budget</th>
    <th>ATS applicants</th><th>Internal movers</th><th>Pool: strong</th><th>Pool: relevant</th><th></th>
  </tr></thead><tbody></tbody></table>
  <p class="sub" style="margin-top:12px;">
    <strong>ATS applicants</strong> = people who actively applied (Comeet). <strong>Pool</strong> = passive
    candidates the system surfaces from past conferences. HR compares both to find the best fit —
    including passive candidates who never applied.
  </p>
</div>

<!-- MATCH -->
<div class="page" id="match">
  <h2>Candidate match</h2>
  <p class="sub">Pick a role, tune what matters for it, and work the ranked shortlist.</p>

  <div class="controls">
    <div class="row">
      <select id="job-select"></select>
      <input type="text" id="search" placeholder="Search the pool — name, skill, company, location…"/>
      <button class="reset" id="reset-weights">Reset weights</button>
    </div>
    <div class="sliders" id="sliders"></div>
    <div style="font-size:11px;color:#94a3b8;margin-top:10px;">
      Weights are normalized automatically. Off-domain candidates (relevance &lt; 25%) are always gated out as noise.
    </div>
  </div>

  <details class="scoring-help" id="scoring-help">
    <summary>&#9432; How is the match score calculated? (click to reveal the full logic)</summary>
    <div class="sh-body" id="sh-body"></div>
  </details>

  <div class="charts" id="mt-charts"></div>
  <div class="kpis" id="mt-kpis"></div>

  <table id="mt-table"><thead><tr>
    <th data-sort="rank">#</th><th data-sort="name">Candidate</th><th data-sort="score">Score</th>
    <th data-sort="tier">Tier</th><th data-sort="referral_strength">Warm intro</th>
    <th data-sort="skill_match">Skills</th><th>Flags</th>
  </tr></thead><tbody></tbody></table>
</div>

<!-- DRAWER -->
<div class="drawer-bg" id="drawer-bg"></div>
<div class="drawer" id="drawer">
  <div class="dh">
    <span class="close" id="drawer-close">&times;</span>
    <h3 id="d-name"></h3><p id="d-sub"></p>
  </div>
  <div class="body" id="d-body"></div>
</div>

<script>
const DATA = /*__DATA__*/;

const TIER_COLOR = { "Strong Match":"#16a34a","Potential":"#f59e0b","Low":"#94a3b8","Weak":"#cbd5e1","Noise (off-domain)":"#ef4444" };
const REL_COLOR = { recommendation:"#7c3aed", worked_together:"#0f766e", mutual_same_dept:"#16a34a", same_org:"#6366f1", mutual:"#94a3b8" };
const FLAG_STYLE = {
  INTERNAL_MOBILITY:["#0d9488","Internal mover"],
  HAS_RECOMMENDATION:["#7c3aed","Recommended"], WORKED_WITH_EMPLOYEE:["#0f766e","Worked w/ employee"],
  STRONG_REFERRAL:["#16a34a","Warm intro"], MOVABLE_SWEET_SPOT:["#2563eb","Movable"],
  RECENTLY_STARTED:["#94a3b8","Just started"], JOB_HOPPER:["#ef4444","Job hopper"],
  ALREADY_APPLIED:["#0891b2","Already applied"], PREVIOUSLY_REJECTED:["#ef4444","Previously rejected"],
  MISSING_LINKEDIN:["#ef4444","No LinkedIn"], PARTIAL_SKILLS:["#f59e0b","Partial skills"],
  NO_MUTUAL_CONNECTION:["#94a3b8","No connection"], OFF_DOMAIN:["#ef4444","Off-domain"]
};
const SUB_LABELS = { skill_match:"Skills", domain_relevance:"Domain", seniority_fit:"Seniority", referral_strength:"Referral", stability:"Stability" };
const DEPT_COLOR = { "AI/ML":"#7c3aed", "Data":"#0891b2", "Engineering":"#0f766e", "Product":"#d97706", "Other / Noise":"#94a3b8" };
// Flags that represent a drag on the score (surfaced explicitly so nothing hides).
const PENALTY_FLAGS = ["JOB_HOPPER","RECENTLY_STARTED","PARTIAL_SKILLS","MISSING_LINKEDIN","NO_MUTUAL_CONNECTION","PREVIOUSLY_REJECTED","OFF_DOMAIN"];
const META = DATA.scoring_meta;

let weights = Object.assign({}, DATA.weights);
let currentJob = DATA.jobs[0].job_id;
let kpiFilter = null;      // tier filter from KPI card
let searchTerm = "";
let sortKey = "score", sortDir = -1;
let confFilter = null;

const $ = s => document.querySelector(s);
const esc = s => (s==null?"":(""+s)).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ---- small UI helpers: help icons + charts ----
function help(text, right){
  return '<span class="help'+(right?' right':'')+'">?<span class="tip">'+esc(text)+'</span></span>';
}
function barChart(title, rows, help_){
  // rows: [{label, n, color}]. Scales bars to the largest value.
  const max = Math.max(1, ...rows.map(r=>r.n));
  const body = rows.map(r=>
    '<div class="barrow"><span class="bl">'+esc(r.label)+'</span>'
    +'<span class="bt"><div style="width:'+Math.round(r.n/max*100)+'%;background:'+(r.color||"#0f3460")+'"></div></span>'
    +'<span class="bn">'+r.n+'</span></div>').join("");
  return '<div class="chart"><h4>'+esc(title)+(help_?help(help_,true):"")+'</h4><div class="bars">'+body+'</div></div>';
}
function histChart(title, buckets, help_){
  // buckets: [{label, n}]. Vertical bars.
  const max = Math.max(1, ...buckets.map(b=>b.n));
  const body = buckets.map(b=>
    '<div class="hb"><div class="hbar" style="height:'+Math.round(b.n/max*100)+'%" title="'+b.n+'"></div>'
    +'<div class="hlbl">'+esc(b.label)+'</div></div>').join("");
  return '<div class="chart"><h4>'+esc(title)+(help_?help(help_,true):"")+'</h4><div class="hist">'+body+'</div></div>';
}
function scoreHistogram(rows){
  const edges = [0,30,50,70,101];
  const labels = ["<30","30-49","50-69","70+"];
  const counts = [0,0,0,0];
  rows.forEach(r=>{ if(r.tier==="Noise (off-domain)") return;
    for(let i=0;i<4;i++){ if(r.score>=edges[i] && r.score<edges[i+1]){ counts[i]++; break; } } });
  return labels.map((l,i)=>({label:l, n:counts[i]}));
}

// ---- scoring in the browser (mirrors scoring.py weighting) ----
function tierFor(score, domain){
  if (domain < DATA.noise_gate) return "Noise (off-domain)";
  if (score >= 70) return "Strong Match";
  if (score >= 50) return "Potential";
  if (score >= 30) return "Low";
  return "Weak";
}
function scoreRow(row){
  const wsum = Object.values(weights).reduce((a,b)=>a+b,0) || 1;
  let s = 0;
  for (const k in weights){ s += (weights[k]/wsum) * (row.subs[k]||0); }
  const score = Math.round(s*1000)/10;
  return { score, tier: tierFor(score, row.domain_relevance) };
}
function computed(jobId){
  return DATA.candidates_by_job[jobId].map(r=>{
    const {score,tier} = scoreRow(r);
    return Object.assign({}, r, {score, tier});
  });
}

// ---- tabs ----
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".page").forEach(x=>x.classList.remove("active"));
  t.classList.add("active"); $("#"+t.dataset.page).classList.add("active");
});

// ---- overview ----
function renderOverview(){
  const jobId = currentJob;
  const rows = computed(jobId);
  const totalPool = DATA.candidates_by_job[DATA.jobs[0].job_id].length;
  const conf = DATA.conferences.length;
  let strong=0, withIntro=0;
  DATA.jobs.forEach(j=>{ computed(j.job_id).forEach(r=>{ if(r.tier==="Strong Match") strong++; }); });
  const el = $("#ov-kpis");
  el.innerHTML = kpi(totalPool,"Candidates in pool",true)
    + kpi(conf,"Conferences captured",true)
    + kpi(DATA.jobs.length,"Open roles",true)
    + kpi(DATA.employees.length,"Employees (referrers)",true);
  const topJob = DATA.jobs.map(j=>({j, n:computed(j.job_id).filter(r=>r.tier==="Strong Match").length}))
                          .sort((a,b)=>b.n-a.n)[0];
  $("#ov-banner").innerHTML = "🔔 <strong>"+topJob.n+" strong matches</strong> already sitting in the pool for <strong>"
     + esc(topJob.j.title) + "</strong> — no new sourcing required. Open the Match tab to act.";

  const deptCounts = {};
  DATA.attendees.forEach(a=>{ deptCounts[a.department]=(deptCounts[a.department]||0)+1; });
  const deptRows = Object.keys(deptCounts).sort((a,b)=>deptCounts[b]-deptCounts[a])
     .map(d=>({label:d, n:deptCounts[d], color:DEPT_COLOR[d]||"#0f3460"}));
  const jobRows = DATA.jobs.map(j=>({label:j.title, n:computed(j.job_id).filter(r=>r.tier==="Strong Match").length, color:"#16a34a"}));
  $("#ov-charts").innerHTML =
    barChart("Talent pool by department", deptRows, "Each conference attendee is classified into the domain they actually work in, from their title and skills. The Other / Noise bucket is people who attended but aren't in a hiring domain — the signal-vs-noise problem.")
    + barChart("Strong matches waiting, by open role", jobRows, "How many Strong-tier candidates already sit in the pool for each open role — talent you can act on without new sourcing.");
}
function kpi(n,l,isStatic,active){
  return '<div class="kpi'+(isStatic?' static':'')+(active?' active':'')+'" '
    +(l?'data-l="'+esc(l)+'"':'')+'><div class="n">'+n+'</div><div class="l">'+esc(l)+'</div></div>';
}

// ---- conferences (department tree + charts) ----
const DEPT_ORDER = ["AI/ML","Data","Engineering","Product","Other / Noise"];
function renderConferences(){
  const att = DATA.attendees;
  const total = att.length;
  const domains = new Set(att.map(a=>a.department!=="Other / Noise"?a.department:null).filter(Boolean));
  const noise = att.filter(a=>a.department==="Other / Noise").length;
  $("#cf-kpis").innerHTML = kpi(DATA.conferences.length,"Conferences",true)
    + kpi(total,"Attendees captured",true)
    + kpi(domains.size,"Real domains",true)
    + kpi(noise,"Off-domain (noise)",true);

  // charts: attendees per conference + attendees per department
  const byConf = DATA.conferences.map(c=>({label:c.name, n:c.attendees, color:"#0f3460"}));
  const deptCounts = {};
  att.forEach(a=>{ deptCounts[a.department]=(deptCounts[a.department]||0)+1; });
  const deptRows = DEPT_ORDER.filter(d=>deptCounts[d]).map(d=>({label:d, n:deptCounts[d], color:DEPT_COLOR[d]}));
  $("#cf-charts").innerHTML =
    barChart("Attendees per conference", byConf)
    + barChart("Attendees by department", deptRows, "The split between real hiring domains and the Other / Noise crowd that any conference attracts.");

  // tree: department -> conference -> attendees
  const grouped = {};
  att.forEach(a=>{
    (grouped[a.department] = grouped[a.department] || {});
    (grouped[a.department][a.conference] = grouped[a.department][a.conference] || []).push(a);
  });
  const maxDept = Math.max(1, ...DEPT_ORDER.map(d=>grouped[d]?Object.values(grouped[d]).reduce((s,x)=>s+x.length,0):0));
  $("#cf-tree").innerHTML = DEPT_ORDER.filter(d=>grouped[d]).map(d=>{
    const confs = grouped[d];
    const n = Object.values(confs).reduce((s,x)=>s+x.length,0);
    const color = DEPT_COLOR[d];
    const kids = Object.keys(confs).sort().map(cf=>{
      const rows = confs[cf].slice().sort((a,b)=>a.name.localeCompare(b.name)).map(a=>
        '<tr class="clickable" data-conf="'+esc(a.conference)+'"><td><strong>'+esc(a.name)+'</strong></td>'
        +'<td>'+esc(a.title||"—")+'</td><td>'+esc(a.company||"—")+'</td>'
        +'<td>'+(a.years!=null?a.years+" yrs":"—")+'</td>'
        +'<td>'+(a.has_linkedin?'<span class="chip chip-ok">LinkedIn</span>':'<span class="chip chip-miss">No LinkedIn</span>')+'</td></tr>').join("");
      return '<div class="tconf">'+esc(cf)+' <span class="muted">('+confs[cf].length+')</span></div>'
        +'<table class="mini-table"><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Exp</th><th>Profile</th></tr></thead><tbody>'+rows+'</tbody></table>';
    }).join("");
    return '<div class="tnode"><div class="tdept" data-d="'+esc(d)+'">'
      +'<span class="caret">▶</span><span class="dname">'+esc(d)+'</span>'
      +'<span class="dcount">'+n+'</span>'
      +'<span class="dbar"><div style="width:'+Math.round(n/maxDept*100)+'%;background:'+color+'"></div></span></div>'
      +'<div class="tkids">'+kids+'</div></div>';
  }).join("");

  $("#cf-tree").querySelectorAll(".tdept").forEach(h=>h.onclick=()=>{
    h.classList.toggle("open"); h.nextElementSibling.classList.toggle("open");
  });
  $("#cf-tree").querySelectorAll("tr.clickable").forEach(tr=>tr.onclick=(e)=>{
    e.stopPropagation();
    confFilter = tr.dataset.conf; searchTerm=""; kpiFilter=null;
    document.querySelector('.tab[data-page="match"]').click(); renderMatch();
  });
}

// ---- jobs ----
function renderJobs(){
  const tb = $("#jobs-table tbody");
  // Priority order: Hot first, then New, then most strong matches waiting.
  const jobs = DATA.jobs.slice().sort((a,b)=>
    (b.is_hot-a.is_hot) || (b.is_new-a.is_new) || (b.strong_matches-a.strong_matches));
  tb.innerHTML = jobs.map(j=>{
    const rows = computed(j.job_id);
    const strong = rows.filter(r=>r.tier==="Strong Match").length;
    const relevant = rows.filter(r=>r.tier==="Strong Match"||r.tier==="Potential").length;
    const statuses = Object.entries(j.applicants_by_status||{}).map(([k,v])=>esc(k)+": "+v).join(", ");
    const budget = j.budget_max ? '$'+(j.budget_max/1000)+'k' : '—';
    const marks = (j.is_hot?'<span class="mark mark-hot" title="High priority — strong talent waiting and/or hard to fill">🔥 Hot</span>':'')
                + (j.is_new?'<span class="mark mark-new" title="Opened in the last '+DATA.new_role_days+' days">✦ New</span>':'');
    const opened = j.days_open!=null ? '<span class="muted" style="font-size:11px;">opened '+j.days_open+'d ago</span>' : '';
    return '<tr class="clickable" data-job="'+j.job_id+'"><td><strong>'+esc(j.title)+'</strong>'+marks+'<br>'
      +'<span class="muted" style="font-size:11px;">'+esc(j.key_domains.join(" · "))+'</span> '+opened+'</td>'
      +'<td>'+esc(j.department)+'</td><td>'+budget+'</td>'
      +'<td>'+j.internal_applicants+' <span class="muted" style="font-size:11px;">'+(statuses?"("+statuses+")":"")+'</span></td>'
      +'<td><strong style="color:#0d9488;">'+(j.internal_movers||0)+'</strong></td>'
      +'<td><strong style="color:#16a34a;">'+strong+'</strong></td><td>'+relevant+'</td>'
      +'<td><span class="muted">Shortlist →</span></td></tr>';
  }).join("");
  tb.querySelectorAll("tr").forEach(tr=>tr.onclick=()=>{
    currentJob = tr.dataset.job; $("#job-select").value = currentJob;
    confFilter=null; kpiFilter=null;
    document.querySelector('.tab[data-page="match"]').click(); renderMatch();
  });
}

// ---- match ----
function buildSliders(){
  $("#sliders").innerHTML = Object.keys(DATA.weights).map(k=>
    '<div class="slider"><label><span>'+SUB_LABELS[k]+help(META.weights_help[k])+'</span> <span id="wv-'+k+'">'+Math.round(weights[k]*100)+'%</span></label>'
    +'<input type="range" min="0" max="100" value="'+Math.round(weights[k]*100)+'" data-k="'+k+'"></div>').join("");
  $("#sliders").querySelectorAll("input").forEach(inp=>inp.oninput=()=>{
    weights[inp.dataset.k] = (+inp.value)/100;
    $("#wv-"+inp.dataset.k).textContent = inp.value+"%";
    renderMatchTable(); renderMatchKpis(); renderMatchCharts();
  });
}
function renderScoringHelp(){
  const w = DATA.weights;
  const bands = Object.entries(META.seniority_bands).map(([k,v])=>esc(k)+" "+v[0]+"-"+v[1]+" yrs").join(" · ");
  const rels = Object.keys(META.relation_strength)
    .sort((a,b)=>META.relation_strength[b]-META.relation_strength[a])
    .map(k=>'<tr><td>'+esc(META.relation_label[k]||k)+'</td><td><strong>'+Math.round(META.relation_strength[k]*100)+'%</strong></td></tr>').join("");
  $("#sh-body").innerHTML =
    '<p>The score is a <strong>transparent weighted sum of five signals</strong> — no black box. '
    +'Every number below traces back to a concrete fact about the candidate.</p>'
    +'<div class="formula">'+esc(META.formula)+'</div>'
    +'<div class="wexp">'+Object.keys(w).map(k=>
        '<div class="wk">'+SUB_LABELS[k]+' ('+Math.round(w[k]*100)+'%)</div><div class="wd">'+esc(META.weights_help[k])+'</div>').join("")+'</div>'
    +'<p style="margin-top:14px;"><strong>Tiers:</strong> Strong ≥ '+META.tiers.strong+' · Potential '+META.tiers.potential+'-'+(META.tiers.strong-1)
      +' · Low '+META.tiers.low+'-'+(META.tiers.potential-1)+' · Weak &lt; '+META.tiers.low
      +' · <span style="color:#dc2626;">Noise</span> = domain relevance &lt; '+Math.round(DATA.noise_gate*100)+'% (gated out).</p>'
    +'<p style="margin-top:8px;"><strong>Seniority bands:</strong> '+bands+'.</p>'
    +'<p style="margin-top:14px;font-weight:700;color:#0f3460;">Referral strength — quality beats quantity</p>'
    +'<table class="mini-table" style="max-width:340px;margin-top:6px;"><thead><tr><th>Relationship</th><th>Weight</th></tr></thead><tbody>'+rels+'</tbody></table>'
    +'<p style="margin-top:14px;font-weight:700;color:#dc2626;">Penalties (what drags a score down)</p>'
    +'<div class="wexp" style="margin-top:6px;">'+Object.keys(META.penalties).map(k=>
        '<div class="wk" style="color:#b91c1c;">'+(FLAG_STYLE[k]?FLAG_STYLE[k][1]:k)+'</div><div class="wd">'+esc(META.penalties[k])+'</div>').join("")+'</div>';
}
function renderMatch(){
  $("#job-select").innerHTML = DATA.jobs.map(j=>
    '<option value="'+j.job_id+'">'+esc(j.title)+' ('+j.job_id+')</option>').join("");
  $("#job-select").value = currentJob;
  renderMatchKpis(); renderMatchTable();
  renderMatchCharts();
  if (confFilter){
    $("#search").placeholder = "Filtered to: "+confFilter+" — type to search…";
  }
}
function renderMatchCharts(){
  const all = computed(currentJob).filter(r=>!confFilter||r.conference===confFilter);
  const tiers = ["Strong Match","Potential","Low","Weak","Noise (off-domain)"];
  const tierRows = tiers.map(t=>({label:t.replace(" (off-domain)",""), n:all.filter(r=>r.tier===t).length, color:TIER_COLOR[t]}));
  const refRows = [
    {label:"Recommendation", n:all.filter(r=>r.referrals.some(x=>x.relation==="recommendation")).length, color:"#7c3aed"},
    {label:"Worked together", n:all.filter(r=>r.referrals.some(x=>x.relation==="worked_together")).length, color:"#0f766e"},
    {label:"Mutual (same team)", n:all.filter(r=>r.referrals.some(x=>x.relation==="mutual_same_dept")).length, color:"#16a34a"},
    {label:"Any mutual", n:all.filter(r=>r.referrals.length>0).length, color:"#94a3b8"},
    {label:"No warm intro", n:all.filter(r=>r.referrals.length===0 && !r.is_internal).length, color:"#cbd5e1"},
  ];
  $("#mt-charts").innerHTML =
    barChart("Shortlist by tier", tierRows, "How the screened pool splits across match tiers for the current weights. Noise is gated out of the shortlist.")
    + histChart("Score distribution", scoreHistogram(all), "Match-score spread across non-noise candidates. Re-tuning the sliders reshapes this live.")
    + barChart("Warm-intro paths", refRows, "The strongest referral relationship available per candidate — how many arrive with a credible internal reference.");
}
function filteredRows(){
  let rows = computed(currentJob);
  if (confFilter) rows = rows.filter(r=>r.conference===confFilter);
  if (kpiFilter){
    if (kpiFilter==="relevant") rows = rows.filter(r=>r.tier==="Strong Match"||r.tier==="Potential");
    else if (kpiFilter==="intro") rows = rows.filter(r=>r.referrals.length>0 && r.tier!=="Noise (off-domain)");
    else if (kpiFilter==="noise") rows = rows.filter(r=>r.tier==="Noise (off-domain)");
    else if (kpiFilter==="recommended") rows = rows.filter(r=>r.flags.includes("HAS_RECOMMENDATION"));
    else if (kpiFilter==="internal") rows = rows.filter(r=>r.is_internal);
    else rows = rows.filter(r=>r.tier===kpiFilter);
  }
  if (searchTerm){
    const q = searchTerm.toLowerCase();
    rows = rows.filter(r=>{
      const hay = [r.name,r.title,r.company,r.location,r.conference,(r.matched||[]).join(" "),
                   (r.referrals||[]).map(x=>x.name).join(" ")].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }
  rows.sort((a,b)=>{
    let av,bv;
    if (sortKey==="score"){av=a.score;bv=b.score;}
    else if (sortKey==="name"){av=a.name;bv=b.name;}
    else if (sortKey==="tier"){av=a.score;bv=b.score;}
    else if (sortKey==="referral_strength"){av=a.subs.referral_strength;bv=b.subs.referral_strength;}
    else if (sortKey==="skill_match"){av=a.subs.skill_match;bv=b.subs.skill_match;}
    else {av=a.score;bv=b.score;}
    if (av<bv) return -sortDir; if (av>bv) return sortDir; return 0;
  });
  return rows;
}
function renderMatchKpis(){
  const all = computed(currentJob).filter(r=>!confFilter||r.conference===confFilter);
  const relevant = all.filter(r=>r.tier==="Strong Match"||r.tier==="Potential").length;
  const strong = all.filter(r=>r.tier==="Strong Match").length;
  const intro = all.filter(r=>r.referrals.length>0 && r.tier!=="Noise (off-domain)").length;
  const noise = all.filter(r=>r.tier==="Noise (off-domain)").length;
  const rec = all.filter(r=>r.flags.includes("HAS_RECOMMENDATION")).length;
  const internal = all.filter(r=>r.is_internal).length;
  const cards = [
    [all.length,"Screened",null],[relevant,"Relevant","relevant"],[strong,"Strong","Strong Match"],
    [intro,"Warm intro","intro"],[rec,"Recommended","recommended"],[internal,"Internal","internal"],
    [noise,"Noise","noise"]
  ];
  $("#mt-kpis").innerHTML = cards.map(([n,l,f])=>
    '<div class="kpi'+(kpiFilter===f?' active':'')+(f===null?' static':'')+'" data-f="'+(f||"")+'">'
    +'<div class="n">'+n+'</div><div class="l">'+l+'</div></div>').join("");
  $("#mt-kpis").querySelectorAll(".kpi").forEach(c=>{
    if (c.classList.contains("static")) return;
    c.onclick=()=>{
      const f = c.dataset.f;
      kpiFilter = (kpiFilter===f)?null:f; renderMatchKpis(); renderMatchTable();
    };
  });
}
function renderMatchTable(){
  let rows = filteredRows();
  const tb = $("#mt-table tbody");
  tb.innerHTML = rows.map((r,i)=>{
    const tc = TIER_COLOR[r.tier]||"#94a3b8";
    const topRef = r.referrals[0];
    const introTxt = topRef ? (topRef.is_external? esc(topRef.name)+" ↔ "+esc(topRef.bridge) : esc(topRef.name))
                            + ' <span class="muted">('+esc(topRef.relation_label)+')</span>' : '<span class="muted">—</span>';
    const flags = r.flags.filter(f=>FLAG_STYLE[f]).slice(0,3).map(f=>
      '<span class="flag" style="--fc:'+FLAG_STYLE[f][0]+'">'+FLAG_STYLE[f][1]+'</span>').join("");
    const badge = r.is_internal ? '<span class="src-int">Internal</span> ' : '';
    return '<tr class="clickable" data-i="'+i+'"><td>'+(i+1)+'</td>'
      +'<td>'+badge+'<strong>'+esc(r.name)+'</strong><br><span class="muted" style="font-size:11px;">'+esc(r.title)+' · '+esc(r.company)+'</span></td>'
      +'<td><strong style="font-size:15px;color:#0f3460;">'+r.score.toFixed(0)+'</strong></td>'
      +'<td><span class="pill" style="background:'+tc+'">'+esc(r.tier)+'</span></td>'
      +'<td style="font-size:12px;">'+introTxt+'</td>'
      +'<td><span class="barmini"><div style="width:'+Math.round(r.subs.skill_match*100)+'%"></div></span></td>'
      +'<td>'+flags+'</td></tr>';
  }).join("") || '<tr><td colspan="7" class="muted" style="text-align:center;padding:24px;">No candidates match these filters.</td></tr>';
  tb.querySelectorAll("tr.clickable").forEach(tr=>tr.onclick=()=>openDrawer(rows[+tr.dataset.i]));
}

// ---- drawer ----
function metric(k,v){
  const pct=Math.round(v*100);
  return '<div class="metric"><div class="ml"><span>'+SUB_LABELS[k]+'</span><span>'+pct+'%</span></div>'
    +'<div class="mt"><div class="mf" style="width:'+pct+'%"></div></div></div>';
}
function scoreMathHtml(r){
  const wsum = Object.values(weights).reduce((a,b)=>a+b,0)||1;
  let total=0;
  const rows = Object.keys(weights).map(k=>{
    const nw = weights[k]/wsum;
    const pts = nw*(r.subs[k]||0)*100;
    total += pts;
    return '<div class="mathrow"><span class="mlbl">'+SUB_LABELS[k]+'</span>'
      +'<span class="mcalc">'+Math.round(nw*100)+'% × '+Math.round((r.subs[k]||0)*100)+'%</span>'
      +'<span class="mpts">'+pts.toFixed(1)+'</span></div>';
  }).join("");
  return rows + '<div class="mathtot"><span>Match score</span><span>'+total.toFixed(1)+' / 100</span></div>';
}
function signalsHtml(r){
  const boostsMap = {
    HAS_RECOMMENDATION:"Public LinkedIn recommendation — the strongest referral signal.",
    WORKED_WITH_EMPLOYEE:"A current WSC employee worked with them — a first-hand reference.",
    STRONG_REFERRAL:"Same-department warm intro — a credible domain peer can vouch.",
    MOVABLE_SWEET_SPOT:"~2-4 yrs into their role — the sweet spot to be open to a move.",
    INTERNAL_MOBILITY:"Existing employee — fastest, cheapest, lowest-risk hire."
  };
  const boosts = r.flags.filter(f=>boostsMap[f]);
  const pens = r.flags.filter(f=>PENALTY_FLAGS.includes(f));
  let html='';
  if (boosts.length)
    html += boosts.map(f=>'<div class="goodrow">▲ <strong>'+(FLAG_STYLE[f]?FLAG_STYLE[f][1]:f)+'</strong> — '+esc(boostsMap[f])+'</div>').join("");
  if (pens.length)
    html += '<div class="penbox">'+pens.map(f=>
      '<div class="penrow">▼ <strong>'+(FLAG_STYLE[f]?FLAG_STYLE[f][1]:f)+'</strong> — <span class="pd">'+esc(META.penalties[f]||"")+'</span></div>').join("")+'</div>';
  if (!html) html='<span class="muted">No penalties flagged — a clean profile for this role.</span>';
  return html;
}
function openDrawer(r){
  $("#d-name").textContent = r.name;
  $("#d-sub").textContent = r.title+" · "+r.company+" · "+(r.location||"—")+" · "+(r.years!=null?r.years+" yrs":"exp n/a");
  const refs = r.referrals.length ? r.referrals.map(ref=>{
    const rc = REL_COLOR[ref.relation]||"#94a3b8";
    const who = ref.is_external
      ? '<strong>'+esc(ref.name)+'</strong> '+esc(ref.title)+' <span class="muted">— external, connected to '+esc(ref.bridge)+' @ WSC</span>'
      : '<strong>'+esc(ref.name)+'</strong> — '+esc(ref.title)+' <span class="muted">('+esc(ref.dept)+')</span>'+(ref.org?' <span class="muted">@ '+esc(ref.org)+'</span>':'');
    return '<div class="refbox">'+who+'<span class="rel" style="background:'+rc+'">'+esc(ref.relation_label)+'</span>'
      +(ref.note?'<div class="note">“'+esc(ref.note)+'”</div>':'')+'</div>';
  }).join("") : '<span class="muted">No mutual connections or recommendations found.</span>';
  const flags = r.flags.filter(f=>FLAG_STYLE[f]).map(f=>
    '<span class="flag" style="--fc:'+FLAG_STYLE[f][0]+'">'+FLAG_STYLE[f][1]+'</span>').join("");
  const ats = r.ats_status ? '<div class="note-info">In ATS for this role — status: <strong>'+esc(r.ats_status)+'</strong></div>' : '';
  const job = DATA.jobs.find(j=>j.job_id===currentJob) || {};
  const bmax = job.budget_max;
  const budgetBlock = '<div class="seclbl">Budget for this role</div><p style="font-size:13px;">'
    + (bmax? 'HR has <strong>up to $'+(bmax/1000)+'k</strong> allocated for this position.'
           : '<span class="muted">No budget set for this role.</span>')
    + '<br><span class="muted" style="font-size:11px;">This is the HR-side hiring budget. Candidate pay expectations are not tracked here — they are a later negotiation, kept out of the merit score.</span></p>';
  const internalBlock = r.is_internal
    ? '<div class="note-info" style="border-left-color:#0d9488;">🔄 <strong>Internal mobility candidate</strong> — current WSC employee'
      +(r.years!=null?' (~'+r.years+' yrs experience)':'')+' who opted to move into this role. Cheapest, fastest, lowest-risk hire and a retention win.</div>'
    : '';
  $("#d-body").innerHTML =
    '<div>'+flags+'</div>'
    + internalBlock
    + '<div class="seclbl">Score breakdown (current weights)</div>'
    + Object.keys(DATA.weights).map(k=>metric(k, r.subs[k])).join("")
    + '<div class="seclbl">How this score is built '+help("Each signal's contribution = its normalized weight × the candidate's sub-score. The five contributions sum to the match score. Move the sliders to change the weights.",true)+'</div>'
    + scoreMathHtml(r)
    + '<div class="seclbl">Boosts &amp; penalties '+help("An explicit read-out of what lifts or drags this candidate — including penalties that are otherwise baked silently into the sub-scores.",true)+'</div>'
    + signalsHtml(r)
    + ats
    + budgetBlock
    + '<div class="seclbl">Matched skills</div>'+ (r.matched.length? r.matched.map(s=>'<span class="chip chip-ok">'+esc(s)+'</span>').join(""):'<span class="muted">—</span>')
    + '<div class="seclbl">Missing skills</div>'+ (r.missing.length? r.missing.map(s=>'<span class="chip chip-miss">'+esc(s)+'</span>').join(""):'<span class="muted">—</span>')
    + '<div class="seclbl">🤝 References & warm-intro paths</div>'+ refs
    + (r.notes? '<div class="seclbl">Recruiter note</div><div class="note-info">'+esc(r.notes)+'</div>':'')
    + '<div class="seclbl">Tenure</div><p style="font-size:13px;">Current role ~'+(r.current_tenure!=null?r.current_tenure+" yr(s)":"n/a")
      +' · avg past tenure '+(r.avg_tenure!=null?r.avg_tenure+" yr(s)":"n/a")+'</p>'
    + '<div class="action">👉 '+esc(r.recommended)+'</div>';
  $("#drawer").classList.add("open"); $("#drawer-bg").classList.add("open");
}
$("#drawer-close").onclick = $("#drawer-bg").onclick = ()=>{
  $("#drawer").classList.remove("open"); $("#drawer-bg").classList.remove("open");
};

// ---- wiring ----
$("#job-select").onchange = e=>{ currentJob=e.target.value; confFilter=null; kpiFilter=null; renderMatch(); };
$("#search").oninput = e=>{ searchTerm=e.target.value; renderMatchTable(); };
$("#reset-weights").onclick = ()=>{ weights=Object.assign({},DATA.weights); buildSliders(); renderMatchTable(); renderMatchKpis(); renderMatchCharts(); };
document.querySelectorAll("#mt-table th[data-sort]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.sort; if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=-1;} renderMatchTable();
});

// ---- init ----
renderOverview(); renderConferences(); renderJobs(); renderScoringHelp(); buildSliders(); renderMatch();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
