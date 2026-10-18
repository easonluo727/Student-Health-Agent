"""Agent service for the web frontend: POST /turn -> text/event-stream.

Run:  uvicorn server:app --port 8000        (from workflow/)
The API is served at /turn, /upload, /plans and again under /api/; when web/out exists (the statically exported
frontend, built by the Dockerfile) it is served at /, so the deployed product is this one service.
Protocol: docs/frontend-protocol.md. Everything the model sees comes from harness/
(main prompt, skills, tools, hooks); this file only runs the loop and speaks SSE.
Confirm and form cards pause the turn; the user's answer resumes it.

Optional SEARCH_WORKER_URL (search_worker/worker.py) adds the search_insurer tool.
AGENT_MODE picks "plans" (default: cost estimates and the Arena on data/plans/policy.sqlite) or "legacy";
in legacy mode AGENT_PLAN and AGENT_RULES pick the plan and its rules file (default prime_100).
The dashboard report is not wired yet.
"""

import asyncio
import json
import os
import uuid

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_tools import DEFAULT_RULES_PATH
from amd_client import amd_model, load_env, make_client
from harness import Harness
from harness.reader import ROOT, db_catalog, load_catalog, read_plan_pdf
from harness.hooks import MAX_REPLY_RETRIES
from harness.hooks import RATE_OPTION_PREFIX
from harness.skills.questionnaire.skill import answers_message, build_form, choice_form

load_env()
MODE = os.environ.get("AGENT_MODE", "plans")  # "plans": cost + Arena on the plan database; "legacy": one plan's JSON
PLAN = os.environ.get("AGENT_PLAN", "prime_100")
RULES = os.environ.get("AGENT_RULES", str(DEFAULT_RULES_PATH))
SEARCH_WORKER_URL = os.environ.get("SEARCH_WORKER_URL")
MAX_STEPS = 8
UPLOAD_DIR = ROOT / "data" / "uploads"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ESTIMATE_STATUSES = {"estimated", "needs_information", "needs_review", "not_covered", "invalid_input"}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST"],  # /plans, /turn and /upload
    allow_headers=["*"],
)
client = make_client(timeout=300.0)  # the shared AMD endpoint can be slow under load
MODEL = amd_model()


class PendingConfirm(Exception):
    def __init__(self, summary):
        super().__init__(summary)
        self.summary = summary


class EndTurn(Exception):
    """A card is waiting for the user; stop the loop until they answer."""


class Session:
    def __init__(self):
        self.harness = Harness(PLAN, confirm=self._confirm, search_worker_url=SEARCH_WORKER_URL, rules_path=RULES,
                               mode=MODE)
        self.plan_id = PLAN if MODE == "legacy" else None
        self.history = []
        self.approved = set()  # confirm summaries the user accepted
        self.pending_confirm = None  # (request_id, summary, call)
        self.open_forms = {}  # request_id -> the form's question dicts
        self.uploads = {}  # file_id -> {"path", "name"}
        self.context = None  # the last home-screen mode applied

    def switch_plan(self, plan_id, rules_path):
        """Use another plan from here on, keeping the conversation and what the user told us."""
        if MODE == "plans":
            return  # every plan in the database is available at once
        old = self.harness.hooks
        self.harness = Harness(plan_id, confirm=self._confirm, search_worker_url=SEARCH_WORKER_URL,
                               rules_path=rules_path)
        self.plan_id = plan_id
        new = self.harness.hooks
        new.user_texts = list(old.user_texts)
        new.answers = dict(old.answers)
        for text in old.user_texts:
            new.guard.record_user(text)
        for value in old.answers.values():
            if isinstance(value, (int, float)):
                new.guard.record_user(str(value))

    def _confirm(self, summary):
        # guards.post_tool calls this synchronously; the web answer arrives in a later request.
        if summary in self.approved:
            return True, "yes"
        raise PendingConfirm(summary)


SESSIONS = {}


class TurnRequest(BaseModel):
    session_id: str
    input: dict
    reply_target: dict | None = None
    context: dict | None = None  # {"mode": "calculate" | "review", "plan_ids": [...]} from the home screen


