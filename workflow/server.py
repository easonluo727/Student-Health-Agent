"""Agent service for the web frontend: POST /turn -> text/event-stream.

Run:  uvicorn server:app --port 8000        (from workflow/)
The API is served at /turn, /upload, /plans and again under /api/; when web/out exists (the statically exported
frontend, built by the Dockerfile) it is served at /, so the deployed product is this one service.
Protocol: docs/frontend-protocol.md. Everything the model sees comes from harness/
(main prompt, skills, tools, hooks); this file only runs the loop and speaks SSE.
Form cards pause the turn; the user's answers resume it. The dashboard report is not wired yet.
"""

import asyncio
import json
import os
import threading
import uuid

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from llm_client import llm_model, load_env, make_client
from harness import Harness
from harness.hooks import MAX_REPLY_RETRIES
from harness.reader import db_catalog, read_plan_pdf
from harness.skills.questionnaire.skill import answers_message, build_form
from plan_db import PlanDB
from provider_directory import ProviderDirectory

load_env()
ROOT = Path(__file__).resolve().parents[1]
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
client = make_client(timeout=300.0)  # a shared endpoint can be slow under load
MODEL = llm_model()
# Price every scenario component in the rate store once, in the background (a few minutes), so turns don't wait.
threading.Thread(target=lambda: PlanDB().warm(), name="warm-rates", daemon=True).start()


class EndTurn(Exception):
    """A card is waiting for the user; stop the loop until they answer."""


class Session:
    def __init__(self):
        self.harness = Harness()
        self.history = []
        self.open_forms = {}  # request_id -> the form's question dicts
        self.uploads = {}  # file_id -> {"path", "name"}
        self.attachment_notes = []
        self.context = None  # the last home-screen mode applied


SESSIONS = {}


class TurnRequest(BaseModel):
    session_id: str
    input: dict
    reply_target: dict | None = None
    context: dict | None = None  # {"mode": "calculate" | "review", "plan_ids": [...]} from the home screen


def available_plans():
    return [{"plan_id": p["plan_id"], "name": p["plan_name"], "insurer": p["insurer"], "plan_year": p["plan_year"]}
            for p in PlanDB().plans()]


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


