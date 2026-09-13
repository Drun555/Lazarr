from pathlib import Path
from alembic import command
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from lazarr.models import Base


def test_staged_migrations_preserve_foundation(tmp_path):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "src/lazarr/migrations"))
    url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role, active, created_at) VALUES (1, 'first', 'hash', 'admin', 1, 0)"
            )
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT username FROM users")) == "first"
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(config, "0001")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT username FROM users")) == "first"
    engine.dispose()
