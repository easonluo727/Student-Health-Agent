"""Agent service for the web frontend: POST /turn -> text/event-stream.

Run:  uvicorn server:app --port 8000        (from workflow/)
The API is served at /turn, /upload, /plans and again under /api/; when web/out exists (the statically exported
frontend, built by the Dockerfile) it is served at /, so the deployed product is this one service.
Protocol: docs/frontend-protocol.md. Everything the model sees comes from harness/
(main prompt, skills, tools, hooks); this file only runs the loop and speaks SSE.
Form cards pause the turn; the user's answers resume it. Conversations are kept in the app database (sessions.py,
appdb.py), so a redeploy doesn't lose them; a signed-in user owns theirs and reaches them under /me. Each finished
compare_plans result is sent as the Arena report (arena_report.py).
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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from accounts import COOKIE_NAME, AccountStore, google_client_id, verify_google_token
from arena_report import arena_report
from llm_client import llm_model, load_env, make_client
from harness import Harness
from harness.hooks import MAX_REPLY_RETRIES
from harness.skills.questionnaire.skill import answers_message, build_form
from plan_db import PlanDB
from memory import FIELDS as MEMORY_FIELDS, QUESTION_FIELDS, MemoryStore, question_answers
from sessions import NotYours, SessionStore, appeal_drafted, bill_review, file_kind, upload_folder

load_env()
ROOT = Path(__file__).resolve().parents[1]
MAX_STEPS = 8
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", ROOT / "data" / "uploads"))  # /data/uploads on Railway
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# The frontend's uuid. Only letters, digits and "-", so each id has its own upload folder (upload_folder).
SESSION_ID = r"^[A-Za-z0-9-]{8,64}$"
NOT_FOUND = "This conversation was not found. It may belong to another account; start a new chat."
ESTIMATE_STATUSES = {"estimated", "needs_information", "needs_review", "not_covered", "invalid_input"}
# The user asks to compare or choose plans: the Arena, whatever mode the home screen set.
COMPARE_INTENT = re.compile(r"\bcompar(?:e|ing|ison)\b|\bwhich plan\b|\bbest plan\b|\bchoose (?:a |between )?plans?\b"
                            r"|对比|比较|哪个(?:保险|计划)", re.I)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # /plans, /turn, /upload, /auth and /me
    allow_headers=["*"],
    allow_credentials=True,  # the login cookie, when the frontend runs on another port in development
)
client = make_client(timeout=300.0)  # a shared endpoint can be slow under load
MODEL = llm_model()
# Price every scenario component in the rate store once, in the background (a few minutes), so turns don't wait.
threading.Thread(target=lambda: PlanDB().warm(), name="warm-rates", daemon=True).start()
STORE = SessionStore(upload_dir=UPLOAD_DIR)
ACCOUNTS = AccountStore()
MEMORY = MemoryStore()


def purge_forever():
    while True:
        STORE.purge()
        ACCOUNTS.purge()
        time.sleep(3600)


threading.Thread(target=purge_forever, name="purge-sessions", daemon=True).start()


class EndTurn(Exception):
    """A card is waiting for the user; stop the loop until they answer."""


class TurnRequest(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
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


def bill_card(result):
    """The bill review card: one row per line, then the findings; amounts are what the user owes."""
    rows = []
    for line in result["lines"]:
        label = " · ".join(filter(None, [line["code"], line["description"]])) or f"Line {line['line']}"
        note = line["note"] or (f"Billed ${line['billed']:,.2f}; negotiated rate ${line['negotiated_rate']:,.2f}"
                                if line["negotiated_rate"] is not None else None)
        source = ({"kind": "mrf", "label": f"{result['network']} negotiated rate"} if line["negotiated_rate"] is not None
                  else {"kind": "user_message", "label": "Your bill photo"})
        rows.append({"label": label, "amount": line["you_owe"], **({"note": note} if note else {}), "source": source})
    for finding in result["findings"]:
        if finding["kind"] != "total_mismatch":
            rows.append({"label": f"Possible overcharge, line {finding['line']}", "amount": finding["amount"],
                         "note": finding["message"]})
    totals, provider = result["totals"], result["provider"]
    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:8]}",
        "title": "Bill review",
        "plan_name": result["plan_name"],
        "status": "estimated" if result["status"] == "audited" else "needs_review",
        "patient_cost": {"mid": totals["you_owe_on_checked_lines"]}
        if result["status"] == "audited" and totals["you_owe_on_checked_lines"] is not None else None,
        "rows": rows,
        "assumptions": [
            f"Provider: {provider['name_on_record'] or provider['name_on_bill']}"
            + (f" (NPI {provider['npi']})" if provider["npi"] else ""),
            f"Network: {result['network']}" + (f" ({result['network_tier']})" if result["network_tier"] else ""),
            f"{totals['lines_checked']} of {totals['lines']} lines checked; you owe "
            f"${totals['you_owe_on_checked_lines'] or 0:,.2f} on them, "
            f"${totals['possible_overcharge']:,.2f} possibly overcharged",
            *[f["message"] for f in result["findings"] if f["kind"] == "total_mismatch"],
        ],
        "message": result["message"],
    }


def send_form(session, form):
    session.open_forms[form["request_id"]] = form["questions"]
    return sse("form", form)


def run_tool(tool, args):
    """A tool's result; an error is a result the model can read and recover from, not a failed turn."""
    try:
        return tool.run(args)
    except ValueError as error:
        return {"status": "invalid_arguments", "message": str(error)[:500]}
    except Exception as error:
        return {"status": "error", "message": f"{type(error).__name__}: {str(error)[:300]}"}


