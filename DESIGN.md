# Design Document

*AI Solution Manager take-home — Conference Talent-Pool Matching Pipeline*

---

## 1. The problem, framed correctly

The task is not "match CSVs." The real pain is **signal-to-noise in recruiting**:

- Conference contacts are captured **nowhere** — they evaporate within days.
- Even when captured, a conference crowd is **mixed**: a DevOps summit has SREs *and* IT
  managers *and* vendor sales reps. Recruiters can't screen everyone by hand.
- When a role opens, there's **no way to look back** at the hundreds of relevant people already
  met — and no way to know **who inside the company could vouch for them**.

So the system must do four things, in order: **capture → enrich → filter to signal → surface the
right people (with a warm intro)**. Everything below serves that framing.

---

## 2. Why this approach

**Rule-based, transparent scoring — not a black-box model.** For a recruiting tool, *trust and
explainability beat marginal accuracy*. A recruiter must be able to answer a hiring manager's
"why is this person #1?" The pipeline gives four visible sub-scores and the exact
matched/missing skills behind every ranking. This is also defensible for bias/fairness review.

**Skills as the primary signal, conference domain as a weak prior.** Being *at* a sports-tech
conference barely moves the score — otherwise all 40 attendees at that event would look
"relevant" and we'd recreate the noise problem. Actual skills and title carry the weight.

**Domain relevance as a gate, not just a weight.** A finance engineer with none of the domain
keywords is *filtered out*, not merely down-ranked. That filtering is the core value.

**Referrals treated as a first-class, prominently surfaced signal.** WSC especially values
candidates who arrive with a reference from someone who worked with them. So the pipeline not
only scores referral strength, it **names the employee**, and a **same-department** connection
(a domain peer whose opinion is more credible) is weighted higher and flagged `STRONG_REFERRAL`.

**Zero dependencies.** Pure standard-library Python so a reviewer can run it with one command,
no environment setup. The modules (`loader` / `scoring` / `report` / `pipeline`) are cleanly
separated so another engineer can extend or swap any layer.

---

## 3. Scoring methodology (transparent)

Five sub-scores, each normalized to 0..1:

| Sub-score | How it's computed |
|---|---|
| **skill_match** | `0.8 × (required skills matched / required) + 0.2 × (nice-to-have matched / nice-to-have)`. Matching is on both full skill phrases and word tokens, so *Computer Vision* matches a *Computer Vision Engineer*. |
| **domain_relevance** | `0.6 × skill_match + 0.3 × domain-keyword hits + 0.1 × conference-domain alignment`. This is also the **noise gate** (< 0.25 ⇒ filtered). |
| **seniority_fit** | Years of experience vs. an expected band per seniority (e.g. Senior = 6–14 yrs). Full credit inside the band, graceful decay outside; unknown ⇒ neutral 0.5. |
| **referral_strength** | Ranked by **relationship quality, not count** — see the hierarchy below. Strongest relation leads; extra connections add a small bonus. |
| **stability** | Retention signal: `0.5 × tenure-stability + 0.5 × movability`. Penalizes job-hoppers (avg past tenure < 1.5 yrs) and factors *movability* — someone ~2–4 yrs into a role is at the sweet spot to move; someone who just started is less likely to. |

Final: `match_score = 100 × (0.35·skill + 0.25·domain + 0.10·seniority + 0.20·referral + 0.10·stability)`.

Tiers: **Strong** ≥ 70 · **Potential** 50–69 · **Low** 30–49 · **Weak** < 30 · **Noise** (gated).

### Referral hierarchy (relationship quality)

A warm intro is only as good as the relationship behind it. We rank the connection
type rather than counting connections:

| Relation | Weight | Why |
|---|---|---|
| **Worked together** (same employer *with overlapping years*) | 0.95 | A current employee was an actual colleague — they can give a **first-hand reference**. Detected by cross-referencing candidate work history against employee work history. |
| **Mutual, same team** | 0.75 | A shared connection who is a domain peer of the role. |
| **Shared employer / school** (same org, no time overlap) | 0.55 | Same alumni network — warm, but not first-hand. |
| **Mutual connection** | 0.45 | A generic shared LinkedIn connection. |

