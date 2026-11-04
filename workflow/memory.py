"""The signed-in user's long-term memory: everything they told us, in any chat or in Settings.

Memory is not a list of chosen facts. It is what the user said: every message and questionnaire answer from their
other conversations (the `input` events in session_events), and the values they set in Settings, oldest first. The
newest answer to a question wins. Deleting a conversation forgets what was said in it.

Every turn, recall() gives the harness three things: the texts (so the hooks count earlier statements as stated by
the user: amounts, prefill, quotes), the questionnaire answers (so nothing is asked twice) and a markdown section of
the prompt. The model never writes here, and the user never sees the markdown; they see their chats and Settings.

Settings values are stored in the app database (appdb.py, user_facts) as JSON, one row per field.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from appdb import APP_DB, migrate, transaction
from harness.skills.questionnaire.bank import BY_ID

ZIP = re.compile(r"^\d{5}$")
FIELDS = ("zip", "age", "plan_id")  # what Settings can set
MAX_REVIEWS = 5
MAX_MEMORY_CHARS = 8000  # what the user said, in the prompt; the oldest lines go first
MAX_LINE_CHARS = 600


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


@dataclass
class Recall:
    texts: list  # what the user said, as text, oldest first
    answers: dict  # question-bank id -> the newest known answer
    prompt: str  # the memory section of the prompt ("" when there is nothing)


class MemoryStore:
    def __init__(self, path: Path = APP_DB):
        migrate(path)
        self.path = path

    def facts(self, user_id: str) -> dict:
        """The Settings values: {field: {"value", "source", "updated_at"}}."""
        with transaction(self.path) as con:
            rows = con.execute("SELECT field, value, source, updated_at FROM user_facts WHERE user_id=?",
                               (user_id,)).fetchall()
        return {f: {"value": json.loads(v), "source": s, "updated_at": t} for f, v, s, t in rows if f in FIELDS}

    def remember(self, user_id: str, values: dict, plan_ids=()) -> None:
        """Set each Settings field to its value (None forgets it), all or nothing. Raises ValueError on a bad value."""
        checked = {f: None if v is None else check(f, v, plan_ids) for f, v in values.items()}
        now = time.time()
        with transaction(self.path) as con:
            for field, value in checked.items():
                if value is None:
                    con.execute("DELETE FROM user_facts WHERE user_id=? AND field=?", (user_id, field))
                else:
                    con.execute("INSERT INTO user_facts (user_id, field, value, source, updated_at) VALUES "
                                "(?, ?, ?, 'settings', ?) ON CONFLICT(user_id, field) DO UPDATE SET "
                                "value=excluded.value, updated_at=excluded.updated_at",
                                (user_id, field, json.dumps(value), now))

    def inputs(self, user_id: str, exclude_session: str) -> list[tuple[float, dict]]:
        """What the user sent in their other conversations, oldest first: [(time, input data)]."""
        with transaction(self.path) as con:
            rows = con.execute(
                "SELECT e.created_at, e.data FROM session_events e JOIN sessions s ON s.session_id = e.session_id "
                "WHERE s.user_id=? AND e.session_id != ? AND e.event='input' ORDER BY e.created_at, e.seq",
                (user_id, exclude_session)).fetchall()
        return [(at, json.loads(data)) for at, data in rows]

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

    def recall(self, user: dict, session_id: str, plan_names: dict) -> Recall:
        """Everything the user told us outside this conversation (this one's own history is already in the chat)."""
        uid = user["user_id"]
        said = statements(self.inputs(uid, session_id), self.facts(uid), plan_names)
        answers = {}
        for s in said:
            answers.update(s["answers"])
        return Recall([s["text"] for s in said], answers,
                      render(user, said, self.bill_reviews(uid), self.uploaded_plans(uid), plan_names))


def _shown(value):
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def statements(inputs, facts, plan_names) -> list[dict]:
    """Each thing the user said as {"at", "where", "text", "answers"}, oldest first. Answers are only real values:
    "I don't know" in an old chat doesn't stop the questionnaire from asking again."""
    out = []
    for at, data in inputs:
        if data.get("type") == "message":
            text = " ".join((data.get("text") or "").split())
            names = [a.get("name") for a in data.get("attachments") or [] if a.get("name")]
            if names:
                text = " ".join(filter(None, [text, "[attached: " + ", ".join(names) + "]"]))
            if text:
                out.append({"at": at, "where": "chat", "text": text, "answers": {}})
        elif data.get("type") == "form_response":
            parts, answers = [], {}
            for qid, answer in (data.get("answers") or {}).items():
                if qid not in BY_ID or not isinstance(answer, dict):
                    continue
                value = None if answer.get("unknown") else answer.get("value")
                if value in (None, "", []):
                    parts.append(f"{BY_ID[qid]['label']} I don't know")
                else:
                    answers[qid] = value
                    parts.append(f"{BY_ID[qid]['label']} {_shown(value)}")
            if parts:
                out.append({"at": at, "where": "questionnaire", "text": "; ".join(parts), "answers": answers})
    labels = {"zip": "My ZIP code is", "age": "My age is"}
    for field, fact in facts.items():
        value = fact["value"]
        if field == "plan_id":
            text = f"My current plan is {plan_names.get(value, value)} (plan_id {value})"
        else:
            text = f"{labels[field]} {value}"
        out.append({"at": fact["updated_at"], "where": "Settings", "text": text,
                    "answers": {field: value} if field in BY_ID else {}})
    return sorted(out, key=lambda s: s["at"])  # stable: a chat's inputs stay in order


def _day(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d")


def _usd(amount):
    return "unknown" if amount is None else f"${amount:,.2f}"


def render(user: dict, said: list, reviews: list, plans: list, plan_names: dict) -> str:
    """The memory section of the prompt: what the user said (newest kept when it's long), then past activity."""
    lines, size = [], 0
    for s in reversed(said):
        text = s["text"] if len(s["text"]) <= MAX_LINE_CHARS else s["text"][:MAX_LINE_CHARS] + " …"
        line = f"- {_day(s['at'])} · {s['where']}: {text}"
        if size + len(line) > MAX_MEMORY_CHARS:
            break
        lines.append(line)
        size += len(line)
    lines.reverse()

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

    if not lines and not past:
        return ""
    return "\n".join([
        "# What this user told you before",
        "",
        "Everything they said in earlier chats, in questionnaires and in Settings, oldest first. Use it and don't ask "
        "for it again: Python already fills the questionnaire and checks amounts against it. If they say something "
        "different now, the new answer wins. When you rely on something that changes during the year (the remaining "
        "deductible, what they've paid, their plan), say when they told you.",
        *([f"Their name: {user['name']}"] if user.get("name") else []),
        "",
        "## What they said",
        *(lines or ["- Nothing yet."]),
        "",
        "## Past activity",
        *(past or ["- Nothing yet."]),
    ])
