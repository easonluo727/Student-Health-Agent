"""Agent service for the web frontend: POST /turn -> text/event-stream.

Run:  uvicorn server:app --port 8000        (from workflow/)
The API is served at /turn, /upload, /plans and again under /api/; when web/out exists (the statically exported
frontend, built by the Dockerfile) it is served at /, so the deployed product is this one service.
Protocol: docs/frontend-protocol.md. Everything the model sees comes from harness/
(main prompt, skills, tools, hooks); this file only runs the loop and speaks SSE.
Form cards pause the turn; the user's answers resume it. Conversations are kept in SQLite (sessions.py), so a
redeploy doesn't lose them. Each finished compare_plans result is sent as the Arena report (arena_report.py).
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from accounts import COOKIE_NAME, AccountStore, google_client_id, verify_google_token
from arena_report import arena_report
from llm_client import llm_model, load_env, make_client
from harness import Harness
from harness.hooks import MAX_REPLY_RETRIES
from harness.reader import db_catalog, read_plan_pdf
from harness.skills.questionnaire.skill import answers_message, build_form
from plan_db import PlanDB
from sessions import SessionStore, upload_folder

load_env()
ROOT = Path(__file__).resolve().parents[1]
MAX_STEPS = 8
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "data" / "uploads"))  # /data/uploads on Railway
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ESTIMATE_STATUSES = {"estimated", "needs_information", "needs_review", "not_covered", "invalid_input"}
# The user asks to compare or choose plans: the Arena, whatever mode the home screen set.
COMPARE_INTENT = re.compile(r"\bcompar(?:e|ing|ison)\b|\bwhich plan\b|\bbest plan\b|\bchoose (?:a |between )?plans?\b"
                            r"|对比|比较|哪个(?:保险|计划)", re.I)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST"],  # /plans, /turn, /upload and /auth
    allow_headers=["*"],
    allow_credentials=True,  # the login cookie, when the frontend runs on another port in development
)
client = make_client(timeout=300.0)  # a shared endpoint can be slow under load
MODEL = llm_model()
# Price every scenario component in the rate store once, in the background (a few minutes), so turns don't wait.
threading.Thread(target=lambda: PlanDB().warm(), name="warm-rates", daemon=True).start()
STORE = SessionStore(upload_dir=UPLOAD_DIR)
ACCOUNTS = AccountStore()


def purge_forever():
    while True:
        STORE.purge()
        ACCOUNTS.purge()
        time.sleep(3600)


threading.Thread(target=purge_forever, name="purge-sessions", daemon=True).start()


class EndTurn(Exception):
    """A card is waiting for the user; stop the loop until they answer."""


class TurnRequest(BaseModel):
    session_id: str
    input: dict
    reply_target: dict | None = None
    context: dict | None = None  # {"mode": "calculate" | "review", "plan_ids": [...]} from the home screen


def available_plans():
    db = PlanDB()
    try:
        return [{"plan_id": p["plan_id"], "name": p["plan_name"], "insurer": p["insurer"],
                 "plan_year": p["plan_year"], "verified": bool(p["verified"]),
                 "uploaded": p["plan_id"].startswith("upload_")} for p in db.plans()]
    finally:
        db.con.close()


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
    plan_name = next((p["name"] for p in available_plans() if p["plan_id"] == result.get("plan_id")),
                     result.get("plan_id"))
    rows = []
    doctor = result.get("doctor")
    if prices.get("total") is not None:
        spread = (f" (most between ${prices['low']:,.2f} and ${prices['high']:,.2f})"
                  if prices.get("low") is not None and prices.get("high") is not None else "")
        note = (f"{doctor['name']}'s own negotiated rate, {doctor['distance_miles']} mi away" if doctor else
                "Median negotiated rate across Los Angeles providers in the network" + spread)
        rows.append({"label": "Allowed amount", "amount": prices["total"], "note": note,
                     "source": {"kind": "mrf", "label": f"{prices.get('network')} price list, {prices.get('mrf_file_date')}"}})
    for line in result.get("lines") or []:
        rows.append({"label": line["benefit"], "amount": line["patient_cost"],
                     "note": (line.get("quote") or "")[:120] or line["status"],
                     "source": {"kind": "policy", "label": plan_name, "page": line.get("source_page"),
                                "quote": (line.get("quote") or "")[:300] or None}})
    cost = result.get("patient_cost")
    tier = result.get("network_tier")
    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:8]}",
        "title": result.get("scenario_label") or "Estimate",
        "plan_name": plan_name,
        "status": result["status"],
        "patient_cost": {"mid": cost} if result["status"] == "estimated" and cost is not None else None,
        "rows": rows,
        "assumptions": [f"Network: {result.get('network')}" + (f" ({tier})" if tier else ""),
                        *([f"Priced at {doctor['name']}, {doctor['address']}"] if doctor else []),
                        *([f"Nearby doctors: ${spread['low']:,.2f} to ${spread['high']:,.2f}"]
                          if (spread := result.get("cost_range")) and spread["low"] != spread["high"] else []),
                        *([result["doctor_search"]] if result.get("doctor_search") and not doctor else [])],
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
    if call.name == "compare_plans" and "plans" in result and not result.get("auto_ask"):
        yield sse("report", {"report": arena_report(result)})

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
        if upload.get("content_type", "application/pdf").startswith("image/"):
            # Bill reading (roadmap step 2) isn't built: the model must not read amounts off the photo itself.
            notes.append(f"[The user uploaded a photo, {upload['name']}. You can't read photos yet. Say so, and if it "
                         "is a bill or EOB, ask for the service, the provider and the billed amount with the "
                         "questionnaire.]")
            continue
        search_id = f"doc_{uuid.uuid4().hex[:8]}"
        result = None
        sha256 = upload.get("sha256") or hashlib.sha256(upload["path"].read_bytes()).hexdigest()
        uploaded_plan_id = "upload_" + sha256[:24]
        policy_db = session.harness.db
        async for kind, data in read_plan_pdf(
                client, MODEL, upload["path"], upload["name"], db_catalog(policy_db),
                policy_db=policy_db, work_dir=upload["path"].parent, plan_id=uploaded_plan_id):
            if kind == "progress":
                yield sse("search_step", {**data, "search_id": search_id, "kind": "document"})
            else:
                result = data
        plan, read = result["plan"], json.dumps(result["identity"], ensure_ascii=False)
        intro = f"[The user uploaded {upload['name']} ({result['pages']} pages). The model read page 1: {read}. "
        if plan and result["uploaded"] and "benefit_rows" in plan:
            notes.append(intro + f"It was not in the verified catalog, so {plan['benefit_rows']} benefit rows were "
                         f"automatically extracted and saved in the shared policy database as plan_id "
                         f"{plan['plan_id']}. "
                         f"{plan['calculable_rows']} structurally complete rows can be estimated. The extraction "
                         "was not human verified; disclose that before every result. It is now available to other "
                         "users and in plan comparisons. Benefits needing negotiated rates remain unavailable "
                         "until a confirmed MRF network bridge exists.]")
        elif plan and result["uploaded"]:
            notes.append(intro + f"It matches the previously uploaded plan {plan['name']} (plan_id "
                         f"{plan['plan_id']}) in the shared database. Its extraction is still not human verified; "
                         "disclose that before every result.]")
        elif plan:
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
                "expect, their priorities, and their age, ZIP and student level (premiums depend on them) with the "
                "questionnaire, then use compare_plans.]")
    return None


async def turn_events(request):
    session = STORE.get(request.session_id)
    try:
        async for chunk in session_turn(session, request):
            yield chunk
    finally:  # also when the client goes away mid-stream
        STORE.save(request.session_id, session)


async def session_turn(session, request):
    hooks = session.harness.hooks
    data = request.input
    kind = data.get("type")
    try:
        if kind == "message":
            text = data.get("text", "").strip()
            note = apply_context(session, request.context)
            if (session.context or {}).get("mode") != "review" and COMPARE_INTENT.search(text):
                note = "\n".join(filter(None, [note, (
                    "[Mode: Arena. The user asked to compare plans, so use compare_plans (not estimate_cost) over the "
                    "plans they named, or every plan in the plan list if they named none. Ask with the questionnaire "
                    "for what it needs first: the care they expect, their priorities, and their age, ZIP and student "
                    "level.]")]))
            if note:
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
            else:  # closing a form means "I don't know" to each question, so Python doesn't ask them again
                hooks.on_form_answers(questions, {})
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
             "plan_year": plan["plan_year"], "verified": plan["verified"], "uploaded": plan["uploaded"]}
            for plan in available_plans()]


def sniff_upload(data):
    """(content_type, suffix) from the file's first bytes, or None if it is not a PDF or a photo."""
    if data.startswith(b"%PDF"):
        return "application/pdf", ".pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heif", b"hevc"):
        return "image/heic", ".heic"
    return None


