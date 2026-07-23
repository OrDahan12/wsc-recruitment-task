# Conference Talent-Pool Matching Pipeline

Turn conference attendee lists into a **living, queryable talent pool** — so that when a role
opens, a recruiter instantly gets a ranked shortlist of the right people, complete with a
**warm-intro path through current employees**.

Built for the *AI Solution Manager* take-home task.

> **Start here:** the recruiter-facing deliverable is the interactive **`output/dashboard.html`**
> — a single self-contained page (no server, no install) with tabs for Conferences, Open Jobs,
> and an interactive Candidate Match view where clicking the KPIs filters the shortlist and
> **weight sliders re-rank candidates live**. Build it with `python src/dashboard.py`.

---

## TL;DR — for the non-technical reader

> Today, the people our recruiters meet at conferences are lost within days — business cards,
> phone notes, badge scans that go nowhere. This pipeline captures every attendee as a
> structured lead, uses their LinkedIn profile to tell **real domain experts apart from the
> noise** (an ML conference is full of ML engineers *and* IT managers and sales reps), and when
> a job opens it produces a ranked shortlist. Crucially, it also tells the recruiter **which
> current employee can make a warm introduction** — the referrals WSC values most.
>
> For *Senior ML Engineer (JOB001)*: 75 attendees screened → **22 filtered out as noise** →
> **27 relevant candidates**, **6 strong matches**, all with a named internal referrer.

---

## What it does (end-to-end flow)

```
  Conference          Capture &            Enrich              Match to role         Recruiter
  attendees    ─────► structure    ─────►  w/ LinkedIn  ─────► (this pipeline) ─────► shortlist
  (badge scan)        (HubSpot)            (skills,             score + rank          CSV + HTML
                                            mutuals)            + filter noise        + warm intros
```

1. **Load & join** — conference attendees are left-joined with their LinkedIn profiles and the
   employee roster.
2. **Score** each attendee against a selected `job_id` on five transparent signals:
   - **Skill match** — overlap with the role's required / nice-to-have skills.
   - **Domain relevance** — is this person genuinely in the role's domain, or conference noise?
   - **Seniority fit** — years of experience vs. the seniority the role expects.
   - **Referral strength** — warm-intro potential, ranked by **relationship quality**: an
     employee who *worked with* the candidate (overlapping years at the same company) outranks a
     generic mutual connection; same-team connections count for more.
   - **Stability / movability** — retention signal: penalizes job-hoppers and factors how likely
     the person is to move (someone ~2–4 years into a role is at the sweet spot).
3. **Rank & filter** — off-domain attendees are gated out as `Noise`; the rest are tiered
   Strong / Potential / Low / Weak.
4. **Output** — a recruiter-ready CSV and a self-contained HTML shortlist view.

---

## Project structure

```
recruitment-task/
├── data/                       # provided CSV exports (simulated end-state)
│   ├── conference_attendees.csv
│   ├── linkedin_profiles.csv
│   ├── wsc_employees.csv
│   ├── job_openings.csv
│   ├── internal_applications.csv    # (generated) Comeet-style ATS applications
│   ├── linkedin_recommendations.csv # (generated) LinkedIn recommendations + bridges
│   ├── internal_candidates.csv      # (generated) employees open to an internal move
│   ├── salary_expectations.csv      # (generated) candidate salary expectations
│   └── job_budgets.csv              # (generated) per-role budget ceilings
├── src/
│   ├── loader.py               # load + join CSVs into Candidate records
│   ├── scoring.py              # transparent scoring logic (the "brain")
│   ├── report.py               # static per-job HTML shortlist renderer
│   ├── dashboard.py            # interactive multi-tab HR dashboard (primary view)
│   ├── make_mock_data.py       # generates the two simulated CSVs above
│   └── pipeline.py             # CLI entry point / orchestration
├── output/                     # generated results
│   ├── dashboard.html          # interactive dashboard (all jobs)
│   ├── JOB001_candidates.csv
│   └── JOB001_report.html
├── DESIGN.md                   # design decisions, production plan, assumptions
└── README.md
```

---

## Setup & run

**No dependencies to install.** The pipeline is pure Python standard library (Python 3.8+).

```bash
# from the recruitment-task/ folder

# 1. (once) generate the simulated ATS + LinkedIn-recommendation data
python src/make_mock_data.py

# 2. build the interactive HR dashboard (recommended starting point)
python src/dashboard.py            # -> output/dashboard.html  (open in any browser)

# 3. or run the per-job CLI shortlist
python src/pipeline.py --job JOB001
```

`dashboard.py` scores every candidate against all open roles and embeds the result in one
self-contained HTML file. `pipeline.py` writes `output/JOB001_candidates.csv` and
`output/JOB001_report.html` and prints a summary.

