"""Concurrent banking ledger with ACID transfers and an anomaly (fraud) detector.

Files that work together:
    schema.sql       -> creates the tables (read by BankingEngine on start-up)
    banking_engine.py -> this file: the logic
    test_banking.py  -> automated tests that exercise this file
    demo.py          -> a small script that prints what happens step by step
""" 
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class InsufficientFundsError(Exception):
    """The sender does not have enough money."""


class FraudDetectedError(Exception):
    """The anomaly detector blocked the transfer."""


class AccountNotFoundError(Exception):
    """An account id does not exist."""


def rupees_to_paise(amount, allow_zero=False):
    """Convert rupees (float/int) to whole paise and validate it.

    All money inside the engine is an integer number of paise, so 10.50 -> 1050.
    """
    paise = round(amount * 100)
    if paise < 0 or (paise == 0 and not allow_zero):
        raise ValueError("Amount must be greater than zero.")
    return paise


class AnomalyDetector:
    """In-memory sliding-window rule engine.

    Rule 1 (velocity):   more than `max_velocity` completed transfers by one sender
                         inside the last `time_window_seconds` -> flagged.
    Rule 2 (high value): a single transfer >= `high_value_threshold_rupees` -> flagged.

    check()  only LOOKS at the history.
    record() adds a completed transfer to the history.
    They are separate so that a transfer which fails (e.g. insufficient funds)
    does not count towards the velocity limit.
    """

    def __init__(self, time_window_seconds=60, max_velocity=3,
                 high_value_threshold_rupees=5000.0, clock=time.monotonic):
        self.window = time_window_seconds
        self.max_velocity = max_velocity
        self.high_value_paise = round(high_value_threshold_rupees * 100)
        self._clock = clock                  # injectable so tests can fake time
        self._history = {}                   # {account_id: deque of timestamps}
        self._lock = threading.Lock()

    def check(self, sender_id, amount_paise):
        """Return (is_suspicious, reason)."""
        now = self._clock()
        with self._lock:
            history = self._history.setdefault(sender_id, deque())
            # Drop timestamps that have slid out of the window.
            while history and now - history[0] > self.window:
                history.popleft()

            if len(history) >= self.max_velocity:
                return True, (f"High velocity: more than {self.max_velocity} "
                              f"transfers in {self.window}s.")
            if amount_paise >= self.high_value_paise:
                return True, (f"High value: Rs {amount_paise / 100:,.2f} is at or above "
                              f"the limit of Rs {self.high_value_paise / 100:,.2f}.")
            return False, ""

    def record(self, sender_id):
        """Remember that `sender_id` just completed a transfer."""
        with self._lock:
            self._history.setdefault(sender_id, deque()).append(self._clock())


