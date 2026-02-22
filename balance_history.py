#!/usr/bin/env python3
"""Show balance history."""
import sqlite3
conn = sqlite3.connect("bot_data.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT timestamp, balance, peak FROM balances ORDER BY id").fetchall()
for r in rows:
    print(f"  {r['timestamp'][:16]}  ${r['balance']:>8.2f}  peak=${r['peak']:.2f}")
