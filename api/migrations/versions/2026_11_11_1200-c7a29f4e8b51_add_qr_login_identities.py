"""add qr_login_identities table

Revision ID: c7a29f4e8b51
Revises: 0cc3158b50b0
Create Date: 2026-11-11 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models

# revision identifiers, used by Alembic.
revision = "c7a29f4e8b51"
down_revision = "0cc3158b50b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qr_login_identities",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("app_id", models.types.StringUUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_openid", sa.String(length=255), nullable=False),
        sa.Column("end_user_id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="qr_login_identities_pkey"),
        sa.UniqueConstraint(
            "app_id", "provider", "provider_openid", name="qr_login_identities_app_provider_openid_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("qr_login_identities")
