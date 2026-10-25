"""Read-only preflight and explicit, idempotent legacy-task migration."""
from __future__ import annotations
import argparse
import asyncio
import json
from sqlalchemy import inspect, text
from ..v2.db.session import SessionLocal, engine
from .service import WorkflowService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["preflight", "migrate-tasks"])
    parser.add_argument("--user-id")
    args = parser.parse_args()
    try:
        if args.operation == "preflight":
            async with engine.connect() as conn:
                tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
                revision = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all() if "alembic_version" in tables else []
                report = {"dialect": conn.dialect.name, "alembic_revisions": revision, "tables": tables,
                          "note": "只检查结构；升级现有部署前请使用数据库原生备份工具创建备份"}
        else:
            if not args.user_id:
                parser.error("migrate-tasks requires --user-id")
            async with SessionLocal.begin() as session:
                report = await WorkflowService(session).migrate_tasks(args.user_id)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
