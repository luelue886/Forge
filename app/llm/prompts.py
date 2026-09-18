from __future__ import annotations

from pathlib import Path

import jinja2

from app.config import PROMPTS_DIR


class PromptManager:
    """prompts/ 目录的 jinja2 渲染器。模板名即相对路径，如 "docmap/user.j2"。"""

    def __init__(self, prompts_dir: Path | None = None):
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(prompts_dir or PROMPTS_DIR)),
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, name: str, **ctx) -> str:
        return self.env.get_template(name).render(**ctx)

    def has(self, name: str) -> bool:
        try:
            self.env.get_template(name)
            return True
        except jinja2.TemplateNotFound:
            return False
