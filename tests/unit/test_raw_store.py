"""Raw payload store + source_payloads index (S2)."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ff_pipeline.archive.raw_store import (
    PayloadMeta,
    RawStore,
    RawStoreError,
    newest_payload,
    record_payload,
    sha256_hex,
)
from ff_pipeline.repository.database import Base
from ff_pipeline.repository.models import SourcePayload

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
URL_A = "https://api.sleeper.app/v1/league/1/rosters"
URL_B = "https://api.sleeper.app/v1/league/2/rosters"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SourcePayload.__table__])  # type: ignore[list-item]
    with Session(engine) as ss:
        yield ss
    engine.dispose()


def _meta(url: str = URL_A) -> PayloadMeta:
    return PayloadMeta("sleeper_league", "rosters", url, "1", 2026, None, 200)


def _record(ss: Session, store: RawStore, body: bytes, url: str, at: datetime) -> bool:
    sha, _ = store.put(body)
    return record_payload(
        ss, _meta(url), sha256=sha, byte_size=len(body), fetched_at=at, run_id="r"
    )


def test_put_is_content_addressed_and_byte_exact(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    body = b'{"a": 1, "b": [1.50, null]}'
    sha, written = store.put(body)
    assert written
    assert sha == sha256_hex(body)
    path = store.path_for(sha)
    assert path == tmp_path / sha[:2] / f"{sha}.json.gz"
    assert gzip.decompress(path.read_bytes()) == body
    assert store.get(sha) == body


def test_same_bytes_write_one_file(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    first = store.put(b"[]")
    second = store.put(b"[]")
    assert first[1] is True
    assert second == (first[0], False)
    assert len(list(tmp_path.rglob("*.gz"))) == 1


def test_file_bytes_are_deterministic(tmp_path: Path) -> None:
    a, b = RawStore(tmp_path / "a"), RawStore(tmp_path / "b")
    sha, _ = a.put(b'{"x": 1}')
    b.put(b'{"x": 1}')
    assert a.path_for(sha).read_bytes() == b.path_for(sha).read_bytes()


def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RawStore(tmp_path)

    def boom(_self: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.put(b"{}")
    assert list(tmp_path.rglob("*")) == [tmp_path / sha256_hex(b"{}")[:2]]


def test_get_detects_missing_and_corrupt(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    with pytest.raises(RawStoreError, match="missing"):
        store.get(sha256_hex(b"nope"))
    sha, _ = store.put(b"{}")
    store.path_for(sha).write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(RawStoreError, match="does not match"):
        store.get(sha)
    with pytest.raises(RawStoreError, match="not a sha256"):
        store.path_for("../../etc/passwd")


def test_identical_refetch_bumps_existing_row(tmp_path: Path, session: Session) -> None:
    store = RawStore(tmp_path)
    assert _record(session, store, b"[1]", URL_A, T0) is True
    assert _record(session, store, b"[1]", URL_A, T0 + timedelta(hours=1)) is False
    rows = session.scalars(select(SourcePayload)).all()
    assert len(rows) == 1
    assert rows[0].fetch_count == 2
    assert rows[0].first_fetched_at.replace(tzinfo=UTC) == T0
    assert rows[0].last_fetched_at.replace(tzinfo=UTC) == T0 + timedelta(hours=1)


def test_same_bytes_two_urls_one_file_two_rows(tmp_path: Path, session: Session) -> None:
    store = RawStore(tmp_path)
    assert _record(session, store, b"[]", URL_A, T0)
    assert _record(session, store, b"[]", URL_B, T0)
    assert session.scalar(select(func.count()).select_from(SourcePayload)) == 2
    assert len(list(tmp_path.rglob("*.gz"))) == 1


def test_changed_bytes_add_a_revision_and_newest_wins(tmp_path: Path, session: Session) -> None:
    store = RawStore(tmp_path)
    _record(session, store, b"[1]", URL_A, T0)
    _record(session, store, b"[2]", URL_A, T0 + timedelta(days=1))
    newest = newest_payload(session, URL_A)
    assert newest is not None
    assert store.get(newest.sha256) == b"[2]"
    as_of = newest_payload(session, URL_A, as_of=T0 + timedelta(hours=1))
    assert as_of is not None
    assert store.get(as_of.sha256) == b"[1]"
    assert newest_payload(session, URL_A, as_of=T0 - timedelta(seconds=1)) is None


def test_reverted_bytes_are_the_newest(tmp_path: Path, session: Session) -> None:
    store = RawStore(tmp_path)
    _record(session, store, b"[1]", URL_A, T0)
    _record(session, store, b"[2]", URL_A, T0 + timedelta(days=1))
    _record(session, store, b"[1]", URL_A, T0 + timedelta(days=2))
    newest = newest_payload(session, URL_A)
    assert newest is not None
    assert store.get(newest.sha256) == b"[1]"