@app.get("/integrations")
async def integrations():
    # Gmail sending (the appeal email) isn't built yet; the frontend shows "Connect Gmail" until it is.
    return {"gmail": {"connected": False, "email": None, "connect_url": None}}


class GoogleSignIn(BaseModel):
    credential: str  # the ID token from Google's sign-in button


@app.get("/auth/me")
async def auth_me(request: Request):
    # The client id is sent here rather than built into the frontend, so it is set once, on the server.
    return {"user": ACCOUNTS.user_for(request.cookies.get(COOKIE_NAME)), "google_client_id": google_client_id()}


@app.post("/auth/google")
async def auth_google(body: GoogleSignIn, request: Request, response: Response):
    client_id = google_client_id()
    if client_id is None:
        raise HTTPException(503, "Google sign-in is not configured on this server.")
    try:
        claims = await asyncio.to_thread(verify_google_token, body.credential, client_id)
    except ValueError as error:
        raise HTTPException(401, f"Google sign-in failed: {error}")
    user, token = ACCOUNTS.sign_in(claims)
    # Railway ends TLS at its proxy, so the app itself sees http; the proxy says what the browser used.
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE_NAME, token, max_age=ACCOUNTS.ttl, httponly=True, secure=https, samesite="lax",
                        path="/")
    return {"user": user}


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    ACCOUNTS.sign_out(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"user": None}