def records_for(session, name, args, result):
    """What this tool call taught us about the user's files, for the database (sessions.SessionStore._write)."""
    status = result.get("status")
    if name in ("find_plan_match", "extract_plan") and status in ("matched", "extracted"):
        return [file_kind(args["file_id"], "plan", result["plan_id"], result.get("name"))]
    if name == "read_bill" and status == "read":
        return [file_kind(args["file_id"], "bill")]
    if name == "audit_bill" and status in ("audited", "unavailable"):
        return [bill_review(args["bill_id"], session.harness.bills[args["bill_id"]].get("file_id"), result)]
    if name == "draft_appeal" and status == "drafted":
        return [appeal_drafted(args["bill_id"])]
    return []


async def run_call(session, call):
    """Run one tool call through hooks and tools, yielding SSE chunks. Raises EndTurn (form card) to pause the turn."""
    harness = session.harness
    args, result = harness.tools.parse(call.name, call.arguments)
    if result is None:
        result = harness.hooks.pre_tool(call.name, args)
    if result is None:
        step_id = f"tool_{uuid.uuid4().hex[:8]}"
        work = asyncio.create_task(asyncio.to_thread(run_tool, harness.tools[call.name], args))
        while not work.done():  # a long tool (extract_plan) reports each page as it goes
            await asyncio.wait({work}, timeout=0.5)
            while not harness.progress.empty():
                yield sse("search_step", {"search_id": step_id, "kind": "document", "step_id": step_id,
                                          "status": "running", "text": harness.progress.get_nowait()})
        result = harness.hooks.post_tool(call.name, args, work.result())
        if step := result.pop("_step", None):
            failed = result.get("status") in ("failed", "unreadable", "invalid_arguments", "error")
            yield sse("search_step", {"search_id": step_id, "kind": "document", "step_id": step_id,
                                      "status": "failed" if failed else "done", "text": step})

    session.records.extend(records_for(session, call.name, args, result))
    if call.name == "estimate_cost" and result.get("status") in ESTIMATE_STATUSES:
        yield sse("estimate", estimate_card(result))
    if call.name == "audit_bill" and result.get("status") in ("audited", "unavailable"):
        yield sse("estimate", bill_card(result))
    if call.name == "draft_appeal" and (draft := result.pop("draft", None)):
        yield sse("email_draft", draft)
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
            instructions="\n\n".join(filter(None, [harness.system_prompt, session.memory])),
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
    """A note per attached file. Nothing is read here: the agent looks at each file with the documents tools."""
    notes = []
    for attachment in attachments:
        upload = session.uploads.get(attachment.get("file_id"))
        if upload is None:
            notes.append(f"[An attachment ({attachment.get('name')}) has expired; ask the user to upload it again.]")
            continue
        kind = "a PDF" if upload.get("content_type") == "application/pdf" else "a photo"
        notes.append(f"[The user attached {kind}, {upload['name']} (file_id {attachment['file_id']}). Look at it with "
                     "the documents tools before answering.]")
    session.attachment_notes = notes
    return
    yield  # an async generator, like the other turn steps


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


