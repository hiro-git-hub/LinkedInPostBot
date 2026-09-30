import logging
from datetime import UTC, datetime, timedelta

import feedparser
import httpx

from linkedin_bot.state import NewsItem

log = logging.getLogger(__name__)

USER_AGENT = "LinkedInPostBot/0.1 (+news digest)"


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=UTC) if parsed else None


def parse_feed(content: bytes, feed_url: str, max_age_hours: int, max_items: int | None = None) -> list[NewsItem]:
    feed = feedparser.parse(content)
    source = feed.feed.get("title") or feed_url
    cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
    items = []
    for entry in feed.entries:
        published = _published(entry)
        if published and published < cutoff:
            continue
        if not entry.get("link") or not entry.get("title"):
            continue
        items.append(
            NewsItem(
                source=source,
                title=entry.title.strip(),
                url=entry.link,
                summary=(entry.get("summary") or "")[:500],
                published=published,
            )
        )
    items.sort(key=lambda i: i.published or cutoff, reverse=True)
    return items[:max_items] if max_items else items


def collect_rss(feed_urls: list[str], max_age_hours: int, max_per_feed: int | None = None) -> list[NewsItem]:
    items: list[NewsItem] = []
    with httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for url in feed_urls:
            try:
                response = client.get(url)
                response.raise_for_status()
                items.extend(parse_feed(response.content, url, max_age_hours, max_per_feed))
            except httpx.HTTPError as exc:
                # Ein kaputter Feed darf den Tageslauf nicht abbrechen.
                log.warning("RSS-Feed %s übersprungen: %s", url, exc)
    return items
