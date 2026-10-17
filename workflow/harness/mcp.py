"""Tool layer: every tool the model can call. No LLM, no conversation rules.

Each tool has the shape of an MCP tool (name, description, JSON schema for its input,
handler), so the registry could be served over MCP later. Tools only do their job and
return a result dict; hooks.py decides whether a call may run and what the model sees.

  get_policy_rule     read one benefit rule from the plan catalog
  calculate_estimate  run the cost calculator on a rule the model has read
  get_definition      look up a defined term in the policy text
  search_insurer      live search of an insurer's website (only when SEARCH_WORKER_URL is set)

Skills add their own tools (skills/questionnaire adds ask_questions).
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx2 as httpx

from agent import reject_constant, unique_object


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], dict] | None = None
    # Streaming tools yield ("progress", data) any number of times, then ("result", data) once.
    stream: Callable[[dict], AsyncIterator[tuple[str, dict]]] | None = None
    validate: Callable[[dict], None] | None = None  # raises ValueError on bad arguments

    def definition(self):
        return {"type": "function", "name": self.name, "description": self.description,
                "strict": True, "parameters": self.parameters}


def schema(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


class ToolRegistry:
    def __init__(self, tools):
        self._tools = {tool.name: tool for tool in tools}

    def __getitem__(self, name):
        return self._tools[name]

    def __contains__(self, name):
        return name in self._tools

    def definitions(self):
        return [tool.definition() for tool in self._tools.values()]

    def parse(self, name, raw_arguments):
        """Return (args, None) or (None, error_result). Checked even though schemas are strict."""
        if name not in self._tools:
            return None, {"status": "invalid_tool", "message": "That tool is unavailable."}
        tool = self._tools[name]
        try:
            args = json.loads(raw_arguments, parse_constant=reject_constant, object_pairs_hook=unique_object)
            if not isinstance(args, dict) or set(args) != set(tool.parameters["properties"]):
                raise ValueError("Arguments must contain exactly the declared fields.")
            if tool.validate:
                tool.validate(args)
        except (ValueError, TypeError) as error:
            return None, {"status": "invalid_arguments", "message": f"{error} Use exactly the declared arguments."}
        return args, None


# Policy catalog and calculator ------------------------------------------------

def _validate_rule_id(args):
    if not isinstance(args["rule_id"], str):
        raise ValueError("rule_id must be a string.")


def _validate_estimate(args):
    _validate_rule_id(args)
    for name in ("allowed_amount", "remaining_deductible"):
        value = args[name]
        if value is None:
            continue
        if type(value) not in (int, float) or value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            raise ValueError(f"{name} must be a finite nonnegative number or null.")


def policy_tools(policy):
    """policy: agent_tools.PolicyTools for the selected plan."""
    return [
        Tool(
            name="get_policy_rule",
            description="Read the full stored benefit and its conditions for a catalog rule ID.",
            parameters=schema(rule_id={"type": "string"}),
            run=lambda args: policy.get_policy_rule(args["rule_id"]),
            validate=_validate_rule_id,
        ),
        Tool(
            name="calculate_estimate",
            description=("Calculate what the user pays for one occurrence of a service, using a rule you "
                         "already read. Pass null for amounts the user hasn't given."),
            parameters=schema(
                rule_id={"type": "string"},
                allowed_amount={"type": ["number", "null"], "minimum": 0},
                remaining_deductible={"type": ["number", "null"], "minimum": 0},
            ),
            run=lambda args: policy.calculate_estimate(**args),
            validate=_validate_estimate,
        ),
    ]


# Policy definitions -----------------------------------------------------------

_TERM = re.compile(r"^([A-Z][A-Z0-9 /&'’,()-]{2,80}?) means\b")
_PAGE = re.compile(r"^=== PDF page (\d+) ===$")


class Definitions:
    """Defined terms parsed from policies/definitions/<plan>.txt (OCR of the certificate)."""

    def __init__(self, path: Path):
        self.terms = {}  # TERM -> {"page": int|None, "text": str}
        if not path.exists():
            return
        page, term, lines = None, None, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                continue
            if match := _PAGE.match(line.strip()):
                page = int(match.group(1))
                continue
            if match := _TERM.match(line):
                if term:
                    self.terms[term]["text"] = "\n".join(lines).strip()
                term, lines = match.group(1).strip(), [line]
                self.terms[term] = {"page": page, "text": ""}
            elif term:
                lines.append(line)
        if term:
            self.terms[term]["text"] = "\n".join(lines).strip()

    def lookup(self, query):
        key = query.strip().upper()
        if key in self.terms:
            return {"status": "found", "term": key, **self.terms[key]}
        close = [t for t in self.terms if key in t or t in key]
        if len(close) == 1:
            return {"status": "found", "term": close[0], **self.terms[close[0]]}
        return {"status": "not_found", "matches": close, "available_terms": sorted(self.terms)}


def definition_tool(definitions, plan_name):
    return Tool(
        name="get_definition",
        description=(f"Look up how the {plan_name} policy defines a term, for example 'allowed amount' or "
                     "'medical emergency'. Returns the policy's own wording and PDF page. Use it to explain "
                     "terms, never to compute costs."),
        parameters=schema(term={"type": "string"}),
        run=lambda args: definitions.lookup(args["term"]),
    )


# Live insurer search ----------------------------------------------------------

def search_tool(worker_url):
    async def stream(args):
        summary = None
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                async with http.stream("POST", f"{worker_url}/search", json={"insurer": args["insurer"]}) as response:
                    event = None
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data = json.loads(line[5:])
                            if event == "search_step":
                                yield "progress", data
                            elif event == "search_summary":
                                summary = data
        except (httpx.HTTPError, json.JSONDecodeError):
            yield "progress", {"step_id": "worker", "status": "failed", "text": "The live search service isn't reachable"}
        yield "result", summary or {"insurer": args["insurer"], "status": "error", "message": "Search failed.",
                                    "facts": [], "pages": []}

    return Tool(
        name="search_insurer",
        description=(
            "Look up an insurer that isn't in the catalog on its official website. Pass the insurer's full "
            "official brand name in English (for example 'UnitedHealthcare', not 'united health'), even if the "
            "user wrote it casually or in another language. Results are background facts for your judgment; "
            "never use them to calculate costs."
        ),
        parameters=schema(insurer={"type": "string"}),
        stream=stream,
    )