def sse_event(chunk):
    """An SSE chunk (from sse()) back as {"event", "data"}, to keep in the transcript."""
    event, data = chunk.split("\n", 1)
    return {"event": event.removeprefix("event: "), "data": json.loads(data.removeprefix("data: "))}


async def turn_events(session, request, user_id):
    # What the user did (with the item they asked about), then what they saw.
    events = [{"event": "input", "data": {**request.input, **({"reply_target": request.reply_target}
                                                               if request.reply_target else {})}}]
    try:
        async for chunk in session_turn(session, request, user_id):
            events.append(sse_event(chunk))
            yield chunk
    finally:  # also when the client goes away mid-stream
        STORE.save(request.session_id, session, user_id, events)


async def session_turn(session, request, user_id=None):
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
                if user_id:
                    remember_answers(user_id, questions, answers)
                session.history.append({"role": "user", "content": answers_message(questions, answers)})
            else:  # closing a form means "I don't know" to each question, so Python doesn't ask them again
                hooks.on_form_answers(questions, {})
                session.history.append({"role": "user", "content": (
                    "[The user closed the questionnaire without answering. Continue with what you have.]")})
        elif kind == "email_response":
            # Gmail sending isn't built (the card offers Copy instead); a closed draft needs no answer.
            if data.get("action") == "send":
                for chunk in say("Sending from Gmail isn't available yet. Copy the letter from the card and send it "
                                 "from your own email."):
                    yield chunk
            session.history.append({"role": "user", "content": f"[The user {'tried to send' if data.get('action') == 'send' else 'closed'} the appeal draft.]"})
            yield sse("done", {})
            return
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
    return {"user": current_user(request), "google_client_id": google_client_id()}


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


def current_user(request: Request) -> dict | None:
    return ACCOUNTS.user_for(request.cookies.get(COOKIE_NAME))


def current_user_id(request: Request) -> str | None:
    return (current_user(request) or {}).get("user_id")


def signed_in(request: Request) -> str:
    """The user id, or 401: the /me routes are only for a signed-in user's own data."""
    if (user_id := current_user_id(request)) is None:
        raise HTTPException(401, "Sign in to see your conversations and files.")
    return user_id


