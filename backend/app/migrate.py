"""Apply Alembic migrations, adopting databases created before migrations existed."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

BASELINE_REVISION = "0001"


def upgrade_database(engine: Engine) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        tables = set(inspect(connection).get_table_names())
        if "invoices" in tables and "alembic_version" not in tables:
            # Created by the earlier create_all() bootstrap, which matches revision 0001.
            command.stamp(config, BASELINE_REVISION)
        command.upgrade(config, "head")
