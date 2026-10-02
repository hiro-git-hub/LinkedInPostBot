"""Minimaler Google-Trends-Client (inoffizielle Web-Schnittstelle, wie sie trends.google.com selbst nutzt).

Google drosselt diese Schnittstelle stark (429), besonders von Rechenzentrums-IPs. Alles hier ist daher
"best effort": Wer es nutzt, muss mit TrendsUnavailable rechnen und ohne Trends weitermachen.
"""

import json
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BASE = "https://trends.google.com/trends"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}
MAX_KEYWORDS_PER_REQUEST = 5  # Grenze der Trends-Oberfläche


class TrendsUnavailable(Exception):
    pass


@dataclass
class TrendSignal:
    term: str
    growth: float  # Suchinteresse der letzten Tage relativ zu davor, z.B. 0.45 = +45 %
    rising_queries: list[str] = field(default_factory=list)


def _json(response: httpx.Response) -> dict:
    """Trends-Antworten beginnen mit einem Schutzpräfix wie )]}' – erst ab der ersten Klammer ist es JSON."""
    text = response.text
    return json.loads(text[text.index("{"):])


def growth_of(values: list[float], recent_share: float = 2 / 7) -> float:
    """Wachstum des Mittelwerts im letzten Abschnitt gegenüber dem Rest der Zeitreihe."""
    if len(values) < 4:
        return 0.0
    split = max(1, int(len(values) * (1 - recent_share)))
    before, recent = values[:split], values[split:]
    before_avg = sum(before) / len(before)
    recent_avg = sum(recent) / len(recent)
    if before_avg == 0:
        return 1.0 if recent_avg > 0 else 0.0
    return recent_avg / before_avg - 1


class GoogleTrends:
    def __init__(self, geo: str = "DE", timeframe: str = "now 7-d", pause_seconds: float = 2.0,
                 retries: int = 2, http: httpx.Client | None = None):
        self.geo, self.timeframe = geo, timeframe
        self.pause_seconds, self.retries = pause_seconds, retries
        self.http = http or httpx.Client(timeout=20, headers=HEADERS, follow_redirects=True)
        self._has_cookies = False

    def _get(self, path: str, params: dict) -> dict:
        if not self._has_cookies:
            self.http.get(f"{BASE}/explore", params={"geo": self.geo})  # setzt das NID-Cookie
            self._has_cookies = True
        params = {"hl": "de-DE", "tz": "-120", **params}
        for attempt in range(self.retries + 1):
            time.sleep(self.pause_seconds * (attempt + 1))  # sanft bleiben, sonst sofort 429
            try:
                response = self.http.get(f"{BASE}/{path}", params=params)
            except httpx.HTTPError as exc:
                raise TrendsUnavailable(f"Google Trends nicht erreichbar: {exc}") from exc
            if response.status_code == 200:
                return _json(response)
            if response.status_code != 429:
                break
            log.info("Google Trends drosselt (429), Versuch %d", attempt + 1)
        raise TrendsUnavailable(f"Google Trends antwortet mit HTTP {response.status_code}")

    def _explore(self, terms: list[str]) -> list[dict]:
        request = {
            "comparisonItem": [{"keyword": t, "geo": self.geo, "time": self.timeframe} for t in terms],
            "category": 0, "property": "",
        }
        return self._get("api/explore", {"req": json.dumps(request)})["widgets"]

    def growth(self, terms: list[str]) -> dict[str, float]:
        """Wachstum des Suchinteresses je Begriff (bis zu 5 Begriffe pro Anfrage)."""
        result: dict[str, float] = {}
        for i in range(0, len(terms), MAX_KEYWORDS_PER_REQUEST):
            batch = terms[i:i + MAX_KEYWORDS_PER_REQUEST]
            widget = next(w for w in self._explore(batch) if w["id"] == "TIMESERIES")
            timeline = self._get("api/widgetdata/multiline",
                                 {"req": json.dumps(widget["request"]), "token": widget["token"]})["default"]["timelineData"]
            for index, term in enumerate(batch):
                result[term] = growth_of([point["value"][index] for point in timeline])
        return result

    def rising_queries(self, term: str, limit: int = 5) -> list[str]:
        """Suchanfragen rund um den Begriff, die gerade stark zulegen."""
        widget = next(w for w in self._explore([term]) if w["id"].startswith("RELATED_QUERIES"))
        lists = self._get("api/widgetdata/relatedsearches",
                          {"req": json.dumps(widget["request"]), "token": widget["token"]})["default"]["rankedList"]
        rising = lists[1]["rankedKeyword"] if len(lists) > 1 else []
        return [entry["query"] for entry in rising[:limit]]
