# TallyHealth: what the software does today

A fact sheet for the video team and for tools like ElevenLabs (2026-10-28). Every statement below was checked
against the code and databases. If something is not listed under "What it can do", don't say the product does it.
The 4-minute narration is at the end.

## In one sentence

TallyHealth is a chat-based assistant that helps people in Los Angeles choose a health insurance plan and see what a
doctor's visit will cost them, using the prices insurers negotiate with each doctor.

## What it can do

### 1. Insurance Arena: compare plans and recommend one
- The user answers short question cards in the chat: age, ZIP code, student level (for UC SHIP), expected visits per
  year (primary care, specialist, urgent care, therapy, blood work), plan types they accept, doctors they want to
  keep, and how much each of these matters to them (1 to 5):
  - a low premium;
  - low costs when seeing a doctor;
  - protection in a bad year (a low out-of-pocket maximum);
  - keeping their current doctors in network.
- For each plan, the code adds up: the yearly premium (list price for their age and region), the cost of their
  expected visits under that plan's rules, and the plan's out-of-pocket maximum.
- Office and specialist visits are priced at the **nearest doctor who has a negotiated rate in that plan's network**.
- The code ranks the plans by the user's priorities. The AI then explains the ranking in plain language.

### 2. Insurance Cost Estimator: what will this visit cost me?
- The user describes a visit, for example "an office visit near UCLA".
- The assistant finds nearby doctors of the right specialty (straight-line distance from the ZIP code) who have a
  negotiated rate in the user's plan network, and prices the visit **at each doctor's own rate**.
- The code applies the plan's deductible, copay and coinsurance, and shows what the user would pay and why.
- If it needs something it doesn't know (for example how much of the deductible is left), it asks. If a doctor has no
  rate, it says "no data" for that doctor; it never fills in another doctor's price.

Supported visits: new and established office visits, specialist follow-up, telehealth, preventive visit, urgent care
for flu, urgent care for a urinary infection, routine blood work, STI screening, flu shot, chest X-ray, knee MRI,
back MRI, therapy intake, and 45- and 60-minute therapy sessions. Doctor-level prices: office visits and specialist
follow-ups. The others use the typical in-network rate in Los Angeles.

### 3. Shared: the data behind both
| Source | What it gives | Size today |
|---|---|---|
| Insurers' price files (federal Transparency in Coverage rule) | The rate each insurer negotiated with each doctor, per service | 5 insurers, 7 networks, 444 GB of files processed into about 630,000 negotiated rates; 41 common services; Los Angeles |
| NPPES (federal provider registry) | Who each doctor is and where they practice | About 68,000 providers, about 90,000 addresses placed on the map |
| Plan documents (Summaries of Benefits and Coverage) | Deductibles, copays, coinsurance, out-of-pocket maximums, network tiers | 9 plans, 818 benefit rules, each with the page and a quote from the official document |

Plans today: UC SHIP for UCLA students (Anthem PPO), and eight Covered California plans: Anthem Gold 80 HMO; Health
Net Silver 70, Gold 80 and Platinum 90 Ambetter HMO; L.A. Care Bronze 60, Silver 70, Gold 80 and Platinum 90 HMO.

### 4. The harness: how the AI is kept accurate
The harness is the code around the AI model that decides what the model may do. The rule: **the AI understands,
chooses and explains; code calculates every dollar amount and the ranking, and checks hard facts.**
- **Skills and tools**: the model works through a fixed set of tools (ask questions, list plans, estimate a visit,
  compare plans, find doctors). It only picks options and fills in parameters; the tools do the lookups and math.
- **Question bank**: the model can only ask questions from a fixed bank, sent as form cards. Each question says which
  calculation it feeds, so it can't invent questions or ask for things the product doesn't use.
- **Hooks**: code that runs at fixed points of every turn. It stops the model from asking again what the user already
  answered, passes the user's answers straight into the calculations, and sends the right form itself when a
  calculation is missing an input.
- **Guards**: before a reply is shown, code checks it. A reply with a dollar amount that came from neither the user
  nor a calculation is rejected and rewritten; after two failed tries, a fixed, safe question replaces it. A reply
  claiming what other plans lack, which the database can't support, is also rejected. An amount the user never gave
  (such as how much of their deductible is left) can't be passed into a calculation.
- **No guessing**: if the user names a plan that isn't in the database, it says so. An uploaded plan document is used
  only to identify the plan; the numbers come from the database.
- Every number in a reply can be opened to see where it came from.

### 5. The AI models
- **The assistant runs on Meta's Muse models** through the Meta Model API: Muse Wonder for the conversation, with Muse
  Spark models as automatic fallbacks when a model is unavailable. The model is configuration, not code: any
  compatible model can be swapped in without changing the product.
- Because the harness does the math and checks, the product doesn't depend on the model being good at arithmetic
  or remembering insurance facts; the model's job is kept narrow (choose, fill in, explain).
