"""Schema (SQLAlchemy Core) and engine. Alembic migrations in backend/migrations must match this."""
from sqlalchemy import (
    JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, LargeBinary, MetaData, String, Table,
    UniqueConstraint, create_engine, event, func, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend import config

metadata = MetaData()

USER_ID = String(255)  # OIDC "sub"

providers = Table(
    "providers", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("base_url", String(255), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
)

credentials = Table(
    "user_provider_credentials", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", USER_ID, nullable=False, index=True),
    Column("provider_id", String(32), ForeignKey("providers.id"), nullable=False),
    Column("encrypted_key", LargeBinary, nullable=False),
    Column("wrapped_dek", LargeBinary, nullable=False),
    Column("kek_id", String(64), nullable=False),
    Column("key_last4", String(4), nullable=False),
    Column("status", String(16), nullable=False),
    Column("validated_at", DateTime(timezone=True)),
    UniqueConstraint("user_id", "provider_id", name="uq_cred_user_provider"),
)

preferences = Table(
    "user_model_preferences", metadata,
    Column("user_id", USER_ID, primary_key=True),
    Column("provider_id", String(32), ForeignKey("providers.id"), nullable=False),
    Column("model_id", String(255), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

base_prompts = Table(
    "system_prompt_base", metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False, unique=True),
    Column("content", JSON, nullable=False),  # [{"name", "text"}] keeps section order
    Column("protected_sections", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("is_current", Boolean, nullable=False, server_default=text("false")),
    Index("uq_base_current", "is_current", unique=True,
          postgresql_where=text("is_current"), sqlite_where=text("is_current")),
)

patches = Table(
    "user_prompt_patches", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", USER_ID, nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("base_version", Integer, nullable=False),
    Column("patch_content", JSON, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("user_id", "version", name="uq_patch_user_version"),
    Index("uq_patch_active", "user_id", unique=True,
          postgresql_where=text("is_active"), sqlite_where=text("is_active")),
)

audit = Table(
    "execution_audit", metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", USER_ID, nullable=False, index=True),
    Column("provider_id", String(32), ForeignKey("providers.id"), nullable=False),
    Column("model_id", String(255), nullable=False),
    Column("base_version", Integer),
    Column("patch_version", Integer),
    Column("tokens_in", Integer),
    Column("tokens_out", Integer),
    Column("latency_ms", Integer),
    Column("status", String(32), nullable=False),
    Column("correlation_id", String(64), index=True),  # matches the 8-64 char ids the API accepts
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

_kw = {} if config.DATABASE_URL.startswith("sqlite") else {"pool_size": config.DB_POOL_SIZE, "max_overflow": 5}
engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, **_kw)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")


def upsert(conn, table, values, conflict_cols):
    ins = (pg_insert if conn.dialect.name == "postgresql" else sqlite_insert)(table).values(**values)
    update = {k: ins.excluded[k] for k in values if k not in conflict_cols}
    conn.execute(ins.on_conflict_do_update(index_elements=conflict_cols, set_=update))


def lock(conn, key):
    """Serialise writers on `key` for this transaction (patch versioning, base publishing)."""
    if conn.dialect.name == "postgresql":
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
