"""
report.py — Render the ranked results as a recruiter-friendly HTML page.

This is the "bonus" deliverable: a view a recruiter can actually use without
opening a spreadsheet. It is a single self-contained HTML file (no external
assets) so it can be emailed or opened straight from disk.
"""

from __future__ import annotations

import html
from typing import List

from loader import Job
from scoring import ScoredCandidate

_TIER_COLOR = {
    "Strong Match": "#16a34a",
    "Potential": "#f59e0b",
    "Low": "#94a3b8",
    "Weak": "#cbd5e1",
    "Noise (off-domain)": "#ef4444",
}

_FLAG_STYLE = {
    "WORKED_WITH_EMPLOYEE": ("#0f766e", "Worked with an employee"),
    "STRONG_REFERRAL": ("#16a34a", "Warm intro"),
    "MOVABLE_SWEET_SPOT": ("#2563eb", "Movable (2-4 yrs)"),
    "RECENTLY_STARTED": ("#94a3b8", "Recently started"),
    "JOB_HOPPER": ("#ef4444", "Frequent job changes"),
    "MISSING_LINKEDIN": ("#ef4444", "No LinkedIn"),
    "NO_MUTUAL_CONNECTION": ("#94a3b8", "No mutual connection"),
    "PARTIAL_SKILLS": ("#f59e0b", "Partial skills"),
    "OFF_DOMAIN": ("#ef4444", "Off-domain"),
}

# Relation type -> small colored tag shown next to each referrer.
_RELATION_STYLE = {
    "worked_together": ("#0f766e", "worked together"),
    "mutual_same_dept": ("#16a34a", "same team"),
    "same_org": ("#6366f1", "shared employer/school"),
    "mutual": ("#94a3b8", "mutual"),
}


def _e(text: str) -> str:
    return html.escape(text or "")


def _bar(label: str, value: float) -> str:
    pct = round(value * 100)
    return f"""
      <div class="metric">
        <div class="metric-label"><span>{_e(label)}</span><span>{pct}%</span></div>
        <div class="metric-track"><div class="metric-fill" style="width:{pct}%"></div></div>
      </div>"""


def _chips(items: List[str], kind: str) -> str:
    if not items:
        return '<span class="muted">—</span>'
    return "".join(f'<span class="chip chip-{kind}">{_e(i)}</span>' for i in items)


def _card(rank: int, sc: ScoredCandidate) -> str:
    c = sc.candidate
    tier_color = _TIER_COLOR.get(sc.tier, "#94a3b8")

    referrals_html = '<span class="muted">No mutual connections</span>'
    if sc.referrals:
        rows = []
        for r in sc.referrals:
            rc, rlabel = _RELATION_STYLE.get(r.relation, ("#94a3b8", "mutual"))
            org = f' @ {_e(r.shared_org)}' if r.shared_org else ""
            rows.append(
                f'<li><strong>{_e(r.employee_name)}</strong> — {_e(r.employee_title)}'
                f' <span class="muted">({_e(r.department)})</span>'
                f' <span class="rel" style="--rc:{rc}">{rlabel}{org}</span></li>'
            )
        referrals_html = f'<ul class="refs">{"".join(rows)}</ul>'

    flags_html = "".join(
        f'<span class="flag" style="--fc:{_FLAG_STYLE[f][0]}">{_FLAG_STYLE[f][1]}</span>'
        for f in sc.flags if f in _FLAG_STYLE
    )

    yrs = f"{c.years_experience} yrs" if c.years_experience is not None else "exp. n/a"
    notes_html = f'<p class="notes">📝 {_e(c.notes)}</p>' if c.notes else ""

    return f"""
    <div class="card">
      <div class="card-head">
        <div class="rank">#{rank}</div>
        <div class="who">
          <h3>{_e(c.full_name)}</h3>
          <p>{_e(sc.candidate.best_title)} · {_e(c.current_company or c.company)} · {_e(c.location or "location n/a")} · {yrs}</p>
          <p class="src">{_e(c.conference_name)} ({_e(c.conference_date)})</p>
        </div>
        <div class="score-box">
          <div class="score">{sc.match_score:.0f}</div>
          <div class="tier" style="background:{tier_color}">{_e(sc.tier)}</div>
        </div>
      </div>

      <div class="flags">{flags_html}</div>

      <div class="metrics">
        {_bar("Skill match", sc.skill_match)}
        {_bar("Domain relevance", sc.domain_relevance)}
        {_bar("Seniority fit", sc.seniority_fit)}
        {_bar("Referral strength", sc.referral_strength)}
        {_bar("Stability / movability", sc.stability)}
      </div>

      <div class="grid2">
        <div>
          <div class="section-label">Matched skills</div>
          {_chips(sc.matched_skills, "ok")}
          <div class="section-label" style="margin-top:10px;">Missing skills</div>
          {_chips(sc.missing_skills, "miss")}
        </div>
        <div>
          <div class="section-label">🤝 Internal references</div>
          {referrals_html}
        </div>
      </div>

      {notes_html}
      <div class="action">👉 {_e(sc.recommended_action)}</div>
    </div>"""


