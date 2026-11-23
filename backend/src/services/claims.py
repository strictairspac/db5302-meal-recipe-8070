"""Batch claiming for reviewed preparation sheets.

Semantics
---------
* Every accepted review appends a row to ``review_ledger`` inside the review
  transaction.  The ledger row is locked against a global counter row until the
  transaction commits, so allocation order equals commit ("落账") order even
  when two reviewers submit at once.
* At most one claim batch is open (unacknowledged) at a time.  A batch is a
  frozen snapshot: its number, item order and contents never change.
* Re-claiming with the same token (or the same terminal name before a takeover)
  returns the same batch with an extended lease, so a dropped connection can
  retry without seeing a different number, order or content.
* Once the lease expires another terminal may take the batch over.  Takeover
  rotates the token, so a late acknowledgement from the previous terminal is
  rejected with 409.
* A new batch is only assembled after the current one is acknowledged; reviews
  that land while a batch is waiting simply queue up for the next batch.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.exc import IntegrityError

from src.config.settings import CLAIM_LEASE_SECONDS, IS_MYSQL, engine
from src.constants.workflow import ClaimStatus
from src.models.tables import claim_items, claims, counters, evidence, review_ledger
from src.services.audit import log
from src.utils.time import utc_time

REVIEW_SEQ = 'review_seq'
CLAIM_LOCK = 'claim_lock'


def _insert_ignore(conn, table, values, key='name'):
    if IS_MYSQL:
        conn.execute(mysql_insert(table).values(**values).on_duplicate_key_update(
            **{key: getattr(table.c, key)}))
    else:
        conn.execute(postgres_insert(table).values(**values)
                     .on_conflict_do_nothing(index_elements=[key]))


def _ensure_counter(conn, name):
    _insert_ignore(conn, counters, {'name': name, 'value': 0})


def _lock_counter(conn, name):
    """Row lock held until the surrounding transaction ends."""
    _ensure_counter(conn, name)
    return int(conn.execute(select(counters.c.value)
                            .where(counters.c.name == name).with_for_update()).scalar_one())


def next_review_seq(conn):
    value = _lock_counter(conn, REVIEW_SEQ) + 1
    conn.execute(counters.update().where(counters.c.name == REVIEW_SEQ).values(value=value))
    return value


def note_review(conn, evidence_id):
    """Append a committed review to the dispatch ledger."""
    conn.execute(review_ledger.insert().values(evidence_id=evidence_id, seq=next_review_seq(conn)))


def backfill_ledger():
    """Adopt reviews recorded before the ledger existed, oldest review first."""
    with engine.begin() as conn:
        _lock_counter(conn, CLAIM_LOCK)
        _lock_counter(conn, REVIEW_SEQ)
        rows = conn.execute(
            select(evidence.c.id)
            .select_from(evidence.outerjoin(review_ledger, review_ledger.c.evidence_id == evidence.c.id))
            .where(evidence.c.reviewed_at.is_not(None), review_ledger.c.id.is_(None))
            .order_by(evidence.c.reviewed_at, evidence.c.id)).scalars().all()
        for evidence_id in rows:
            conn.execute(review_ledger.insert().values(evidence_id=evidence_id, seq=next_review_seq(conn)))


def _lease_expired(value):
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except (TypeError, ValueError):
        return True


def _pending(conn):
    """Reviewed evidence never dispatched, in review (ledger) order."""
    return conn.execute(
        select(review_ledger.c.evidence_id, review_ledger.c.seq)
        .select_from(review_ledger.outerjoin(claim_items,
                                             claim_items.c.evidence_id == review_ledger.c.evidence_id))
        .where(claim_items.c.id.is_(None))
        .order_by(review_ledger.c.seq)).all()


def _items(conn, claim_id, include_report=True):
    columns = [claim_items.c.position, claim_items.c.evidence_id, claim_items.c.review_seq,
               evidence.c.run_id, evidence.c.digest, evidence.c.reviewer,
               evidence.c.review_note, evidence.c.reviewed_at]
    if include_report:
        columns.append(evidence.c.report)
    rows = conn.execute(
        select(*columns)
        .select_from(claim_items.join(evidence, evidence.c.id == claim_items.c.evidence_id))
        .where(claim_items.c.claim_id == claim_id)
        .order_by(claim_items.c.position)).mappings().all()
    return [dict(row) for row in rows]


def _payload(conn, claim, reveal_token=False, include_report=True):
    body = {
        'id': claim['id'],
        'number': f'PB-{claim["id"]:06d}',
        'status': claim['status'],
        'terminal': claim['terminal'],
        'claimed_at': claim['claimed_at'],
        'lease_expires_at': claim['lease_expires_at'],
        'acknowledged_at': claim['acknowledged_at'],
        'acknowledged_by': claim['acknowledged_by'],
        'item_count': claim['item_count'],
        'items': _items(conn, claim['id'], include_report),
    }
    if reveal_token:
        body['claim_token'] = claim['claim_token']
    return body


def _row(conn, claim_id):
    return conn.execute(select(claims).where(claims.c.id == claim_id)).mappings().one()


def _open(conn):
    return conn.execute(select(claims).where(claims.c.open_slot == 1)
                        .with_for_update()).mappings().first()


def claim(terminal, token=None, actor='buyer'):
    terminal = (terminal or '').strip()
    if not terminal:
        raise HTTPException(422, '终端标识不能为空')
    with engine.begin() as conn:
        _lock_counter(conn, CLAIM_LOCK)
        # Wait out any review that already allocated a sequence number but has
        # not committed yet; otherwise a later review could be dispatched ahead
        # of an earlier one.  Reviews that acquire this lock after us simply
        # land in a later batch.
        _lock_counter(conn, REVIEW_SEQ)
        if token is not None:
            # If the acknowledgement response was lost, replaying the token
            # must return the batch that was already acknowledged rather than
            # opening a new one.
            settled = conn.execute(select(claims).where(claims.c.claim_token == token)
                                   .with_for_update()).mappings().first()
            if settled is not None and settled['status'] == ClaimStatus.ACKNOWLEDGED:
                return _payload(conn, settled, reveal_token=True)
        current = _open(conn)
        if current is not None:
            if token is not None:
                same_holder = token == current['claim_token']
            else:
                same_holder = terminal == current['terminal']
            if same_holder:
                # Retry or lease renewal: same batch, same token.  The token
                # may be presented from a terminal name that differs from the
                # one on record (e.g. after a reconnect), so ownership follows
                # the caller.
                conn.execute(claims.update().where(claims.c.id == current['id']).values(
                    terminal=terminal, lease_expires_at=utc_time(CLAIM_LEASE_SECONDS)))
                log(conn, actor, 'claim_resume', current['id'])
                return _payload(conn, _row(conn, current['id']), reveal_token=True)
            if not _lease_expired(current['lease_expires_at']):
                raise HTTPException(409, '当前批次尚未确认且原终端租约未到期，请稍后再领')
            return _takeover(conn, current, terminal, actor)

        pending = _pending(conn)
        if not pending:
            return None
        claim_token = str(uuid.uuid4())
        try:
            claim_id = conn.execute(claims.insert().values(
                status=ClaimStatus.OPEN, open_slot=1, claim_token=claim_token, terminal=terminal,
                claimed_at=utc_time(), lease_expires_at=utc_time(CLAIM_LEASE_SECONDS),
                last_review_seq=pending[-1].seq, item_count=len(pending))).inserted_primary_key[0]
            conn.execute(claim_items.insert(), [
                {'claim_id': claim_id, 'evidence_id': row.evidence_id,
                 'position': position, 'review_seq': row.seq}
                for position, row in enumerate(pending, start=1)])
        except IntegrityError:
            # The unique open_slot constraint is the last guard against a second
            # open batch; the loser observes the winner on retry.
            raise HTTPException(409, '另一终端已先领走当前批次，请重试') from None
        log(conn, actor, 'claim_create', claim_id)
        return _payload(conn, _row(conn, claim_id), reveal_token=True)


def _takeover(conn, current, terminal, actor):
    """Keep the batch number and item order, but invalidate the old holder."""
    claim_token = str(uuid.uuid4())
    conn.execute(claims.update().where(claims.c.id == current['id']).values(
        claim_token=claim_token, terminal=terminal, claimed_at=utc_time(),
        lease_expires_at=utc_time(CLAIM_LEASE_SECONDS)))
    log(conn, actor, 'claim_takeover', current['id'])
    return _payload(conn, _row(conn, current['id']), reveal_token=True)


def acknowledge(claim_id, token, actor='buyer'):
    with engine.begin() as conn:
        _lock_counter(conn, CLAIM_LOCK)
        current = conn.execute(select(claims).where(claims.c.id == claim_id)
                               .with_for_update()).mappings().first()
        if current is None:
            raise HTTPException(404, '找不到该领单批次')
        if token != current['claim_token']:
            raise HTTPException(409, '该批次已在其他终端续领，旧终端的确认无效')
        if current['status'] != ClaimStatus.ACKNOWLEDGED:
            conn.execute(claims.update().where(claims.c.id == claim_id).values(
                status=ClaimStatus.ACKNOWLEDGED, open_slot=None,
                acknowledged_at=utc_time(), acknowledged_by=actor))
            log(conn, actor, 'claim_ack', claim_id)
        return _payload(conn, _row(conn, claim_id))


def recent(limit=100):
    with engine.connect() as conn:
        rows = conn.execute(select(claims).order_by(claims.c.id.desc()).limit(limit)).mappings().all()
        return [_payload(conn, row, include_report=False) for row in rows]


def detail(claim_id):
    with engine.connect() as conn:
        row = conn.execute(select(claims).where(claims.c.id == claim_id)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该领单批次')
        return _payload(conn, row)
