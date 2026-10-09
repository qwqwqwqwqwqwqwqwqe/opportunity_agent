"""Create an isolated, named evaluation database; never change the business database."""
import argparse
import asyncio
import json
import os

import asyncpg


async def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--database", default="opportunity_research_eval_real150_v2")
    parser.add_argument("--host", default=os.getenv("RESEARCH_EVAL_PG_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RESEARCH_EVAL_PG_PORT", "5432")))
    parser.add_argument("--user", default=os.getenv("RESEARCH_EVAL_PG_USER", "opportunity"))
    parser.add_argument("--password", default=os.getenv("RESEARCH_EVAL_PG_PASSWORD", "opportunity_dev_only"))
    parser.add_argument("--admin-database", default=os.getenv("RESEARCH_EVAL_PG_ADMIN_DATABASE", "opportunity_agent"))
    args = parser.parse_args()
    if not args.database.startswith("opportunity_research_eval_") or not args.database.replace("_", "").isalnum():
        raise ValueError("Only a dedicated opportunity_research_eval_* database is allowed")
    connection = await asyncpg.connect(host=args.host, port=args.port,
        user=args.user, password=args.password, database=args.admin_database)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", args.database)
        if not exists:
            await connection.execute('CREATE DATABASE "' + args.database + '"')
        version = await connection.fetchval("SHOW server_version")
    finally:
        await connection.close()
    eval_connection = await asyncpg.connect(host=args.host, port=args.port,
        user=args.user, password=args.password, database=args.database)
    try:
        fts_ok = await eval_connection.fetchval(
            "SELECT to_tsvector('simple', 'chip design') @@ websearch_to_tsquery('simple', 'chip OR design')")
        print(json.dumps({"database": args.database, "created": not bool(exists), "postgres": version,
                          "full_text_search": bool(fts_ok)}))
    finally:
        await eval_connection.close()


if __name__ == "__main__":
    asyncio.run(main())
