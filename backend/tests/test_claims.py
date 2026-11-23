"""Regression checks for the purchase-claim batch semantics.

Run against a throwaway database (these tests drop and recreate every table):

    CLAIM_TEST=1 DB_HOST=127.0.0.1 DB_NAME=workflow DB_USER=app DB_PASSWORD=... \\
        python -m unittest tests.test_claims -v

Set ``CLAIM_LEASE_SECONDS`` small (for example ``2``) so lease expiry is quick.
"""

import os
import threading
import unittest

from sqlalchemy import func, select

from src.config.settings import engine
from src.constants.workflow import ClaimStatus
from src.models.tables import (cases, claim_items, counters, evidence, review_ledger,
                               runs, schema)
from src.models.tables import claims as claims_t
from src.services import claims, workflow
from src.utils.time import utc_time

ENABLED = os.getenv('CLAIM_TEST') == '1'
LEASE = int(os.getenv('CLAIM_LEASE_SECONDS', '2'))


def reset():
    schema.drop_all(engine)
    schema.create_all(engine)
    with engine.begin() as conn:
        conn.execute(counters.insert(), [{'name': 'review_seq', 'value': 0},
                                         {'name': 'claim_lock', 'value': 0}])
        return conn.execute(cases.insert().values(
            slug='t', title='t', payload={}, author='op', created_at=utc_time())).inserted_primary_key[0]


def make_evidence(case_id, count):
    out = []
    with engine.begin() as conn:
        for _ in range(count):
            run_id = conn.execute(runs.insert().values(
                case_id=case_id, status='complete', owner='op',
                created_at=utc_time())).inserted_primary_key[0]
            out.append(conn.execute(evidence.insert().values(
                run_id=run_id, report={}, digest='d')).inserted_primary_key[0])
    return out


