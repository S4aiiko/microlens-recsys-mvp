from __future__ import annotations

import asyncio
import signal

import uvicorn
from alembic import command
from alembic.config import Config

from apps.api.app.auth.security import PasswordService, normalize_username
from apps.api.app.bootstrap import bootstrap_plan
from apps.api.app.db.seed import seed_demo_users, seed_password_from_environment
from apps.api.app.internal_main import create_internal_app
from apps.api.app.main import create_public_app
from apps.api.app.runtime import RuntimeContext, create_runtime
from apps.api.app.settings import AppSettings


def migrate_and_seed(runtime: RuntimeContext) -> None:
    """Apply the exact Alembic head, then idempotently seed before listeners bind."""

    command.upgrade(Config(str(runtime.settings.alembic_ini)), "head")
    password = seed_password_from_environment()
    passwords = PasswordService()
    with runtime.sessions() as session, session.begin():
        seed_demo_users(
            session,
            password=password,
            hash_password=passwords.hash,
            normalize_username=normalize_username,
        )


async def serve() -> None:
    settings = AppSettings.from_environment()
    runtime = create_runtime(settings)
    plan = bootstrap_plan()
    if plan.migrate_and_seed:
        migrate_and_seed(runtime)
    if not await runtime.ping_redis():
        raise RuntimeError("Redis PING failed before API startup")
    if plan.restore_active_model:
        runtime.restore_active_model()
    public = uvicorn.Server(
        uvicorn.Config(
            create_public_app(settings, runtime),
            host="0.0.0.0",
            port=8000,
            workers=1,
            log_level="info",
            access_log=True,
        )
    )
    internal = uvicorn.Server(
        uvicorn.Config(
            create_internal_app(settings, runtime),
            host="0.0.0.0",
            port=8001,
            workers=1,
            log_level="info",
            access_log=True,
        )
    )
    # Both listeners intentionally share one process/runtime model slot. Install a
    # single signal owner so stopping one listener cannot orphan the other.
    public.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    internal.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()

    def stop_both() -> None:
        public.should_exit = True
        internal.should_exit = True

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_both)
        except NotImplementedError:
            pass
    try:
        await asyncio.gather(public.serve(), internal.serve())
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