@app.post("/upload")
async def upload(session_id: str = Form(...), file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is larger than 25 MB.")
    kind = sniff_upload(data)
    if kind is None:
        raise HTTPException(415, "Only PDF files and photos (JPEG, PNG, WebP, HEIC) can be uploaded.")
    content_type, suffix = kind
    session = STORE.get(session_id)
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    folder = upload_folder(UPLOAD_DIR, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}{suffix}"
    path.write_bytes(data)
    name = Path(file.filename or f"upload{suffix}").name
    session.uploads[file_id] = {"path": path, "name": name, "content_type": content_type,
                                "sha256": hashlib.sha256(data).hexdigest()}
    STORE.save(session_id, session)
    return {"file_id": file_id, "name": name, "size": len(data), "content_type": content_type}


@app.post("/turn")
async def turn(request: TurnRequest):
    return StreamingResponse(turn_events(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform"})


# One service for the whole product: the API under /api (the built frontend calls it on the same origin), and the
# statically exported frontend (web/out, built by the Dockerfile) at /. Mounted last so the API routes win.
for _path, _endpoint, _methods in (("/api/turn", turn, ["POST"]), ("/api/upload", upload, ["POST"]),
                                   ("/api/plans", plans, ["GET"]),
                                   ("/api/integrations", integrations, ["GET"]),
                                   ("/api/auth/me", auth_me, ["GET"]), ("/api/auth/google", auth_google, ["POST"]),
                                   ("/api/auth/logout", auth_logout, ["POST"])):
    app.add_api_route(_path, _endpoint, methods=_methods)
WEB_DIR = Path(os.environ.get("WEB_DIR", ROOT / "web" / "out"))
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
