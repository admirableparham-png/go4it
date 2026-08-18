"""Create (or reset) an admin user — for a fresh production deploy, instead of the demo seed.

    docker exec go4it-app python scripts/create_admin.py you@example.com 'StrongPassword' "Your Name"

Idempotent: if the email exists, it's upgraded to an active admin with the new password.
"""
import sys

sys.path.insert(0, "/app")
from sqlmodel import Session, select  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.db import engine, init_db  # noqa: E402
from app.models import User  # noqa: E402


def run(email, password, name="Admin"):
    if len(password) < 6:
        print("password must be at least 6 characters")
        sys.exit(1)
    init_db()
    email = email.strip().lower()
    with Session(engine) as s:
        u = s.exec(select(User).where(User.email == email)).first()
        if u:
            u.password_hash, u.role, u.active, u.name = hash_password(password), "admin", True, name
            action = "updated"
        else:
            u = User(email=email, name=name, role="admin", active=True,
                     password_hash=hash_password(password))
            s.add(u)
            action = "created"
        s.commit()
    print(f"admin {action}: {email}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: create_admin.py <email> <password> [name]")
        sys.exit(1)
    run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "Admin")