def available_plans():
    """Plans the agent can price: the plan database (plans mode), or catalog plans whose rules file exists."""
    if MODE == "plans":
        from plan_db import PlanDB

        return [{"plan_id": p["plan_id"], "name": p["plan_name"], "insurer": p["insurer"], "plan_year": p["plan_year"],
                 "rules": None} for p in PlanDB().plans()]
    return [plan for plan in load_catalog() if (ROOT / plan["rules"]).exists()]


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def say(text):
    message_id = f"msg_{uuid.uuid4().hex[:8]}"
    return [
        sse("message_start", {"message_id": message_id}),
        sse("message_delta", {"message_id": message_id, "text": text}),
        sse("message_end", {"message_id": message_id}),
    ]


def tool_output(call, result):
    return {"type": "function_call_output", "call_id": call.call_id,
            "output": json.dumps(result, ensure_ascii=False, allow_nan=False)}


def estimate_card(session, args, result):
    harness = session.harness
    quote = result.get("rate_quote") or {}
    rule = harness.policy.get_policy_rule(args["rule_id"]).get("rule") or {}
    policy_source = {
        "kind": "policy",
        "label": rule.get("plan") or harness.policy.selected_plan,
        "page": rule.get("source_page"),
        "quote": (rule.get("source_text") or "")[:300] or None,
    }

    def user_source(field):
        kind = harness.hooks.provenance(field, args.get(field))
        return {"kind": kind, "label": "Your questionnaire" if kind == "user_form" else "Your message"}

    deductible_note = {True: "Deductible applies", False: "Deductible doesn't apply"}.get(
        rule.get("deductible_applies"), "Unknown whether the deductible applies"
    )
    if quote:
        allowed_row = {
            "label": "Allowed amount", "amount": quote.get("allowed_amount"),
            "note": "Typical negotiated rate across the network" if quote.get("aggregate") else "Negotiated rate",
            "source": {"kind": "mrf", "label": f"{quote.get('insurer')} {quote.get('mrf_network')} price list, "
                                              f"{quote.get('mrf_snapshot_date')}"},
        }
    else:
        allowed_row = {
            "label": "Allowed amount", "amount": args.get("allowed_amount"),
            "note": "Not provided yet" if args.get("allowed_amount") is None else "You provided",
            "source": user_source("allowed_amount"),
        }
    rows = [
        allowed_row,
        {"label": "Remaining deductible", "amount": args.get("remaining_deductible"), "note": deductible_note,
         "source": user_source("remaining_deductible")},
    ]
    if rule.get("copay_usd") is not None:
        rows.append({"label": "Copay", "amount": rule["copay_usd"], "source": policy_source})
    if rule.get("patient_pays_percentage") is not None:
        rows.append({"label": "Coinsurance", "amount": None,
                     "note": f"You pay {rule['patient_pays_percentage']}%", "source": policy_source})
    cost = result.get("patient_cost")
    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:8]}",
        "title": (rule.get("service") or "Estimate").split("\n")[0].capitalize(),
        "plan_name": harness.policy.selected_plan,
        "status": result["status"],
        "patient_cost": {"mid": cost} if result["status"] == "estimated" and cost is not None else None,
        "rows": rows,
        "assumptions": [f"Network: {rule.get('network') or 'unknown'}"],
        "message": result.get("message"),
    }


def plan_estimate_card(result):
    """Estimate card for plans mode: one row per benefit the visit is paid under."""
    prices = result.get("allowed_amount") or {}
    rows = []
    if prices.get("total") is not None:
        rows.append({"label": "Allowed amount", "amount": prices["total"],
                     "note": "Typical negotiated rate across the network",
                     "source": {"kind": "mrf", "label": f"{prices.get('network')} price list, {prices.get('mrf_file_date')}"}})
    for line in result.get("lines") or []:
        rows.append({"label": line["benefit"], "amount": line["patient_cost"],
                     "note": (line.get("quote") or "")[:120] or line["status"],
                     "source": {"kind": "policy", "label": result["plan_id"], "page": line.get("source_page"),
                                "quote": (line.get("quote") or "")[:300] or None}})
    cost = result.get("patient_cost")
    tier = result.get("network_tier")
    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:8]}",
        "title": result.get("scenario_label") or "Estimate",
        "plan_name": result.get("plan_id"),
        "status": result["status"],
        "patient_cost": {"mid": cost} if result["status"] == "estimated" and cost is not None else None,
        "rows": rows,
        "assumptions": [f"Network: {result.get('network')}" + (f" ({tier})" if tier else "")],
        "message": result.get("message"),
    }


