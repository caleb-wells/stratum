"""Create the chat-history database and its tables if they are missing. Safe to re-run."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    parts = urlsplit(url)
    name = parts.path.lstrip("/")
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
            await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(url)
    try:
        await conn.execute((Path(__file__).parent / "schema.sql").read_text())
    finally:
        await conn.close()
    print("chat history database ready")


if __name__ == "__main__":
    asyncio.run(main())
