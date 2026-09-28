from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from linkedin_bot.config import AppConfig


def get_model(cfg: AppConfig, role: str) -> BaseChatModel:
    """Liefert das Chat-Modell für eine Rolle (scorer, writer, ...) laut config.yaml."""
    role_cfg = cfg.models[role]
    return init_chat_model(role_cfg.model, **(role_cfg.model_extra or {}))
