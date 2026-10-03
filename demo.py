"""Run `python demo.py` to watch the engine work. It prints what happens at each step."""
import os
import sqlite3

from banking_engine import (
    AnomalyDetector, BankingEngine, FraudDetectedError, InsufficientFundsError,
) 

DB = "demo_bank.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)

engine = BankingEngine(DB, AnomalyDetector(max_velocity=3, high_value_threshold_rupees=5000))


def show(label):
    print(f"  balances -> Alice: Rs {engine.get_balance(alice):,.2f} | "
          f"Bob: Rs {engine.get_balance(bob):,.2f}   ({label})")


def attempt(description, sender, receiver, amount):
    print(f"\n{description}")
    try:
        engine.transfer_funds(sender, receiver, amount)
        print("  result   -> COMPLETED")
    except (FraudDetectedError, InsufficientFundsError, ValueError) as err:
        print(f"  result   -> REJECTED ({type(err).__name__}: {err})")
    show("after")


print("1) Schema loaded from schema.sql; creating two accounts")
alice = engine.create_account("Alice", "alice@sastra.ac.in", 10000.0)
bob = engine.create_account("Bob", "bob@sastra.ac.in", 500.0)
show("start")

attempt("2) Normal transfer: Alice -> Bob Rs 200", alice, bob, 200)
attempt("3) Bob tries to send Rs 4,000 but only has Rs 700 (under the fraud limit)", bob, alice, 4000)
attempt("4) High-value rule: Rs 6,000 (limit is Rs 5,000)", alice, bob, 6000)
attempt("5) Invalid amount: -Rs 100", alice, bob, -100)
attempt("6) Velocity rule: transfers #2 and #3 pass...", alice, bob, 10)
attempt("   ...", alice, bob, 10)
attempt("7) ...the 4th within 60 seconds is blocked", alice, bob, 10)

print("\n8) The ledger (every attempt is recorded, including blocked ones):")
for txn_id, sender, receiver, amount, status, reason in engine.get_transactions(alice):
    print(f"  #{txn_id} {sender}->{receiver} Rs {amount:>8,.2f}  {status:<9} {reason or ''}")

print("\n9) Proof the index is used (EXPLAIN QUERY PLAN):")
conn = sqlite3.connect(DB)
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT * FROM transactions WHERE sender_account_id = ?", (alice,)
).fetchall()
print("  ", plan[0][-1])
conn.close()

for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)
