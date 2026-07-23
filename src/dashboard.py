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
from typing import Dict, List

from loader import (
    Candidate, load_candidates, load_employees, load_internal_applications, load_jobs,
    load_internal_candidates, load_job_budgets,
)
from scoring import WEIGHTS, score_all


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
        budget_max = (budgets.get(job_id) or {}).get("budget_max")
        # Internal movers who opted into *this* role join the same scoring pool.
        pool = candidates + [ic for ic in internal_candidates if ic.target_job_id == job_id]
        scored = score_all(pool, job, employees)
        rows = []
        for sc in scored:
            c = sc.candidate
            over_budget = bool(c.expected_salary and budget_max and c.expected_salary > budget_max)
            flags = list(sc.flags)
            if over_budget:
                flags.append("OVER_BUDGET")
            rows.append({
                "hubspot_id": c.hubspot_id,
                "name": c.full_name,
                "title": c.best_title,
                "company": c.current_company or c.company,
                "location": c.location,
                "years": c.years_experience,
                "conference": c.conference_name,
                "conference_date": c.conference_date,
                "source": "Internal Mobility" if c.is_internal else "Conference",
                "is_internal": c.is_internal,
                "expected_salary": c.expected_salary,
                "salary_currency": c.salary_currency,
                "over_budget": over_budget,
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
                "flags": flags,
                "ats_status": _ats_status_for(c, job_id),
                "recommended": sc.recommended_action,
                "notes": c.notes,
                "current_tenure": c.current_tenure_years,
                "avg_tenure": round(c.avg_past_tenure_years, 1) if c.avg_past_tenure_years else None,
                "email": c.email,
                "linkedin_url": c.linkedin_url,
            })
        candidates_by_job[job_id] = rows

        job_apps = apps_by_job.get(job_id, [])
        status_counts = Counter(a.get("ats_status", "") for a in job_apps)
        budget = budgets.get(job_id) or {}
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
        })

    return {
        "weights": WEIGHTS,
        "conferences": _conferences(candidates),
        "employees": [
            {"id": e.employee_id, "name": e.full_name, "title": e.title, "department": e.department}
            for e in employees.values()
        ],
        "jobs": jobs_json,
        "candidates_by_job": candidates_by_job,
        "noise_gate": 0.25,
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
  <h2>Conferences</h2>
  <p class="sub">Where we met the talent. Click a conference to see its attendees in the Match tab.</p>
  <div class="kpis" id="cf-kpis"></div>
  <table id="cf-table"><thead><tr>
    <th data-sort="name">Conference</th><th data-sort="date">Date</th>
    <th data-sort="domain">Domain</th><th data-sort="attendees">Attendees</th>
  </tr></thead><tbody></tbody></table>
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
  INTERNAL_MOBILITY:["#0d9488","Internal mover"], OVER_BUDGET:["#dc2626","Over budget"],
  HAS_RECOMMENDATION:["#7c3aed","Recommended"], WORKED_WITH_EMPLOYEE:["#0f766e","Worked w/ employee"],
  STRONG_REFERRAL:["#16a34a","Warm intro"], MOVABLE_SWEET_SPOT:["#2563eb","Movable"],
  RECENTLY_STARTED:["#94a3b8","Just started"], JOB_HOPPER:["#ef4444","Job hopper"],
  ALREADY_APPLIED:["#0891b2","Already applied"], PREVIOUSLY_REJECTED:["#ef4444","Previously rejected"],
  MISSING_LINKEDIN:["#ef4444","No LinkedIn"], PARTIAL_SKILLS:["#f59e0b","Partial skills"],
  NO_MUTUAL_CONNECTION:["#94a3b8","No connection"], OFF_DOMAIN:["#ef4444","Off-domain"]
};
const SUB_LABELS = { skill_match:"Skills", domain_relevance:"Domain", seniority_fit:"Seniority", referral_strength:"Referral", stability:"Stability" };

let weights = Object.assign({}, DATA.weights);
let currentJob = DATA.jobs[0].job_id;
let kpiFilter = null;      // tier filter from KPI card
let searchTerm = "";
let sortKey = "score", sortDir = -1;
let confFilter = null;

const $ = s => document.querySelector(s);
const esc = s => (s==null?"":(""+s)).replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

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
}
function kpi(n,l,isStatic,active){
  return '<div class="kpi'+(isStatic?' static':'')+(active?' active':'')+'" '
    +(l?'data-l="'+esc(l)+'"':'')+'><div class="n">'+n+'</div><div class="l">'+esc(l)+'</div></div>';
}

// ---- conferences ----
function renderConferences(){
  const total = DATA.conferences.reduce((a,c)=>a+c.attendees,0);
  $("#cf-kpis").innerHTML = kpi(DATA.conferences.length,"Conferences",true)+kpi(total,"Total attendees",true);
  const tb = $("#cf-table tbody");
  tb.innerHTML = DATA.conferences.map(c=>
    '<tr class="clickable" data-conf="'+esc(c.name)+'"><td><strong>'+esc(c.name)+'</strong></td><td>'+esc(c.date)
    +'</td><td>'+esc(c.domain)+'</td><td>'+c.attendees+'</td></tr>').join("");
  tb.querySelectorAll("tr").forEach(tr=>tr.onclick=()=>{
    confFilter = tr.dataset.conf; searchTerm=""; $("#search").value="";
    document.querySelector('.tab[data-page="match"]').click(); renderMatch();
  });
}

