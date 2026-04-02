"""ORM models — import side effects register tables on Base.metadata for init_db()."""

from v1.Models.follow_account import FollowAccount  # noqa: F401
from v1.Models.user import User  # noqa: F401

__all__ = ["FollowAccount", "User"]
