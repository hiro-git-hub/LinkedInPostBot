import logging
import time
from datetime import UTC, datetime

import httpx

from linkedin_bot.config import HackerNewsConfig
from linkedin_bot.state import NewsItem

log = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


def parse_hits(hits: list[dict]) -> list[NewsItem]:
    items = []
    for hit in hits:
        if not hit.get("title"):
            continue
        # Ask-HN & Co. haben keine externe URL -> auf den Thread verlinken.
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        items.append(
            NewsItem(
                source="Hacker News",
                title=hit["title"],
                url=url,
                published=datetime.fromtimestamp(hit["created_at_i"], UTC),
                points=hit.get("points"),
            )
        )
    return items


def collect_hackernews(cfg: HackerNewsConfig) -> list[NewsItem]:
    since = int(time.time()) - cfg.lookback_hours * 3600
    params = {
        "tags": "story",
        "numericFilters": f"points>={cfg.min_points},created_at_i>{since}",
        "hitsPerPage": 50,
    }
    try:
        response = httpx.get(ALGOLIA_URL, params=params, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Hacker News übersprungen: %s", exc)
        return []
    return parse_hits(response.json()["hits"])
