"""Agent service for the web frontend: POST /turn -> text/event-stream.

Run:  uvicorn server:app --port 8000        (from workflow/)
Protocol: docs/frontend-protocol.md. Same prompt, tools, calculator and guards as
agent.py; the terminal confirmation becomes a confirm card that resumes the turn.

Optional SEARCH_WORKER_URL (search_worker/worker.py) adds a search_insurer tool.
Forms and the dashboard report are not wired yet.
"""

import asyncio
import json
import os
import uuid

import httpx2 as httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent import INSTRUCTIONS, execute_tool, tool_definitions
from agent_tools import DEFAULT_RULES_PATH, PolicyTools
from amd_client import amd_model, load_env, make_client
from guards import MAX_REPLY_RETRIES, ConversationGuard

load_env()
PLAN = os.environ.get("AGENT_PLAN", "prime_100")
SEARCH_WORKER_URL = os.environ.get("SEARCH_WORKER_URL")
MAX_STEPS = 8

SEARCH_TOOL = {
    "type": "function",
    "name": "search_insurer",
    "description": (
        "Look up an insurer that is not the selected plan on its official website. "
        "Pass the insurer's full official brand name in English (for example 'UnitedHealthcare', "
        "not 'united health'), even if the user wrote it casually or in another language. "
        "Results are background facts for your judgment; never use them to calculate costs."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {"insurer": {"type": "string"}},
        "required": ["insurer"],
        "additionalProperties": False,
    },
}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["POST"],
    allow_headers=["*"],
)
client = make_client()
MODEL = amd_model()


class PendingConfirm(Exception):
    def __init__(self, summary):
        super().__init__(summary)
        self.summary = summary


class Session:
    def __init__(self):
        self.tools = PolicyTools(selected_plan=PLAN, rules_path=DEFAULT_RULES_PATH)
        self.history = []
        self.approved = set()  # confirm summaries the user accepted
        self.guard = ConversationGuard(confirm=self._confirm)
        self.inspected_ids = set()
        self.pending = None  # (request_id, summary, call) while a confirm card is open
        self.instructions = (
            INSTRUCTIONS.replace("You cannot browse, import PDFs", "You cannot import PDFs")
            + ("\nUse search_insurer when the user asks about an insurer other than the selected plan."
               if SEARCH_WORKER_URL else "")
            + "\nSelected plan and available catalog (data):\n"
            + json.dumps({"plan": self.tools.selected_plan, "catalog": self.tools.get_catalog()}, ensure_ascii=False)
        )

    def _confirm(self, summary):
        # guards.post_tool calls this synchronously; the web answer arrives in a later request.
        if summary in self.approved:
            return True, "yes"
        raise PendingConfirm(summary)

    def tool_list(self):
        return tool_definitions() + ([SEARCH_TOOL] if SEARCH_WORKER_URL else [])


SESSIONS = {}


class TurnRequest(BaseModel):
    session_id: str
    input: dict
    reply_target: dict | None = None


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def say(text):
    message_id = f"msg_{uuid.uuid4().hex[:8]}"
    return [
        sse("message_start", {"message_id": message_id}),
        sse("message_delta", {"message_id": message_id, "text": text}),
        sse("message_end", {"message_id": message_id}),
    ]


def estimate_card(session, args, result):
    rule = session.tools.get_policy_rule(args["rule_id"]).get("rule") or {}
    source = {
        "kind": "policy",
        "label": rule.get("plan") or session.tools.selected_plan,
        "page": rule.get("source_page"),
        "quote": (rule.get("source_text") or "")[:300] or None,
    }
    deductible_note = {True: "Deductible applies", False: "Deductible doesn't apply"}.get(
        rule.get("deductible_applies"), "Unknown whether the deductible applies"
    )
    rows = [
        {"label": "Allowed amount", "amount": args.get("allowed_amount"), "note": "You provided",
         "source": {"kind": "user_message", "label": "Your message"}},
        {"label": "Remaining deductible", "amount": args.get("remaining_deductible"), "note": deductible_note,
         "source": source},
    ]
    if rule.get("copay_usd") is not None:
        rows.append({"label": "Copay", "amount": rule["copay_usd"], "source": source})
    if rule.get("patient_pays_percentage") is not None:
        rows.append({"label": "Coinsurance", "amount": None,
                     "note": f"You pay {rule['patient_pays_percentage']}%", "source": source})
    cost = result.get("patient_cost")
    return {
        "estimate_id": f"est_{uuid.uuid4().hex[:8]}",
        "title": (rule.get("service") or "Estimate").split("\n")[0].capitalize(),
        "plan_name": session.tools.selected_plan,
        "status": result["status"],
        "patient_cost": {"mid": cost} if result["status"] == "estimated" and cost is not None else None,
        "rows": rows,
        "assumptions": [f"Network: {rule.get('network') or 'unknown'}"],
        "message": result.get("message"),
    }


