"""backfill team-defense ``players.position`` to 'DEF'

Corrects a legacy scrape artifact: 15 of the 32 NFL team defenses have
``players.position`` set to the literal NFL.com UI banner
``"Season is Over Add to Watch List"`` (and other rows could carry NULL)
instead of ``"DEF"``. Those rows were written by an earlier parser that
treated the banner as a position; the current parser
(``ff_pipeline.crawlers.nfl_com.parsers._clean_position``) already rejects
the banner, but the upsert path never blanks a stored position back, so
the stale rows persist.

This is the repo's first DATA migration (all prior revisions are
schema-only). It is scoped strictly to the contiguous DST id range
NFL.com assigns team defenses (``nfl_com_player_id`` 100001-100032) so no
offensive player is touched, and is idempotent — rerunning only updates
rows that are not already 'DEF'. There is no meaningful downgrade for a
data correction, so ``downgrade`` is intentionally a no-op.

Revision ID: d5a2c7b9f3e1
Revises: c4d8a1f6e2b9
Create Date: 2026-06-02 02:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5a2c7b9f3e1"
down_revision: str | Sequence[str] | None = "c4d8a1f6e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Stamp 'DEF' on every team-defense row in the DST id range."""
    op.execute(
        """
        UPDATE players
           SET position = 'DEF'
         WHERE CAST(nfl_com_player_id AS INTEGER) BETWEEN 100001 AND 100032
           AND (position IS NULL OR position <> 'DEF')
        """
    )


def downgrade() -> None:
    """No-op — a data correction has no meaningful inverse."""
