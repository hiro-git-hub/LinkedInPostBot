from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware, ToolCallLimitMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable

from linkedin_bot.config import AppConfig
from linkedin_bot.models import ModelWithFallback
from linkedin_bot.state import Research, State
from linkedin_bot.tools.fetch import fetch_article, fetch_url
from linkedin_bot.tools.search import make_search_tool


def build_research_agent(cfg: AppConfig, model: BaseChatModel | ModelWithFallback) -> Runnable:
    middleware = [ToolCallLimitMiddleware(run_limit=cfg.research.max_tool_calls)]
    if isinstance(model, ModelWithFallback):
        model, middleware = model.primary, middleware + [ModelFallbackMiddleware(model.fallback)]
    return create_agent(
        model,
        tools=[make_search_tool(), fetch_url],
        system_prompt=(cfg.prompts_dir / "researcher.md").read_text().format(audience=cfg.audience),
        response_format=Research,
        # Tool-Limit: danach bekommt der Agent Tool-Fehler zurück und muss mit dem Vorhandenen antworten.
        middleware=middleware,
    )


def make_researcher(agent: Runnable):
    def research_node(state: State) -> dict:
        item = state["selected"].item
        # Den Originalartikel holen wir deterministisch – darauf soll sich der Agent nicht verlassen müssen.
        article = fetch_article(item.url)
        brief = (
            f"Thema: {item.title}\n"
            f"Quelle: {item.source} – {item.url}\n"
            f"Geplanter Aufhänger des Posts: {state.get('angle', '-')}\n\n"
            f"Originalartikel:\n{article or '(nicht abrufbar – recherchiere das Thema über die Websuche)'}"
        )
        result = agent.invoke({"messages": [HumanMessage(brief)]})
        return {"article": article, "research": result["structured_response"]}

    return research_node
