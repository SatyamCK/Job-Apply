"""Insert a sample local user and emit one traced event for it. Idempotent.

Run inside the stack: `make seed`.
"""

from __future__ import annotations

from sqlalchemy import select

from common.db.models import User
from common.db.session import session_scope
from common.events import stage
from common.runtime import init_service
from common.tracing import current_trace_id

SEED_SUB = "local-seed-user"


def main() -> None:
    rt = init_service("seed", poll_config=False)
    try:
        with session_scope(rt.require_sessions()) as session:
            user = session.scalar(select(User).where(User.cognito_sub == SEED_SUB))
            if user is None:
                user = User(
                    cognito_sub=SEED_SUB,
                    email="seed.user@example.com",
                    name="Seed User",
                    timezone="UTC",
                )
                session.add(user)
                session.flush()
        with stage("seed", entity_type="user", entity_id=user.id, user_id=user.id) as result:
            result["seeded"] = True
            trace_id = current_trace_id()
        print(f"seeded user_id={user.id} trace_id={trace_id}")
    finally:
        rt.shutdown()


if __name__ == "__main__":
    main()
