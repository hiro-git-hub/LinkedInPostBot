import logging

import httpx
import trafilatura
from langchain_core.tools import tool

from linkedin_bot.collectors.rss import USER_AGENT

log = logging.getLogger(__name__)

MAX_CHARS = 12_000  # reicht für einen Artikel, hält den Kontext klein


def fetch_article(url: str) -> str:
    """Lädt eine Seite und extrahiert den Haupttext. Leerer String, wenn nichts Brauchbares kommt."""
    try:
        response = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Artikel %s nicht abrufbar: %s", url, exc)
        return ""
    text = trafilatura.extract(response.text, url=url, include_comments=False) or ""
    return text[:MAX_CHARS]


@tool
def fetch_url(url: str) -> str:
    """Lädt den Haupttext einer Webseite (Artikel, Blogpost, Doku), um Details nachzulesen."""
    return fetch_article(url) or "Seite nicht abrufbar oder ohne lesbaren Text."
