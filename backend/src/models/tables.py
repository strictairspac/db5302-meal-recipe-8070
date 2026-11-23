from sqlalchemy import (JSON, BigInteger, Column, ForeignKey, Integer, MetaData,
                        String, Table, Text, UniqueConstraint)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
# Reviewed evidence in the exact order reviews were committed.  A separate
# table (instead of a column on business_evidence) means existing databases can
# adopt the feature without altering the original table.
review_ledger = Table('review_ledger', schema,
    Column('id', Integer, primary_key=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False, unique=True),
    Column('seq', BigInteger, nullable=False, unique=True))
counters = Table('workflow_counters', schema,
    Column('name', String(40), primary_key=True),
    Column('value', BigInteger, nullable=False))
claims = Table('purchase_claims', schema,
    Column('id', Integer, primary_key=True),
    Column('status', String(20), nullable=False, index=True),
    # Only the single unacknowledged batch has open_slot = 1.  Acknowledged
    # batches keep NULL, so the unique constraint makes the database itself
    # reject creation of a second open batch.
    Column('open_slot', Integer, unique=True),
    Column('claim_token', String(36), nullable=False),
    Column('terminal', String(64)),
    Column('claimed_at', String(32), nullable=False),
    Column('lease_expires_at', String(32), nullable=False),
    Column('acknowledged_at', String(32)),
    Column('acknowledged_by', String(40)),
    Column('last_review_seq', BigInteger, nullable=False),
    Column('item_count', Integer, nullable=False))
claim_items = Table('purchase_claim_items', schema,
    Column('id', Integer, primary_key=True),
    Column('claim_id', ForeignKey('purchase_claims.id'), nullable=False, index=True),
    Column('evidence_id', ForeignKey('business_evidence.id'), nullable=False),
    Column('position', Integer, nullable=False),
    Column('review_seq', BigInteger, nullable=False),
    UniqueConstraint('claim_id', 'position', name='uq_claim_item_position'),
    UniqueConstraint('claim_id', 'evidence_id', name='uq_claim_item_evidence'))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