// ---- jobs ----
function renderJobs(){
  const tb = $("#jobs-table tbody");
  tb.innerHTML = DATA.jobs.map(j=>{
    const rows = computed(j.job_id);
    const strong = rows.filter(r=>r.tier==="Strong Match").length;
    const relevant = rows.filter(r=>r.tier==="Strong Match"||r.tier==="Potential").length;
    const statuses = Object.entries(j.applicants_by_status||{}).map(([k,v])=>esc(k)+": "+v).join(", ");
    const budget = j.budget_max ? '$'+(j.budget_max/1000)+'k' : '—';
    return '<tr class="clickable" data-job="'+j.job_id+'"><td><strong>'+esc(j.title)+'</strong><br>'
      +'<span class="muted" style="font-size:11px;">'+esc(j.key_domains.join(" · "))+'</span></td>'
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
    '<div class="slider"><label>'+SUB_LABELS[k]+' <span id="wv-'+k+'">'+Math.round(weights[k]*100)+'%</span></label>'
    +'<input type="range" min="0" max="100" value="'+Math.round(weights[k]*100)+'" data-k="'+k+'"></div>').join("");
  $("#sliders").querySelectorAll("input").forEach(inp=>inp.oninput=()=>{
    weights[inp.dataset.k] = (+inp.value)/100;
    $("#wv-"+inp.dataset.k).textContent = inp.value+"%";
    renderMatchTable(); renderMatchKpis();
  });
}
function renderMatch(){
  $("#job-select").innerHTML = DATA.jobs.map(j=>
    '<option value="'+j.job_id+'">'+esc(j.title)+' ('+j.job_id+')</option>').join("");
  $("#job-select").value = currentJob;
  renderMatchKpis(); renderMatchTable();
  if (confFilter){
    $("#search").placeholder = "Filtered to: "+confFilter+" — type to search…";
  }
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
    else if (kpiFilter==="over_budget") rows = rows.filter(r=>r.over_budget);
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
  const over = all.filter(r=>r.over_budget).length;
  const cards = [
    [all.length,"Screened",null],[relevant,"Relevant","relevant"],[strong,"Strong","Strong Match"],
    [intro,"Warm intro","intro"],[rec,"Recommended","recommended"],[internal,"Internal","internal"],
    [over,"Over budget","over_budget"],[noise,"Noise","noise"]
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
    const sal = r.expected_salary ? '<span class="muted" style="font-size:11px;"> · $'+(r.expected_salary/1000)+'k'
                 +(r.over_budget?' <span style="color:#dc2626;">over budget</span>':'')+'</span>' : '';
    return '<tr class="clickable" data-i="'+i+'"><td>'+(i+1)+'</td>'
      +'<td>'+badge+'<strong>'+esc(r.name)+'</strong><br><span class="muted" style="font-size:11px;">'+esc(r.title)+' · '+esc(r.company)+'</span>'+sal+'</td>'
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
  let salaryBlock = '';
  const bmax = job.budget_max;
  if (r.expected_salary){
    const fit = (bmax && r.over_budget)
      ? '<span style="color:#dc2626;font-weight:600;">$'+(r.expected_salary/1000)+'k expected — over the $'+(bmax/1000)+'k ceiling</span>'
      : (bmax? '<span style="color:#16a34a;font-weight:600;">$'+(r.expected_salary/1000)+'k expected — within the $'+(bmax/1000)+'k budget</span>'
             : '$'+(r.expected_salary/1000)+'k expected');
    const src = r.is_internal ? 'Known internal compensation.' : 'Disclosed during a recruiter phone screen.';
    salaryBlock = '<div class="seclbl">Salary vs. budget</div><p style="font-size:13px;">'+fit
      +'<br><span class="muted" style="font-size:11px;">'+src+' A business constraint shown alongside the merit score — never folded into it.</span></p>';
  } else {
    salaryBlock = '<div class="seclbl">Salary vs. budget</div><p style="font-size:13px;"><span class="muted">Not yet known</span>'
      + (bmax? ' — role budget is up to <strong>$'+(bmax/1000)+'k</strong>.':'')
      + '<br><span class="muted" style="font-size:11px;">Salary expectation only surfaces once a recruiter speaks with the candidate; it is not known for a passive pool lead.</span></p>';
  }
  const internalBlock = r.is_internal
    ? '<div class="note-info" style="border-left-color:#0d9488;">🔄 <strong>Internal mobility candidate</strong> — current WSC employee'
      +(r.years!=null?' (~'+r.years+' yrs experience)':'')+' who opted to move into this role. Cheapest, fastest, lowest-risk hire and a retention win.</div>'
    : '';
  $("#d-body").innerHTML =
    '<div>'+flags+'</div>'
    + internalBlock
    + '<div class="seclbl">Score breakdown (current weights)</div>'
    + Object.keys(DATA.weights).map(k=>metric(k, r.subs[k])).join("")
    + ats
    + salaryBlock
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
$("#reset-weights").onclick = ()=>{ weights=Object.assign({},DATA.weights); buildSliders(); renderMatchTable(); renderMatchKpis(); };
document.querySelectorAll("#mt-table th[data-sort]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.sort; if(sortKey===k) sortDir*=-1; else {sortKey=k; sortDir=-1;} renderMatchTable();
});
document.querySelectorAll("#cf-table th[data-sort]").forEach(th=>th.onclick=()=>{
  const k=th.dataset.sort;
  DATA.conferences.sort((a,b)=> (a[k]<b[k]?-1:a[k]>b[k]?1:0));
  renderConferences();
});

// ---- init ----
renderOverview(); renderConferences(); renderJobs(); buildSliders(); renderMatch();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
