from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config


class Database:
    def __init__(self, path: Path):
        self.url = f"sqlite:///{path}"
        self.engine = create_engine(self.url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")

        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        config.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        command.upgrade(config, "head")

    @contextmanager
    def session(self):
        with self.factory.begin() as session:
            yield session