def estimate_card(result):
    """One row per benefit the visit is paid under."""
    prices = result.get("allowed_amount") or {}
    rows = []
    if prices.get("total") is not None:
        spread = (f" (most between ${prices['low']:,.2f} and ${prices['high']:,.2f})"
                  if prices.get("low") is not None and prices.get("high") is not None else "")
        rows.append({"label": "Allowed amount", "amount": prices["total"],
                     "note": "Median negotiated rate across Los Angeles providers in the network" + spread,
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
    """Run one tool call through hooks and tools, yielding SSE chunks. Raises EndTurn (form card) to pause the turn."""
    harness = session.harness
    args, result = harness.tools.parse(call.name, call.arguments)
    if result is None:
        result = harness.hooks.pre_tool(call.name, args)
    if result is None:
        result = harness.tools[call.name].run(args)
        result = harness.hooks.post_tool(call.name, args, result)

    if call.name == "estimate_cost" and result.get("status") in ESTIMATE_STATUSES:
        yield sse("estimate", estimate_card(result))

    if form := result.pop("form", None):  # the model asked with ask_questions
        session.history.append(tool_output(call, result))
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
            # Some chat templates (Qwen) allow a system message only at the start.
            session.history.append({"role": "user", "content": "[Automatic check, not the user] Your reply was rejected: " + feedback})
            continue
        for call in calls:
            try:
                async for chunk in run_call(session, call):
                    yield chunk
            except EndTurn:
                return
    raise RuntimeError("Tool-loop limit reached. Please try a more specific request.")


async def read_attachments(session, attachments):
    """Read each attached PDF and match it against the plan database. Yields SSE chunks."""
    notes = []
    for attachment in attachments:
        upload = session.uploads.get(attachment.get("file_id"))
        if upload is None:
            notes.append(f"[An attachment ({attachment.get('name')}) has expired; ask the user to upload it again.]")
            continue
        search_id = f"doc_{uuid.uuid4().hex[:8]}"
        result = None
        async for kind, data in read_plan_pdf(client, MODEL, upload["path"], upload["name"], db_catalog()):
            if kind == "progress":
                yield sse("search_step", {**data, "search_id": search_id, "kind": "document"})
            else:
                result = data
        plan, read = result["plan"], json.dumps(result["identity"], ensure_ascii=False)
        intro = f"[The user uploaded {upload['name']} ({result['pages']} pages). The model read page 1: {read}. "
        if plan:
            notes.append(intro + f"It matches {plan['name']} in the plan database: use plan_id {plan['plan_id']}. "
                         "Benefit numbers come from the database, not from the upload.]")
        else:
            notes.append(intro + "It doesn't match any plan in the database, so you can't price it or quote its "
                         "benefits. Describe only what page 1 shows, and offer the plans you can price.]")
    session.attachment_notes = notes


def apply_context(session, context):
    """A note for the model about the home-screen mode, or None when nothing changed."""
    if not context or context == session.context:
        return None
    session.context = context
    plans = {plan["plan_id"]: plan for plan in available_plans()}
    chosen = [plans[pid] for pid in context.get("plan_ids") or [] if pid in plans]
    names = "; ".join(f"{p['name']} (plan_id {p['plan_id']})" for p in chosen) or "any plans in the plan list"
    if context.get("mode") == "calculate":
        return (f"[Mode: cost of a visit. Plans the user picked: {names}. Ask which visit they mean if they "
                "haven't said, then use estimate_cost.]")
    if context.get("mode") == "review":
        return (f"[Mode: Arena. The user wants to compare and choose between: {names}. Ask about the care they "
                "expect and their priorities with the questionnaire, then use compare_plans.]")
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
                text = f"{note}\n{text}"
            if request.reply_target:
                text = f"(About: {request.reply_target.get('label')}) {text}"
            if attachments := data.get("attachments"):
                async for chunk in read_attachments(session, attachments):
                    yield chunk
                text = "\n".join([*session.attachment_notes, text])
            hooks.on_user_message(text)
            session.history.append({"role": "user", "content": text})
        elif kind in ("form_response", "form_dismiss") and data.get("request_id") in session.open_forms:
            questions = session.open_forms.pop(data["request_id"])
            if kind == "form_response":
                answers = data.get("answers") or {}
                hooks.on_form_answers(questions, answers)
                session.history.append({"role": "user", "content": answers_message(questions, answers)})
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


@app.get("/api/providers/options")
async def provider_options():
    """Read-only service and plan choices for the directory form."""
    db = PlanDB()
    try:
        plans = db.plans()
        confirmed = {}
        for row in db.con.execute("SELECT plan_id, network_tier FROM plan_networks WHERE confirmed=1"):
            confirmed.setdefault(row["plan_id"], []).append(row["network_tier"])
        return {"scenarios": db.scenarios(), "plans": [
            {"plan_id": p["plan_id"], "name": p["plan_name"],
             "confirmed_tiers": confirmed.get(p["plan_id"], [])} for p in plans]}
    finally:
        db.con.close()


@app.get("/api/providers/search")
async def provider_search(latitude: float | None = None, longitude: float | None = None,
                          zip_code: str | None = None, radius_miles: float = 10,
                          scenario: str | None = None, cpt: str | None = None,
                          taxonomy: str | None = None, plan_ids: list[str] = Query(default=[]),
                          plan_tiers: list[str] = Query(default=[]),
                          remaining_deductible: float | None = None,
                          limit: int = 20, offset: int = 0, sort: str = "nearest"):
    """Search actual NPPES practice locations; every price is strictly NPI-specific."""
    try:
        tiers = dict(item.split("|", 1) for item in plan_tiers)
        if len(tiers) != len(plan_tiers):
            raise ValueError("Duplicate plan tier")
        return await asyncio.to_thread(
            ProviderDirectory().search, latitude=latitude, longitude=longitude,
            zip_code=zip_code, radius_miles=radius_miles, scenario=scenario,
            cpt=cpt, taxonomy=taxonomy, plan_ids=plan_ids, plan_tiers=tiers,
            remaining_deductible=remaining_deductible, limit=limit, offset=offset, sort=sort)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


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
