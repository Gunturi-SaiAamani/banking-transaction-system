-- schema.sql : the database structure. banking_engine.py runs this file on start-up.
-- Every statement uses IF NOT EXISTS, so running it again on an existing database is safe.

-- Make SQLite enforce FOREIGN KEY rules (it ignores them by default).
-- This setting is per-connection, so banking_engine.py also sets it on every connection.
PRAGMA foreign_keys = ON;
 
-- One row per customer.
CREATE TABLE IF NOT EXISTS users (
    user_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT    NOT NULL,
    email     TEXT    NOT NULL UNIQUE
);

-- One row per bank account. Money is stored as whole PAISE (integers), never floats,
-- because floats cannot represent amounts like 0.1 exactly.
-- CHECK means the database itself refuses a negative balance, even if the Python code has a bug.
CREATE TABLE IF NOT EXISTS accounts (
    account_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(user_id),
    balance_paise INTEGER NOT NULL CHECK (balance_paise >= 0)
);

-- The ledger: every attempted transfer is recorded, either COMPLETED or FLAGGED.
CREATE TABLE IF NOT EXISTS transactions (
    txn_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    receiver_account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    amount_paise        INTEGER NOT NULL CHECK (amount_paise > 0),
    status              TEXT    NOT NULL CHECK (status IN ('COMPLETED', 'FLAGGED')),
    flag_reason         TEXT,
    created_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Indexes speed up the lookups the engine performs (finding a user's accounts and
-- an account's history). Primary keys are indexed automatically; these cover the rest.
CREATE INDEX IF NOT EXISTS idx_accounts_user         ON accounts(user_id);
CREATE INDEX IF NOT EXISTS idx_txn_sender_time       ON transactions(sender_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_txn_receiver_time     ON transactions(receiver_account_id, created_at);
