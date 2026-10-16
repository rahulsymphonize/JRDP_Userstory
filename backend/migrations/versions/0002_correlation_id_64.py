"""widen execution_audit.correlation_id to 64 chars

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("execution_audit") as t:
        t.alter_column("correlation_id", type_=sa.String(64), existing_type=sa.String(36))


def downgrade():
    with op.batch_alter_table("execution_audit") as t:
        t.alter_column("correlation_id", type_=sa.String(36), existing_type=sa.String(64))