- **A bill-reading model, fine-tuned by the team**: Meta's open Muse Glimmer 30B, fine-tuned to read photos and scans
  of medical bills and return each line (date, code, description, units, amount) as structured data. Because it is an
  open model, it can run on our own servers, so patients' bills don't have to leave them.
  - Training data: a generator of realistic synthetic bills with exact answers. Muse designed 30 bill layouts and 180
    ways of wording services; code fills in real doctors, services and prices, checks every value is visible on the
    rendered page, and adds scanned and phone-photo effects. 1,000 bills for training, 500 for testing, and the test
    layouts are never seen in training.
  - Results: **[fill in from the team's evaluation: accuracy on the 500 test bills, compared with the untrained model
    and with Muse Spark]**. Show only numbers from that evaluation.
  - Whether the bill reader is in the product yet: **[fill in]**. Until it is, the bill check below stays under
    "can't do".

## What it can't do (don't say or show it)

- Plans or doctors outside Los Angeles; plans other than the nine above; Medicare or Medicaid.
- Emergency care or prescription drug costs.
- Premium subsidies (premiums are list prices).
- Guarantee a final bill, or guarantee a doctor takes the plan or new patients: a negotiated rate means the doctor is
  in the network's price file; the user should confirm with the office.
- Driving distance (distances are straight-line).
- Check a medical bill (planned, not in the product yet).

## Demo inputs that work

- Arena: age 26, ZIP 90024, 6 primary care visits and 4 specialist visits a year, priorities: low premium, low visit
  costs, protection in a bad year.
- Estimator: same user and ZIP; "How much would an office visit near UCLA cost on my plan?"

Before recording, check that the live site has doctor-level pricing (merged 2026-10-27) and walk through both demos.

---

## Narration, about 4 minutes (about 520 words at 130 words a minute)

Generate each scene as its own clip so timing and pronunciation can be fixed one at a time.

**Pronunciation**: NPPES → "N-P-P-E-S"; KFF → "K-F-F"; UCLA → "U-C-L-A"; TallyHealth → "Tally Health".

### Scene 1 — The problem (0:00–0:35, about 75 words)
Two college students visit doctors for similar care. One pays thirty-five dollars. The other pays two hundred and
eighty. Why? Their plans, networks, and deductibles work differently, and almost no one can see that in advance.
According to a K-F-F survey, half of insured adults find at least part of their health insurance hard to understand, and nearly a
third can't tell what they'll owe for care. The prices exist. They're just buried where patients can't use them.

### Scene 2 — TallyHealth (0:35–1:00, about 55 words)
Introducing TallyHealth. It does two things. Insurance Arena compares health plans on premiums, the care you expect,
and how well each plan protects you in a bad year, weighted by what matters to you. And our Cost Estimator shows what
a visit will cost you, at a specific doctor near you.

### Scene 3 — The data, the harness and the model (1:00–2:00, about 135 words)
What makes this possible is the data behind it. Insurers are now required to publish the prices they negotiate with
every doctor. We processed four hundred and forty-four gigabytes of those files into about six hundred and thirty
thousand negotiated rates, matched them with the federal N-P-P-E-S registry of about sixty-eight thousand providers,
and turned each plan's official documents into rules, every one with a quote from the source.
The assistant runs on Meta's Muse models. Around it, we built a harness: the model understands you, picks the right
questions and tools, and explains. But it never does the math. Code calculates every dollar amount and every ranking,
and checks every reply. If a number can't be traced to its source, the reply is blocked and rewritten.

### Scene 4 — Demo (2:00–3:15, about 150 words)
Let's see it work. A twenty-six-year-old in Los Angeles is choosing a plan. They expect six primary care visits and
four specialist visits this year, and they care about a low premium, low costs at the doctor, and protection if the
year goes badly. They answer a few questions and enter their ZIP code. TallyHealth prices their visits at real doctors
near them, in each plan's network, adds each plan's premium, and weighs each plan's out-of-pocket limit. The ranking
is calculated, and the assistant explains why one plan fits them best.
Now they have a plan and want to see a doctor near U-C-L-A. They ask what an office visit would cost. TallyHealth
finds nearby doctors in their network and applies their plan's rules to each doctor's own negotiated rate. If it
needs something, like how much of their deductible is left, it asks. It's an estimate, not a final bill, but it's a
clear answer before they walk in.

### Scene 5 — Who it's for (3:15–3:38, about 55 words)
We're starting with college students and young adults choosing their own coverage, including those leaving a
parent's plan at twenty-six. The consumer experience stays free. To build a business, we plan to offer paid tools to
independent insurance brokers first, then partner with universities and benefits organizations.

### Scene 6 — Close (3:38–4:00, about 55 words)
Today, TallyHealth covers nine plans in Los Angeles. Next, we'll price more services at specific doctors, add more
plans, and test with students and brokers. We've also fine-tuned Meta's open Glimmer model to read medical bills, so patients' bills can stay on our own servers. Our mission is simple: help people understand their coverage and know their
costs before the bill arrives. TallyHealth. Understand your coverage. Anticipate your costs. Choose with confidence.
