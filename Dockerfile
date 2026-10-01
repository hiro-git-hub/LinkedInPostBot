FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Abhängigkeiten zuerst – bleiben gecacht, solange sich uv.lock nicht ändert.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY prompts ./prompts
COPY voice_examples ./voice_examples
COPY config.yaml ./
RUN uv sync --frozen --no-dev

RUN useradd --create-home bot
USER bot

CMD ["uv", "run", "--no-sync", "linkedin-bot", "-v", "serve"]
