# Tri-City Inator

An automated trading system built around the **Tri-City Inator ENHANCED** TradingView indicator. Claude AI monitors your watchlist every 3 minutes, detects momentum entries, and executes trades automatically through Alpaca — with full position management, scale-out targets, and a trade journal.

**Designed for novice traders who want a professional-grade system without coding experience.**

---

## What This Does

Every 3 minutes during market hours, the system:
1. Scans your watchlist for gap-up stocks with strong volume
2. Checks 8 entry conditions (RSI, EMA, VWAP, relative strength, market regime, and more)
3. Automatically places bracket orders through Alpaca when conditions are met
4. Manages open positions: moves stop to breakeven at T1, locks gains at T2, trails at T3
5. Closes all positions at 3:45 PM CT
6. Logs every trade with P&L and R-multiple

---

## Before You Start — New to This?

This system uses **Claude Code** as its brain. Claude Code is Anthropic's AI assistant that runs in your computer's terminal (the black text window). It's free to get started and you don't need any coding experience to use it — you just talk to it in plain English.

If you've never used a terminal before, follow these steps first:

### Step 0: Install the prerequisites (do this before anything else)

**A. Install Python**
1. Go to [python.org/downloads](https://python.org/downloads)
2. Download the latest version for your operating system (Mac or Windows)
3. Run the installer — check the box that says "Add Python to PATH" if you see it
4. To verify: open Terminal (Mac) or Command Prompt (Windows) and type `python3 --version`. You should see a version number.

**B. Install Claude Code**
1. Go to [claude.ai/code](https://claude.ai/code)
2. Follow the installation instructions for your operating system
3. When installation is complete, type `claude` in your terminal. It should open a chat prompt.
4. You can close it for now with `Ctrl+C`

**C. Install Git**
- Mac: Git is usually pre-installed. Type `git --version` to check. If not found, it will prompt you to install it.
- Windows: Download from [git-scm.com](https://git-scm.com/downloads) and run the installer.

**D. Open TradingView Desktop**
- Download from [tradingview.com](https://tradingview.com) if you haven't already
- A Pro plan or higher is required for real-time data

Once all four are installed, come back here and continue with the installation below.

---

## Prerequisites

| Requirement | Cost | Link |
|-------------|------|------|
| TradingView Pro (minimum) | $14.95/mo | [tradingview.com](https://tradingview.com) |
| Alpaca account (paper trading) | Free | [alpaca.markets](https://alpaca.markets) |
| Claude Code | Free tier available | [claude.ai/code](https://claude.ai/code) |
| Python 3.9+ | Free | [python.org](https://python.org) |
| The Tri-City Inator Pine Script | Included in `pine/` | See below |

> **Start with paper trading.** Alpaca paper trading uses fake money — it's identical to live trading but with no financial risk. Do not switch to live mode until you've validated the system over several sessions.

---

## Installation

### Step 1: Clone the repo

```bash
cd ~
git clone https://github.com/djmusstrd/tri-city-inator.git
cd tri-city-inator
```

### Step 2: Run the installer

```bash
bash install.sh
```

This will:
- Verify Python 3.9+
- Install required packages (`alpaca-py`, `python-dotenv`, `requests`, `pandas`)
- Create the `logs/` and `shared/` directories
- Copy `.env.example` → `.env`
- Add shell aliases to `~/.zshrc`

### Step 3: Add your Alpaca API keys

1. Go to [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys
2. Generate a new key pair
3. Open `~/.env` and fill in:

```bash
nano ~/tri-city-inator/.env
```

```env
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true
```

### Step 4: Add the Tri-City Inator to TradingView

1. Open **TradingView Desktop**
2. Click **Pine Editor** (bottom of screen)
3. Click **Open** → **New blank indicator**
4. Paste the complete Tri-City Inator ENHANCED source code
5. Click **Save** → name it `Tri-City Inator ENHANCED`
6. Click **Add to Chart**

> See `pine/README.md` for full setup details.

### Step 5: Connect TradingView MCP

```bash
cp ~/tri-city-inator/.mcp.json.example ~/tri-city-inator/.mcp.json
```

Edit `.mcp.json` and update the path to match your TradingView MCP installation.

### Step 6: Reload your shell

```bash
source ~/.zshrc
```

### Step 7: Test the installation

```bash
cd ~/tri-city-inator

# Test with a dry run (no orders placed)
python -W ignore scripts/tri_city_execute.py \
  --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \
  --signal ENTER --setup ENTER --dry-run
```

You should see a signal breakdown with entry, stop, and target prices printed. If you see `❌ EXECUTION FAILED`, check your `.env` API keys.

---

## Daily Workflow

### Morning (7:00 AM CT)

**Start Claude:**
```bash
tv                 # Open TradingView Desktop
cd ~/tri-city-inator && claude
```

**Inside Claude, start the premarket scanner cron:**
```
/loop 7am weekdays Run the Tri-City premarket scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash and report the full output including ranked candidate table.
```

The scanner will rank today's gap-up candidates. Add the top 3-5 to your TradingView watchlist.

---

### Market Open (9:30 AM CT)

**Start the signal monitor:**
```
/loop 3m Execute `python -W ignore ~/tri-city-inator/scripts/tri_city_monitor.py` via Bash. If there is output, print it. If there is no output, stay silent.
```

From this point, the system runs automatically. You will see output only when:
- A qualifying ENTER or CONV signal is detected
- A position reaches T1, T2, or T3
- The trailing stop triggers
- EOD close fires at 3:45 PM CT

---

### During the Session

Watch the TradingView chart for visual confirmation. The Tri-City Inator dashboard shows:
- Current signal status
- Trend Strength score
- Smart Money activity
- Market Regime

You don't need to take any action — the system manages positions automatically.

---

### End of Day (3:45 PM CT)

All positions are closed automatically. To review the day:

```bash
tricity          # Today's trade report
tricity-all      # All-time performance
```

---

## Signal Types

| Signal | Meaning | Auto-Execute? |
|--------|---------|--------------|
| 🚀 ENTER | All 8 conditions met — strong setup | Yes |
| 💎 CONV | Moving average convergence breakout | Yes |
| ⚠️ SETUP | Almost ready — watch closely | No (alert only) |
| 🔵 ACCUM | Base forming — early warning | No (alert only) |
| 🟢 RE-ENTRY | Pullback after T1 — second chance | Yes (half size) |
| ⛔ EXIT | Position manager closes position | Auto (by system) |

---

## Entry Conditions (ENTER signal)

All 8 must be true:

| # | Condition | Default |
|---|-----------|---------|
| 1 | Price above EMA20 | Yes |
| 2 | Price above VWAP | Yes |
| 3 | Relative Volume ≥ 1.5x | Yes |
| 4 | RSI between 50 and 75 | Yes |
| 5 | Gap ≥ 3% OR intraday move ≥ 2% | Yes |
| 6 | Within 30% of 52-week high | Yes |
| 7 | Outperforming SPY today | Yes |
| 8 | Market not in bear regime | Yes |

All thresholds are configurable in `.env`.

---

## Position Structure

Every trade uses a **50-25-25 scale-out**:

```
Entry price: $100.00
Stop loss:    $95.00  (-5%)  ← Exits ALL shares if hit

T1: $110.00 (+10%) → Sell 50% of shares → Stop moves to breakeven ($100)
T2: $120.00 (+20%) → Sell 25% of shares → Stop locks at T2 ($120)
T3: $130.00 (+30%) → Trail 25% of shares → Exits if price drops below EMA20+VWAP
                    → Closes at EOD (3:45 PM CT)
```

This means:
- After T1: you cannot lose money on this trade
- After T2: you have locked in T2 gains on the remaining shares
- T3 runs until the trend breaks or end of day

---

## Entry Guards

Seven automatic checks run before any order is placed:

| Guard | Default | Purpose |
|-------|---------|---------|
| Already executed today | — | Prevents duplicate signals |
| Already in position | — | No duplicate symbols |
| Max positions | 3 | Controls exposure |
| Daily loss limit | -$300 | Circuit breaker |
| Time window | 1:00 PM CT | No late entries |
| Market regime (SPY) | -1.5% | No longs in bear market |
| Relative volume | 1.5x | Confirms institutional participation |

To change any default, edit the values in your `.env` file.

---

## Backtesting

Run historical simulations before trading live:

```bash
# Backtest one symbol (2024)
python -W ignore scripts/tri_city_backtest.py \
  --symbols NVDA --start 2024-01-01

# Backtest multiple symbols
python -W ignore scripts/tri_city_backtest.py \
  --symbols NVDA TSLA AAPL META --start 2024-01-01

# Save results to logs/
python -W ignore scripts/tri_city_backtest.py \
  --symbols NVDA --start 2024-01-01 --save
```

The backtest simulates daily gap-up entries and tracks T1/T2/T3 hit rates, win rate, and P&L per trade.

> Note: The backtest uses daily bars for conditions. Live trading uses 5-minute bars for RSI/EMA calculations, so results will differ slightly.

---

## Manual Commands

```bash
# Today's trade report
tricity

# All-time P&L report
tricity-all

# Check open positions
tricity-status

# Run premarket scanner manually
tricity-scan

# Dry-run a signal (no order placed)
python -W ignore scripts/tri_city_execute.py \
  --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \
  --signal ENTER --setup ENTER --dry-run

# EOD close all positions immediately
python -W ignore scripts/tri_city_position_manager.py --eod

# View position status
python -W ignore scripts/tri_city_position_manager.py --status
```

---

## Configuration Reference

All settings are optional. Defaults work for most traders.

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_PAPER` | `true` | Paper or live trading |
| `RISK_PCT` | `2.0` | % of account equity risked per trade |
| `STOP_PCT` | `5.0` | Stop loss % below entry |
| `T1_PCT` | `10.0` | First target % |
| `T2_PCT` | `20.0` | Second target % |
| `T3_PCT` | `30.0` | Third target % (trailing) |
| `MAX_POSITIONS` | `3` | Max concurrent trades |
| `MAX_DAILY_LOSS` | `-300` | Circuit breaker ($) |
| `MIN_RVOL` | `1.5` | Minimum relative volume |
| `MIN_GAP_PCT` | `3.0` | Minimum gap % |
| `RSI_MIN` | `50` | RSI lower bound |
| `RSI_MAX` | `75` | RSI upper bound |
| `NO_ENTRY_HOUR` | `13` | No entries after 1:00 PM CT |
| `SPY_BEAR_THRESHOLD` | `-1.5` | Block entries when SPY < -1.5% |
| `EOD_HOUR` | `15` | EOD close hour (CT) |
| `EOD_MINUTE` | `45` | EOD close minute |

---

## Project Structure

```
tri-city-inator/
├── scripts/
│   ├── tri_city_scanner.py          Morning gap scanner
│   ├── tri_city_monitor.py          Intraday signal loop (strategy-agnostic)
│   ├── tri_city_execute.py          Order execution + 7-guard gate
│   ├── tri_city_position_manager.py T1/T2/T3 management + EOD close
│   ├── tri_city_backtest.py         Historical backtesting
│   └── journal_report.py            P&L performance report
├── strategies/
│   ├── tri_city_strategy.py         Default — Tri-City Inator ENHANCED logic
│   └── custom_template.py           Starting point for your own strategy
├── managers/
│   ├── trade_executor.py            Alpaca order placement
│   └── trade_journal.py             Trade logging + metrics
├── watchlists/
│   └── default-watchlist.txt        Symbols to monitor
├── pine/
│   └── README.md                    Pine Script setup guide
├── shared/                          Runtime data (gitignored)
├── logs/                            Trade logs (gitignored)
├── .env.example                     Config template
├── .mcp.json.example                TradingView MCP template
├── install.sh                       One-shot installer
└── CLAUDE.md                        Claude session instructions
```

---

## Troubleshooting

### No signals appearing
- Check market hours (9:30 AM – 1:00 PM CT for new entries)
- Verify Alpaca keys in `.env`
- Check if SPY is down > 1.5% (bear regime blocks entries)
- Lower `MIN_RVOL` to `1.2` in `.env` if volume is low

### "Alpaca credentials not set" error
- Open `~/tri-city-inator/.env` and add your API keys
- Verify the keys are for the correct environment (paper vs live)

### Orders not filling
- Check your Alpaca paper trading account at [paper-api.alpaca.markets](https://paper-api.alpaca.markets)
- Verify the symbol is tradeable on Alpaca
- Check account has sufficient buying power

### Scanner finds no candidates
- This is normal in slow or down markets
- Expand the watchlist by adding symbols to `watchlists/default-watchlist.txt`
- Lower `MIN_GAP_PCT` to `2.0` in `.env`

### Position manager not moving stops
- Verify the symbol appears in `logs/tri-city-executions.json`
- Check that the execution `success` field is `true`
- Confirm the current price has actually reached T1

---

## Using Your Own Strategy

The execution engine (position sizing, bracket orders, stop management, journaling) is completely independent of the signal logic. You can plug in any indicator or strategy without touching any of the execution code.

### How it works

The monitor fetches real-time data (price, RSI, EMA, RVol, VWAP, 52W high) and passes it to the active strategy. The strategy's only job is to look at that data and return a signal type — `ENTER`, `CONV`, `SETUP`, or `None`. Everything else is handled automatically.

### Steps

**1. Copy the template**

```bash
cp ~/tri-city-inator/strategies/custom_template.py \
   ~/tri-city-inator/strategies/my_strategy.py
```

**2. Edit your signal conditions**

Open `strategies/my_strategy.py` and replace the example logic in `classify_signal()` with your own entry conditions. The template is fully commented with all available data fields.

**3. Activate it in `.env`**

```env
CUSTOM_STRATEGY=true
STRATEGY_FILE=strategies/my_strategy.py
```

**4. Restart the monitor loop**

That's it. The position manager, journaling, and scale-out targets all continue working exactly the same.

### What data is available to your strategy

| Field | Type | Description |
|-------|------|-------------|
| `price` | float | Current price |
| `vwap` | float | VWAP |
| `ema20` | float | EMA(20) from 5-min bars |
| `rsi` | float | RSI(14) from 5-min bars |
| `rvol` | float | Relative volume vs 20-day avg |
| `gap_pct` | float | Gap % from previous close |
| `from_open` | float | % move from today's open |
| `dist_52w` | float | % below 52-week high |
| `above_vwap` | bool | Price above VWAP? |
| `above_ema20` | bool | Price above EMA20? |
| `momentum_ok` | bool | Gap or intraday move meets minimum |
| `near_high` | bool | Within `MAX_52W_DIST` of 52-week high |
| `rvol_ok` | bool | RVol meets minimum |
| `rs_vs_spy` | bool | Outperforming SPY today? |
| `is_conv` | bool | EMA20/SMA50 convergence? |

### Default strategy

If `CUSTOM_STRATEGY` is not set (or set to `false`), the system uses `strategies/tri_city_strategy.py` — the full Tri-City Inator ENHANCED signal logic. No configuration needed.

---

## Risk Warning

Trading stocks involves substantial risk of loss. This software is provided for educational and research purposes only. Past performance does not guarantee future results.

- Always start with **paper trading** until you understand the system
- Never risk money you cannot afford to lose
- This is not financial advice
- You are solely responsible for your trading decisions

---

## License

MIT License — see LICENSE file for details.
