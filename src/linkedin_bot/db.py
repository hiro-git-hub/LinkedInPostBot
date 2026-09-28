from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg_pool import ConnectionPool

from linkedin_bot.integrations.linkedin import LinkedInAuth
from linkedin_bot.state import NewsItem

# Einzelne Statements: der Pool nutzt Prepared Statements, die keine Mehrfachbefehle erlauben.
SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS seen_items (
    url_key    TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    source     TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """
CREATE TABLE IF NOT EXISTS posts (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    topic_title TEXT NOT NULL,
    topic_url   TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS post_urn TEXT",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS error TEXT",
    # Genau eine Zeile: der aktuelle LinkedIn-Zugang des Autors.
    """
CREATE TABLE IF NOT EXISTS linkedin_auth (
    id           INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    access_token TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    person_urn   TEXT NOT NULL,
    name         TEXT NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
]

# Posts, die als eigenes Thema zählen (für "kürzlich gepostet")
POSTED_STATUSES = ("approved", "published")


class Repository(Protocol):
    def unseen_keys(self, keys: list[str]) -> set[str]: ...
    def mark_seen(self, items: dict[str, NewsItem]) -> None: ...
    def recent_topics(self, days: int) -> list[str]: ...
    def save_post(self, thread_id: str, item: NewsItem, text: str, status: str,
                  post_urn: str | None = None, error: str | None = None) -> None: ...
    def get_linkedin_auth(self) -> LinkedInAuth | None: ...
    def ping(self) -> None: ...
    def save_linkedin_auth(self, auth: LinkedInAuth) -> None: ...


class PostgresRepository:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def setup(self) -> None:
        with self.pool.connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def unseen_keys(self, keys: list[str]) -> set[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT url_key FROM seen_items WHERE url_key = ANY(%s)", (keys,)).fetchall()
        return set(keys) - {row["url_key"] for row in rows}

    def mark_seen(self, items: dict[str, NewsItem]) -> None:
        with self.pool.connection() as conn:
            conn.cursor().executemany(
                "INSERT INTO seen_items (url_key, title, source) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(key, item.title, item.source) for key, item in items.items()],
            )

    def recent_topics(self, days: int) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT topic_title FROM posts WHERE status = ANY(%s) AND created_at > now() - %s "
                "ORDER BY created_at DESC",
                (list(POSTED_STATUSES), timedelta(days=days)),
            ).fetchall()
        return [row["topic_title"] for row in rows]

    def save_post(self, thread_id: str, item: NewsItem, text: str, status: str,
                  post_urn: str | None = None, error: str | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO posts (thread_id, topic_title, topic_url, text, status, post_urn, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (thread_id, item.title, item.url, text, status, post_urn, error),
            )

    def get_linkedin_auth(self) -> LinkedInAuth | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT access_token, expires_at, person_urn, name FROM linkedin_auth").fetchone()
        return LinkedInAuth(**row) if row else None

    def save_linkedin_auth(self, auth: LinkedInAuth) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO linkedin_auth (id, access_token, expires_at, person_urn, name) VALUES (1, %s, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET access_token = EXCLUDED.access_token, expires_at = EXCLUDED.expires_at, "
                "person_urn = EXCLUDED.person_urn, name = EXCLUDED.name, updated_at = now()",
                (auth.access_token, auth.expires_at, auth.person_urn, auth.name),
            )


    def ping(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("SELECT 1")


class InMemoryRepository:
    """Für Tests und `linkedin-bot run --no-db`: gleiche Schnittstelle, kein Postgres nötig."""

    def __init__(self):
        self.seen: dict[str, NewsItem] = {}
        self.posts: list[dict] = []
        self.linkedin_auth: LinkedInAuth | None = None

    def unseen_keys(self, keys: list[str]) -> set[str]:
        return set(keys) - self.seen.keys()

    def mark_seen(self, items: dict[str, NewsItem]) -> None:
        self.seen.update(items)

    def recent_topics(self, days: int) -> list[str]:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        return [p["item"].title for p in self.posts if p["status"] in POSTED_STATUSES and p["created_at"] > cutoff]

    def save_post(self, thread_id: str, item: NewsItem, text: str, status: str,
                  post_urn: str | None = None, error: str | None = None) -> None:
        self.posts.append({"thread_id": thread_id, "item": item, "text": text, "status": status,
                           "post_urn": post_urn, "error": error, "created_at": datetime.now(UTC)})

    def get_linkedin_auth(self) -> LinkedInAuth | None:
        return self.linkedin_auth

    def save_linkedin_auth(self, auth: LinkedInAuth) -> None:
        self.linkedin_auth = auth

    def ping(self) -> None:
        pass
