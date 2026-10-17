"""Skills: packaged abilities, each in its own folder.

A skill folder holds SKILL.md and a Python module:
  SKILL.md   frontmatter (name, description) and the instructions the model follows.
             {{PLACEHOLDERS}} in the body are filled by the module.
  skill.py   build() -> Skill, with the skill's tools and placeholder values.

Every skill's instructions are placed in the main prompt under "Skills".
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    tools: list = field(default_factory=list)

    def prompt_section(self):
        return f"## {self.name}\n\n*{self.description}*\n\n{self.instructions.strip()}"


def read_skill_md(path: Path, **placeholders):
    """Return (meta, body) with {{KEY}} placeholders in the body replaced."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path} needs a frontmatter block with name and description.")
    meta = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    meta = {key.strip(): value.strip() for key, value in meta.items()}
    body = text[match.end():]
    for key, value in placeholders.items():
        body = body.replace("{{" + key + "}}", value)
    if leftover := re.findall(r"\{\{[A-Z_]+\}\}", body):
        raise ValueError(f"{path}: unfilled placeholders {leftover}")
    return meta, body


def load_skills():
    from .questionnaire import skill as questionnaire

    return [questionnaire.build()]