def send_form(session, form):
    session.open_forms[form["request_id"]] = form["questions"]
    return sse("form", form)


async def run_call(session, call):
    """Run one tool call through hooks and tools, yielding SSE chunks.

    Raises PendingConfirm (confirm card) or EndTurn (form card) to pause the turn."""
    harness = session.harness
    args, result = harness.tools.parse(call.name, call.arguments)
    if result is None:
        result = harness.hooks.pre_tool(call.name, args)
    if result is None:
        tool = harness.tools[call.name]
        if tool.stream:
            search_id = f"search_{uuid.uuid4().hex[:8]}"
            async for kind, data in tool.stream(args):
                if kind == "progress":
                    yield sse("search_step", {**data, "search_id": search_id})
                else:
                    result = data
        else:
            result = tool.run(args)
        result = harness.hooks.post_tool(call.name, args, result)

    if call.name in ("calculate_estimate", "calculate_with_quote") and result.get("status") in ESTIMATE_STATUSES:
        yield sse("estimate", estimate_card(session, args, result))
    if call.name == "estimate_cost" and result.get("status") in ESTIMATE_STATUSES:
        yield sse("estimate", plan_estimate_card(result))

    if form := result.pop("form", None):  # the model asked with ask_questions
        session.history.append(tool_output(call, result))
        yield send_form(session, form)
        raise EndTurn
    if clarify := result.pop("clarify", None):  # the price depends on a choice: Python asks
        form = choice_form(RATE_OPTION_PREFIX + args["rule_id"], clarify["question"],
                           [{"value": o["option_id"], "label": o["label"]} for o in clarify["options"]],
                           "One quick question", "The price depends on this.")
        session.history.append(tool_output(call, {
            **result, "message": "Python sent the user this question as a form. Wait for the answer.",
        }))
        yield send_form(session, form)
        raise EndTurn
    if ask := result.pop("auto_ask", None):  # the calculator is missing inputs: Python asks
        form = build_form(ask, "A bit more to calculate this",
                          "The calculator needs these to work out your cost.")
        session.history.append(tool_output(call, {
            **result, "message": (result.get("message") or "")
            + " Python sent the user a form for: " + ", ".join(ask) + ". Wait for the answers.",
        }))
        yield send_form(session, form)
        raise EndTurn
    session.history.append(tool_output(call, result))


async def agent_loop(session):
    harness = session.harness
    rejected = 0
    for _ in range(MAX_STEPS):
        response = await asyncio.to_thread(
            client.responses.create,
            model=MODEL,
            instructions=harness.system_prompt,
            input=session.history,
            tools=harness.tools.definitions(),
            parallel_tool_calls=False,
            max_output_tokens=6000,
            store=False,
        )
        if response.status != "completed":
            raise RuntimeError("The model response was incomplete.")
        session.history.extend(response.output)
        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            text = response.output_text.strip()
            if not text:
                raise RuntimeError("The model returned no answer.")
            feedback = harness.hooks.check_reply(text)
            if feedback is None:
                for chunk in say(text):
                    yield chunk
                return
            rejected += 1
            if rejected > MAX_REPLY_RETRIES:
                reply = harness.hooks.fallback_reply()
                session.history.append({"role": "assistant", "content": reply})
                for chunk in say(reply):
                    yield chunk
                return
            # Qwen's chat template allows a system message only at the start.
            session.history.append({"role": "user", "content": "[Automatic check, not the user] Your reply was rejected: " + feedback})
            continue
        for call in calls:
            try:
                async for chunk in run_call(session, call):
                    yield chunk
            except PendingConfirm as pending:
                request_id = f"confirm_{uuid.uuid4().hex[:8]}"
                session.pending_confirm = (request_id, pending.summary, call)
                yield sse("confirm", confirm_card(request_id, pending.summary))
                return
            except EndTurn:
                return
    raise RuntimeError("Tool-loop limit reached. Please try a more specific request.")


def confirm_card(request_id, summary):
    items = []
    for line in summary.splitlines()[1:]:
        label, _, value = line.strip().partition(":")
        items.append({"label": label.strip(), "value": value.strip(), "source": "inferred"})
    return {"request_id": request_id, "title": "Before I show an estimate, is this right?", "items": items}


