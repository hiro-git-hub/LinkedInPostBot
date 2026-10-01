import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from linkedin_bot.db import Repository
from linkedin_bot.state import NewsItem, State

TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid", "wt_mc"}  # wt_mc: heise


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.startswith("utm_") and k not in TRACKING_PARAMS
    ]
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, parts.path.rstrip("/"), urlencode(query), ""))


def _better(a: NewsItem, b: NewsItem) -> NewsItem:
    return b if (b.points or 0) > (a.points or 0) else a


def _title_key(title: str) -> str:
    return re.sub(r"[^\w ]", "", title.lower()).strip()


def dedupe(items: list[NewsItem], similar_title_threshold: float = 1.0) -> list[NewsItem]:
    """Entfernt Duplikate über normalisierte URL und – ab der Schwelle – über ähnliche Titel.

    Bei Kollision gewinnt das Item mit mehr Punkten (Community-Signal)."""
    by_url: dict[str, NewsItem] = {}
    for item in items:
        key = normalize_url(item.url)
        by_url[key] = _better(by_url[key], item) if key in by_url else item

    result: list[NewsItem] = []
    for item in by_url.values():
        title = _title_key(item.title)
        for i, kept in enumerate(result):
            if SequenceMatcher(None, title, _title_key(kept.title)).ratio() >= similar_title_threshold:
                result[i] = _better(kept, item)
                break
        else:
            result.append(item)
    return result


def make_dedup(repo: Repository, ttl_days: int | None = None, similar_title_threshold: float = 1.0):
    def dedup_node(state: State) -> dict:
        unique = dedupe(state.get("items", []), similar_title_threshold)
        # Was an früheren Tagen schon bewertet wurde, kommt nicht noch einmal.
        unseen = repo.unseen_keys([normalize_url(i.url) for i in unique], ttl_days)
        # "items" hat einen Append-Reducer und lässt sich nicht überschreiben -> eigenes Feld.
        return {"unique": [i for i in unique if normalize_url(i.url) in unseen]}

    return dedup_node
