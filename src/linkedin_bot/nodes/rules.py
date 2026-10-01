import re

from linkedin_bot.config import AppConfig

BULLET = re.compile(r"^\s*([-•*–]|\d+[.)])\s+")
HASHTAG = re.compile(r"(^|\s)#\w")
URL = re.compile(r"https?://|www\.\S+\.\w{2,}", re.IGNORECASE)
# Emojis und Piktogramme; einfache Satzzeichen wie »« oder – bleiben erlaubt.
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
MAX_BULLET_LINES = 2


def hook_of(draft: str) -> str:
    """Hook = alle Zeilen bis zur ersten Leerzeile."""
    lines = []
    for line in draft.strip().splitlines():
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def phrase_pattern(phrase: str) -> re.Pattern:
    """Floskel als Regex; die Platzhalter X/Y stehen für beliebige Wörter, Gedankenstriche sind austauschbar."""
    parts = []
    for token in re.split(r"(\s+|[–—-])", phrase):
        if token in ("X", "Y"):
            parts.append(r".{1,60}?")
        elif token in ("–", "—", "-"):
            parts.append(r"\s*[–—,:-]\s*")
        elif token.isspace():
            parts.append(r"\s*")
        elif token:
            parts.append(re.escape(token))
    return re.compile("".join(parts), re.IGNORECASE)


def rule_issues(draft: str, cfg: AppConfig) -> list[str]:
    """Harte Regeln, die das LLM erfahrungsgemäß übersieht – lieber deterministisch prüfen."""
    writing = cfg.writing
    issues = []

    hook = hook_of(draft)
    hook_lines = len(hook.splitlines())
    if hook_lines > writing.hook_max_lines or len(hook) > writing.hook_max_chars:
        issues.append(
            f"Hook zu lang ({hook_lines} Zeilen, {len(hook)} Zeichen vor der ersten Leerzeile) – maximal "
            f"{writing.hook_max_lines} Zeile(n) und {writing.hook_max_chars} Zeichen, danach eine Leerzeile."
        )

    length = len(draft)
    if length > writing.length_chars.max:
        issues.append(f"Zu lang: {length} Zeichen, erlaubt sind maximal {writing.length_chars.max}. Kürzen.")
    elif length < writing.length_chars.min:
        issues.append(f"Zu kurz: {length} Zeichen, mindestens {writing.length_chars.min}.")

    bullets = sum(1 for line in draft.splitlines() if BULLET.match(line))
    if bullets > MAX_BULLET_LINES:
        issues.append(f"{bullets} Aufzählungszeilen – keine Checklisten, in erzählenden Fließtext umformulieren.")

    used = [p for p in writing.banned_phrases if phrase_pattern(p).search(draft)]
    if used:
        issues.append(f"Abgenutzte Formulierung(en) ersetzen: {', '.join(repr(p) for p in used)} – anders formulieren.")

    if HASHTAG.search(draft):
        issues.append("Keine Hashtags im Text – die werden automatisch angehängt. Entfernen.")
    if writing.emojis == "none" and EMOJI.search(draft):
        issues.append("Keine Emojis verwenden – alle entfernen.")
    if not writing.links_in_post and URL.search(draft):
        issues.append("Keine Links im Post – die Quelle kommt in den ersten Kommentar. Link entfernen.")
    if writing.end_with_question and not draft.rstrip().endswith("?"):
        issues.append("Mit einer konkreten, beantwortbaren Frage enden (kein \"Was meint ihr?\").")
    return issues


def form_rules(cfg: AppConfig) -> str:
    """Die Formregeln aus config.yaml als Prompt-Text – dieselbe Quelle wie rule_issues, damit nichts auseinanderläuft."""
    w = cfg.writing
    rules = [
        f"Hook: genau {w.hook_max_lines} Zeile(n) Überschrift, höchstens {w.hook_max_chars} Zeichen, danach eine Leerzeile.",
        f"Länge: {w.length_chars.min}-{w.length_chars.max} Zeichen. Harte Grenze.",
        "Keine Emojis." if w.emojis == "none" else "Emojis nur sparsam als Stilmittel.",
        "Keine Hashtags im Text – sie werden separat angehängt.",
    ]
    if not w.links_in_post:
        rules.append("Keine Links im Post – der Text muss ohne Quelle funktionieren.")
    if w.source_in_first_comment:
        rules.append("Die Quelle wird automatisch als erster Kommentar gepostet; der Satz \"Link in den Kommentaren\" ist erlaubt.")
    if w.end_with_question:
        rules.append("Ende mit genau einer konkreten, beantwortbaren Frage an die Leser:innen – kein \"Was meint ihr?\".")
    if w.banned_phrases:
        rules.append("Verbotene Formulierungen (X/Y = beliebige Wörter): " + "; ".join(f'"{p}"' for p in w.banned_phrases))
    if w.avoid_patterns:
        rules.append("Vermeide: " + "; ".join(w.avoid_patterns))
    return "\n".join(f"- {r}" for r in rules)
