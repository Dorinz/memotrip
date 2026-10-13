"""baseline: the schema as it stood when Alembic was introduced

IF NOT EXISTS throughout, so this also runs cleanly on a database that
already had these tables (prod), where it only gets recorded as done.

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, display_name TEXT, created TIMESTAMPTZ)""")
    # user_id: nullable - a guest's trip has none, and is never listed
    # under any account; deleting a user deletes their trips' rows
    op.execute("""CREATE TABLE IF NOT EXISTS trips(
        id TEXT PRIMARY KEY, created TIMESTAMPTZ, description TEXT, region_hint TEXT,
        status TEXT, stage TEXT, error TEXT, picker_uri TEXT, picker_sid TEXT,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, photo_owner_key TEXT)""")
    # one row per logline() call - see webapp.logline()
    op.execute("""CREATE TABLE IF NOT EXISTS trip_logs(
        id BIGSERIAL PRIMARY KEY,
        trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
        ts TIMESTAMPTZ NOT NULL, msg TEXT NOT NULL)""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_trip_logs_trip ON trip_logs(trip_id, id)")
    # Google Photos OAuth tokens, keyed by owner_key - "user:<id>" for a
    # logged-in account or "guest:<random>" for an anonymous session (see
    # webapp.photo_owner_key), not a users.id FK, so a guest can connect
    # their own Google Photos without ever creating a MemoTrip account.
    op.execute("""CREATE TABLE IF NOT EXISTS photo_accounts(
        owner_key TEXT PRIMARY KEY,
        refresh_token TEXT, access_token TEXT, token_expiry TEXT, granted TEXT)""")
    # one row per Gemini-backed generation action (new trip / rerun / photo
    # pick finish), keyed by the same identity as photo_owner_key, plus the
    # client IP for a second, per-network daily cap (a guest identity
    # resets by just clearing cookies) - see webapp.quota_exceeded().
    op.execute("""CREATE TABLE IF NOT EXISTS generations(
        id SERIAL PRIMARY KEY, identity TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, ip TEXT)""")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_generations_identity_ts ON generations(identity, ts)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_generations_ip_ts ON generations(ip, ts)")


def downgrade() -> None:
    # undoing the baseline means deleting every table and all its data - in
    # prod, restore the daily Cloud SQL backup instead
    raise NotImplementedError("the baseline can't be downgraded")
