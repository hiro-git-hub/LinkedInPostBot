import re

from linkedin_bot.config import AppConfig

BULLET = re.compile(r"^\s*([-•*–]|\d+[.)])\s+")
HASHTAG = re.compile(r"(^|\s)#\w")
MAX_BULLET_LINES = 2


def rule_issues(draft: str, cfg: AppConfig) -> list[str]:
    """Harte Regeln, die das LLM erfahrungsgemäß übersieht – lieber deterministisch prüfen."""
    issues = []
    length = len(draft)
    if length > cfg.writing.max_chars:
        issues.append(f"Zu lang: {length} Zeichen, erlaubt sind maximal {cfg.writing.max_chars}. Kürzen.")
    elif length < cfg.writing.min_chars:
        issues.append(f"Zu kurz: {length} Zeichen, mindestens {cfg.writing.min_chars}.")
    bullets = sum(1 for line in draft.splitlines() if BULLET.match(line))
    if bullets > MAX_BULLET_LINES:
        issues.append(f"{bullets} Aufzählungszeilen – keine Checklisten, in erzählenden Fließtext umformulieren.")
    used = [p for p in cfg.writing.banned_phrases if p.lower() in draft.lower()]
    if used:
        issues.append(f"Abgenutzte Formulierung(en) ersetzen: {', '.join(repr(p) for p in used)} – anders formulieren.")
    if HASHTAG.search(draft):
        issues.append("Keine Hashtags im Text – die werden automatisch angehängt. Entfernen.")
    return issues
