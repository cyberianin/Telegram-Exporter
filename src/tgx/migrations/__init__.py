from contextlib import closing
from pathlib import Path


def upgrade_database(path: Path) -> None:
    import sqlite3

    with closing(sqlite3.connect(path)) as connection:
        try:
            version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            if version and version[0] == "0001":
                return
        except sqlite3.OperationalError:
            pass
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import URL

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent))
    config.set_main_option(
        "sqlalchemy.url",
        URL.create("sqlite", database=str(path.resolve()))
        .render_as_string(hide_password=False)
        .replace("%", "%%"),
    )
    command.upgrade(config, "head")
