"""Telegram authorization requests and persistent replies."""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telegram_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bot_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("reply_version", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.Float(), nullable=False),
        sa.Column("delivery_error", sa.String(), nullable=False),
        sa.UniqueConstraint("bot_id", "user_id"),
    )


def downgrade():
    op.drop_table("telegram_users")
