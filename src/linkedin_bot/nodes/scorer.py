import logging
from datetime import UTC, datetime

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.nodes.dedup import normalize_url
from linkedin_bot.state import NewsItem, ScoreBatch, ScoredItem, State

log = logging.getLogger(__name__)

_EPOCH = datetime.min.replace(tzinfo=UTC)


def format_items(items: list[NewsItem]) -> str:
    lines = []
    for i, item in enumerate(items):
        points = f" | {item.points} HN-Punkte" if item.points else ""
        lines.append(f"[{i}] {item.title} ({item.source}{points})\n    {item.summary[:300]}")
    return "\n".join(lines)


def make_scorer(cfg: AppConfig, model: BaseChatModel, repo: Repository):
    system = (cfg.prompts_dir / "scorer.md").read_text().format(audience=cfg.audience, topics=cfg.topics)
    structured = model.with_structured_output(ScoreBatch)

    def scorer_node(state: State) -> dict:
        # Neueste zuerst, damit das Limit alte Einträge abschneidet.
        items = sorted(state.get("unique", []), key=lambda i: i.published or _EPOCH, reverse=True)
        items = items[: cfg.limits.max_items_to_score]
        if not items:
            return {"scored": []}

        batch: ScoreBatch = structured.invoke(
            [SystemMessage(system), HumanMessage(format_items(items))]
        )
        scored = [
            ScoredItem(item=items[s.index], relevance=max(0, min(10, s.relevance)), category=s.category, reason=s.reason)
            for s in batch.scores
            if 0 <= s.index < len(items)
        ]
        repo.mark_seen({normalize_url(i.url): i for i in items})
        log.info("%d von %d Items bewertet", len(scored), len(items))
        return {"scored": sorted(scored, key=lambda s: s.relevance, reverse=True)}

    return scorer_node