async def search_insurer(insurer):
    """Yield (sse_chunk, None) per step, then (None, summary)."""
    search_id = f"search_{uuid.uuid4().hex[:8]}"
    summary = None
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            async with http.stream("POST", f"{SEARCH_WORKER_URL}/search", json={"insurer": insurer}) as response:
                event = None
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        if event == "search_step":
                            yield sse("search_step", {**data, "search_id": search_id}), None
                        elif event == "search_summary":
                            summary = data
    except (httpx.HTTPError, json.JSONDecodeError):
        yield sse("search_step", {"search_id": search_id, "step_id": "worker", "status": "failed",
                                  "text": "The live search service isn't reachable"}), None
    yield None, summary or {"insurer": insurer, "status": "error", "message": "Search failed.", "facts": [], "pages": []}


def tool_output(call, result):
    return {"type": "function_call_output", "call_id": call.call_id,
            "output": json.dumps(result, ensure_ascii=False, allow_nan=False)}


async def run_call(session, call):
    """Run one tool call, yielding SSE chunks. Raises PendingConfirm to pause the turn."""
    if call.name == "search_insurer" and SEARCH_WORKER_URL:
        insurer = json.loads(call.arguments).get("insurer", "")
        summary = None
        async for chunk, result in search_insurer(insurer):
            if chunk:
                yield chunk
            summary = result
        session.guard.post_tool(call.name, {"insurer": insurer}, summary, None)
        session.history.append(tool_output(call, summary))
        return
    result = execute_tool(call, session.tools, session.inspected_ids, session.guard)
    if call.name == "calculate_estimate" and result.get("status") in {
        "estimated", "needs_information", "needs_review", "not_covered", "invalid_input"
    }:
        yield sse("estimate", estimate_card(session, json.loads(call.arguments), result))
    session.history.append(tool_output(call, result))


async def agent_loop(session):
    rejected = 0
    for _ in range(MAX_STEPS):
        response = await asyncio.to_thread(
            client.responses.create,
            model=MODEL,
            instructions=session.instructions,
            input=session.history,
            tools=session.tool_list(),
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
            feedback = session.guard.check_reply(text)
            if feedback is None:
                for chunk in say(text):
                    yield chunk
                return
            rejected += 1
            if rejected > MAX_REPLY_RETRIES:
                reply = session.guard.fallback_reply()
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
                session.pending = (request_id, pending.summary, call)
                yield sse("confirm", confirm_card(request_id, pending.summary))
                return
    raise RuntimeError("Tool-loop limit reached. Please try a more specific request.")


def confirm_card(request_id, summary):
    items = []
    for line in summary.splitlines()[1:]:
        label, _, value = line.strip().partition(":")
        items.append({"label": label.strip(), "value": value.strip(), "source": "inferred"})
    return {"request_id": request_id, "title": "Before I show an estimate, is this right?", "items": items}


async def turn_events(request):
    session = SESSIONS.setdefault(request.session_id, Session())
    data = request.input
    try:
        if data.get("type") == "message":
            text = data.get("text", "").strip()
            if request.reply_target:
                text = f"(About: {request.reply_target.get('label')}) {text}"
            session.pending = None
            session.inspected_ids = set()
            session.guard.start_turn()
            session.guard.record_user(text)
            session.history.append({"role": "user", "content": text})
        elif data.get("type") == "confirm_response" and session.pending and session.pending[0] == data.get("request_id"):
            _, summary, call = session.pending
            session.pending = None
            if data.get("confirmed"):
                session.approved.add(summary)
                async for chunk in run_call(session, call):
                    yield chunk
            else:
                correction = data.get("correction") or "Something is off."
                session.guard.record_user(correction)
                session.history.append(tool_output(call, {
                    "status": "user_rejected", "user_reply": correction,
                    "message": "The user did not confirm these details, so no estimate was released. "
                               "Use the user's reply and ask what still needs to change.",
                }))
        else:
            for chunk in say("That card isn't connected to the live agent yet. Please type your question instead."):
                yield chunk
            yield sse("done", {})
            return
        async for chunk in agent_loop(session):
            yield chunk
    except Exception as error:  # report, never leak request contents or keys
        yield sse("error", {"message": f"{type(error).__name__}: {str(error)[:300]}"})
    yield sse("done", {})


@app.post("/turn")
async def turn(request: TurnRequest):
    return StreamingResponse(turn_events(request), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform"})
