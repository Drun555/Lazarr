"""Persist Telegram conversations and approval actor."""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("telegram_users") as batch:
        batch.add_column(sa.Column("approved_by", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_telegram_approver", "users", ["approved_by"], ["id"])
        batch.add_column(sa.Column("dialog", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("inbox", sa.JSON(), nullable=False, server_default="[]"))
    # Older approvals did not record an actor on the row; recover it from the audit trail.
    op.execute("""UPDATE telegram_users SET approved_by = (
        SELECT user_id FROM audit_events WHERE action = 'telegram.approved'
        AND target = CAST(telegram_users.user_id AS TEXT) ORDER BY id DESC LIMIT 1
    ) WHERE status = 'approved'""")


def downgrade():
    with op.batch_alter_table("telegram_users") as batch:
        batch.drop_constraint("fk_telegram_approver", type_="foreignkey")
        batch.drop_column("approved_by")
        batch.drop_column("dialog")
        batch.drop_column("inbox")
