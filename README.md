# Concurrent Banking Transaction & Anomaly Detection System

A small banking ledger written in Python and SQLite. Money transfers run as atomic database transactions, are safe to call from multiple threads, and are screened by a simple anomaly detector.

## Features

- **Atomic transfers:** a transfer either fully completes or changes nothing (SQLite `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`).
- **Exact money handling:** balances are stored as integer paise, not floats.
- **Database-level safety:** `CHECK` constraints stop negative balances and non-positive amounts; foreign keys link users, accounts and transactions.
- **Thread safety:** transfers are serialised with a lock, and the sender is debited with a single conditional `UPDATE ... WHERE balance_paise >= amount`, so an account can never be overdrawn.
- **Anomaly detection:** a sliding-window rule engine flags a sender with too many transfers in 60 seconds (velocity) or any single transfer of Rs 5,000 or more (high value). Flagged attempts are stored in the ledger with a reason and move no money.
- **Indexes** on the lookups the engine uses (checked with `EXPLAIN QUERY PLAN` in `demo.py`).

## Project structure

| File | Purpose |
|---|---|
| `schema.sql` | Table and index definitions, loaded by the engine on start-up |
| `banking_engine.py` | `BankingEngine`, `AnomalyDetector` and the custom exceptions |
| `test_banking.py` | 13 `unittest` tests, including concurrency stress tests |
| `demo.py` | Prints each step of a sample run |

## Requirements

Python 3.8+ (standard library only, no packages to install).

## Run it

```bash
python -m unittest -v    # run the tests
python demo.py           # watch a sample run
```

## How a transfer works

1. Validate the amount (must be positive) and that sender and receiver differ.
2. Take the transfer lock and open a `BEGIN IMMEDIATE` transaction.
3. Confirm both accounts exist.
4. Ask the anomaly detector. If flagged, write a `FLAGGED` ledger row and raise `FraudDetectedError`.
5. Otherwise debit the sender (only if funds are sufficient), credit the receiver and write a `COMPLETED` ledger row.
6. Commit. Any error rolls everything back.
7. Only after a successful commit, record the transfer in the detector's history.

## Known limitations

- Transfers are serialised by one global lock. This is simple and correct but limits throughput; per-account locks acquired in a fixed order would scale better.
- The anomaly detector keeps its history in memory, so it resets when the program restarts.
- The amount limits and detector settings are constructor arguments, not a config file.