def run_concurrently(*functions):
    barrier = threading.Barrier(len(functions))
    results = []

    def invoke(function):
        def inner():
            barrier.wait()
            try:
                results.append(('ok', function()))
            except Exception as exc:  # noqa: BLE001 - recorded for assertions
                results.append(('error', exc))
        return inner

    threads = [threading.Thread(target=invoke(fn)) for fn in functions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@unittest.skipUnless(ENABLED, 'set CLAIM_TEST=1 and point the app at a throwaway database')
class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.case_id = reset()

    def review(self, ids):
        for identity in ids:
            workflow.review(identity, 'ok', 'reviewer')

    def test_dispatch_order_follows_commit_order(self):
        first, second = make_evidence(self.case_id, 2)
        # Reviews run from two threads; the ledger must be a contiguous ordering.
        run_concurrently(lambda: workflow.review(first, 'ok', 'reviewer'),
                         lambda: workflow.review(second, 'ok', 'reviewer'))
        with engine.connect() as conn:
            rows = conn.execute(select(review_ledger.c.evidence_id, review_ledger.c.seq)
                                .order_by(review_ledger.c.seq)).all()
        self.assertEqual([row.seq for row in rows], [1, 2])
        batch = claims.claim('T-A')
        # Dispatch order matches ledger order, not insertion-into-evidence order.
        self.assertEqual([item['evidence_id'] for item in batch['items']],
                         [row.evidence_id for row in rows])

    def test_claim_waits_for_in_flight_review(self):
        """A claim that starts while a review is committing still includes it,
        because both take the sequence lock and the review holds it to commit."""
        identity, = make_evidence(self.case_id, 1)
        allocated = threading.Event()
        release = threading.Event()

        def hold_review():
            with engine.begin() as conn:
                from src.models.tables import evidence as evidence_t
                conn.execute(evidence_t.update().where(evidence_t.c.id == identity)
                             .values(review_note='ok', reviewer='reviewer', reviewed_at=utc_time()))
                seq = claims.next_review_seq(conn)
                conn.execute(review_ledger.insert().values(evidence_id=identity, seq=seq))
                allocated.set()
                release.wait(5)

        holder = threading.Thread(target=hold_review)
        holder.start()
        self.assertTrue(allocated.wait(5))

        claimed = {}

        def take():
            claimed['batch'] = claims.claim('T-A')

        claimer = threading.Thread(target=take)
        claimer.start()
        claimer.join(0.3)
        self.assertTrue(claimer.is_alive(), 'claim should wait for the in-flight review')
        release.set()
        holder.join()
        claimer.join(5)
        self.assertFalse(claimer.is_alive())
        self.assertEqual([item['evidence_id'] for item in claimed['batch']['items']], [identity])

    def test_uncommitted_review_is_not_dispatched(self):
        committed, = make_evidence(self.case_id, 1)
        ghost, = make_evidence(self.case_id, 1)
        workflow.review(committed, 'ok', 'reviewer')
        # A review that stamped the evidence row but whose transaction rolled
        # back leaves no ledger row and must not be dispatched.
        connection = engine.connect()
        transaction = connection.begin()
        from src.models.tables import evidence as evidence_t
        connection.execute(evidence_t.update().where(evidence_t.c.id == ghost)
                           .values(review_note='ok', reviewer='reviewer', reviewed_at=utc_time()))
        connection.execute(review_ledger.insert().values(evidence_id=ghost, seq=claims.next_review_seq(connection)))
        transaction.rollback()
        connection.close()
        batch = claims.claim('T-A')
        self.assertEqual([item['evidence_id'] for item in batch['items']], [committed])

    def test_failed_review_is_not_dispatched(self):
        identity, = make_evidence(self.case_id, 1)
        self.review([identity])
        with self.assertRaises(Exception) as raised:
            workflow.review(identity, 'again', 'reviewer')
        self.assertEqual(getattr(raised.exception, 'status_code', None), 409)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(select(func.count()).select_from(review_ledger)).scalar_one(), 1)

    def test_retry_returns_same_batch(self):
        first, second = make_evidence(self.case_id, 2)
        self.review([first, second])
        original = claims.claim('T-A')
        retry = claims.claim('T-A', original['claim_token'])
        self.assertEqual(retry['id'], original['id'])
        self.assertEqual(retry['number'], original['number'])
        self.assertEqual(retry['claim_token'], original['claim_token'])
        self.assertEqual([item['evidence_id'] for item in retry['items']],
                         [item['evidence_id'] for item in original['items']])

    def test_new_reviews_wait_for_acknowledgement(self):
        first, second = make_evidence(self.case_id, 2)
        self.review([first])
        batch = claims.claim('T-A')
        self.review([second])
        self.assertEqual([item['evidence_id'] for item in batch['items']], [first])
        with self.assertRaises(Exception) as raised:
            claims.claim('T-B')
        self.assertEqual(getattr(raised.exception, 'status_code', None), 409)
        claims.acknowledge(batch['id'], batch['claim_token'], 'buyer')
        following = claims.claim('T-A')
        self.assertEqual([item['evidence_id'] for item in following['items']], [second])

    def test_takeover_after_lease_and_stale_ack(self):
        identity, = make_evidence(self.case_id, 1)
        self.review([identity])
        original = claims.claim('T-A')
        with engine.begin() as conn:
            conn.execute(claims_t.update().where(claims_t.c.id == original['id'])
                         .values(lease_expires_at=utc_time(-1)))
        taken = claims.claim('T-B')
        self.assertEqual(taken['number'], original['number'])
        self.assertEqual([item['evidence_id'] for item in taken['items']],
                         [item['evidence_id'] for item in original['items']])
        self.assertNotEqual(taken['claim_token'], original['claim_token'])
        with self.assertRaises(Exception) as raised:
            claims.acknowledge(taken['id'], original['claim_token'])
        self.assertEqual(getattr(raised.exception, 'status_code', None), 409)
        acknowledged = claims.acknowledge(taken['id'], taken['claim_token'], 'buyer')
        self.assertEqual(acknowledged['status'], ClaimStatus.ACKNOWLEDGED)

    def test_only_one_open_batch_under_concurrent_claim(self):
        first, second = make_evidence(self.case_id, 2)
        self.review([first, second])
        results = run_concurrently(lambda: claims.claim('T-A'), lambda: claims.claim('T-B'))
        winners = [value for status, value in results if status == 'ok' and value is not None]
        self.assertEqual(len(winners), 1)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(select(func.count()).select_from(claims_t)).scalar_one(), 1)
            self.assertEqual(conn.execute(select(func.count()).select_from(claim_items)).scalar_one(), 2)

    def test_replay_after_lost_ack_returns_same_batch(self):
        first, second = make_evidence(self.case_id, 2)
        self.review([first])
        batch = claims.claim('T-A')
        claims.acknowledge(batch['id'], batch['claim_token'], 'buyer')
        self.review([second])
        replay = claims.claim('T-A', batch['claim_token'])
        self.assertEqual(replay['id'], batch['id'])
        self.assertEqual(replay['number'], batch['number'])
        self.assertEqual(replay['status'], 'acknowledged')
        self.assertEqual([item['evidence_id'] for item in replay['items']], [first])
        following = claims.claim('T-A')
        self.assertEqual([item['evidence_id'] for item in following['items']], [second])

    def test_acknowledged_batch_does_not_reopen(self):
        identity, = make_evidence(self.case_id, 1)
        self.review([identity])
        batch = claims.claim('T-A')
        claims.acknowledge(batch['id'], batch['claim_token'], 'buyer')
        self.assertIsNone(claims.claim('T-A'))
        with engine.connect() as conn:
            row = conn.execute(select(claims_t.c.open_slot).where(claims_t.c.id == batch['id'])).scalar_one()
        self.assertIsNone(row)


if __name__ == '__main__':
    unittest.main()
