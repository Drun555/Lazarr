from alembic import context
from sqlalchemy import create_engine
from lazarr.models import Base

config = context.config
if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
