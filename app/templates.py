"""Load/validate layout template configs. Templates are a fixed input contract
(spec.md §6) — loaded generically so a second template is a config change, not
a code change."""

import json
import os

from app.schemas import TemplateConfig

TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR", "config/templates")


class TemplateNotFoundError(Exception):
    def __init__(self, template_id: str):
        self.template_id = template_id
        super().__init__(f"template '{template_id}' not found")


def load_template(template_id: str) -> TemplateConfig:
    path = os.path.join(TEMPLATES_DIR, f"{template_id}.json")
    if not os.path.isfile(path):
        raise TemplateNotFoundError(template_id)
    with open(path) as f:
        data = json.load(f)
    return TemplateConfig.model_validate(data)
