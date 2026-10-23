"""index execution_audit.correlation_id (admin trace lookup)

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_execution_audit_correlation_id", "execution_audit", ["correlation_id"])


def downgrade():
    op.drop_index("ix_execution_audit_correlation_id", "execution_audit")
