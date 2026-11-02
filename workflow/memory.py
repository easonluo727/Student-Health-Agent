"""The signed-in user's long-term memory: facts that last across conversations, and the markdown the agent reads.

The user sets the facts in Settings; Python also keeps their questionnaire answers to the same questions, so the newest
answer wins wherever it was given. The model never writes here. Every turn, render() turns the facts and the user's
past bill reviews and uploaded plans into a markdown section of the prompt. The user never sees that markdown; they
see Settings and their files.

Facts are stored in the app database (appdb.py, user_facts) as JSON, one row per field, with where they came from.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from appdb import APP_DB, migrate, transaction
from harness.skills.questionnaire.bank import BY_ID

ZIP = re.compile(r"^\d{5}$")
# Fields kept for the user. The ones named like a question-bank id fill that question, so it isn't asked again.
FIELDS = ("zip", "age", "plan_id")
QUESTION_FIELDS = ("zip", "age")
MAX_REVIEWS = 5


def check(field, value, plan_ids=()):
    """The value in its stored type; ValueError with a message for the user if it isn't valid."""
    if field not in FIELDS:
        raise ValueError(f"Unknown setting {field!r}.")
    if field == "zip":
        value = str(value).strip()
        if not ZIP.match(value):
            raise ValueError("ZIP code must be 5 digits.")
        return value
    if field == "age":
        q = BY_ID["age"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError("Age must be a whole number.")
        if not q["min"] <= value <= q["max"]:
            raise ValueError(f"Age must be between {q['min']} and {q['max']}; plans are priced for those ages.")
        return int(value)
    if value not in plan_ids:
        raise ValueError("Choose a plan from the list.")
    return value


class MemoryStore:
    def __init__(self, path: Path = APP_DB):
        migrate(path)
        self.path = path

    def facts(self, user_id: str) -> dict:
        """{field: {"value", "source", "updated_at"}} for the fields the user has."""
        with transaction(self.path) as con:
            rows = con.execute("SELECT field, value, source, updated_at FROM user_facts WHERE user_id=?",
                               (user_id,)).fetchall()
        return {f: {"value": json.loads(v), "source": s, "updated_at": t} for f, v, s, t in rows if f in FIELDS}

    def remember(self, user_id: str, values: dict, source: str, plan_ids=()) -> None:
        """Set each field to its value (None forgets it), all or nothing. Raises ValueError on an invalid value."""
        checked = {f: None if v is None else check(f, v, plan_ids) for f, v in values.items()}
        now = time.time()
        with transaction(self.path) as con:
            for field, value in checked.items():
                if value is None:
                    con.execute("DELETE FROM user_facts WHERE user_id=? AND field=?", (user_id, field))
                else:
                    con.execute("INSERT INTO user_facts (user_id, field, value, source, updated_at) VALUES "
                                "(?, ?, ?, ?, ?) ON CONFLICT(user_id, field) DO UPDATE SET value=excluded.value, "
                                "source=excluded.source, updated_at=excluded.updated_at",
                                (user_id, field, json.dumps(value), source, now))

    def bill_reviews(self, user_id: str, limit: int = MAX_REVIEWS) -> list[dict]:
        """The user's latest bill reviews, newest first."""
        with transaction(self.path) as con:
            rows = con.execute(
                "SELECT b.provider, b.statement_date, b.plan_name, b.billed, b.you_owe, b.possible_overcharge, "
                "b.findings, b.appeal_drafted, b.status, b.updated_at FROM bill_audits b JOIN sessions s "
                "ON s.session_id = b.session_id WHERE s.user_id=? ORDER BY b.updated_at DESC LIMIT ?",
                (user_id, limit)).fetchall()
        keys = ("provider", "statement_date", "plan_name", "billed", "you_owe", "possible_overcharge", "findings",
                "appeal_drafted", "status", "updated_at")
        return [dict(zip(keys, row)) for row in rows]

    def uploaded_plans(self, user_id: str) -> list[dict]:
        """Plans the user uploaded (each once), newest first."""
        with transaction(self.path) as con:
            rows = con.execute(
                "SELECT u.plan_id, u.plan_name, MAX(u.created_at) FROM uploads u JOIN sessions s "
                "ON s.session_id = u.session_id WHERE s.user_id=? AND u.kind='plan' AND u.plan_id IS NOT NULL "
                "GROUP BY u.plan_id ORDER BY MAX(u.created_at) DESC", (user_id,)).fetchall()
        return [{"plan_id": p, "plan_name": n, "uploaded_at": t} for p, n, t in rows]

    def memory(self, user: dict, plan_names: dict) -> str:
        return render(user, self.facts(user["user_id"]), self.bill_reviews(user["user_id"]),
                      self.uploaded_plans(user["user_id"]), plan_names)


def question_answers(facts: dict) -> dict:
    """The facts that answer questionnaire questions, as the hooks' answers ({question id: value})."""
    return {f: facts[f]["value"] for f in QUESTION_FIELDS if f in facts}


def _day(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def _usd(amount):
    return "unknown" if amount is None else f"${amount:,.2f}"


def render(user: dict, facts: dict, reviews: list, plans: list, plan_names: dict) -> str:
    """The memory section of the prompt, in three parts: how to use it (fixed), about the user, past activity."""
    said = {"settings": "set in Settings", "form": "answered in a questionnaire"}
    about = [f"- Name: {user['name']}" if user.get("name") else None]
    if "plan_id" in facts:
        f = facts["plan_id"]
        about.append(f"- Current plan: {plan_names.get(f['value'], f['value'])} (plan_id {f['value']}) · "
                     f"{said[f['source']]} {_day(f['updated_at'])}")
    labels = {"zip": "ZIP", "age": "Age"}
    for field in QUESTION_FIELDS:
        if field in facts:
            f = facts[field]
            about.append(f"- {labels[field]}: {f['value']} · {said[f['source']]} {_day(f['updated_at'])}")
    about = [line for line in about if line]

    past = []
    for r in reviews:
        what = " · ".join(filter(None, [r["provider"], f"statement {r['statement_date']}" if r["statement_date"] else None,
                                        r["plan_name"]]))
        if r["status"] == "audited":
            result = (f"billed {_usd(r['billed'])}, should owe {_usd(r['you_owe'])} on the checked lines, "
                      f"{_usd(r['possible_overcharge'])} possibly overcharged "
                      f"({r['findings']} finding{'' if r['findings'] == 1 else 's'})")
        else:
            result = "could not be checked against negotiated rates"
        appeal = "; appeal letter drafted" if r["appeal_drafted"] else ""
        past.append(f"- Bill review {_day(r['updated_at'])}: {what or 'a bill'}: {result}{appeal}")
    for p in plans:
        name = plan_names.get(p["plan_id"]) or p["plan_name"] or p["plan_id"]  # the plan database is the authority
        past.append(f"- Uploaded plan {_day(p['uploaded_at'])}: {name} (plan_id {p['plan_id']})")

    if not about and not past:
        return ""
    return "\n".join([
        "# What you know about this user (from earlier chats and their settings)",
        "",
        "## 1. How to use this memory",
        "- The user set these in Settings or told you in an earlier chat. Use them; don't ask for them again.",
        "- ZIP and age already fill the questionnaire and compare_plans; Python does that, not you.",
        "- If the user says something different now, the new answer wins.",
        "- Amounts that change during the year (remaining deductible, what they've paid so far) are never kept here; "
        "ask for them each time.",
        "- Past bill reviews are summaries. To discuss a bill again, the user opens that chat or uploads it again.",
        "",
        "## 2. About the user",
        *(about or ["- Nothing yet."]),
        "",
        "## 3. Past activity",
        *(past or ["- Nothing yet."]),
    ])
