from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.state import ScoredItem, State, TopicChoice

RECENT_TOPIC_DAYS = 21


def format_shortlist(shortlist: list[ScoredItem]) -> str:
    return "\n".join(
        f"[{i}] {s.relevance}/10 [{s.category}] {s.item.title} ({s.item.source})\n"
        f"    {s.item.summary[:300]}\n    Warum relevant: {s.reason}"
        for i, s in enumerate(shortlist)
    )


def make_selector(cfg: AppConfig, model: BaseChatModel, repo: Repository):
    system = (cfg.prompts_dir / "selector.md").read_text().format(audience=cfg.audience)
    structured = model.with_structured_output(TopicChoice)

    def select_node(state: State) -> dict:
        rejected = set(state.get("rejected_urls", []))
        candidates = [
            s for s in state.get("scored", [])
            if s.relevance >= cfg.limits.min_relevance and s.item.url not in rejected
        ]
        shortlist = sorted(candidates, key=lambda s: (s.relevance, s.item.points or 0), reverse=True)
        shortlist = shortlist[: cfg.limits.shortlist_size]
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
