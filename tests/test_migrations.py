import asyncio

from alembic import command
from alembic.config import Config


async def test_migrations_downgrade_and_upgrade_roundtrip(alembic_config: Config) -> None:
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    await asyncio.to_thread(command.upgrade, alembic_config, "head")


async def test_models_match_migrations(alembic_config: Config) -> None:
    await asyncio.to_thread(command.check, alembic_config)
