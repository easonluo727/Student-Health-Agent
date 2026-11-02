"""The agent harness, in four parts:

  prompts/system.md   main prompt: who the agent is, ground rules, how to work, style
  skills/             packaged abilities, each with SKILL.md instructions and its own tools
  mcp.py              the tool layer: tool shape, schema and argument parsing
  hooks.py            code that checks every message, tool call and reply

Harness puts them together for one conversation over the plan database (plan_db.PlanDB).
"""

import json
import queue
from pathlib import Path

from plan_db import PlanDB
from provider_directory import ProviderDirectory

from . import mcp
from .hooks import Hooks
from .skills.bills import skill as bills_skill
from .skills.documents import skill as documents_skill
from .skills.plans import skill as plans_skill
from .skills.providers import skill as providers_skill
from .skills.questionnaire import skill as questionnaire

PROMPT_PATH = Path(__file__).with_name("prompts") / "system.md"


class Harness:
    def __init__(self, db: PlanDB | None = None):
        self.db = db or PlanDB()
        directory = ProviderDirectory(policy_path=self.db.path) if self.db.path else ProviderDirectory()
        # Saved with the session (sessions.py); the skills hold these same dicts.
        self.files = {}  # file_id -> {"path", "name", "content_type", "sha256"}: the user's uploads
        self.bills = {}  # bill_id -> {"bill", "name", "file_id", "page", "model", "audit"}
        self.progress = queue.Queue()  # progress lines from a running tool, shown in the chat as it works
        self.skills = [questionnaire.build(), documents_skill.build(self.db, self.files, self.progress.put),
                       plans_skill.build(self.db, directory), providers_skill.build(self.db, directory),
                       bills_skill.build(self.db, directory, self.bills, self.files)]
        self.tools = mcp.ToolRegistry([tool for skill in self.skills for tool in skill.tools])
        self.hooks = Hooks()
        plan_context = ("```json\n" + "\n".join(json.dumps(p, ensure_ascii=False)
                                                for p in plans_skill.plan_summary(self.db)) + "\n```")
        text = PROMPT_PATH.read_text(encoding="utf-8")
        for key, value in {"SKILLS": "\n\n".join(skill.prompt_section() for skill in self.skills),
                           "PLAN_CONTEXT": plan_context}.items():
            text = text.replace("{{" + key + "}}", value)
        self.system_prompt = text
