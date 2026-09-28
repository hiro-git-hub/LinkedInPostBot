from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from linkedin_bot.collectors.hackernews import collect_hackernews
from linkedin_bot.collectors.rss import collect_rss
from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.integrations.linkedin import LinkedInClient
from linkedin_bot.models import get_model
from linkedin_bot.nodes.approval import approval_node, make_archive, route_after_approval
from linkedin_bot.nodes.critic import make_critic
from linkedin_bot.nodes.dedup import make_dedup
from linkedin_bot.nodes.publish import make_publisher
from linkedin_bot.nodes.research import build_research_agent, make_researcher
from linkedin_bot.nodes.scorer import make_scorer
from linkedin_bot.nodes.select import make_selector
from linkedin_bot.nodes.writer import make_writer
from linkedin_bot.state import SourceTask, State

ModelFactory = Callable[[AppConfig, str], BaseChatModel]


def build_graph(
    cfg: AppConfig,
    repo: Repository,
    checkpointer,
    model_factory: ModelFactory = get_model,
    research_agent: Runnable | None = None,
    linkedin: LinkedInClient | None = None,
):
    """Checkpointer ist Pflicht: ohne ihn kann der Freigabe-Interrupt nicht fortgesetzt werden."""
    collectors: dict[str, Callable[[], list]] = {}
    if cfg.sources.rss:
        collectors["rss"] = lambda: collect_rss(cfg.sources.rss, cfg.limits.max_age_hours)
    if cfg.sources.hackernews:
        collectors["hackernews"] = lambda: collect_hackernews(cfg.sources.hackernews)

    if research_agent is None:
        research_agent = build_research_agent(cfg, model_factory(cfg, "researcher"))

    def fan_out(_: State) -> list[Send]:
        return [Send("collect", SourceTask(source=name)) for name in collectors]

    def collect(task: SourceTask) -> dict:
        return {"items": collectors[task["source"]]()}

    def route_after_select(state: State) -> str:
        return "research" if state.get("selected") else END

    def route_after_critic(state: State) -> str:
        # revisions zählt alle Entwürfe, also 1 Erstentwurf + max_revisions Überarbeitungen.
        if state["critique"].approved or state["revisions"] > cfg.writing.max_revisions:
            return "approval"
        return "writer"

    graph = StateGraph(State)
    graph.add_node("collect", collect)
    graph.add_node("dedup", make_dedup(repo))
    graph.add_node("scorer", make_scorer(cfg, model_factory(cfg, "scorer"), repo))
    graph.add_node("select", make_selector(cfg, model_factory(cfg, "selector"), repo))
    graph.add_node("research", make_researcher(research_agent))
    graph.add_node("writer", make_writer(cfg, model_factory(cfg, "writer")))
    graph.add_node("critic", make_critic(cfg, model_factory(cfg, "critic")))
    graph.add_node("approval", approval_node)
    graph.add_node("publish", make_publisher(cfg, repo, linkedin))
    graph.add_node("archive", make_archive(repo))

    graph.add_conditional_edges(START, fan_out, ["collect"])
    graph.add_edge("collect", "dedup")
    graph.add_edge("dedup", "scorer")
    graph.add_edge("scorer", "select")
    graph.add_conditional_edges("select", route_after_select, ["research", END])
    graph.add_edge("research", "writer")
    graph.add_edge("writer", "critic")
    graph.add_conditional_edges("critic", route_after_critic, ["writer", "approval"])
    graph.add_conditional_edges("approval", route_after_approval, ["approval", "writer", "select", "publish", "archive"])
    graph.add_edge("publish", "archive")
    graph.add_edge("archive", END)
    return graph.compile(checkpointer=checkpointer)
