# LinkedInPostBot

LangGraph-Pipeline, die KI-/Developer-News sammelt, bewertet, recherchiert und daraus LinkedIn-Post-Entwürfe schreibt,
die du per Telegram freigibst und die dann auf deinem LinkedIn-Profil erscheinen. **Stand: Phase 4 – lokal lauffähig, Deployment folgt in Phase 5.**

```
START ─┬─ collect(rss) ─┐
       └─ collect(hn)  ─┴─▶ dedup ─▶ scorer ─▶ select ─▶ research ─▶ writer ─▶ critic ─▶ approval ─▶ publish ─▶ archive ─▶ END
                                               ▲                       ▲    └─(max. 2x)─┘  │ ▲    │
                                               │                       └── überarbeiten ───┤ └────┘ bearbeiten /
                                               └────────────── anderes Thema ──────────────┘        Platzhalter offen
```

`approval` ist ein LangGraph-`interrupt`: Der Lauf pausiert im Postgres-Checkpointer, bis du entscheidest –
auch über Tage und Container-Neustarts hinweg.

## Setup

```bash
uv sync
cp .env.example .env            # Keys eintragen (OpenAI, Tavily, Telegram)
docker compose -f docker-compose.dev.yml up -d   # Postgres auf localhost:5433
```

**Telegram einrichten:** Bei [@BotFather](https://t.me/BotFather) mit `/newbot` einen Bot anlegen → Token als
`TELEGRAM_BOT_TOKEN` eintragen. `uv run linkedin-bot serve` starten, dem Bot `/start` schicken → er antwortet mit
deiner Chat-ID → als `TELEGRAM_CHAT_ID` eintragen und neu starten. Der Bot reagiert nur auf diese Chat-ID.

**LinkedIn einrichten:** Im [Developer Portal](https://www.linkedin.com/developers/apps) eine App anlegen (braucht eine
verknüpfte LinkedIn-Seite – eine leere eigene Seite reicht), Produkte *Share on LinkedIn* und *Sign In with LinkedIn using
OpenID Connect* hinzufügen. Unter *Auth* → *Authorized redirect URLs* `http://localhost:8765/callback` eintragen,
Client ID/Secret in die `.env`. Dann lokal `uv run linkedin-bot linkedin-login` oder im Telegram-Chat `/login` – der Zugang gilt 60 Tage, der Bot erinnert
7 Tage vorher. Veröffentlicht wird erst, wenn in `config.yaml` `linkedin.dry_run: false` steht.

## Nutzung

```bash
uv run linkedin-bot serve         # Telegram-Bot + täglicher Lauf laut config.yaml (schedule)
uv run linkedin-bot run           # ein Lauf im Terminal, Freigabe per Tastatur
uv run linkedin-bot run --no-db   # dito ohne Postgres (kein Dedup über Tage)
uv run linkedin-bot linkedin-login   # LinkedIn-Anmeldung (alle 60 Tage)
uv run pytest                     # Tests (ohne Netzwerk/LLM/DB)
```

Im Telegram-Chat: `/run` startet sofort einen Lauf. Unter jedem Entwurf: **Freigeben**, **Bearbeiten** (eigenen Text
schicken), **Überarbeiten lassen** (Feedback an den Writer), **Anderes Thema**, **Verwerfen**.
Solange ein `[EIGENE ERFAHRUNG: …]`-Platzhalter im Text steht, lässt sich nicht freigeben.

## Anpassen

- **`config.yaml`** – Zielgruppe, Themenrahmen, Modelle pro Rolle (`provider:model`, Standard OpenAI GPT-5),
  Quellen, Schwellen, Länge, Zeitplan.
- **`prompts/style_guide.md`** – Tonalität und Aufbau deiner Posts.
- **`prompts/examples/*.md`** – eigene Posts als Few-Shot-Beispiele.

## Struktur

| Pfad | Inhalt |
|---|---|
| `src/linkedin_bot/graph.py` | Graph-Aufbau |
| `src/linkedin_bot/runtime.py` | Checkpointer/DB verdrahten, Lauf starten/fortsetzen |
| `src/linkedin_bot/telegram_bot.py` | Freigabe-Bot (Long-Polling) + Zeitplan |
| `src/linkedin_bot/db.py` | Gesehene Items, Post-Archiv (Postgres / In-Memory) |
| `src/linkedin_bot/state.py` | Graph-State und Pydantic-Modelle |
| `src/linkedin_bot/nodes/` | Dedup, Scorer, Selector, Research-Agent, Writer, Critic, Approval/Archiv |
| `src/linkedin_bot/nodes/rules.py` | Harte, deterministische Regeln (Länge, Checklisten, Hashtags) |
| `src/linkedin_bot/integrations/linkedin.py` | OAuth, Posts-API, little-Text-Escaping |
| `src/linkedin_bot/login.py` | Browser-Login mit lokalem Callback-Server |
| `src/linkedin_bot/collectors/`, `tools/` | RSS, Hacker News, Tavily-Suche, Artikel-Extraktion |
