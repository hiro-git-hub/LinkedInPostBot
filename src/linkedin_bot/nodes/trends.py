import logging
from typing import Protocol

from linkedin_bot.config import AppConfig
from linkedin_bot.integrations.trends import TrendsUnavailable
from linkedin_bot.state import State

log = logging.getLogger(__name__)


class TrendsClient(Protocol):
    def growth(self, terms: list[str]) -> dict[str, float]: ...
    def rising_queries(self, term: str, limit: int = 5) -> list[str]: ...


def trend_bonus(cfg: AppConfig, growth: float) -> float:
    """Multiplikator fürs Ranking: erst ab min_growth, halbes Wachstum als Bonus, gedeckelt bei max_bonus."""
    if growth < cfg.trends.min_growth:
        return 1.0
    return 1.0 + min(cfg.trends.max_bonus, growth / 2)


def make_trend_booster(cfg: AppConfig, client: TrendsClient | None):
    def trends_node(state: State) -> dict:
        if not cfg.trends.enabled or client is None:
            return {"trends_status": None}
        scored = state.get("scored", [])
        # Nur die Kandidaten, die überhaupt beim Selector landen könnten – jede Abfrage riskiert Drosselung.
        candidates = [s for s in scored if s.relevance >= cfg.scoring.min_score and s.search_term]
        candidates = sorted(candidates, key=lambda s: s.weighted, reverse=True)[: cfg.scoring.max_candidates]
        terms = list(dict.fromkeys(s.search_term for s in candidates))
        if not terms:
            return {"trends_status": "ok"}
        try:
            growth = client.growth(terms)
        except TrendsUnavailable as exc:
            log.warning("Ohne Google Trends weiter: %s", exc)
            return {"trends_status": str(exc)}

        boosted = []
        for s in scored:
            if s in candidates and s.search_term in growth:
                g = growth[s.search_term]
                s = s.model_copy(update={"trend_growth": g, "weighted": s.weighted * trend_bonus(cfg, g)})
            boosted.append(s)
        return {"scored": sorted(boosted, key=lambda s: s.weighted, reverse=True), "trends_status": "ok"}

    return trends_node


def make_trend_context(cfg: AppConfig, client: TrendsClient | None):
    def trend_context_node(state: State) -> dict:
        selected = state.get("selected")
        if state.get("trends_status") != "ok" or client is None or not selected or not selected.search_term:
            return {"trend_queries": []}
        try:
            return {"trend_queries": client.rising_queries(selected.search_term, cfg.trends.rising_queries)}
        except TrendsUnavailable as exc:
            log.warning("Steigende Suchanfragen nicht verfügbar: %s", exc)
            return {"trend_queries": []}

    return trend_context_node
