"""Übersetzt typische Laufzeitfehler in eine kurze Meldung für den Autor (Telegram)."""

import openai
import psycopg

from linkedin_bot.integrations.linkedin import LinkedInError

OPENAI_BILLING_URL = "https://platform.openai.com/settings/organization/billing"


def _chain(exc: BaseException):
    """Die Exception und alle Ursachen – langchain & Co. verpacken die eigentlichen API-Fehler."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def describe_error(exc: BaseException) -> str:
    for err in _chain(exc):
        if isinstance(err, openai.AuthenticationError):
            return "OpenAI: API-Key ungültig – OPENAI_API_KEY in Coolify prüfen."
        if isinstance(err, openai.RateLimitError):
            if err.code in ("insufficient_quota", "credit_balance_exhausted"):
                return f"OpenAI: Guthaben aufgebraucht – bitte aufladen: {OPENAI_BILLING_URL}"
            return "OpenAI: Rate-Limit erreicht – in ein paar Minuten erneut /run."
        if isinstance(err, openai.APIConnectionError):
            return "OpenAI nicht erreichbar – Netzwerkproblem, später erneut /run."
        if isinstance(err, openai.APIStatusError):
            return f"OpenAI-Fehler ({err.status_code}) – später erneut versuchen."
        if isinstance(err, psycopg.OperationalError):
            return "Datenbank nicht erreichbar – Postgres-Container in Coolify prüfen."
        if isinstance(err, LinkedInError):
            return f"LinkedIn: {err}"
        if "tavily" in type(err).__module__.lower() or "tavily" in str(err).lower():
            return "Tavily-Websuche fehlgeschlagen – TAVILY_API_KEY bzw. Kontingent prüfen."
    return f"Unerwarteter Fehler ({type(exc).__name__}) – Details im Log."
