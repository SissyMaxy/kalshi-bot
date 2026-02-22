# Kalshi Trading Bot — Claude Code Setup

## What This Is

An autonomous trading bot for Kalshi prediction markets. It scans weather, economics, and financial markets, calculates fair values using NWS forecast data, and places trades when it finds edges ≥8%.

**Bankroll:** $100 starting  
**Strategy:** Weather markets primary, quarter-Kelly sizing, 6% max per position  

---

## Claude Code Instructions

Open a terminal and paste this entire block into Claude Code:

```
I need you to set up and run a Kalshi prediction market trading bot. Here's what to do:

1. Create a directory ~/kalshi-bot and set up a Python virtual environment:
   mkdir -p ~/kalshi-bot && cd ~/kalshi-bot
   python3 -m venv venv && source venv/bin/activate

2. Install dependencies:
   pip install requests python-dotenv cryptography anthropic

3. I'm going to give you the bot source files. There are 7 Python files:
   - bot.py (main entry point)
   - kalshi_client.py (API auth with RSA-PSS signing)
   - scanner.py (market discovery and filtering)
   - fair_value.py (NWS forecast → probability estimates)
   - position_sizer.py (quarter-Kelly sizing)
   - risk_manager.py (circuit breakers, exposure limits)
   - database.py (SQLite trade logging)

4. Create a .env file with:
   KALSHI_API_KEY_ID=<my key id>
   KALSHI_PRIVATE_KEY_PATH=./kalshi-private-key.pem

5. I'll provide my private key PEM file separately — save it as kalshi-private-key.pem

6. Run a scan-only test first:
   python bot.py --scan-only --once

7. If that looks good, run live:
   python bot.py --live --once

Then for continuous operation:
   python bot.py --live
```

Then paste all the source files from the downloaded zip.

---

## Quick Start (Manual)

If you'd rather set it up yourself without Claude Code:

```bash
# 1. Create project
mkdir ~/kalshi-bot && cd ~/kalshi-bot
python3 -m venv venv
source venv/bin/activate

# 2. Install deps
pip install requests python-dotenv cryptography anthropic

# 3. Copy all .py files into ~/kalshi-bot/

# 4. Set up credentials
cp .env.example .env
# Edit .env — put your API key ID
# Put your private key as kalshi-private-key.pem in same folder

# 5. Test (scan only, no trades)
python bot.py --scan-only --once

# 6. Go live (one cycle)
python bot.py --live --once

# 7. Continuous mode (scans every 10 min)
python bot.py --live
```

---

## Commands

| Command | What it does |
|---------|-------------|
| `python bot.py --scan-only --once` | Show edges without trading, run once |
| `python bot.py --scan-only` | Continuous scanning, no trades |
| `python bot.py --once` | One cycle on demo API |
| `python bot.py --live --once` | One real trade cycle |
| `python bot.py --live` | Continuous live trading (10 min intervals) |

---

## Your Credentials

- **API Key ID:** `baa7079f-22a4-4be4-ba56-2d4f5b8cc9ac`
- **Private Key:** Save the PEM file you generated as `kalshi-private-key.pem`

---

## What the Bot Does Each Cycle

1. **Checks bankroll** and circuit breakers (halt if >10% daily loss)
2. **Scans ~40 market series** (weather, econ, crypto/financial)
3. **Filters** for liquidity (volume >100, spread <15¢, not near 0 or 1)
4. **Fetches NWS forecasts** for weather markets
5. **Calculates fair value** using normal distribution model
6. **Finds edges** ≥8% between fair value and market price
7. **Sizes positions** with quarter-Kelly (max 6% of bankroll per trade)
8. **Places limit orders** on Kalshi
9. **Logs everything** to SQLite database and bot.log

---

## Risk Controls

- **Max 6%** of bankroll per single position
- **Max 20%** exposure per category (weather, econ, financial)
- **Max 50%** total exposure
- **Quarter-Kelly** sizing (conservative)
- **10% daily loss limit** → halts trading
- **Survival mode** below $10 → raises edge threshold to 15%
- **5-second countdown** before live mode starts (Ctrl+C to cancel)

---

## Files

```
kalshi-bot/
├── bot.py              # Main loop
├── kalshi_client.py    # Kalshi API + RSA-PSS auth
├── scanner.py          # Market discovery
├── fair_value.py       # NWS → probability model
├── position_sizer.py   # Kelly criterion sizing
├── risk_manager.py     # Circuit breakers
├── database.py         # SQLite logging
├── requirements.txt    # Dependencies
├── .env.example        # Credentials template
├── .env                # Your actual credentials (create this)
└── kalshi-private-key.pem  # Your private key (create this)
```
