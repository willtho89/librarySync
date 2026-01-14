"""normalize_genres_to_strings

Revision ID: 34ca9376da38
Revises: f5e85859e1da
Create Date: 2026-01-14 14:57:32.130308

"""

import json

from alembic import op
from sqlalchemy import text

revision = "34ca9376da38"
down_revision = "f5e85859e1da"
branch_labels = None
depends_on = None


def normalize_genres(genres):
    if not isinstance(genres, list):
        return None
    normalized = []
    for g in genres:
        if isinstance(g, dict) and "name" in g:
            normalized.append(g["name"])
        elif isinstance(g, str):
            normalized.append(g)
        else:
            normalized.append(str(g))
    return normalized if normalized else None


def upgrade() -> None:
    # Update genres column to normalize dicts to strings
    conn = op.get_bind()
    result = conn.execute(text("SELECT id, genres FROM media_items WHERE genres IS NOT NULL"))
    updates = []
    for row in result:
        item_id, genres = row
        normalized = normalize_genres(genres)
        if normalized != genres:
            updates.append((item_id, normalized))

    for item_id, normalized in updates:
        conn.execute(
            text("UPDATE media_items SET genres = :genres WHERE id = :id"),
            {"genres": json.dumps(normalized), "id": item_id},
        )


def downgrade() -> None:
    # Cannot reverse normalization
    pass
