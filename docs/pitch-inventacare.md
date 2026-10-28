# TallyHealth — Inventacare Break the Barrier video script (corrected)

Due 2026-11-01. This is the team's "MASTER VIDEO GENERATION PROMPT" with every technical claim checked against the
code and databases on 2026-10-28. Production specifications, visual identity, visual storytelling instructions,
source references, required assets and the final checklist (sections 2, 5–8 of the original) are unchanged; this
file replaces sections 3 and 4.

## What changed from the original, and why

| # | Original | Corrected | Why (what the code does) |
|---|---|---|---|
| 1 | Demo A profile: "Wants protection against unexpected emergency expenses" | "Wants protection in a bad year" (the Arena's out-of-pocket-max priority) | Emergency care is out of the product (2026-10-27); there is no emergency question. The Arena does rank by out-of-pocket max (`plan_db.FACTORS["worst_case"]`). |
| 2 | Geography "surfaces physicians, hospitals, and healthcare providers" | "nearby doctors who have a negotiated rate in the plan's network" | We find and price doctors by specialty (`provider_directory.py`); hospitals are not searched or priced. |
| 3 | Geography feeds every estimate | Office and specialist visits are priced at a specific nearby doctor; other services at the typical Los Angeles in-network rate | `DOCTOR_SCENARIOS`; everything else still uses the network median (interim). |
| 4 | "Do not imply a nearby location confirms in-network status" | Say the stronger, true thing: we only price doctors who appear with a negotiated rate in that plan's network files; that is not a promise they take new patients | Doctors come from the network's own rate file. |
| 5 | AI parses policy documents "during plan comparisons and cost estimation" | Policy documents are converted ahead of time into rules; each rule keeps the source page and a verbatim quote and was reviewed by the team. An uploaded plan document is only used to identify the plan. | `scripts/policy/`, `harness/reader.py`. Rules are marked verified by a team decision on 2026-10-17, not row by row, so don't say "every rule was checked by hand". |
| 6 | "Platform provides a recommendation" | The ranking is computed by code from premium, expected care costs and the user's priorities; the AI explains it. | `plan_db.rank_plans`; the core design rule. |
| 7 | No numbers for the data | 5 insurers, 7 networks, 444 GB of insurer files, about 630,000 negotiated rates, about 68,000 providers, 9 Los Angeles plans, 818 benefit rules | Counted from `rates.sqlite`, `providers.sqlite`, `policy.sqlite`. |
| 8 | Coverage scope not stated | Today: 9 plans in Los Angeles (UC SHIP and Covered California), 41 common services | Judges will ask; say it plainly. |
| 9 | Premiums | Premiums are list prices before any subsidy | Subsidies are not computed. |
| 10 | No model training | Scene 6 adds about 25 seconds on the bill-reading model we trained, with its test results; Demo B and Scene 5 are 5 seconds shorter each to make room | Same facts as `docs/pitch.md` section 3: Muse Glimmer 30B fine-tuned on 900 generated bills, scored on 100 held-out generated bills by `scripts/bills/score.py` (2026-10-28). |

KFF figures were checked against the source: 51% of insured adults find at least one aspect of their insurance at
least somewhat difficult to understand; 30% find it difficult to understand what they will owe out of pocket
(KFF Survey of Consumer Experiences with Health Insurance, Feb–Mar 2023).

---

## 3. Video storyboard and narration

### Scene 1 — The Healthcare Barrier (0:00–0:45)

Visuals unchanged: two fictional students, split-screen bills of $35 and $280 labelled "Illustrative example — not
actual patient bills", the tangle of insurance terms, then the KFF figures with the source on screen.

> "Two college students visit doctors for similar care. One pays thirty-five dollars. The other pays two hundred and
> eighty.
> Why such a difference? Their insurance plans, provider networks, negotiated rates, and deductibles may work
> differently.
> But why is it so hard to know these costs before receiving care?
> According to a KFF survey, fifty-one percent of insured adults find at least one part of their health insurance
> hard to understand, and thirty percent find it hard to understand what they'll owe out of pocket.
> The pricing information exists. Insurers are now required to publish the rates they negotiate with every doctor.
> But it's scattered across enormous files that ordinary patients can't use.
> We believe healthcare decisions shouldn't require becoming an insurance expert."

### Scene 2 — Introducing TallyHealth (0:45–1:20)

Visuals unchanged: wordmark, then two panels.
- Left, Insurance Arena: premiums, expected visits, estimated care costs, the plan's out-of-pocket maximum, the
  user's priorities, a ranked recommendation with an explanation.
- Right, Insurance Cost Estimator: a service and a plan in, an estimated out-of-pocket cost and the coverage rule
  behind it out.
- Geography is shown as a shared layer under both panels, not a third function.

> "Introducing TallyHealth, an AI-powered platform that makes health insurance decisions easier.
> TallyHealth has two core functions.
> The first is Insurance Arena. It compares health insurance plans on premiums, your expected care, what that care
> would cost under each plan, and how much each plan protects you in a bad year, weighted by what matters to you.
> The second is our Insurance Cost Estimator. It shows what you might pay for a medical service under a particular
> plan, at a particular doctor.
> Together, they answer two essential questions: Which plan should I choose? And how much will I pay when I use it?"

### Scene 3 — Technology and Data Architecture (1:20–2:25)

Architecture diagram, three levels (give viewers time to read it).

**Level 1 — three data sources**, each with its number:
- **NPPES provider data** (CMS): NPIs, names, practice addresses, located on a map. *About 68,000 providers.* Role:
  who and where. NPPES alone does not show network participation.
- **Insurers' machine-readable files** (Transparency in Coverage): in-network negotiated rates per billing code
  (CPT/HCPCS) and per provider. *5 insurers, 7 networks, 444 GB of files, about 630,000 negotiated rates for 41
  common services in Los Angeles.* Role: what each doctor's contracted price is. Negotiated rates are not the
  patient's final cost; out-of-network amounts are a separate kind of data we don't use.
- **Insurance policy documents** (Summaries of Benefits and Coverage): deductibles, copays, coinsurance,
  out-of-pocket maximums, service-level rules, network tiers. *9 Los Angeles plans, 818 benefit rules.* Role:
  converted ahead of time, with AI assistance, into structured rules; every rule keeps its source page and a
  verbatim quote so it can be checked.

**Level 2 — shared technology:**
- **Data integration and matching**: links plans to their networks, networks to doctors' NPIs, and services to
  billing codes.
- **Geographic intelligence**: from the user's ZIP code, finds nearby doctors of the right specialty who have a
  negotiated rate in the plan's network. Used by both functions.
- **AI interpretation**: understands the user's request, asks for what's missing, and explains results. It never
  calculates a dollar amount.
- **Rules-based cost calculation**: applies deductibles, copays and coinsurance to the doctor's negotiated rate;
  when information is missing, it says so instead of guessing.

**Level 3 — two functions**: Insurance Arena and Insurance Cost Estimator, both on the same layer.

> "What makes TallyHealth different is the data behind it.
> We bring together three sources.
> First, the federal NPPES registry tells us who about sixty-eight thousand providers are and where they practice.
> Second, the price files insurers are now required to publish. We processed four hundred and forty-four gigabytes of
> them into about six hundred and thirty thousand negotiated rates, each tied to a specific doctor.
> Third, insurance policy documents. We turn each plan's deductibles, copays, coinsurance and out-of-pocket limits
> into rules, and every rule keeps a quote from the official document, so it can be checked.
> From your ZIP code, we find nearby doctors who have a negotiated rate in your plan's network.
> An AI agent understands your questions and explains the results, but it never does the math. Every number comes
> from our calculation engine and can be traced to its source."

### Scene 4 — Live Product Demonstration (2:25–3:50)

Use only real TallyHealth footage; never a mockup presented as the product.

**Demo A — Insurance Arena (2:25–3:15).** Fictional profile, all supported by the questionnaire:
- ZIP 90024 (Los Angeles), age 26.
- 6 primary care visits and 4 specialist visits a year (pick the specialty the product asks for).
- Priorities: a low premium, low costs when seeing a doctor, and protection in a bad year (a low out-of-pocket
  max).

Show: answering the question cards; entering the ZIP; the Arena comparing the supported plans; each plan's
premium (list price), care cost for the year, and out-of-pocket maximum; the doctors near 90024 used for the visit
prices; the ranking and the explanation. Use the real plan names and numbers the product returns. If a premium is
missing, show it as missing.

**Demo B — Insurance Cost Estimator (3:15–3:50).** Same user, ZIP 90024: "I have this plan and want to see a doctor
near UCLA. How much might I pay for an office visit?"

Show: the plan being identified or asked for; nearby doctors with their own negotiated rate; the estimate and its
breakdown (rate, deductible, copay or coinsurance). Use an office visit: that's the service priced at a specific
doctor. If data is missing, show the product saying so.

> "Let's see how this works.
> Imagine a student in Los Angeles choosing health insurance. They expect several doctor visits this year, and they
> also want to be protected if the year goes badly.
> In Insurance Arena, they enter their ZIP code, their expected visits, and what matters most to them.
> TallyHealth prices those visits at real doctors near them, adds each plan's premium, and weighs each plan's
> out-of-pocket limit. The ranking is calculated, not guessed, and the AI explains why one plan fits better.
> Now the student has a plan and wants to see a doctor near UCLA.
> They ask what an office visit would cost. TallyHealth finds nearby doctors in their network and applies their
> plan's rules to each doctor's own negotiated rate.
> Instead of searching insurance documents, provider directories and pricing files separately, they get one clear
> answer.
> It's an estimate, not a guaranteed final bill, but it gives patients a clear starting point before they get care."

### Scene 5 — Impact and Commercialization (3:50–4:15)

Unchanged (a plan to validate, no claims of customers, partners or revenue).

> "We're starting with college students who choose their own health insurance, including young adults leaving their
> parents' coverage at twenty-six.
> We want the consumer experience to stay accessible and free.
> To build a business, we plan a business-to-business-to-consumer model: paid tools for independent insurance
> brokers first, then partnerships with universities and benefits organizations."

### Scene 6 — Our Own Model, Roadmap, and Vision (4:15–4:55)

First (about 25 seconds), the bill-reading model. On screen, the results table, labelled "100 generated test bills,
not real patient bills":

| | Muse Glimmer 30B, before training | Muse Glimmer 30B, after training | Muse Spark |
|---|---|---|---|
| Bills with every field right | 73% | **86%** | 76% |
| All fields | 97.4% | **99.2%** | 85.2% |
| Bills it refused to read | 0 | 0 | 6 |

Then the three-phase roadmap, with "today" stated plainly:
- **Today**: 9 plans in Los Angeles, 41 common services; office and specialist visits priced at specific doctors; a
  bill-reading model trained and tested.
- **Phase 1, validate and improve**: bill audit (check each line of a bill against that doctor's negotiated rate);
  price more services at specific doctors; more plans in California; validate rules and rates; student user testing.
- **Phase 2, pilots**: broker workflows, university and institutional pilots, willingness to pay.
- **Phase 3, expansion**: beyond California; APIs; more services; prescription drugs and emergency care when
  reliable data allows.

Closing card: **TallyHealth — Understand your coverage. Anticipate your costs. Choose with confidence.**

> "Next, we're adding bill audit: checking each line of a medical bill against that doctor's negotiated rate.
> Bills hold private health information, so we trained our own model to read them. On a hundred generated test
> bills, our fine-tuned Muse Glimmer read every field correctly on eighty-six percent, up from seventy-three before
> training, and ahead of Muse Spark at seventy-six. Muse Spark refused six bills because they listed tests like HIV
> screening.
> Today we cover nine plans in Los Angeles. Our mission is simple: to help people make confident healthcare
> decisions before the bill arrives."

---

## 4. Technical and factual requirements (replaces the original section 4)

Keep every rule of the original, and add or tighten these:

**Scope**
- Two user-facing functions only: Insurance Arena and Insurance Cost Estimator. Geography is a shared layer.
- Today's data: 9 Los Angeles plans (UC SHIP and Covered California), 41 common services. Say so; don't imply
  national coverage.
- Emergency care and prescription drugs are not supported. Don't show or narrate estimates for them.

**Pricing**
- Negotiated rate ≠ what the patient pays: deductibles, copays and coinsurance decide that.
- Doctor-level prices: office and specialist visits only. Other services use the typical Los Angeles in-network rate.
- A doctor with a negotiated rate in the network is not a promise that they accept new patients.
- Premiums are list prices before subsidies.
- The Arena's "bad year" factor is the plan's out-of-pocket maximum from the policy document, not a simulated bad
  year.

**AI**
- The AI understands, asks and explains; code calculates every amount and the ranking. Never show or say that the AI
  computed a price.
- Policy rules were converted before use and carry a source quote. Don't claim each rule was checked by hand.
- An uploaded plan document identifies the plan; the numbers come from the database.

**Model training** (same rules as `docs/pitch.md` section 3)
- Say "generated test bills". The model has not been tested on real bills.
- Bill audit is in development; don't show or narrate it as a working feature.
- Don't say the test bills use layouts the model never saw: the 100 were drawn at random from the same 1,000 bills,
  so layouts may overlap with training.
- Don't compare speed: Glimmer read the 100 bills in one batch, Muse Spark one API call at a time.
- Say "ahead of" Muse Spark, not "far ahead": 100 bills, p = 0.04. Before vs after training is p = 0.004.
- Training ran on Modal; Fireworks was only a 20-bill trial.

**Before recording**
- [ ] The live site (Railway) runs a build with doctor-level pricing (merged 2026-10-27, PRs #15 and #16).
- [ ] Walk through Demo A and Demo B with the exact inputs above; nearby doctors appear in both.
- [ ] Record a backup of the demo in case the model API is slow on the day.
