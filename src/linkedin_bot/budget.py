"""Kostenbremse: zählt die LLM-Kosten eines Laufs mit und bricht ab, sobald das Budget überschritten ist."""

import logging
import threading

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from linkedin_bot.config import AppConfig, Price

log = logging.getLogger(__name__)

COST_EVENT = "linkedin_bot.cost"  # Custom Event für Kosten außerhalb von LangChain-Modellen (z.B. Bilder)


class BudgetExceeded(Exception):
    def __init__(self, spent: float, limit: float):
        super().__init__(f"Kostenbremse: {spent:.2f} $ verbraucht, Limit {limit:.2f} $")
        self.spent, self.limit = spent, limit


def price_for(pricing: dict[str, Price], model_name: str) -> Price | None:
    """Längster passender Präfix – "gpt-5.5-2026-04-23" nutzt den Eintrag "gpt-5.5"."""
    matches = [key for key in pricing if model_name.startswith(key)]
    return pricing[max(matches, key=len)] if matches else None


def token_cost(price: Price, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
    cached_rate = price.cached_input if price.cached_input is not None else price.input
    uncached = max(0, input_tokens - cached_tokens)
    return (uncached * price.input + cached_tokens * cached_rate + output_tokens * price.output) / 1_000_000


class CostTracker(BaseCallbackHandler):
    """Callback für alle Modelle eines Laufs (auch parallele Varianten) – daher mit Lock."""

    raise_error = True  # sonst würde LangChain die BudgetExceeded-Exception verschlucken

    def __init__(self, cfg: AppConfig):
        self.pricing = cfg.pricing
        self.limit = cfg.budget.max_usd_per_run
        self.usd = 0.0
        self._lock = threading.Lock()

    def add(self, usd: float) -> None:
        with self._lock:
            self.usd += usd
            spent = self.usd
        if spent > self.limit:
            raise BudgetExceeded(spent, self.limit)

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        usd = 0.0
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if not usage:
                    continue
                model = (message.response_metadata or {}).get("model_name", "")
                price = price_for(self.pricing, model)
                if price is None:
                    log.warning("Kein Preis für Modell %r in config.yaml (pricing) – Kosten nicht erfasst", model)
                    continue
                cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
                usd += token_cost(price, usage.get("input_tokens", 0), usage.get("output_tokens", 0), cached)
        if usd:
            self.add(usd)

    def on_custom_event(self, name: str, data, **kwargs) -> None:
        if name == COST_EVENT:
            self.add(float(data["usd"]))