> **Important nuance (a real recruiter's caution):** "worked together" is a
> *reference **opportunity**, not a guaranteed endorsement*. Two people can
> overlap at a company and simply not have clicked — which says little about the
> candidate's fit. So the system treats it as "someone who can speak first-hand,
> **verify it was a positive relationship**", never as automatic approval. The
> truly positive, explicit signal is a **LinkedIn recommendation** — see § 4a.

### Stability & movability (retention signal)

From parsed employment date ranges we derive two things a seasoned recruiter weighs:
- **Job-hopping**: someone changing roles every 6–12 months is a higher retention risk
  (`JOB_HOPPER` flag, score penalty). ~2–3 years average tenure is healthy.
- **Movability**: someone who *just started* a role is less likely to move
  (`RECENTLY_STARTED`); the ~2–4 year mark is the sweet spot (`MOVABLE_SWEET_SPOT`).

All weights, bands, thresholds and relation strengths are constants at the top of
[`scoring.py`](src/scoring.py) — trivially tunable, and a natural place to later learn weights
from recruiter feedback (see §6).

---

## 4. What this would look like in production

The provided CSVs simulate the **end state** of a real system. Here's how each mock becomes a
live integration:

| Mock CSV | Real source | How it's built |
|---|---|---|
| `conference_attendees.csv` | **HubSpot** contacts | Badge-scan / event-list exports (or Zapier/Make) land contacts in HubSpot with a `conference` source tag. A HubSpot webhook triggers the pipeline on new conference contacts. |
| `linkedin_profiles.csv` | **LinkedIn / enrichment API** | Enrich each contact via LinkedIn's API or a compliant provider (e.g. Proxycurl). Cache results; refresh on a cadence. Store skills, tenure, mutual connections. |
| `wsc_employees.csv` | **HR system + LinkedIn company graph** | Employee roster synced from HRIS; mutual-connection data from LinkedIn's company connections. |
| `job_openings.csv` | **Comeet** ATS | Open requisitions pulled via Comeet API/webhook; opening a new req can auto-trigger a shortlist. |
| `internal_applications.csv` *(added)* | **Comeet** ATS | Who already applied to each open role and their pipeline status — powers the Open Jobs tab and the `ALREADY_APPLIED`/`PREVIOUSLY_REJECTED` flags. |
| `linkedin_recommendations.csv` *(added)* | **LinkedIn** | Public recommendations (explicit endorsements) authored by a real WSC employee who is a mutual connection — the strongest referral signal. |
| `internal_candidates.csv` *(added)* | **Internal-mobility / HR opt-in** | Existing employees who want to move to an open role — scored as a distinct, prioritised candidate source. |
| `salary_expectations.csv` *(added)* | **Recruiter screen / enrichment** | Candidate salary expectations, weighed against the role budget as a business constraint. |
| `job_budgets.csv` *(added)* | **Finance / HR** | Per-role budget ceiling used for the `OVER_BUDGET` signal. |

> The *added* CSVs are generated by [`make_mock_data.py`](src/make_mock_data.py) (deterministic, seed 42) to simulate the end state of the Comeet, LinkedIn, internal-mobility and finance integrations that weren't part of the original dataset.

**Real deployment architecture:**

```
 Conference export ─► HubSpot (talent pool, tagged by domain)
                              │  webhook on new contact
                              ▼
                      Enrichment worker ──► LinkedIn/enrichment API (+cache)
                              │
                              ▼
   New Comeet req ─────► Matching service (this scoring engine)
                              │
                              ▼
          Ranked shortlist ─► back to Comeet / Slack alert to recruiter + HTML report
```

**"When a role opens" — the trigger.** A new requisition in Comeet (or a recruiter clicking
"find candidates") fires the pipeline for that `job_id` and **pushes a shortlist + Slack alert**
to the recruiter — closing the loop the task describes: *job opens → HR is notified → good
candidates surface, including who can refer them.*

---

## 4a. Additional signals

Some of these are now implemented against simulated end-state data
(`linkedin_recommendations.csv`, `internal_applications.csv` — see §4b); others still need
sources beyond the provided CSVs and are called out rather than faked.

| Signal | Value | Source / how it plugs in |
|---|---|---|
| **LinkedIn recommendations** *(implemented)* | The **explicit positive endorsement** that "worked together" lacks: someone publicly vouched for how this person works. It's the **strongest** referral signal (relation weight `1.0`, above `worked_together`). | Modeled in `linkedin_recommendations.csv`, authored by a **real WSC employee** (from `wsc_employees.csv`) who is one of the candidate's mutual connections — fully grounded in the provided data, no invented outside people. It raises `referral_strength` and flags `HAS_RECOMMENDATION`. |
| **ATS status (already applied / rejected)** *(implemented)* | Avoids re-surfacing someone already in-flight or previously rejected, and lets HR compare pool matches against people who already applied. | Modeled in `internal_applications.csv` (Comeet-style). Flags `ALREADY_APPLIED` / `PREVIOUSLY_REJECTED`; powers the **Open Jobs** dashboard tab (applicant counts + status breakdown per role). |
| **Internal mobility** *(implemented)* | An existing employee who wants to move into a newly-opened role is often the **fastest, cheapest, lowest-risk hire — and a retention win**. They belong in the shortlist as a distinct, prioritised source, not mixed in with cold external leads. | Modeled in `internal_candidates.csv` (in production: an internal-mobility / HR opt-in system). They flow through the *same* scoring, are flagged `INTERNAL_MOBILITY`, badged **Internal** in the table, filterable via a KPI card, and get a "prioritise — loop in their manager" recommendation. |
| **Salary expectation vs. budget** *(implemented)* | A top-scoring candidate who wants far more than the role's budget can afford is a real constraint — HR needs to see the **next-in-line who fits the budget**. | **Only known once a recruiter has actually spoken with the candidate** — it is *not* available for a passive conference lead. So it is populated only for internal employees (known comp) and ATS applicants who reached a phone screen; everyone else shows **"not yet known"**. Shown **alongside** the score, never folded into it (fit is capability; budget is a separate business decision — a candidate slightly over is a *negotiation*, not a disqualification). Flagged `OVER_BUDGET` where the data legitimately exists. |
| **Connection degree (1st / 2nd / 3rd)** | A 1st-degree connection of an employee is a far warmer intro than a distant 3rd. The current data only exposes "mutual" (effectively 2nd). | LinkedIn relationship API — becomes a multiplier on `referral_strength`. |
| **Open-to-work / recent-move status** | Someone signalling *open to work*, or who just left a role, is more reachable; someone 3 months into a new job is unlikely to move. Complements the tenure-based movability we already compute. | LinkedIn "open to work" flag + current-role start date. Adjusts `stability`/movability. |
| **Beyond LinkedIn — industry presence** | Conference *speakers*, active GitHub authors, personal portfolios, blog/talks — strong competence signals for passive candidates. We already partially capture this from the recruiter `notes` field (e.g. "published papers on sports event detection"). | GitHub API, conference speaker lists, enrichment providers → feed a `credibility` signal. |
| **Soft skills & culture fit** | The hardest to quantify and often the real make-or-break. Deliberately **not** reduced to a number. | This is exactly where the human reference matters most — hence recommendations and "worked with" connections are surfaced for the recruiter to have a real conversation, not auto-scored. |

---

## 4b. The interactive HR dashboard (primary deliverable)

The CLI + per-job CSV/HTML remain, but the recruiter-facing centerpiece is a **single,
self-contained `output/dashboard.html`** (built by [`dashboard.py`](src/dashboard.py); no server,
no build step — the scoring payload for all jobs is embedded as JSON and re-scored live in the
browser). Four tabs, each answering a concrete recruiter question:

| Tab | Answers |
|---|---|
| **Overview** | State of the living pool + an alert banner ("N strong matches already sitting in the pool for role X — no new sourcing needed"). Shows the capture → enrich → filter → match story. |
| **Conferences** | Every event with **name, date, domain/classification, attendee count** — click a conference to jump into its attendees. |
| **Open Jobs** | Every open role with **how many people already applied internally** (Comeet-style) and their **ATS status breakdown**, how many **internal movers** opted in, the **budget** ceiling, and how many pool matches exist — so HR can compare an applicant against a stronger passive match. |
| **Candidate Match** | The interactive shortlist. Clickable KPI cards **filter** (Relevant / Strong / Warm-intro / Recommended / **Internal** / **Over-budget** / Noise); a **search box** queries the pool; the table sorts; a **drawer** opens the full transparent breakdown per candidate — including salary-vs-budget and internal-mobility context. |
**Recruiter-controlled weight sliders.** The key point from the brief — *thousands of candidates
per role, and each role needs a slightly different emphasis* — is met with five sliders (skill /
domain / seniority / referral / stability). They start at sensible **defaults** but the recruiter
can dial any signal up or down; the entire shortlist **re-ranks live** (weights are normalized,
the domain noise-gate still applies). This keeps the scoring transparent *and* under human
control instead of a fixed black box. A production version would persist per-role weight presets
and feed them back into the learned-weights loop (§6).

---

## 5. At scale (hundreds of conferences, thousands of contacts)

- **Storage**: move from CSV to a proper store (Postgres for structured data; the talent pool
  itself stays in HubSpot as the source of truth).
- **Enrichment**: batch + rate-limit LinkedIn calls, cache aggressively, refresh stale profiles
  on a schedule rather than per-request.
- **Matching**: the scoring is O(candidates) per job and embarrassingly parallel. For semantic
  skill matching at scale, add a **vector index** (embed skills/titles) so *"YOLO"* matches
  *"object detection"* without hand-maintained synonym lists — an enhancement, not a rewrite.
- **Incremental**: re-score only affected candidates when a profile or job changes.

### The pool as a living, retrieval-augmented knowledge base (RAG)

Jobs, conferences and LinkedIn profiles are **not a static export — they're a living bank** that
grows after every event and refreshes as profiles change. At scale this is best modeled as a
**RAG (retrieval-augmented) layer** over the pool:

1. **Ingest & refresh** — every conference import and LinkedIn re-enrichment upserts records
   (candidates, recommendations, jobs). The dashboard's tagline ("auto-updates after every
   conference & LinkedIn refresh") is the product promise of this pipeline.
2. **Embed** — each candidate's skills / titles / notes and each job's description are embedded
   into vectors and stored in a **vector index** (e.g. pgvector / FAISS / Pinecone), refreshed on
   change.
3. **Retrieve** — when a role opens, retrieve the top-k semantically closest candidates so
   *"YOLO"* matches *"object detection"* and *"platform engineer"* ≈ *"SRE"* with **no
   hand-maintained synonym lists**. Retrieval narrows thousands of contacts to a candidate set;
   the transparent rule-based scorer then ranks that set (retrieval for recall, rules for the
   explainable ranking a recruiter must justify).
4. **Ground the LLM** — outreach drafting and notes-summarization (§6) run *grounded* on the
   retrieved profile facts, avoiding hallucination.

The dashboard's **search box is a lightweight, in-browser stand-in** for this retrieval step —
in production it becomes a semantic query against the vector index.

---

## 6. Where AI/LLMs add value (and where they don't)

Deliberately **not** used for the core ranking — an LLM there would be slower, costlier, and
unexplainable for a decision recruiters must justify. Where an LLM *does* earn its place:

- **Skill normalization / semantic matching** — mapping messy titles and skills to a canonical
  taxonomy, so "platform engineer" ≈ "SRE" without brittle keyword lists.
- **Outreach drafting** — generate a personalized first-contact message per candidate that cites
  the mutual connection and the conference where we met them.
- **Notes summarization** — turn free-text recruiter notes into structured signals.

The rule-based core stays the backbone; the LLM is enrichment at the edges.

---

## 7. Stated assumptions

1. **Domain relevance** is inferred from skills + title/industry keyword overlap with the role's
   domain, with conference domain as a weak prior. Skills dominate; simply attending the event
   is near-zero signal. Threshold `0.25` chosen to filter clearly off-domain attendees while
   keeping adjacent-but-plausible ones.
2. **Attendee with no LinkedIn match** is **kept, not dropped** — scored on conference data and
   flagged `MISSING_LINKEDIN`. Dropping would silently lose real leads; the recruiter decides.
3. **Referral quality outranks referral quantity.** A single employee who *worked with* the
   candidate (overlapping years at the same org) outweighs several generic mutual connections.
   Detected by cross-referencing work histories. Crucially, this is treated as a *reference
   opportunity to verify* — not a guaranteed positive endorsement, since coworkers don't always
   get along. The explicit-positive signal (LinkedIn recommendations) is on the roadmap (§4a).
4. **Stability is inferred from parsed employment date ranges.** Job-hoppers are flagged and
   penalized; the ~2–4 year tenure mark is treated as the movability sweet spot. "present/now"
   in a role is anchored to a reference year of 2025 (the dataset's timeframe; `date.today()` in
   production).
5. **Comeet/ATS status is simulated** via `internal_applications.csv` (no live Comeet export was
   given). Candidates already in the ATS are flagged `ALREADY_APPLIED` / `PREVIOUSLY_REJECTED`
   and the Open Jobs tab shows applicant counts and status breakdowns per role. In production a
   live Comeet lookup replaces the mock and can optionally suppress known/rejected candidates.
6. **Refresh cadence**: modeled as an on-demand batch (run per `job_id`). In production it runs
   (a) after each conference to enrich the pool and (b) whenever a role opens.
7. **Trigger**: manual (recruiter runs it) for this task; automated via Comeet webhook in
   production.
8. **Privacy/GDPR**: only the provided synthetic data is used — no live calls, no real PII. At
   scale we'd need lawful basis for storing enrichment data, retention limits, and a
   delete-on-request path; LinkedIn data use must follow their ToS (hence a compliant provider).
9. **Seniority bands** are reasonable industry defaults, easily tuned per company norms.
10. **Internal-mobility candidates** are scored through the *same* engine as external leads but
    are flagged, badged and prioritised separately — they carry no external referral signal (so
    `NO_MUTUAL_CONNECTION` is suppressed for them) and are surfaced as the cheapest/fastest hire.
    In production this list comes from an employee opt-in / internal-mobility system.
11. **Salary is a business constraint, not a merit signal, and is only known post-contact.**
    Expected salary is *not* available for a passive conference lead — it surfaces only once a
    recruiter screens the candidate. So it is populated only for internal employees (known comp)
    and ATS applicants who reached a phone screen; all other candidates show "not yet known".
    When known, it is shown *alongside* the score and flagged `OVER_BUDGET`, but never folded
    into `match_score` — a great candidate slightly over budget is a negotiation, and HR must be
    able to see the next-best candidate who *does* fit. Budgets/expectations are simulated here.
13. **Every person in the system is real / from the provided dataset.** All candidates come from
    `conference_attendees.csv`; internal movers and recommendation authors are actual employees
    from `wsc_employees.csv`. No people are invented — the added CSVs only layer extra *attributes*
    (ATS status, salary, recommendations) onto people who already exist in the assignment data.

---

## 8. What I'd add with more time

**Already delivered** in this iteration (were on the roadmap): LinkedIn recommendations as the top
referral signal (authored by real WSC employees); simulated Comeet/ATS status; the interactive web
dashboard with pool search, filtering, and recruiter-tunable weight sliders.

Still ahead:
- **Live integrations** — real Comeet webhook + LinkedIn/enrichment API replacing the mock CSVs.
- **Vector-based semantic skill matching / RAG index** (§5) to replace keyword overlap and power
  true semantic pool search.
- **Connection-degree and open-to-work** signals to sharpen referral warmth and reachability.
- **LLM-drafted, referral-aware outreach messages** per candidate, grounded on retrieved facts.
- **Feedback loop**: learn (and persist per-role) the slider weights from which shortlisted
  candidates recruiters actually advance.
- **Automated alerting** (Slack) when a new role has strong matches sitting in the pool.
