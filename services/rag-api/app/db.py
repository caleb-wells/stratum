"""Postgres access: schema, connection pool, usage counters."""
from __future__ import annotations

import json
from contextlib import contextmanager

from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'chunks')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'chunks' AND column_name = 'seq')
  THEN DROP TABLE chunks; END IF;   -- corpus table is rebuilt by `ingest load`
END $$;

CREATE TABLE IF NOT EXISTS chunks (
    id                text PRIMARY KEY,
    doc_id            text NOT NULL,
    seq               int  NOT NULL,          -- reading order within the document
    jurisdiction      text NOT NULL,
    jurisdiction_name text NOT NULL,
    layer             text NOT NULL,          -- 'local' or 'state'
    doc_title         text NOT NULL,
    doc_date          text,
    url               text NOT NULL,
    section_path      text NOT NULL,
    page_start        int  NOT NULL,
    page_end          int  NOT NULL,
    text              text NOT NULL,
    tsv               tsvector GENERATED ALWAYS AS
                      (to_tsvector('english', section_path || ' ' || text)) STORED,
    embedding         vector({config.EMBED_DIMS}) NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_jurisdiction ON chunks (jurisdiction);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS daily_usage (
    day           date PRIMARY KEY,
    requests      int    NOT NULL DEFAULT 0,
    input_tokens  bigint NOT NULL DEFAULT 0,
    output_tokens bigint NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS request_log (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    mode        text NOT NULL,
    model       text NOT NULL,
    question    text NOT NULL,
    route       jsonb,
    chunk_ids   text[],
    input_tokens  int,
    output_tokens int,
    latency_ms  int
);
"""

_pool: ConnectionPool | None = None


def _configure(conn):
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=8,
                               kwargs={"row_factory": dict_row, "autocommit": True},
                               configure=_configure, open=True)
    return _pool


def init_schema() -> None:
    import psycopg
    with psycopg.connect(config.DATABASE_URL, autocommit=True) as conn:
        conn.execute(SCHEMA)


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


def tokens_used_today() -> int:
    with conn() as c:
        row = c.execute("SELECT input_tokens + output_tokens AS t FROM daily_usage "
                        "WHERE day = (now() AT TIME ZONE 'utc')::date").fetchone()
    return int(row["t"]) if row else 0


def record_usage(input_tokens: int, output_tokens: int) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO daily_usage (day, requests, input_tokens, output_tokens) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 1, %s, %s) "
            "ON CONFLICT (day) DO UPDATE SET requests = daily_usage.requests + 1, "
            "input_tokens = daily_usage.input_tokens + EXCLUDED.input_tokens, "
            "output_tokens = daily_usage.output_tokens + EXCLUDED.output_tokens",
            (input_tokens, output_tokens))


def reserve_tokens(estimate: int, limit: int) -> bool:
    """Atomically set aside `estimate` tokens for a request; False if it would pass the limit.

    The reservation is counted as input tokens until `settle_usage` replaces it with the
    real numbers, so concurrent requests cannot all slip under the limit together.
    """
    with conn() as c:
        row = c.execute(
            "INSERT INTO daily_usage (day, requests, input_tokens, output_tokens) "
            "VALUES ((now() AT TIME ZONE 'utc')::date, 0, %(e)s, 0) "
            "ON CONFLICT (day) DO UPDATE SET input_tokens = daily_usage.input_tokens + %(e)s "
            "WHERE daily_usage.input_tokens + daily_usage.output_tokens + %(e)s <= %(l)s "
            "RETURNING day", {"e": estimate, "l": limit}).fetchone()
    return row is not None and estimate <= limit


def settle_usage(estimate: int, input_tokens: int, output_tokens: int) -> None:
    """Swap a reservation for what the request really used."""
    with conn() as c:
        c.execute(
            "UPDATE daily_usage SET requests = requests + 1, "
            "input_tokens = GREATEST(input_tokens - %s + %s, 0), "
            "output_tokens = output_tokens + %s WHERE day = (now() AT TIME ZONE 'utc')::date",
            (estimate, input_tokens, output_tokens))


def log_request(**row) -> None:
    row["route"] = json.dumps(row.get("route"))
    with conn() as c:
        c.execute(
            "INSERT INTO request_log (mode, model, question, route, chunk_ids, "
            "input_tokens, output_tokens, latency_ms) VALUES (%(mode)s, %(model)s, "
            "%(question)s, %(route)s, %(chunk_ids)s, %(input_tokens)s, %(output_tokens)s, "
            "%(latency_ms)s)", row)