class BankingEngine:
    """Thread-safe ledger. Every public method opens its own short-lived connection."""

    def __init__(self, db_path="bank.db", detector=None):
        # NOTE: use a real file path. ":memory:" gives every new connection its OWN empty
        # database, so tables created in one connection would be invisible to the next.
        self.db_path = str(db_path)
        self.detector = detector or AnomalyDetector()
        self._transfer_lock = threading.Lock()
        self._init_db()

    # ---------- infrastructure ----------

    def _connect(self):
        # isolation_level=None -> Python does not start transactions behind our back;
        # we issue BEGIN / COMMIT / ROLLBACK ourselves so it is obvious what happens.
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")   # readers do not block the writer
        return conn

    @contextmanager
    def _transaction(self):
        """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if anything raises.

        BEGIN IMMEDIATE takes SQLite's write lock up front, *before* we read any balance,
        so no other connection (even in another process) can change data under us.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def _init_db(self):
        script = SCHEMA_PATH.read_text()
        conn = self._connect()
        try:
            conn.executescript(script)
        finally:
            conn.close()

    # ---------- public API ----------

    def create_account(self, name, email, initial_deposit):
        """Create a user and their account together. Returns the new account_id."""
        deposit = rupees_to_paise(initial_deposit, allow_zero=True)
        with self._transaction() as conn:
            user_id = conn.execute(
                "INSERT INTO users (full_name, email) VALUES (?, ?)", (name, email)
            ).lastrowid
            account_id = conn.execute(
                "INSERT INTO accounts (user_id, balance_paise) VALUES (?, ?)",
                (user_id, deposit),
            ).lastrowid
        return account_id

    def get_balance(self, account_id):
        """Balance in rupees."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT balance_paise FROM accounts WHERE account_id = ?", (account_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise AccountNotFoundError(f"Account {account_id} does not exist.")
        return row[0] / 100

    def get_transactions(self, account_id):
        """Ledger rows (oldest first) where the account is sender or receiver."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT txn_id, sender_account_id, receiver_account_id, amount_paise / 100.0, "
                "status, flag_reason FROM transactions "
                "WHERE sender_account_id = ? OR receiver_account_id = ? ORDER BY txn_id",
                (account_id, account_id),
            ).fetchall()
        finally:
            conn.close()

    def transfer_funds(self, sender_id, receiver_id, amount):
        """Move `amount` rupees from sender to receiver, atomically.

        Raises ValueError, AccountNotFoundError, FraudDetectedError or InsufficientFundsError.
        On ANY error no balance changes (a FLAGGED audit row is the only thing written).
        """
        # Step 1: cheap validation, before touching the database.
        amount_paise = rupees_to_paise(amount)
        if sender_id == receiver_id:
            raise ValueError("Sender and receiver must be different accounts.")

        flag_reason = ""
        # Step 2: only one transfer at a time inside this process. This also makes
        # "check the fraud rules -> do the transfer -> record it" one indivisible unit.
        with self._transfer_lock:
            # Step 3: open a database transaction (all-or-nothing).
            with self._transaction() as conn:
                # 3a. Both accounts must exist.
                for account_id in (sender_id, receiver_id):
                    exists = conn.execute(
                        "SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)
                    ).fetchone()
                    if exists is None:
                        raise AccountNotFoundError(f"Account {account_id} does not exist.")

                # 3b. Ask the anomaly detector.
                suspicious, flag_reason = self.detector.check(sender_id, amount_paise)

                if suspicious:
                    # 3c. Blocked: write an audit row, move no money.
                    conn.execute(
                        "INSERT INTO transactions (sender_account_id, receiver_account_id, "
                        "amount_paise, status, flag_reason) VALUES (?, ?, ?, 'FLAGGED', ?)",
                        (sender_id, receiver_id, amount_paise, flag_reason),
                    )
                else:
                    # 3d. Debit the sender ONLY IF they have enough, in a single statement.
                    #     (No separate "read balance then update" gap for a race to slip into.)
                    debit = conn.execute(
                        "UPDATE accounts SET balance_paise = balance_paise - ? "
                        "WHERE account_id = ? AND balance_paise >= ?",
                        (amount_paise, sender_id, amount_paise),
                    )
                    if debit.rowcount != 1:
                        raise InsufficientFundsError("Sender has insufficient funds.")

                    # 3e. Credit the receiver.
                    credit = conn.execute(
                        "UPDATE accounts SET balance_paise = balance_paise + ? "
                        "WHERE account_id = ?",
                        (amount_paise, receiver_id),
                    )
                    if credit.rowcount != 1:      # should be impossible after 3a; be safe
                        raise RuntimeError("Credit failed; transfer rolled back.")

                    # 3f. Write the ledger entry.
                    conn.execute(
                        "INSERT INTO transactions (sender_account_id, receiver_account_id, "
                        "amount_paise, status) VALUES (?, ?, ?, 'COMPLETED')",
                        (sender_id, receiver_id, amount_paise),
                    )
            # Leaving the `with` block above COMMITs (or ROLLBACKs if something was raised).

            # Step 4: only after a successful commit do we react.
            if flag_reason:
                raise FraudDetectedError(f"Transaction blocked: {flag_reason}")
            self.detector.record(sender_id)
        return True