async def resume_confirm(session, data):
    _, summary, call = session.pending_confirm
    session.pending_confirm = None
    if data.get("confirmed"):
        session.approved.add(summary)
        async for chunk in run_call(session, call):
            yield chunk
        return
    correction = data.get("correction") or "Something is off."
    session.harness.hooks.guard.record_user(correction)
    session.history.append(tool_output(call, {
        "status": "user_rejected", "user_reply": correction,
        "message": "The user did not confirm these details, so no estimate was released. "
                   "Use the user's reply and ask what still needs to change.",
    }))


def rate_option_note(questions, answers):
    """For a price clarification, tell the model exactly which call comes next."""
    for q in questions:
        if q["field"].startswith(RATE_OPTION_PREFIX):
            value = (answers.get(q["field"]) or {}).get("value")
            rule_id = q["field"][len(RATE_OPTION_PREFIX):]
            if value:
                return f"Now call get_allowed_amount with rule_id {rule_id!r} and option_id {value!r}."
            return "The user doesn't know. Explain the options briefly, or ask for the allowed amount instead."
    return None


async def read_attachments(session, attachments):
    """Read each attached PDF and switch to its plan when it matches the library. Yields SSE chunks."""
    notes = []
    for attachment in attachments:
        upload = session.uploads.get(attachment.get("file_id"))
        if upload is None:
            notes.append(f"[An attachment ({attachment.get('name')}) has expired; ask the user to upload it again.]")
            continue
        search_id = f"doc_{uuid.uuid4().hex[:8]}"
        result = None
        catalog = db_catalog() if MODE == "plans" else None
        async for kind, data in read_plan_pdf(client, MODEL, upload["path"], upload["name"], catalog):
            if kind == "progress":
                yield sse("search_step", {**data, "search_id": search_id, "kind": "document"})
            else:
                result = data
        plan, identity = result["plan"], result["identity"]
        read = json.dumps(identity, ensure_ascii=False)
        if MODE == "plans" and plan:
            notes.append(f"[The user uploaded {upload['name']} ({result['pages']} pages). The AMD model read page 1: "
                         f"{read}. It matches {plan['name']} in the plan database: use plan_id {plan['plan_id']}. "
                         "Benefit numbers come from the database, not from the upload.]")
        elif MODE == "plans":
            notes.append(f"[The user uploaded {upload['name']} ({result['pages']} pages). The AMD model read page 1: "
                         f"{read}. It doesn't match any plan in the database, so you can't price it or quote its "
                         "benefits. Describe only what page 1 shows, and offer the plans you can price.]")
        elif plan and result["rules_path"]:
            session.switch_plan(plan["plan_id"], result["rules_path"])
            notes.append(f"[The user uploaded {upload['name']} ({result['pages']} pages). The AMD model read page 1: "
                         f"{read}. It matches {plan['name']} in the verified plan library, so this conversation now "
                         f"uses that plan's catalog. Benefit numbers come from the catalog, not from the upload.]")
        elif plan:
            notes.append(f"[The user uploaded {upload['name']}. It is {plan['name']}, but that plan's rules aren't "
                         f"loaded yet, so you can't price it. Page 1 says: {read}. Say so plainly.]")
        else:
            notes.append(f"[The user uploaded {upload['name']} ({result['pages']} pages). Page 1 says: {read}. "
                         "This plan isn't in the verified plan library, so you can't price it or quote its "
                         "benefits. Describe only what page 1 shows, and say what you can do instead.]")
    session.attachment_notes = notes


