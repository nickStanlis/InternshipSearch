
import json
from pydantic import BaseModel, ConfigDict, Field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

class SearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a misspelled key is an error, not silently ignored

    label: str
    what: str
    where: str | None = None  # null in JSON = no location (e.g. remote)

class PipelineSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gemini_model: str = "gemini-3.5-flash-lite"
    max_pages_per_search: int = Field(2, ge=1)
    max_days_old: int = Field(14, ge=1)
    seconds_between_gemini_calls: float = Field(4.5, ge=0)
    max_evaluations_per_run: int = Field(150, ge=1)
    search_keywords_or: str = "software data analytics analyst python developer statistics quantitative"
    search_plans: list[SearchPlan] = Field(min_length=1)

class Config(BaseModel):
    candidate_profile: dict
    search_and_matching_criteria: dict
    pipeline_settings: PipelineSettings

def load_config(path: str | Path = CONFIG_PATH) -> Config:
    with open(path) as f:
        return Config.model_validate(json.load(f))