"""QR login identity persistence.

Maps a provider identity (``provider`` + ``provider_openid``) to an anonymous
web-app visitor (``EndUser``) per app, so the same scanning user always
rebinds to the same visitor identity across QR logins.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import StringUUID, TypeBase


class QrLoginIdentity(TypeBase):
    """Stable per-app binding between a QR login provider identity and a visitor.

    Contracts:
    - At most one row per (app_id, provider, provider_openid), enforced by the
      unique constraint; concurrent first scans converge on the same end_user
      (the loser of the insert race rolls back and re-reads the winner's row).
    - ``end_user_id`` references the anonymous ``EndUser`` created on the first
      confirmed login and is never reassigned afterwards.
    """

    __tablename__ = "qr_login_identities"
    __table_args__ = (
        UniqueConstraint("app_id", "provider", "provider_openid", name="qr_login_identities_app_provider_openid_key"),
        {"extend_existing": True},
    )

    id: Mapped[str] = mapped_column(
        StringUUID,
        primary_key=True,
        insert_default=lambda: str(uuid4()),
        default_factory=lambda: str(uuid4()),
        init=False,
    )
    app_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_openid: Mapped[str] = mapped_column(String(255), nullable=False)
    # NOTE: ``app_id`` / ``end_user_id`` are declared as StringUUID (not plain String)
    # to match the migrations and the native uuid columns on PostgreSQL; a plain
    # String would surface uuid.UUID objects on reads and break JSON serialization.
    end_user_id: Mapped[str] = mapped_column(StringUUID, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        init=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "app_id": self.app_id,
            "provider": self.provider,
            "provider_openid": self.provider_openid,
            "end_user_id": self.end_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
