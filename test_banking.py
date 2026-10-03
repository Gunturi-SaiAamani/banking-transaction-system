import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from banking_engine import (
    AccountNotFoundError, AnomalyDetector, BankingEngine,
    FraudDetectedError, InsufficientFundsError,
)
 

class BankingTestCase(unittest.TestCase):
    """Each test gets a brand-new database file in a temporary folder."""

    def make_engine(self, detector=None):
        return BankingEngine(self.db_path, detector=detector)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_bank.db"
        self.engine = self.make_engine()
        self.alice = self.engine.create_account("Alice", "alice@sastra.ac.in", 1000.0)
        self.bob = self.engine.create_account("Bob", "bob@sastra.ac.in", 500.0)

    def tearDown(self):
        self._tmp.cleanup()

    def statuses(self, account_id):
        return [row[4] for row in self.engine.get_transactions(account_id)]


class TestTransfers(BankingTestCase):
    def test_successful_transfer(self):
        self.engine.transfer_funds(self.alice, self.bob, 200.0)
        self.assertEqual(self.engine.get_balance(self.alice), 800.0)
        self.assertEqual(self.engine.get_balance(self.bob), 700.0)
        self.assertEqual(self.statuses(self.alice), ["COMPLETED"])

    def test_insufficient_funds_changes_nothing(self):
        with self.assertRaises(InsufficientFundsError):
            self.engine.transfer_funds(self.alice, self.bob, 1500.0)
        self.assertEqual(self.engine.get_balance(self.alice), 1000.0)
        self.assertEqual(self.engine.get_balance(self.bob), 500.0)
        self.assertEqual(self.statuses(self.alice), [])

    def test_amount_must_be_positive(self):
        for bad in (-100.0, 0.0):
            with self.assertRaises(ValueError):
                self.engine.transfer_funds(self.alice, self.bob, bad)
        self.assertEqual(self.engine.get_balance(self.alice), 1000.0)
        self.assertEqual(self.engine.get_balance(self.bob), 500.0)

    def test_cannot_transfer_to_self(self):
        with self.assertRaises(ValueError):
            self.engine.transfer_funds(self.alice, self.alice, 10.0)

    def test_unknown_receiver_loses_no_money(self):
        with self.assertRaises(AccountNotFoundError):
            self.engine.transfer_funds(self.alice, 999, 10.0)
        self.assertEqual(self.engine.get_balance(self.alice), 1000.0)

    def test_paise_are_exact(self):
        # 0.1 + 0.2 is the classic float trap; integer paise stay exact.
        self.engine.transfer_funds(self.alice, self.bob, 0.10)
        self.engine.transfer_funds(self.alice, self.bob, 0.20)
        self.assertEqual(self.engine.get_balance(self.bob), 500.30)


class TestAnomalyRules(BankingTestCase):
    def test_high_value_is_flagged_and_logged(self):
        rich = self.engine.create_account("Rich", "rich@sastra.ac.in", 20000.0)
        with self.assertRaises(FraudDetectedError):
            self.engine.transfer_funds(rich, self.bob, 6000.0)
        self.assertEqual(self.engine.get_balance(rich), 20000.0)      # no money moved
        self.assertEqual(self.statuses(rich), ["FLAGGED"])            # but it is auditable

    def test_velocity_limit_blocks_fourth_transfer(self):
        for _ in range(3):
            self.engine.transfer_funds(self.alice, self.bob, 10.0)
        with self.assertRaises(FraudDetectedError):
            self.engine.transfer_funds(self.alice, self.bob, 10.0)
        self.assertEqual(self.engine.get_balance(self.alice), 970.0)

    def test_velocity_window_slides(self):
        now = [0.0]
        detector = AnomalyDetector(time_window_seconds=60, max_velocity=3,
                                   clock=lambda: now[0])
        engine = self.make_engine(detector)
        for _ in range(3):
            engine.transfer_funds(self.alice, self.bob, 10.0)
        with self.assertRaises(FraudDetectedError):
            engine.transfer_funds(self.alice, self.bob, 10.0)
        now[0] = 61.0                       # a minute later the old transfers expire
        engine.transfer_funds(self.alice, self.bob, 10.0)

    def test_failed_transfer_does_not_count_towards_velocity(self):
        for _ in range(5):
            with self.assertRaises(InsufficientFundsError):
                self.engine.transfer_funds(self.alice, self.bob, 4000.0)
        self.engine.transfer_funds(self.alice, self.bob, 10.0)       # still allowed


class TestConcurrency(BankingTestCase):
    """The detector's velocity rule is raised so these tests measure *locking*, not fraud rules."""

    def setUp(self):
        super().setUp()
        self.engine = self.make_engine(AnomalyDetector(max_velocity=10_000))

    def run_parallel(self, tasks, workers=8):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(task) for task in tasks]
            outcomes = []
            for f in futures:
                try:
                    f.result()
                    outcomes.append("ok")
                except InsufficientFundsError:
                    outcomes.append("insufficient")
            return outcomes

    def test_all_transfers_apply_exactly_once(self):
        tasks = [lambda: self.engine.transfer_funds(self.alice, self.bob, 10.0)] * 50
        outcomes = self.run_parallel(tasks)
        self.assertEqual(outcomes.count("ok"), 50)
        self.assertEqual(self.engine.get_balance(self.alice), 500.0)   # 1000 - 50*10
        self.assertEqual(self.engine.get_balance(self.bob), 1000.0)    # 500 + 50*10

    def test_no_overdraft_under_contention(self):
        poor = self.engine.create_account("Poor", "poor@sastra.ac.in", 100.0)
        tasks = [lambda: self.engine.transfer_funds(poor, self.bob, 10.0)] * 30
        outcomes = self.run_parallel(tasks)
        self.assertEqual(outcomes.count("ok"), 10)              # exactly 100/10 succeed
        self.assertEqual(outcomes.count("insufficient"), 20)
        self.assertEqual(self.engine.get_balance(poor), 0.0)    # never negative
        self.assertEqual(self.engine.get_balance(self.bob), 600.0)

    def test_opposite_directions_conserve_money(self):
        a_to_b = lambda: self.engine.transfer_funds(self.alice, self.bob, 10.0)
        b_to_a = lambda: self.engine.transfer_funds(self.bob, self.alice, 10.0)
        outcomes = self.run_parallel([a_to_b, b_to_a] * 20)
        self.assertEqual(outcomes.count("ok"), 40)
        self.assertEqual(self.engine.get_balance(self.alice), 1000.0)
        self.assertEqual(self.engine.get_balance(self.bob), 500.0)


if __name__ == "__main__":
    unittest.main()
