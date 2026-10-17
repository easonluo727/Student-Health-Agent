"""The question bank. The model may only ask questions listed here.

Each question is one form field (docs/frontend-protocol.md, Form). `maps_to` names the
database query or calculator input the answer feeds; a question that feeds nothing does
not belong here. `when` tells the model when the question is worth asking.
The frontend adds an "I don't know" choice to every question.
"""

GROUPS = {
    "A": "About you",
    "B": "Health and care use",
    "C": "Doctors and networks",
    "D": "Pricing one visit",
    "E": "Budget and priorities",
}

WEIGHT_LABELS = ["Don't care", "Very important"]


def _visits(qid, label, encounter, when):
    return {
        "id": qid, "group": "B", "kind": "number", "label": label, "unit": "per year", "min": 0, "max": 50,
        "maps_to": f"price DB encounter `{encounter}` x expected visits", "when": when,
    }


def _weight(qid, label, when):
    return {
        "id": qid, "group": "E", "kind": "weight", "label": label, "scale_labels": WEIGHT_LABELS,
        "maps_to": "ranking weight", "when": when,
    }


QUESTIONS = [
    # A. About you
    {"id": "age", "group": "A", "kind": "number", "label": "How old are you?", "min": 18, "max": 64,
     "maps_to": "premium tables (priced by age)", "when": "comparing or recommending plans"},
    {"id": "zip", "group": "A", "kind": "text", "label": "What's your ZIP code?",
     "help": "Premiums and nearby doctors depend on where you live.",
     "maps_to": "premium rating region; nearby providers", "when": "comparing or recommending plans"},
    {"id": "current_coverage", "group": "A", "kind": "single", "label": "What coverage do you have now?",
     "options": [
         {"value": "uc_ship", "label": "UC SHIP"},
         {"value": "parent_plan_ending", "label": "A parent's plan that's ending"},
         {"value": "none", "label": "No coverage"},
         {"value": "other", "label": "Something else"},
     ],
     "maps_to": "baseline plan for comparison", "when": "the user's current plan is unclear"},

    # B. Health and care use
    {"id": "conditions", "group": "B", "kind": "multi", "label": "Do you have any ongoing conditions?",
     "options": [
         {"value": "asthma", "label": "Asthma"},
         {"value": "diabetes", "label": "Diabetes"},
         {"value": "mental_health", "label": "Anxiety or depression"},
         {"value": "none", "label": "None"},
     ],
     "maps_to": "which visit questions matter; comparison rows to highlight",
     "when": "evaluating a plan or recommending one"},
    _visits("specialist_visits", "How many specialist visits do you expect this year?", "specialist_followup",
            "the user has an ongoing condition or sees a specialist"),
    _visits("primary_care_visits", "How many primary care visits do you expect this year?",
            "office_visit_established", "estimating a year of care"),
    _visits("urgent_care_visits", "How many urgent care visits do you expect this year?", "urgent_care_flu",
            "estimating a year of care"),
    _visits("er_visits", "How many emergency room visits do you expect this year?", "er_ankle_xray",
            "estimating a year of care, or the user mentions injuries or ER visits"),
    _visits("therapy_sessions", "How many therapy sessions do you expect this year?", "therapy_session_45",
            "the user mentions mental health care"),
    _visits("lab_tests", "How many times do you expect to get blood work this year?", "routine_labs",
            "the user has a condition that needs regular labs"),
    {"id": "regular_rx", "group": "B", "kind": "single", "label": "Do you take a medication regularly?",
     "options": [
         {"value": "generic", "label": "Yes, a generic"},
         {"value": "brand", "label": "Yes, a brand-name drug (for example an inhaler)"},
         {"value": "no", "label": "No"},
     ],
     "maps_to": "drug tier in the policy rules (copays only; no drug prices in the price DB)",
     "when": "evaluating a plan or recommending one"},
    {"id": "planned_events", "group": "B", "kind": "multi", "label": "Are you planning any of these this year?",
     "options": [
         {"value": "surgery", "label": "A surgery"},
         {"value": "pregnancy", "label": "Pregnancy"},
         {"value": "none", "label": "None of these"},
     ],
     "maps_to": "out-of-pocket maximum scenario (the price DB has no prices for these)",
     "when": "recommending a plan, or the user asks about a bad year"},

    # C. Doctors and networks
    {"id": "keep_providers", "group": "C", "kind": "single", "label": "Is there a doctor or hospital you want to keep?",
     "options": [
         {"value": "ucla_health", "label": "UCLA Health"},
         {"value": "ashe", "label": "Ashe Student Health Center"},
         {"value": "no_preference", "label": "No preference"},
     ],
     "maps_to": "plan-to-network map; in-network check", "when": "evaluating or recommending a plan"},
    {"id": "network_types_ok", "group": "C", "kind": "multi", "label": "Which plan types are you OK with?",
     "help": "PPO lets you see any doctor; EPO and HMO keep you inside a network, usually for less.",
     "options": [
         {"value": "ppo", "label": "PPO"},
         {"value": "epo", "label": "EPO"},
         {"value": "hmo", "label": "HMO"},
     ],
     "maps_to": "filter candidate plans by network type", "when": "recommending a plan"},

    # D. Pricing one visit
    {"id": "service_network", "group": "D", "kind": "single", "label": "Is this provider in your plan's network?",
     "options": [
         {"value": "in-network", "label": "In network"},
         {"value": "out-of-network", "label": "Out of network"},
     ],
     "maps_to": "calculator: which network rule applies", "when": "pricing one visit and the network is unclear"},
    {"id": "allowed_amount", "group": "D", "kind": "number", "label": "Do you know the allowed amount for this visit?",
     "help": "The price your insurer agreed to pay, from an Explanation of Benefits. It's not the billed price.",
     "unit": "USD", "min": 0, "max": 1000000,
     "maps_to": "calculator: allowed_amount", "when": "the calculator needs the allowed amount"},
    {"id": "remaining_deductible", "group": "D", "kind": "number",
     "label": "How much of your deductible is left this year?",
     "help": "What you still have to pay before coinsurance starts. It's not the annual deductible.",
     "unit": "USD", "min": 0, "max": 100000,
     "maps_to": "calculator: remaining_deductible", "when": "the calculator needs the remaining deductible"},

    # E. Budget and priorities
    {"id": "monthly_budget", "group": "E", "kind": "number",
     "label": "What's the most you'd pay each month for insurance?", "unit": "USD per month", "min": 0, "max": 3000,
     "maps_to": "filter and rank plans by premium", "when": "recommending a plan"},
    _weight("weight_premium", "A low monthly premium", "recommending a plan"),
    _weight("weight_visit_costs", "Low costs when you see a doctor", "recommending a plan"),
    _weight("weight_worst_case", "Protection in a bad year (a low out-of-pocket max)", "recommending a plan"),
    _weight("weight_keep_doctors", "Keeping your current doctors in network", "recommending a plan"),
]

BY_ID = {q["id"]: q for q in QUESTIONS}

# Calculator fields that Python asks for itself when calculate_estimate reports them missing.
CALCULATOR_FIELDS = {"allowed_amount": "allowed_amount", "remaining_deductible": "remaining_deductible"}
