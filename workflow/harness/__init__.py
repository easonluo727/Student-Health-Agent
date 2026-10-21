"""The agent harness, in four parts:

  prompts/system.md   main prompt: who the agent is, ground rules, how to work, style
  skills/             packaged abilities, each with SKILL.md instructions and its own tools
  mcp.py              the tool layer: tool shape, schema and argument parsing
  hooks.py            code that checks every message, tool call and reply

Harness puts them together for one conversation over the plan database (plan_db.PlanDB).
"""

import json
from pathlib import Path

from plan_db import PlanDB

from . import mcp
from .hooks import Hooks
from .skills.plans import skill as plans_skill
from .skills.questionnaire import skill as questionnaire

PROMPT_PATH = Path(__file__).with_name("prompts") / "system.md"


class Harness:
    def __init__(self):
        self.db = PlanDB()
        self.skills = [questionnaire.build(), plans_skill.build(self.db)]
        self.tools = mcp.ToolRegistry([tool for skill in self.skills for tool in skill.tools])
        self.hooks = Hooks()
        plan_context = ("```json\n" + "\n".join(json.dumps(p, ensure_ascii=False)
                                                for p in plans_skill.plan_summary(self.db)) + "\n```")
        text = PROMPT_PATH.read_text(encoding="utf-8")
        for key, value in {"SKILLS": "\n\n".join(skill.prompt_section() for skill in self.skills),
                           "PLAN_CONTEXT": plan_context}.items():
            text = text.replace("{{" + key + "}}", value)
        self.system_prompt = text
