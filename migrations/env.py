"""Alembic environment: runs migrations against DATABASE_URL (Cloud SQL in
prod, the docker-compose Postgres locally, memotrip_test in tests)."""

from __future__ import annotations

import pathlib
import sys

from alembic import context
from sqlalchemy import create_engine, make_url, pool, text

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import local_env  # noqa: F401  (loads .env when run from the alembic CLI)
except Exception:
    pass

import config  # noqa: E402

if not config.DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set - see BUILD_README.md")

# the same psycopg2 driver the app uses (SQLAlchemy 2.1 would otherwise pick
# psycopg 3 for a plain postgresql:// URL)
url = make_url(config.DATABASE_URL).set(drivername="postgresql+psycopg2")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection)
    # all pending migrations run in one transaction: a failing one leaves the
    # database exactly as it was
    with context.begin_transaction():
        # several Cloud Run instances can start at once - only one migrates
        # at a time (the lock is released at commit)
        connection.execute(text("SELECT pg_advisory_xact_lock(4243001)"))
        context.run_migrations()
