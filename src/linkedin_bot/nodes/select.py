from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.state import ScoredItem, State, TopicChoice

RECENT_TOPIC_DAYS = 21


def blocked_categories(cfg: AppConfig, recent: list[str]) -> set[str]:
    """Kategorien, die in den letzten quota_window_posts Posts ihren max_share schon ausgeschöpft haben."""
    window = recent[: cfg.quota_window_posts]
    return {
        category for category, rule in cfg.topic_weights.items()
        if rule.max_share is not None and window.count(category) >= rule.max_share * cfg.quota_window_posts
    }


def format_shortlist(shortlist: list[ScoredItem]) -> str:
    return "\n".join(
        f"[{i}] {s.relevance}/10 [{s.category}] {s.item.title} ({s.item.source})\n"
        f"    {s.item.summary[:300]}\n    Warum relevant: {s.reason}"
        for i, s in enumerate(shortlist)
    )


def make_selector(cfg: AppConfig, model: BaseChatModel, repo: Repository):
    system = (cfg.prompts_dir / "selector.md").read_text().format(audience=cfg.audience, author=cfg.author)
    structured = model.with_structured_output(TopicChoice)

    def select_node(state: State) -> dict:
        rejected = set(state.get("rejected_urls", []))
        blocked = blocked_categories(cfg, repo.recent_categories(cfg.quota_window_posts))
        candidates = [
            s for s in state.get("scored", [])
            if s.relevance >= cfg.scoring.min_score and s.item.url not in rejected and s.category not in blocked
        ]
        shortlist = sorted(candidates, key=lambda s: (s.weighted, s.item.points or 0), reverse=True)
        shortlist = shortlist[: cfg.scoring.max_candidates]
        if not shortlist:
            return {"selected": None}

        recent = repo.recent_topics(RECENT_TOPIC_DAYS)
        prompt = format_shortlist(shortlist)
        if recent:
            prompt += "\n\nKürzlich veröffentlicht – nicht dasselbe Thema noch einmal wählen:\n" + "\n".join(f"- {t}" for t in recent)
        choice: TopicChoice = structured.invoke([SystemMessage(system), HumanMessage(prompt)])
        # Ungültiger Index -> bestbewertetes Thema statt Abbruch.
        selected = shortlist[choice.index] if 0 <= choice.index < len(shortlist) else shortlist[0]
        return {"selected": selected, "angle": choice.angle}

    return select_node
