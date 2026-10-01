import httpx
import openai
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from linkedin_bot.config import AppConfig, ModelConfig

# Nur bei Anbieter-/Netzwerkfehlern auf das Ersatzmodell wechseln – nicht bei der Kostenbremse.
FALLBACK_ERRORS: tuple[type[BaseException], ...] = (openai.APIError, httpx.HTTPError, ConnectionError, TimeoutError)
try:
    import anthropic

    FALLBACK_ERRORS += (anthropic.APIError,)
except ImportError:
    pass


class ModelWithFallback:
    """Chat-Modell mit Ersatzmodell; bietet dieselben zwei Methoden, die die Nodes benutzen."""

    def __init__(self, primary: BaseChatModel, fallback: BaseChatModel):
        self.primary, self.fallback = primary, fallback

    def invoke(self, messages, config=None, **kwargs):
        return self.primary.with_fallbacks([self.fallback], exceptions_to_handle=FALLBACK_ERRORS).invoke(
            messages, config, **kwargs)

    def with_structured_output(self, schema, **kwargs) -> Runnable:
        return self.primary.with_structured_output(schema, **kwargs).with_fallbacks(
            [self.fallback.with_structured_output(schema, **kwargs)], exceptions_to_handle=FALLBACK_ERRORS)


def _init(model: str, **kwargs) -> BaseChatModel:
    if model.startswith("openai:"):
        # Neuere OpenAI-Modelle (z.B. gpt-5.4-mini) erlauben Tool-Aufrufe mit reasoning_effort nur über die
        # Responses-API – Chat Completions antwortet sonst mit 400.
        kwargs.setdefault("use_responses_api", True)
    return init_chat_model(model, **kwargs)


def role_config(cfg: AppConfig, role: str) -> ModelConfig:
    return cfg.carousel.model if role == "carousel" else cfg.models[role]


def get_model(cfg: AppConfig, role: str) -> BaseChatModel | ModelWithFallback:
    """Modell für eine Rolle (scorer, writer, ...) laut config.yaml, mit Fallback aus model_fallbacks."""
    primary_cfg = role_config(cfg, role)
    fallback = cfg.model_fallbacks.get(role) or cfg.model_fallbacks.get("default")
    primary = _init(primary_cfg.model, **(primary_cfg.model_extra or {}))
    if not fallback or fallback == primary_cfg.model:
        return primary
    return ModelWithFallback(primary, _init(fallback))
