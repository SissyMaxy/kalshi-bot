"""Check bot log entries around the SPX duplicate trades."""
with open("bot.log", "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        # Show log entries between 10:00 and 10:35 UTC on Feb 12
        if "2026-02-12 10:" in line:
            ts = line[11:16]  # extract HH:MM
            if "10:00" <= ts <= "10:35":
                print(line.rstrip())