def apply_context(session, context):
    """Switch plan for the chosen mode and return a note for the model, or None when nothing changed."""
    if not context or context == session.context:
        return None
    session.context = context
    plans = {plan["plan_id"]: plan for plan in available_plans()}
    chosen = [plans[pid] for pid in context.get("plan_ids") or [] if pid in plans]
    if MODE == "plans":
        names = "; ".join(f"{p['name']} (plan_id {p['plan_id']})" for p in chosen) or "any plans in the plan list"
        if context.get("mode") == "calculate":
            return (f"[Mode: cost of a visit. Plans the user picked: {names}. Ask which visit they mean if they "
                    "haven't said, then use estimate_cost.]")
        if context.get("mode") == "review":
            return (f"[Mode: Arena. The user wants to compare and choose between: {names}. Ask about the care they "
                    "expect and their priorities with the questionnaire, then use compare_plans.]")
        return None
    if context.get("mode") == "calculate":
        if len(chosen) == 1 and chosen[0]["plan_id"] != session.plan_id:
            session.switch_plan(chosen[0]["plan_id"], str(ROOT / chosen[0]["rules"]))
        if len(chosen) == 1:
            return (f"[Mode: calculate. The user picked {chosen[0]['name']} from the plan library and wants to know "
                    "what care will cost on it. Ask which visit or service they mean if they haven't said.]")
        return "[Mode: calculate. The user wants to know what care will cost.]"
    if context.get("mode") == "review":
        names = "; ".join(plan["name"] for plan in chosen) or "the plans in the library"
        current = plans.get(session.plan_id, {}).get("name", session.plan_id)
        return (f"[Mode: review. The user wants to compare these plans and find the one that fits them: {names}. Ask about them "
                f"and their priorities with the questionnaire before recommending. Only {current} is loaded for "
                "pricing in this session; say so instead of pricing the others.]")
    return None


async def turn_events(request):
    session = SESSIONS.setdefault(request.session_id, Session())
    hooks = session.harness.hooks
    data = request.input
    kind = data.get("type")
    try:
        if kind == "message":
            text = data.get("text", "").strip()
            if note := apply_context(session, request.context):
                hooks = session.harness.hooks  # the plan may have changed
                text = f"{note}\n{text}"
            if request.reply_target:
                text = f"(About: {request.reply_target.get('label')}) {text}"
            session.pending_confirm = None
            if attachments := data.get("attachments"):
                async for chunk in read_attachments(session, attachments):
                    yield chunk
                hooks = session.harness.hooks  # the plan may have changed
                text = "\n".join([*session.attachment_notes, text])
            hooks.on_user_message(text)
            session.history.append({"role": "user", "content": text})
        elif kind == "confirm_response" and session.pending_confirm and session.pending_confirm[0] == data.get("request_id"):
            async for chunk in resume_confirm(session, data):
                yield chunk
        elif kind in ("form_response", "form_dismiss") and data.get("request_id") in session.open_forms:
            questions = session.open_forms.pop(data["request_id"])
            if kind == "form_response":
                answers = data.get("answers") or {}
                hooks.on_form_answers(questions, answers)
                session.history.append({"role": "user", "content": answers_message(
                    questions, answers, note=rate_option_note(questions, answers))})
            else:
                session.history.append({"role": "user", "content": (
                    "[The user closed the questionnaire without answering. Continue with what you have.]")})
        else:
            for chunk in say("That card has expired. Please type your question instead."):
                yield chunk
            yield sse("done", {})
            return
        async for chunk in agent_loop(session):
            yield chunk
    except EndTurn:
        pass  # a card resumed by the user opened another card
    except Exception as error:  # report, never leak request contents or keys
        yield sse("error", {"message": f"{type(error).__name__}: {str(error)[:300]}"})
    yield sse("done", {})


@app.get("/plans")
async def plans():
    return [{"plan_id": plan["plan_id"], "name": plan["name"], "insurer": plan["insurer"],
             "plan_year": plan["plan_year"]} for plan in available_plans()]


@app.post("/upload")
async def upload(session_id: str = Form(...), file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is larger than 25 MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "Only PDF files can be uploaded.")
    session = SESSIONS.setdefault(session_id, Session())
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    folder = UPLOAD_DIR / "".join(c for c in session_id if c.isalnum() or c == "-")[:64]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}.pdf"
    path.write_bytes(data)
    name = Path(file.filename or "plan.pdf").name
    session.uploads[file_id] = {"path": path, "name": name}
    return {"file_id": file_id, "name": name, "size": len(data)}


@app.post("/turn")
async def turn(request: TurnRequest):
    return StreamingResponse(turn_events(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform"})


# One service for the whole product: the API under /api (the built frontend calls it on the same origin), and the
# statically exported frontend (web/out, built by the Dockerfile) at /. Mounted last so the API routes win.
for _path, _endpoint, _methods in (("/api/turn", turn, ["POST"]), ("/api/upload", upload, ["POST"]),
                                   ("/api/plans", plans, ["GET"])):
    app.add_api_route(_path, _endpoint, methods=_methods)
WEB_DIR = Path(os.environ.get("WEB_DIR", ROOT / "web" / "out"))
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
