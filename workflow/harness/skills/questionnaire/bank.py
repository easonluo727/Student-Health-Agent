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
# Shown once above the weight questions in a form.
WEIGHT_HEADING = "What matters most to you?"


def _visits(qid, label, encounter, when):
    return {
        "id": qid, "group": "B", "kind": "number", "label": label, "unit": "per year", "min": 0, "max": 50,
        "maps_to": f"compare_plans: encounter `{encounter}` priced this many times a year", "when": when,
        "encounter": encounter,
    }


def _weight(qid, label, when):
    return {
        "id": qid, "group": "E", "kind": "weight", "heading": WEIGHT_HEADING, "label": label,
        "scale_labels": WEIGHT_LABELS, "maps_to": "compare_plans ranking: weight = rating - 1 (plan_db.rank_plans)",
        "when": when,
    }


QUESTIONS = [
    # A. About you
    {"id": "age", "group": "A", "kind": "number", "label": "How old are you?", "min": 18, "max": 64,
     "maps_to": "premium tables (priced by age)", "when": "comparing or recommending plans"},
    {"id": "zip", "group": "A", "kind": "text", "label": "What's your ZIP code?",
     "help": "Premiums and nearby doctors depend on where you live.",
     "maps_to": "premium rating region; nearby providers; the doctors an office or specialist visit is priced at",
     "when": "comparing or recommending plans, or pricing an office or specialist visit"},
    {"id": "student_level", "group": "A", "kind": "single", "label": "Are you an undergraduate or a graduate student?",
     "help": "UC SHIP costs more for graduate students.",
     "options": [
         {"value": "undergraduate", "label": "Undergraduate"},
         {"value": "graduate", "label": "Graduate"},
         {"value": "not_student", "label": "Not a student"},
     ],
     "maps_to": "UC SHIP premium (by student level)", "when": "comparing or recommending plans"},
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
    _visits("therapy_sessions", "How many therapy sessions do you expect this year?", "therapy_session_45",
            "the user mentions mental health care"),
    _visits("lab_tests", "How many times do you expect to get blood work this year?", "routine_labs",
            "the user has a condition that needs regular labs"),
    {"id": "planned_events", "group": "B", "kind": "multi", "label": "Are you planning any of these this year?",
     "options": [
         {"value": "surgery", "label": "A surgery"},
         {"value": "pregnancy", "label": "Pregnancy"},
         {"value": "none", "label": "None of these"},
     ],
     "maps_to": "out-of-pocket maximum scenario (the price DB has no prices for these)",
     "when": "recommending a plan, or the user asks about a bad year"},

    # C. Doctors and networks
    {"id": "keep_providers", "group": "C", "kind": "multi", "label": "Is there a doctor or hospital you want to keep?",
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
    {"id": "telehealth_type", "group": "D", "kind": "single",
     "label": "Which kind of telehealth service will you use?",
     "help": "Some plans charge less only through their designated online service, such as LiveHealth Online.",
     "options": [
         {"value": "preferred", "label": "My plan's preferred online service"},
         {"value": "other", "label": "Another telehealth provider"},
     ],
     "maps_to": "scenario: preferred_telehealth_visit or telehealth_visit",
     "when": "pricing or comparing telehealth and the platform is unclear"},
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
     "maps_to": "compare_plans ranking: plans over budget rank last (plan_db.rank_plans)", "when": "recommending a plan"},
    _weight("weight_premium", "A low monthly premium", "recommending a plan"),
    _weight("weight_visit_costs", "Low costs when you see a doctor", "recommending a plan"),
    _weight("weight_worst_case", "Protection in a bad year (a low out-of-pocket max)", "recommending a plan"),
    _weight("weight_keep_doctors", "Keeping your current doctors in network", "recommending a plan"),
]

BY_ID = {q["id"]: q for q in QUESTIONS}

# Calculator fields that Python asks for itself when calculate_estimate reports them missing.
CALCULATOR_FIELDS = {"allowed_amount": "allowed_amount", "remaining_deductible": "remaining_deductible"}
# Premium inputs: PlanDB.premium field -> question-bank id. Python fills them from the answers and asks for the rest.
PREMIUM_FIELDS = {"age": "age", "zip": "zip", "student_level": "student_level"}
# Visit counts: question-bank id -> scenario. Python passes the answers to compare_plans as times a year.
VISIT_FIELDS = {q["id"]: q["encounter"] for q in QUESTIONS if "encounter" in q}
# keep_providers option -> the plan tier that place is (UC SHIP's Ashe Center tier). compare_plans prices visits there
# first. A place with no tier of its own (UCLA Health is an ordinary in-network provider) is not listed.
PROVIDER_TIERS = {"ashe": "Ashe Center"}
# Ranking weights: plan_db.FACTORS key -> question-bank id. Python passes the answers to compare_plans.
WEIGHT_FIELDS = {"premium": "weight_premium", "visit_costs": "weight_visit_costs", "worst_case": "weight_worst_case",
                 "keep_doctors": "weight_keep_doctors"}
