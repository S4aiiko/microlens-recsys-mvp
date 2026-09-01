from __future__ import annotations

import argparse

from alembic import command
from alembic.config import Config

from apps.api.app.auth.security import PasswordService, normalize_username
from apps.api.app.db.seed import seed_demo_users, seed_password_from_environment
from apps.api.app.runtime import create_runtime
from apps.api.app.settings import AppSettings


def migrate(settings: AppSettings) -> None:
    command.upgrade(Config(str(settings.alembic_ini)), "head")


def seed(settings: AppSettings) -> None:
    runtime = create_runtime(settings)
    try:
        password = seed_password_from_environment()
        passwords = PasswordService()
        with runtime.sessions() as session, session.begin():
            users = seed_demo_users(
                session,
                password=password,
                hash_password=passwords.hash,
                normalize_username=normalize_username,
            )
        print(f"seeded_users={len(users)} status=PASS")
    finally:
        runtime.engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 2D platform integration commands")
    parser.add_argument("command", choices=("migrate", "seed", "migrate-seed"))
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    settings = AppSettings.from_environment()
    if arguments.command in {"migrate", "migrate-seed"}:
        migrate(settings)
        print("migration_status=PASS")
    if arguments.command in {"seed", "migrate-seed"}:
        seed(settings)


if __name__ == "__main__":
    main()
