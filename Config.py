
import json
from pydantic import BaseModel

class SearchPlan(BaseModel):
    label: str
    what: str
    where: str | None = None

class PipelineSettings(BaseModel):
    gemini_model: str = "gemini-3.5-flash-lite"
    max_pages_per_search: int = 2
    seconds_between_gemini_calls: float = 4.5
    max_evaluations_per_run: int = 150

class Config(BaseModel):
    candidate_profile: dict
    search_and_matching_criteria: dict
    pipeline_settings: PipelineSettings = PipelineSettings()
    search_plans: list[SearchPlan] = []

def load_config(path: str = "config.json") -> Config:
    with open(path) as f:
        return Config.model_validate(json.load(f))