"""Find all TRADE log entries and cycle starts from 09:30 to 10:40."""
with open("bot.log", "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        if "2026-02-12 09:" in line or "2026-02-12 10:" in line:
            ts = line[11:16]
            if ts >= "09:30" and ts <= "10:40":
                if any(kw in line for kw in ["TRADE:", "Scan cycle start", "Bankroll:",
                    "Cycle complete", "Starting Kalshi", "Starting balance",
                    "Candidates:", "HALTED"]):
                    print(line.rstrip())