### Options

| Flag              | Default            | Description                                          |
|-------------------|--------------------|------------------------------------------------------|
| `--job`           | *(required)*       | Job id to match against: `JOB001`–`JOB004`.          |
| `--data`          | `../data`          | Path to the data folder.                             |
| `--out`           | `../output`        | Output folder.                                       |
| `--top`           | `15`               | Number of candidates shown in the HTML view.         |
| `--include-noise` | off                | Also write off-domain rows to the CSV (audit mode).  |

Examples:
```bash
python src/pipeline.py --job JOB003              # Senior Product Manager
python src/pipeline.py --job JOB001 --top 10     # top 10 in the HTML view
```

---

## Output format

`output/<job_id>_candidates.csv` — one ranked row per candidate (noise excluded by default):

| Column | Meaning |
|---|---|
| `rank`, `match_score`, `tier` | Overall ranking, 0–100 score, and Strong/Potential/Low/Weak label |
| `full_name`, `current_title`, `current_company`, `location`, `years_experience` | Who they are |
| `conference_name`, `conference_date` | Where we met them |
| `skill_match_pct`, `domain_relevance_pct`, `seniority_fit_pct`, `referral_strength_pct`, `stability_pct` | The five sub-scores, transparently |
| `matched_skills`, `missing_skills` | Exactly which required skills they have / lack |
| `top_referral_type`, `mutual_connections`, `worked_with_employee`, `has_same_dept_referral` | The warm-intro path, ranked by relationship quality (who worked with them, at which company) |
| `current_tenure_years`, `avg_past_tenure_years` | Retention / movability context |
| `recommended_action` | A plain-language next step for the recruiter |
| `flags` | e.g. `WORKED_WITH_EMPLOYEE`, `STRONG_REFERRAL`, `JOB_HOPPER`, `MOVABLE_SWEET_SPOT`, `PARTIAL_SKILLS` |
| `email`, `linkedin_url` | Contact / verification |

The **HTML report** presents the same information as ranked candidate cards with score bars,
matched/missing skill chips, and highlighted internal referrers — usable without a spreadsheet.

---

## Scoring at a glance

```
match_score = 100 × ( 0.35·skill_match + 0.25·domain_relevance + 0.10·seniority_fit
                      + 0.20·referral_strength + 0.10·stability )
```

`domain_relevance` additionally acts as a **gate**: below 0.25 the candidate is labeled
`Noise (off-domain)` and excluded from the shortlist — this is the signal-to-noise filter that
is the whole point of the exercise. Full rationale, weights, and tuning knobs live in
[`src/scoring.py`](src/scoring.py) and [`DESIGN.md`](DESIGN.md).

The logic is deliberately **rule-based and explainable** — every number a recruiter sees traces
back to a concrete signal, not a black box. See DESIGN.md for where an LLM adds value in
production (and where it deliberately does not).

---

## Edge cases handled

- **No LinkedIn profile for an attendee** → kept with `has_linkedin=False`, scored on
  conference data alone, flagged `MISSING_LINKEDIN` (not silently dropped).
- **No mutual connections** → referral score 0, flagged `NO_MUTUAL_CONNECTION`; the candidate is
  still ranked on merit.
- **Missing / unparseable years of experience** → neutral seniority score (0.5), never a crash.
- **Off-domain attendees** (finance/IT/sales at a sports-tech event) → gated out as noise.
- **Partial skill match** → surfaced explicitly (`matched_skills` vs `missing_skills`) rather
  than hidden behind a single number.
- **"Worked together" ≠ automatic endorsement** → an employee who overlapped with the candidate
  is surfaced as a *reference to verify*, not a guaranteed positive (coworkers don't always get
  along). The explicit-positive signal (LinkedIn recommendations) is on the roadmap — see DESIGN.md §4a.
- **Job-hoppers / recent movers** → flagged (`JOB_HOPPER`, `RECENTLY_STARTED`) so a high skill
  score doesn't hide a retention risk.
- **Internal-mobility candidates** → existing employees who opted to move are scored through the
  same engine but badged **Internal**, prioritised (cheapest/fastest/lowest-risk hire) and
  filterable — not mixed in with cold external leads.
- **Salary over budget** → shown *alongside* the merit score (never folded into it) and flagged
  `OVER_BUDGET` — but only for candidates a recruiter has actually screened (it is *not* known
  for a passive pool lead; those show "not yet known"), so a top scorer who wants more than the
  role can afford doesn't hide the next-best candidate who fits the budget.

See [`DESIGN.md`](DESIGN.md) for the full assumptions list and the production architecture.
