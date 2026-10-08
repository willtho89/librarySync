"""Database-level snapshot invalidation, including bulk SQL and provider imports.

Progress and receipt writes deliberately do not invalidate the watched snapshot.
SQLite uses the same invariants as PostgreSQL so contract tests exercise invalidation.
"""

from sqlalchemy import Connection

USER_TABLES = ("watched_items", "watchlist_items", "watchlist_sources", "watchlist_source_items")
CATALOG_TABLES = ("media_items", "episode_items")


def install_watch_state_triggers(connection: Connection) -> None:
    dialect = connection.dialect.name
    if dialect not in {"sqlite", "postgresql"}:
        raise RuntimeError(f"Watch State revisions do not support {dialect}")
    if dialect == "postgresql":
        connection.exec_driver_sql("""
            CREATE OR REPLACE FUNCTION librarysync_watch_revision() RETURNS trigger AS $$
            BEGIN
                IF TG_TABLE_NAME = 'watch_state_entries' THEN
                    IF TG_OP = 'DELETE' THEN
                        IF OLD.category = 'playback' THEN RETURN OLD; END IF;
                    ELSE
                        IF NEW.category = 'playback' THEN RETURN NEW; END IF;
                    END IF;
                END IF;
                IF TG_OP <> 'INSERT' THEN
                    INSERT INTO watch_state_revisions(user_id, revision)
                    SELECT OLD.user_id, 1 WHERE EXISTS(SELECT 1 FROM users WHERE id = OLD.user_id)
                    ON CONFLICT(user_id) DO UPDATE SET revision = watch_state_revisions.revision + 1;
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    INSERT INTO watch_state_revisions(user_id, revision) VALUES(NEW.user_id, 1)
                    ON CONFLICT(user_id) DO UPDATE SET revision = watch_state_revisions.revision + 1;
                    RETURN NEW;
                END IF;
                RETURN OLD;
            END; $$ LANGUAGE plpgsql
        """)
        connection.exec_driver_sql("""
            CREATE OR REPLACE FUNCTION librarysync_catalog_revision() RETURNS trigger AS $$
            BEGIN
                INSERT INTO watch_state_catalog_revision(id, revision) VALUES(1, 1)
                ON CONFLICT(id) DO UPDATE SET revision = watch_state_catalog_revision.revision + 1;
                RETURN NULL;
            END; $$ LANGUAGE plpgsql
        """)
        for table in (*USER_TABLES, "watch_state_entries"):
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS watch_revision ON {table}")
            connection.exec_driver_sql(
                f"CREATE TRIGGER watch_revision AFTER INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION librarysync_watch_revision()"
            )
        for table in CATALOG_TABLES:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS catalog_revision ON {table}")
            connection.exec_driver_sql(
                f"CREATE TRIGGER catalog_revision AFTER INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION librarysync_catalog_revision()"
            )
        return

    for table in (*USER_TABLES, "watch_state_entries"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            refs = ["NEW"] if operation == "INSERT" else ["OLD"] if operation == "DELETE" else ["OLD", "NEW"]
            condition = ""
            if table == "watch_state_entries":
                condition = f" WHEN {refs[-1]}.category <> 'playback'"
            statements = " ".join(
                "INSERT INTO watch_state_revisions(user_id, revision) "  # noqa: S608 - fixed identifiers only
                f"SELECT {ref}.user_id, 1 WHERE EXISTS(SELECT 1 FROM users WHERE id = {ref}.user_id) "
                "ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;"
                for ref in refs
            )
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS watch_revision_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table}{condition} BEGIN {statements} END"
            )
    for table in CATALOG_TABLES:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS catalog_revision_{table}_{operation.lower()} "
                f"AFTER {operation} ON {table} BEGIN "
                "INSERT INTO watch_state_catalog_revision(id, revision) VALUES(1, 1) "
                "ON CONFLICT(id) DO UPDATE SET revision = revision + 1; END"
            )


def remove_watch_state_triggers(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        for table in (*USER_TABLES, "watch_state_entries"):
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS watch_revision ON {table}")
        for table in CATALOG_TABLES:
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS catalog_revision ON {table}")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS librarysync_watch_revision()")
        connection.exec_driver_sql("DROP FUNCTION IF EXISTS librarysync_catalog_revision()")
    else:
        for table in (*USER_TABLES, "watch_state_entries", *CATALOG_TABLES):
            prefix = "catalog_revision" if table in CATALOG_TABLES else "watch_revision"
            for operation in ("insert", "update", "delete"):
                connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {prefix}_{table}_{operation}")
