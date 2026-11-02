"""Pages skill: the agent decides when a tool result becomes a page on the canvas (cards.py builds it).

  show_page   one stored result (by result_id) as a page, with the agent's picks and a sentence on each.

The data tools' results are stored by Python (store_result) with an id the model sees; the page is built from the
stored result, never from the model's text. Results live on the harness (Harness.results), saved with the session:
result_id -> {"kind", "tool", "args", "result", "shown"}.
"""

from pathlib import Path

from guards import dollar_amounts_in

from .. import Skill, read_skill_md
from ...mcp import Tool, schema

# Data tool -> page kind, and when its result can be a page.
KINDS = {
    "find_providers": ("doctors", lambda r: r.get("status") == "ok" and bool(r.get("providers"))),
    "compare_plans": ("compare", lambda r: "plans" in r and not r.get("auto_ask")),
    "estimate_cost": ("estimate", lambda r: r.get("status") in ("estimated", "needs_review", "not_covered")),
    "audit_bill": ("bill", lambda r: r.get("status") in ("audited", "unavailable")),
}
# Pages the user should get whenever the result exists (the reply is checked for them; see missing).
REQUIRED = ("doctors", "compare", "bill")
MAX_PICKS = 5
MAX_NOTE = 200
MAX_TITLE = 80


def store_result(results: dict, tool: str, args: dict, result: dict) -> str | None:
    """Keep a data tool's result for show_page; returns its id (also added to the result), or None."""
    if tool not in KINDS or not KINDS[tool][1](result):
        return None
    kind = KINDS[tool][0]
    result_id = f"{kind}_{sum(1 for r in results.values() if r['kind'] == kind) + 1}"
    results[result_id] = {"kind": kind, "tool": tool, "args": args, "result": dict(result), "shown": False}
    result["result_id"] = result_id
    return result_id


def missing(results: dict, ids) -> list[str]:
    """Of these result ids (this turn's), the ones that must be shown and weren't."""
    return [i for i in ids if i in results and results[i]["kind"] in REQUIRED and not results[i]["shown"]]


def named(results: dict, text: str, shown_this_turn) -> list[str]:
    """The latest doctor result whose doctors the reply names, unless shown this turn: doctors belong on the page,
    also when the agent answers from an earlier search instead of searching again."""
    low = text.lower()
    hits = [rid for rid, r in results.items() if r["kind"] == "doctors"
            and any(p.get("name") and p["name"].lower() in low for p in r["result"]["providers"])]
    return [] if any(rid in shown_this_turn for rid in hits) else hits[-1:]


def _validate(results, guard, args):
    stored = results.get(args["result_id"])
    if stored is None:
        known = ", ".join(results) or "none yet"
        raise ValueError(f"Unknown result_id {args['result_id']!r}; results in this conversation: {known}.")
    if args["title"] is not None and len(args["title"]) > MAX_TITLE:
        raise ValueError(f"title must be at most {MAX_TITLE} characters.")
    notes = {n["id"]: n["text"].strip() for n in args["notes"]}
    if stored["kind"] != "doctors":
        if args["highlight"] or notes:
            raise ValueError("highlight and notes are for doctor pages; leave them empty.")
        return
    npis = {p["npi"] for p in stored["result"]["providers"]}
    picks = list(dict.fromkeys(args["highlight"]))
    if len(picks) > MAX_PICKS:
        raise ValueError(f"Highlight at most {MAX_PICKS} doctors.")
    if unknown := [n for n in picks if n not in npis]:
        raise ValueError(f"Not in {args['result_id']}: {', '.join(unknown)}. Use npi values from that result.")
    if extra := [n for n in notes if n not in picks]:
        raise ValueError(f"Notes are for highlighted doctors only: {', '.join(extra)}.")
    if long := [n for n, text in notes.items() if len(text) > MAX_NOTE]:
        raise ValueError(f"Keep each note to one sentence (at most {MAX_NOTE} characters): {', '.join(long)}.")
    untraced = dollar_amounts_in(" ".join(notes.values())) - guard._user_numbers - guard._tool_numbers
    if untraced:
        raise ValueError("Notes contain amounts that are not in the tool results: "
                         + ", ".join(f"${n:,}" for n in sorted(untraced)) + ". Use only amounts from the result.")


def show(results, args):
    stored = results[args["result_id"]]
    stored["shown"] = True
    return {"status": "shown", "result_id": args["result_id"], "kind": stored["kind"],
            "message": ("The page is open beside the chat and already lists every item with its numbers, address and "
                        "phone. Reply with the conclusion in one or two sentences (your top pick and why) and say the "
                        "details are on the page. Do not list the items again in the chat."),
            "_page": {"kind": stored["kind"], "result_id": args["result_id"],
                      "highlight": list(dict.fromkeys(args["highlight"])),
                      "notes": {n["id"]: n["text"].strip() for n in args["notes"]}, "title": args["title"]}}


def build(results: dict, guard):
    meta, body = read_skill_md(Path(__file__).with_name("SKILL.md"))
    tool = Tool(
        name="show_page",
        description=("Show a tool result (by its result_id) as a page on the canvas beside the chat: doctors on a "
                     "map, plans side by side, a cost breakdown or a checked bill. Python builds the page."),
        parameters=schema(
            result_id={"type": "string"},
            highlight={"type": "array", "items": {"type": "string"}},
            notes={"type": "array", "items": schema(id={"type": "string"}, text={"type": "string"})},
            title={"type": ["string", "null"]},
        ),
        run=lambda args: show(results, args),
        validate=lambda args: _validate(results, guard, args),
    )
    return Skill(name=meta["name"], description=meta["description"], instructions=body, tools=[tool])
