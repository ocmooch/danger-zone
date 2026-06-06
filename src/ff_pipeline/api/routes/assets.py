"""``/assets/*`` routes — stream stored avatar bytes.

The ``assets`` table holds only metadata; the raw bytes live on disk under
the configured assets root (content-addressed). This route resolves an
``asset_id`` to its file and streams it with the recorded Content-Type, so
clients can reference ``GET /assets/{asset_id}`` directly as an ``<img src>``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ff_pipeline.api.deps import SessionDep  # noqa: TC001 — used at runtime by FastAPI
from ff_pipeline.api.errors import not_found
from ff_pipeline.repository.models import Asset

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/{asset_id}")
def get_asset_bytes_endpoint(
    asset_id: int, session: SessionDep, request: Request
) -> FileResponse:
    """Stream a stored asset's raw bytes (its team logo / owner avatar)."""
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise not_found(f"No asset with id {asset_id}")
    root = Path(request.app.state.assets_dir)
    path = root / asset.storage_path
    if not path.is_file():
        # Metadata row exists but the file is missing (store relocated /
        # never backfilled on this host) — a 404 is the honest answer.
        raise not_found(f"Asset {asset_id} bytes are not present on this host")
    return FileResponse(
        path,
        media_type=asset.content_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
