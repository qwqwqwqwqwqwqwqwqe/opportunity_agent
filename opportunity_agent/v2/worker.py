"""Small Redis queue worker.  It deliberately does not crawl arbitrary URLs."""
from __future__ import annotations

import asyncio
import json

import redis.asyncio as redis

from .core.config import settings
from .db.session import SessionLocal
from .research.ingestion import ingest_page
from .services.memory_consolidator import run_memory_worker


async def run_ingestion() -> None:
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while True:
            job = await client.brpop("opportunity:v2:official-ingest", timeout=5)
            if job is None:
                continue
            _, raw = job
            try:
                payload = json.loads(raw)
                async with SessionLocal.begin() as session:
                    await ingest_page(session, payload)
            except Exception as exc:
                # Preserve failed jobs for inspection/retry; never silently lose work.
                await client.lpush("opportunity:v2:official-ingest:failed",
                                   json.dumps({"job": raw, "error": type(exc).__name__}))
    finally:
        await client.aclose()


async def run() -> None:
    # Preference durability is in PostgreSQL; a Redis outage must not stop it.
    async def resilient_ingestion():
        while True:
            try:
                await run_ingestion()
            except Exception:
                await asyncio.sleep(5)
    async with asyncio.TaskGroup() as group:
        group.create_task(resilient_ingestion())
        group.create_task(run_memory_worker(SessionLocal))


if __name__ == "__main__":
    asyncio.run(run())
