"""initial schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

USER_ID = sa.String(255)


def upgrade():
    op.create_table(
        "providers",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_table(
        "user_provider_credentials",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", USER_ID, nullable=False),
        sa.Column(
            "provider_id",
            sa.String(32),
            sa.ForeignKey("providers.id"),
            nullable=False,
        ),
        sa.Column("encrypted_key", sa.LargeBinary, nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary, nullable=False),
        sa.Column("kek_id", sa.String(64), nullable=False),
        sa.Column("key_last4", sa.String(4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "user_id", "provider_id", name="uq_cred_user_provider"
        ),
    )
    op.create_index(
        "ix_user_provider_credentials_user_id",
        "user_provider_credentials",
        ["user_id"],
    )
    op.create_table(
        "user_model_preferences",
        sa.Column("user_id", USER_ID, primary_key=True),
        sa.Column(
            "provider_id",
            sa.String(32),
            sa.ForeignKey("providers.id"),
            nullable=False,
        ),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "system_prompt_base",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("version", sa.Integer, nullable=False, unique=True),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("protected_sections", sa.JSON, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "is_current",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "uq_base_current",
        "system_prompt_base",
        ["is_current"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current"),
    )
    op.create_table(
        "user_prompt_patches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", USER_ID, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("base_version", sa.Integer, nullable=False),
        sa.Column("patch_content", sa.JSON, nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "version", name="uq_patch_user_version"),
    )
    op.create_index(
        "ix_user_prompt_patches_user_id", "user_prompt_patches", ["user_id"]
    )
    op.create_index(
        "uq_patch_active",
        "user_prompt_patches",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active"),
    )
    op.create_table(
        "execution_audit",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", USER_ID, nullable=False),
        sa.Column(
            "provider_id",
            sa.String(32),
            sa.ForeignKey("providers.id"),
            nullable=False,
        ),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("base_version", sa.Integer),
        sa.Column("patch_version", sa.Integer),
        sa.Column("tokens_in", sa.Integer),
        sa.Column("tokens_out", sa.Integer),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("correlation_id", sa.String(36)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_execution_audit_user_id", "execution_audit", ["user_id"]
    )


def downgrade():
    for t in (
        "execution_audit",
        "user_prompt_patches",
        "system_prompt_base",
        "user_model_preferences",
        "user_provider_credentials",
        "providers",
    ):
        op.drop_table(t)
