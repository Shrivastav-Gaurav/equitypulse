"""Append-only raw-payload store (`snapshots` table).

Every fetched payload is kept so any number we computed can be traced back to exactly what the
source said on that date. An identical payload for the same (source, endpoint, entity, as_of)
is stored once; the same payload on a later as_of is stored again.
"""

import datetime as dt
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from equitypulse.db.models import Snapshot
from equitypulse.db.types import utcnow


def canonical_json(payload: Any) -> str:
    """Stable serialisation, so the same content always hashes the same."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def store_snapshot(
    session: Session,
    *,
    source: str,
    endpoint: str,
    entity: str,
    as_of: dt.date,
    payload: Any,
    fetched_at: dt.datetime | None = None,
) -> bool:
    """Add the payload unless an identical one exists for this key. True if a row was added.
    Does not commit: it belongs to the caller's transaction."""
    payload_json = canonical_json(payload)
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    exists = session.scalar(
        select(Snapshot.id).where(
            Snapshot.source == source,
            Snapshot.endpoint == endpoint,
            Snapshot.entity == entity,
            Snapshot.as_of == as_of,
            Snapshot.payload_hash == payload_hash,
        )
    )
    if exists is not None:
        return False
    session.add(
        Snapshot(
            source=source,
            endpoint=endpoint,
            entity=entity,
            as_of=as_of,
            fetched_at=fetched_at or utcnow(),
            payload_json=payload_json,
            payload_hash=payload_hash,
        )
    )
    session.flush()
    return True
