from langchain_tavily import TavilySearch


def make_search_tool() -> TavilySearch:
    return TavilySearch(
        name="web_search",
        description="Websuche nach aktuellen Informationen, Hintergründen und Reaktionen zu einem Thema.",
        max_results=5,
    )
