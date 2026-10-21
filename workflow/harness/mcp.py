"""Tool layer: the shape of every tool the model can call. No LLM, no conversation rules.

Each tool has the shape of an MCP tool (name, description, JSON schema for its input,
handler), so the registry could be served over MCP later. Tools only do their job and
return a result dict; hooks.py decides whether a call may run and what the model sees.
The tools themselves live in the skills (skills/plans, skills/questionnaire).
"""

import json
from dataclasses import dataclass
from typing import Callable


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate argument key.")
        result[key] = value
    return result


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], dict]
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