@app.post("/upload")
async def upload(request: Request, session_id: str = Form(..., pattern=SESSION_ID), file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is larger than 25 MB.")
    kind = sniff_upload(data)
    if kind is None:
        raise HTTPException(415, "Only PDF files and photos (JPEG, PNG, WebP, HEIC) can be uploaded.")
    content_type, suffix = kind
    user_id = current_user_id(request)
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    folder = upload_folder(UPLOAD_DIR, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_id}{suffix}"
    path.write_bytes(data)
    name = Path(file.filename or f"upload{suffix}").name
    try:
        STORE.add_upload(session_id, user_id, file_id, path, name, content_type, len(data),
                         hashlib.sha256(data).hexdigest())
    except NotYours:  # checked with the write, in one transaction; the file must not stay in their folder
        path.unlink(missing_ok=True)
        raise HTTPException(404, NOT_FOUND)
    return {"file_id": file_id, "name": name, "size": len(data), "content_type": content_type}


@app.post("/turn")
async def turn(body: TurnRequest, request: Request):
    user = current_user(request)
    user_id = (user or {}).get("user_id")
    try:  # checked before the stream starts, so the frontend gets a plain 404
        session = STORE.get(body.session_id, user_id)
    except NotYours:
        raise HTTPException(404, NOT_FOUND)
    if user:  # what the user told us before: fills the questionnaire, and the agent reads it (memory.py)
        session.harness.hooks.use_memory(question_answers(MEMORY.facts(user_id)))
        session.memory = MEMORY.memory(user, {p["plan_id"]: p["name"] for p in available_plans()})
    return StreamingResponse(turn_events(session, body, user_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform"})


def remember_answers(user_id, questions, answers):
    """Keep a signed-in user's answers to the questions that last (ZIP, age) in their memory, so the
    newest answer wins next time too. An answer that doesn't pass the settings check is not kept."""
    for q in questions:
        answer = answers.get(q["field"]) or {}
        if q["field"] in QUESTION_FIELDS and not answer.get("unknown") and answer.get("value") is not None:
            try:
                MEMORY.remember(user_id, {q["field"]: answer["value"]}, "form")
            except ValueError:
                pass


# The signed-in user's own data: their conversations (to reopen or delete) and their files. Anything that isn't
# theirs is a 404, the same as something that doesn't exist.

def public_upload(upload):
    return {k: v for k, v in upload.items() if k != "path"}  # the server's file path isn't the user's business


@app.get("/me/sessions")
async def my_sessions(request: Request):
    return {"sessions": STORE.sessions_of(signed_in(request))}


@app.get("/me/sessions/{session_id}")
async def my_session(session_id: str, request: Request):
    user_id = signed_in(request)
    events = STORE.transcript(session_id, user_id)
    if events is None:
        raise HTTPException(404, NOT_FOUND)
    return {"session_id": session_id, "events": events,
            "uploads": [public_upload(u) for u in STORE.uploads_of(user_id, session_id)]}


@app.delete("/me/sessions/{session_id}")
async def delete_my_session(session_id: str, request: Request):
    if not STORE.delete(session_id, signed_in(request)):
        raise HTTPException(404, NOT_FOUND)
    return {"deleted": session_id}


@app.get("/me/uploads")
async def my_uploads(request: Request):
    return {"uploads": [public_upload(u) for u in STORE.uploads_of(signed_in(request))]}


@app.get("/me/uploads/{file_id}")
async def my_upload(file_id: str, request: Request):
    upload = STORE.upload_of(file_id, signed_in(request))
    if upload is None or not upload["path"].is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(upload["path"], media_type=upload["content_type"], filename=upload["name"])


def settings_of(user):
    facts = MEMORY.facts(user["user_id"])
    return {"user": user, "settings": {f: facts[f]["value"] if f in facts else None for f in MEMORY_FIELDS}}


@app.get("/me/settings")
async def my_settings(request: Request):
    signed_in(request)
    return settings_of(current_user(request))


@app.put("/me/settings")
async def update_my_settings(body: dict, request: Request):
    """Change any of: name (null = the Google name), zip, age, plan_id (null forgets one)."""
    user_id = signed_in(request)
    unknown = set(body) - {"name", *MEMORY_FIELDS}
    if unknown:
        raise HTTPException(422, f"Unknown settings: {', '.join(sorted(unknown))}.")
    facts = {f: body[f] for f in MEMORY_FIELDS if f in body}
    name = body.get("name")
    if name is not None and (not isinstance(name, str) or len(name.strip()) > 80):
        raise HTTPException(422, "Name must be text of at most 80 characters.")
    try:
        MEMORY.remember(user_id, facts, "settings", plan_ids={p["plan_id"] for p in available_plans()})
    except ValueError as error:
        raise HTTPException(422, str(error))
    user = ACCOUNTS.set_name(user_id, (name or "").strip() or None) if "name" in body else current_user(request)
    return settings_of(user)


@app.delete("/me")
async def delete_me(request: Request, response: Response):
    """Delete the account and everything in it: conversations, transcripts, files, logins."""
    user_id = signed_in(request)
    sessions = STORE.delete_user(user_id)
    ACCOUNTS.delete_user(user_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"user": None, "deleted_sessions": sessions}


# One service for the whole product: the API under /api (the built frontend calls it on the same origin), and the
# statically exported frontend (web/out, built by the Dockerfile) at /. Mounted last so the API routes win.
for _path, _endpoint, _methods in (("/api/turn", turn, ["POST"]), ("/api/upload", upload, ["POST"]),
                                   ("/api/plans", plans, ["GET"]),
                                   ("/api/integrations", integrations, ["GET"]),
                                   ("/api/auth/me", auth_me, ["GET"]), ("/api/auth/google", auth_google, ["POST"]),
                                   ("/api/auth/logout", auth_logout, ["POST"]),
                                   ("/api/me/sessions", my_sessions, ["GET"]),
                                   ("/api/me/sessions/{session_id}", my_session, ["GET"]),
                                   ("/api/me/sessions/{session_id}", delete_my_session, ["DELETE"]),
                                   ("/api/me/uploads", my_uploads, ["GET"]),
                                   ("/api/me/uploads/{file_id}", my_upload, ["GET"]),
                                   ("/api/me/settings", my_settings, ["GET"]),
                                   ("/api/me/settings", update_my_settings, ["PUT"]),
                                   ("/api/me", delete_me, ["DELETE"])):
    app.add_api_route(_path, _endpoint, methods=_methods)
WEB_DIR = Path(os.environ.get("WEB_DIR", ROOT / "web" / "out"))
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
