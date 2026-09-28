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


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Entfernt Duplikate über normalisierte URL; bei Kollision gewinnt das Item mit mehr Punkten."""
    best: dict[str, NewsItem] = {}
    for item in items:
        key = normalize_url(item.url)
        current = best.get(key)
        if current is None or (item.points or 0) > (current.points or 0):
            best[key] = item
    return list(best.values())


def make_dedup(repo: Repository):
    def dedup_node(state: State) -> dict:
        unique = dedupe(state.get("items", []))
        # Was an früheren Tagen schon bewertet wurde, kommt nicht noch einmal.
        unseen = repo.unseen_keys([normalize_url(i.url) for i in unique])
        # "items" hat einen Append-Reducer und lässt sich nicht überschreiben -> eigenes Feld.
        return {"unique": [i for i in unique if normalize_url(i.url) in unseen]}

    return dedup_node