def render_html(job: Job, scored: List[ScoredCandidate], shown: int = 15) -> str:
    total = len(scored)
    real = [s for s in scored if s.tier in ("Strong Match", "Potential")]
    noise = [s for s in scored if s.tier == "Noise (off-domain)"]
    strong = [s for s in scored if s.tier == "Strong Match"]
    with_ref = [s for s in real if s.referrals]
    top = scored[:shown]

    cards = "".join(_card(i + 1, sc) for i, sc in enumerate(top))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Candidate Shortlist — {_e(job.title)}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#f1f5f9; color:#1e293b; line-height:1.55; }}
  .hero {{ background:linear-gradient(135deg,#0f3460,#16213e); color:#fff; padding:32px 40px; }}
  .hero .tag {{ font-size:12px; letter-spacing:1px; text-transform:uppercase; opacity:.7; }}
  .hero h1 {{ font-size:26px; margin:6px 0; }}
  .hero p {{ opacity:.75; font-size:14px; }}
  .stats {{ display:flex; gap:14px; flex-wrap:wrap; padding:20px 40px; background:#fff;
           border-bottom:1px solid #e2e8f0; }}
  .stat {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px; padding:12px 18px; min-width:130px; }}
  .stat .n {{ font-size:24px; font-weight:700; color:#0f3460; }}
  .stat .l {{ font-size:12px; color:#64748b; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:24px 20px 60px; }}
  .card {{ background:#fff; border:1px solid #e2e8f0; border-radius:14px; padding:20px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.04); }}
  .card-head {{ display:flex; gap:14px; align-items:flex-start; }}
  .rank {{ font-size:13px; font-weight:700; color:#94a3b8; padding-top:4px; }}
  .who {{ flex:1; }}
  .who h3 {{ font-size:18px; }}
  .who p {{ font-size:13px; color:#475569; }}
  .who .src {{ color:#94a3b8; font-size:12px; }}
  .score-box {{ text-align:center; }}
  .score {{ font-size:30px; font-weight:800; color:#0f3460; }}
  .tier {{ color:#fff; font-size:11px; font-weight:700; border-radius:20px; padding:2px 10px; display:inline-block; }}
  .flags {{ display:flex; gap:6px; flex-wrap:wrap; margin:10px 0; }}
  .flag {{ font-size:11px; font-weight:600; color:var(--fc); border:1px solid var(--fc);
          border-radius:20px; padding:1px 9px; background:color-mix(in srgb, var(--fc) 8%, white); }}
  .metrics {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 22px; margin:12px 0; }}
  .metric-label {{ display:flex; justify-content:space-between; font-size:12px; color:#475569; }}
  .metric-track {{ background:#e2e8f0; border-radius:6px; height:7px; margin-top:2px; }}
  .metric-fill {{ background:#0f3460; height:7px; border-radius:6px; }}
  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-top:12px;
           border-top:1px solid #f1f5f9; padding-top:12px; }}
  .section-label {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#94a3b8;
                   font-weight:700; margin-bottom:6px; }}
  .chip {{ display:inline-block; font-size:12px; border-radius:6px; padding:2px 8px; margin:0 4px 4px 0; }}
  .chip-ok {{ background:#dcfce7; color:#166534; }}
  .chip-miss {{ background:#fee2e2; color:#991b1b; }}
  .refs {{ list-style:none; font-size:13px; }}
  .refs li {{ padding:3px 0; }}
  .rel {{ font-size:11px; font-weight:600; color:var(--rc); border:1px solid var(--rc);
         border-radius:20px; padding:0 7px; margin-left:4px;
         background:color-mix(in srgb, var(--rc) 8%, white); white-space:nowrap; }}
  .muted {{ color:#94a3b8; font-size:13px; }}
  .notes {{ background:#fffbeb; border-left:3px solid #f59e0b; padding:8px 12px; border-radius:6px;
           font-size:13px; margin-top:12px; color:#78350f; }}
  .action {{ margin-top:12px; background:#eff6ff; border:1px solid #bfdbfe; color:#1e3a8a;
            border-radius:8px; padding:10px 14px; font-size:14px; font-weight:600; }}
</style></head><body>
  <div class="hero">
    <div class="tag">Talent Pool · Candidate Shortlist</div>
    <h1>{_e(job.title)} <span style="opacity:.6;font-size:16px;">({_e(job.job_id)})</span></h1>
    <p>{_e(job.department)} · {_e(job.seniority)} · domains: {_e(", ".join(job.key_domains))}</p>
  </div>
  <div class="stats">
    <div class="stat"><div class="n">{total}</div><div class="l">Attendees screened</div></div>
    <div class="stat"><div class="n">{len(real)}</div><div class="l">Relevant candidates</div></div>
    <div class="stat"><div class="n">{len(strong)}</div><div class="l">Strong matches</div></div>
    <div class="stat"><div class="n">{len(with_ref)}</div><div class="l">With warm intro</div></div>
    <div class="stat"><div class="n">{len(noise)}</div><div class="l">Filtered as noise</div></div>
  </div>
  <div class="wrap">
    <p class="muted" style="margin-bottom:14px;">Showing top {len(top)} of {total} screened attendees, ranked by match score.</p>
    {cards}
  </div>
</body></html>"""
