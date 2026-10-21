"""Questionnaire skill: the ask_questions tool, form building, and the question-bank hint.

The model picks question IDs from the bank (an enum in the tool schema, so it can't
invent questions). Python builds the form card from the bank, and turns submitted
answers into a message the model reads. The rules about which questions may be asked
and which prefills are allowed live in hooks.py.
"""

import json
import uuid
from pathlib import Path

from .. import Skill, read_skill_md
from ...mcp import Tool, schema
from .bank import BY_ID, GROUPS, QUESTIONS

MAX_QUESTIONS = 8
FORM_FIELDS = ("kind", "label", "help", "options", "unit", "min", "max", "scale_labels")


def bank_hint():
    """The question bank as it appears in the prompt: one table per group."""
    sections = []
    for group, title in GROUPS.items():
        rows = ["| ID | Asks | Type | Ask when |", "| --- | --- | --- | --- |"]
        for q in QUESTIONS:
            if q["group"] != group:
                continue
            kind = q["kind"]
            if q.get("options"):
                kind += ": " + ", ".join(o["value"] for o in q["options"])
            rows.append(f"| `{q['id']}` | {q['label']} | {kind} | {q['when']} |")
        sections.append(f"**{group} · {title}**\n\n" + "\n".join(rows))
    return "\n\n".join(sections)


def build_form(question_ids, title, intro=None, prefill=None):
    """A Form event payload (docs/frontend-protocol.md). prefill: {question_id: value}, already checked."""
    prefill = prefill or {}
    questions = []
    for qid in question_ids:
        q = BY_ID[qid]
        item = {"field": qid, **{key: q[key] for key in FORM_FIELDS if key in q}}
        if qid in prefill:
            item["prefill"] = {"value": prefill[qid], "source": "user_message"}
        questions.append(item)
    return {"request_id": f"form_{uuid.uuid4().hex[:8]}", "title": title, "intro": intro, "questions": questions}


def answers_message(questions, answers):
    """How submitted answers are shown to the model.

    questions: the form's question dicts. answers: {field: {"value", "unknown"}}."""
    lines = ["[Questionnaire answers · source: user_form]"]
    for q in questions:
        answer = answers.get(q["field"]) or {"value": None, "unknown": True}
        if answer.get("unknown") or answer.get("value") in (None, "", []):
            shown = "unknown (the user doesn't know)"
        else:
            shown = json.dumps(answer["value"], ensure_ascii=False)
        lines.append(f"- `{q['field']}` ({q['label']}): {shown}")
    lines.append("These come from the user. Pass numbers to the calculator exactly as given.")
    return "\n".join(lines)


def _run(args):
    form = build_form(args["question_ids"], args["title"], args["intro"], args.get("checked_prefill"))
    return {"status": "form_sent", "request_id": form["request_id"], "question_ids": args["question_ids"],
            "message": "The form is in the chat. Stop here and wait for the user's answers.", "form": form}


def build():
    ids = [q["id"] for q in QUESTIONS]
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"), QUESTION_BANK=bank_hint())
    tool = Tool(
        name="ask_questions",
        description=("Send the user a questionnaire card built from the question bank. Use the IDs from the "
                     "question bank in the questionnaire skill. Stop after calling it."),
        parameters=schema(
            question_ids={"type": "array", "items": {"type": "string", "enum": ids}},
            title={"type": "string", "description": "Short form title, for example 'How you use care'."},
            intro={"type": "string", "description": "One sentence saying why you're asking."},
            prefill={
                "type": "array",
                "description": "Only values the user already stated in this conversation. Use [] when none.",
                "items": schema(question_id={"type": "string", "enum": ids}, value={"type": "string"}),
            },
        ),
        run=_run,
    )
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=[tool])
