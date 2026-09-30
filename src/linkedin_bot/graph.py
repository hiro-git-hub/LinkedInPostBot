from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from linkedin_bot.collectors.hackernews import collect_hackernews
from linkedin_bot.collectors.rss import collect_rss
from linkedin_bot.config import AppConfig
from linkedin_bot.db import Repository
from linkedin_bot.integrations.images import OpenAIImageGenerator
from linkedin_bot.integrations.linkedin import LinkedInClient
from linkedin_bot.models import get_model
from linkedin_bot.nodes.approval import approval_node, compose_tasks, make_archive, route_after_approval
from linkedin_bot.nodes.compose import make_compose
from linkedin_bot.nodes.dedup import make_dedup
from linkedin_bot.nodes.hashtags import make_hashtagger
from linkedin_bot.nodes.image import ImageGenerator, make_imager
from linkedin_bot.nodes.publish import make_publisher
from linkedin_bot.nodes.research import build_research_agent, make_researcher
from linkedin_bot.nodes.scorer import make_scorer
from linkedin_bot.nodes.select import make_selector
from linkedin_bot.state import VARIANTS, SourceTask, State

ModelFactory = Callable[[AppConfig, str], BaseChatModel]


def build_graph(
    cfg: AppConfig,
    repo: Repository,
    checkpointer,
    model_factory: ModelFactory = get_model,
    research_agent: Runnable | None = None,
    linkedin: LinkedInClient | None = None,
    image_generator: ImageGenerator | None = None,
):
    """Checkpointer ist Pflicht: ohne ihn kann der Freigabe-Interrupt nicht fortgesetzt werden."""
    collectors: dict[str, Callable[[], list]] = {}
    if cfg.sources.rss:
        collectors["rss"] = lambda: collect_rss(cfg.sources.rss, cfg.limits.max_age_hours, cfg.limits.max_items_per_feed)
    if cfg.sources.hackernews:
        collectors["hackernews"] = lambda: collect_hackernews(cfg.sources.hackernews)

    if research_agent is None:
        research_agent = build_research_agent(cfg, model_factory(cfg, "researcher"))
    if image_generator is None:
        image_generator = OpenAIImageGenerator(cfg.images.model, cfg.images.size, cfg.images.quality)

    def fan_out(_: State) -> list[Send]:
        return [Send("collect", SourceTask(source=name)) for name in collectors]

    def collect(task: SourceTask) -> dict:
        return {"items": collectors[task["source"]]()}

    def route_after_select(state: State) -> str:
        return "research" if state.get("selected") else END

    def fan_out_variants(state: State) -> list[Send]:
        # Normal- und Humor-Version entstehen parallel aus derselben Recherche.
        return compose_tasks(state, VARIANTS)

    graph = StateGraph(State)
    graph.add_node("collect", collect)
    graph.add_node("dedup", make_dedup(repo))
    graph.add_node("scorer", make_scorer(cfg, model_factory(cfg, "scorer"), repo))
    graph.add_node("select", make_selector(cfg, model_factory(cfg, "selector"), repo))
    graph.add_node("research", make_researcher(research_agent))
    graph.add_node("compose", make_compose(cfg, model_factory(cfg, "writer"), model_factory(cfg, "critic")))
    graph.add_node("hashtags", make_hashtagger(cfg, model_factory(cfg, "hashtags")))
    graph.add_node("approval", approval_node)
    graph.add_node("image", make_imager(cfg, model_factory(cfg, "image_prompt"), repo, image_generator))
    graph.add_node("publish", make_publisher(cfg, repo, linkedin))
    graph.add_node("archive", make_archive(repo))

    graph.add_conditional_edges(START, fan_out, ["collect"])
    graph.add_edge("collect", "dedup")
    graph.add_edge("dedup", "scorer")
    graph.add_edge("scorer", "select")
    graph.add_conditional_edges("select", route_after_select, ["research", END])
    graph.add_conditional_edges("research", fan_out_variants, ["compose"])
    graph.add_edge("compose", "hashtags")
    graph.add_edge("hashtags", "approval")
    graph.add_conditional_edges("approval", route_after_approval, ["approval", "compose", "select", "image", "publish", "archive"])
    graph.add_edge("image", "approval")
    graph.add_edge("publish", "archive")
    graph.add_edge("archive", END)
    return graph.compile(checkpointer=checkpointer)
