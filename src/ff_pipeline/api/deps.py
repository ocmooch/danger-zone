"""FastAPI dependency-injection wiring.

Every route that needs DB access pulls a Session via ``SessionDep``
(a type alias around ``Annotated[Session, Depends(get_session)]``). The
engine is stored on ``app.state`` so tests can swap in a temp-file
SQLite engine without monkey-patching the module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine


def get_session(request: Request) -> Iterator[Session]:
    """Yield a SQLAlchemy session bound to the app's engine."""
    engine: Engine = request.app.state.engine
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
