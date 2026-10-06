"""Repair Stremio bitfield imports that attached every episode to the last one.

A closure bug in the Stremio series-bitfield import resolved every episode
candidate of a show to the final episode of the batch. Affected watches are
identified by their Stremio sync external id (``<show>:<season>:<episode>``)
disagreeing with the linked episode while sibling Stremio watches share the
same episode and watched_at. They are moved to the correct episode, stale
downstream sync rows are dropped and a new_item_added job re-delivers them.
"""

from alembic import op

revision = "fb3b3e7932d5"
down_revision = "8e9f0a1b2c3d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE TEMP TABLE stremio_episode_repair AS
        WITH parsed AS (
            SELECT
                watched_item_id,
                external_id,
                CASE WHEN external_id ~ '^[^:]+:[0-9]{1,6}:[0-9]{1,6}$'
                    THEN split_part(external_id, ':', 2)::int END AS season_number,
                CASE WHEN external_id ~ '^[^:]+:[0-9]{1,6}:[0-9]{1,6}$'
                    THEN split_part(external_id, ':', 3)::int END AS episode_number
            FROM watch_syncs
            WHERE provider = 'stremio'
        )
        SELECT DISTINCT ON (wi.id)
            wi.id AS watched_id,
            wi.user_id,
            p.external_id AS video_id,
            cur.id AS current_episode_id,
            tgt.id AS target_episode_id
        FROM parsed p
        JOIN watched_items wi ON wi.id = p.watched_item_id
        JOIN episode_items cur ON cur.id = wi.episode_item_id
        JOIN episode_items tgt
            ON tgt.show_media_item_id = cur.show_media_item_id
            AND tgt.season_number = p.season_number
            AND tgt.episode_number = p.episode_number
        WHERE wi.source = 'stremio'
            AND p.season_number IS NOT NULL
            AND tgt.id <> cur.id
            AND EXISTS (
                SELECT 1
                FROM watched_items sibling
                WHERE sibling.user_id = wi.user_id
                    AND sibling.episode_item_id = wi.episode_item_id
                    AND sibling.watched_at = wi.watched_at
                    AND sibling.source = 'stremio'
                    AND sibling.id <> wi.id
            )
        ORDER BY wi.id
        """
    )
    op.execute(
        """
        UPDATE watched_items wi
        SET episode_item_id = r.target_episode_id
        FROM stremio_episode_repair r
        WHERE wi.id = r.watched_id
        """
    )
    op.execute(
        """
        UPDATE watch_events e
        SET episode_item_id = r.target_episode_id
        FROM stremio_episode_repair r
        WHERE e.user_id = r.user_id
            AND e.event_type = 'stremio_imported'
            AND e.episode_item_id = r.current_episode_id
            AND e.raw ->> 'video_id' = r.video_id
        """
    )
    # Downstream providers received the wrong episode; forget those deliveries
    # and cancel anything still queued so the correct episode is pushed instead.
    op.execute(
        """
        DELETE FROM watch_syncs ws
        USING stremio_episode_repair r
        WHERE ws.watched_item_id = r.watched_id
            AND ws.provider <> 'stremio'
        """
    )
    op.execute(
        """
        UPDATE outbox o
        SET status = 'failed_permanent',
            dedupe_key = NULL,
            run_after = NULL,
            last_error = 'Superseded by Stremio episode repair',
            updated_at = now()
        FROM stremio_episode_repair r
        WHERE o.status IN ('pending', 'failed_retryable')
            AND o.payload ->> 'watched_item_id' = r.watched_id
        """
    )
    op.execute(
        """
        INSERT INTO outbox (
            id, user_id, target_provider, job_type, payload, status, attempts, created_at, updated_at
        )
        SELECT
            gen_random_uuid()::text,
            r.user_id,
            'internal',
            'new_item_added',
            json_build_object('watched_item_id', r.watched_id, 'is_rewatch', false),
            'pending',
            0,
            now(),
            now()
        FROM stremio_episode_repair r
        """
    )
    op.execute("DROP TABLE stremio_episode_repair")


def downgrade() -> None:
    # Data repair only; the previous (incorrect) episode links are not restored.
    pass
