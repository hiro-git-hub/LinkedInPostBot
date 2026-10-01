import logging
import re
import time
from datetime import UTC, datetime

import httpx

from linkedin_bot.config import HackerNewsConfig
from linkedin_bot.state import NewsItem

log = logging.getLogger(__name__)

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"
ALGOLIA_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"


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


def gravity_score(item: NewsItem, gravity: float, now: float | None = None) -> float:
    """Punkte pro Zeit (HN-Formel) – sonst gewinnen immer die ältesten Stories mit den meisten Punkten."""
    now = now or time.time()
    age_hours = max(0.0, (now - item.published.timestamp()) / 3600) if item.published else 0.0
    return (item.points or 0) / (age_hours + 2) ** gravity


def title_matches(title: str, keyword: str) -> bool:
    """Stichwort als ganzes Wort im Titel – Algolia sucht unscharf und auch im Text ("rag" fände sonst "storage")."""
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", title, re.IGNORECASE) is not None


def _search(client: httpx.Client, url: str, params: dict) -> list[dict]:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Hacker News übersprungen (%s): %s", params.get("query", "Top"), exc)
        return []
    return response.json()["hits"]


def collect_hackernews(cfg: HackerNewsConfig) -> list[NewsItem]:
    since = int(time.time()) - cfg.lookback_hours * 3600
    with httpx.Client(timeout=15) as client:
        hits = _search(client, ALGOLIA_URL, {
            "tags": "story",
            "numericFilters": f"points>={cfg.min_points},created_at_i>{since}",
            "hitsPerPage": 100,
        })
        top = parse_hits(hits)
        if cfg.ranking.method == "gravity":
            top.sort(key=lambda i: gravity_score(i, cfg.ranking.gravity), reverse=True)
        else:
            top.sort(key=lambda i: i.points or 0, reverse=True)
        items = top[: cfg.max_items]

        search = cfg.keyword_search
        if search.enabled:
            seen = {i.url for i in items}
            for keyword in search.keywords:
                hits = _search(client, ALGOLIA_BY_DATE_URL, {
                    "query": keyword,
                    "tags": "story",
                    "numericFilters": f"points>={search.min_points},created_at_i>{since}",
                    "restrictSearchableAttributes": "title",
                    "typoTolerance": "false",
                    "hitsPerPage": 20,
                })
                for item in parse_hits(hits):
                    if item.url not in seen and title_matches(item.title, keyword):
                        seen.add(item.url)
                        items.append(item)
    return items
