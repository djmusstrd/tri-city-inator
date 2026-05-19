#!/usr/bin/env python3
"""Generate Tri-City Inator printable cheatsheet PDF."""
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

OUT = "/Users/georgestarks/tri-city-inator/TRI_CITY_CHEATSHEET.pdf"

doc = SimpleDocTemplate(OUT, pagesize=letter,
    leftMargin=0.65*inch, rightMargin=0.65*inch,
    topMargin=0.6*inch, bottomMargin=0.6*inch)

BG      = colors.HexColor("#0d1117")
GREEN   = colors.HexColor("#00c853")
YELLOW  = colors.HexColor("#ffd600")
BLUE    = colors.HexColor("#0091ea")
ORANGE  = colors.HexColor("#ff6f00")
WHITE   = colors.white
GREY    = colors.HexColor("#888888")
DKGREY  = colors.HexColor("#1a1a2e")
HDR_BG  = colors.HexColor("#1a3a1a")
ROW_BG  = colors.HexColor("#111820")

styles = getSampleStyleSheet()

def style(name, **kw):
    s = ParagraphStyle(name, **kw)
    return s

TITLE   = style("title",   fontSize=20, fontName="Helvetica-Bold", textColor=GREEN,  alignment=TA_CENTER, spaceAfter=2)
SUBHEAD = style("subhead", fontSize=9,  fontName="Helvetica",      textColor=GREY,   alignment=TA_CENTER, spaceAfter=10)
H1      = style("h1",      fontSize=11, fontName="Helvetica-Bold", textColor=GREEN,  spaceBefore=10, spaceAfter=4)
MONO    = style("mono",    fontSize=8,  fontName="Courier",        textColor=WHITE,  backColor=DKGREY, leading=13, leftIndent=8, rightIndent=8, spaceBefore=2, spaceAfter=2)
BODY    = style("body",    fontSize=8,  fontName="Helvetica",      textColor=WHITE,  leading=12)

def h1(txt):     return Paragraph(txt, H1)
def body(txt):   return Paragraph(txt, BODY)
def mono(txt):   return Paragraph(txt.replace(" ", "&nbsp;"), MONO)
def sp(n=4):     return Spacer(1, n)
def rule():      return HRFlowable(width="100%", thickness=0.5, color=HDR_BG, spaceAfter=4, spaceBefore=4)

def tbl(data, col_widths, hdr_rows=1):
    t = Table(data, colWidths=col_widths)
    style_cmds = [
        ("BACKGROUND",  (0,0), (-1, hdr_rows-1), HDR_BG),
        ("TEXTCOLOR",   (0,0), (-1, hdr_rows-1), GREEN),
        ("FONTNAME",    (0,0), (-1, hdr_rows-1), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,-1), 8),
        ("FONTNAME",    (0,hdr_rows), (-1,-1), "Helvetica"),
        ("TEXTCOLOR",   (0,hdr_rows), (-1,-1), WHITE),
        ("BACKGROUND",  (0,hdr_rows), (-1,-1), ROW_BG),
        ("ROWBACKGROUNDS", (0,hdr_rows), (-1,-1), [ROW_BG, DKGREY]),
        ("GRID",        (0,0), (-1,-1), 0.4, colors.HexColor("#333333")),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING",(0,0), (-1,-1), 6),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("ALIGN",       (0,0), (-1,-1), "LEFT"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
    ]
    t.setStyle(TableStyle(style_cmds))
    return t

# ── Build content ──────────────────────────────────────────────────────────────
W = 7.2*inch  # usable width

story = []

# Header
story += [
    Paragraph("TRI-CITY INATOR", style("t", fontSize=22, fontName="Helvetica-Bold", textColor=GREEN, alignment=TA_CENTER)),
    Paragraph("TRADING CHEATSHEET", style("t2", fontSize=13, fontName="Helvetica", textColor=GREY, alignment=TA_CENTER, spaceAfter=2)),
    Paragraph("Updated 2026-05-19  ·  Paper mode ON  ·  15-min ORB  ·  github.com/djmusstrd/tri-city-inator",
              style("sub", fontSize=7, fontName="Helvetica", textColor=GREY, alignment=TA_CENTER, spaceAfter=8)),
    HRFlowable(width="100%", thickness=1, color=GREEN, spaceAfter=10),
]

# ── Session Flow ───────────────────────────────────────────────────────────────
story += [h1("DAILY SESSION FLOW  (all times CT)")]
flow_data = [
    ["TIME", "ACTION", "WHO"],
    ["Before 7:30 AM", "Open TradingView → TRI CITY INATOR III layout", "You"],
    ["Before 7:30 AM", "Run tricity in terminal to register crons", "You"],
    ["7:30 AM", "Premarket scanner — ranks gap-up candidates, pushes to TV watchlist", "Auto"],
    ["8:30 AM", "Symbol swap — top 15 candidates loaded into scanner", "Auto"],
    ["8:45 AM", "ORH/ORL lock — signal monitor starts (8:30 + 15 min ORB)", "Auto"],
    ["Every 3 min", "Signal check on all 15 symbols — auto-executes qualifying setups", "Auto"],
    ["3:45 PM", "EOD close — all open positions exited", "Auto"],
]
story += [tbl(flow_data, [1.1*inch, 4.9*inch, 0.8*inch]), sp(8)]

# ── Signals ────────────────────────────────────────────────────────────────────
story += [h1("SIGNAL TYPES & ENTRY CONDITIONS")]
sig_data = [
    ["SIGNAL", "REQUIRED CONDITIONS", "STOP"],
    ["BREAKOUT",     "Price > ORH  ·  RSI > 50  ·  EMA Dev% > 0  ·  RVOL ≥ 2.0x",        "13¢ below ORH"],
    ["CONTINUATION", "Price > ORH  ·  EMA Dev% 0% to +1.0%  ·  RVOL ≥ 1.75x",             "13¢ below ORH"],
    ["PULLBACK",     "EMA Dev% 0% to +0.8%  ·  RSI 38–55 (rising)  ·  RVOL ≥ 1.5x",      "EMA20 – 30¢ (widened stop)"],
]
story += [tbl(sig_data, [1.3*inch, 3.9*inch, 1.6*inch]), sp()]
story += [body("<font color='#ffd600'>CUP = YES</font> &nbsp;→&nbsp; High-conviction setup. Logged with --cup flag."), sp(8)]

# ── Position Structure ─────────────────────────────────────────────────────────
story += [h1("POSITION STRUCTURE  (50 – 25 – 25)")]
pos_data = [
    ["TARGET", "TRIGGER", "ACTION", "STOP MOVES TO"],
    ["T1", "+10%", "Sell 50% of position", "Breakeven"],
    ["T2", "+20%", "Sell 25% of position", "T2 price (locked)"],
    ["T3", "+30%", "Trail remaining 25%",  "Exit on EMA20 + VWAP breach or EOD"],
    ["STOP", "-5%", "Sell ALL",            "—"],
]
story += [tbl(pos_data, [0.6*inch, 0.7*inch, 2.4*inch, 3.1*inch]), sp(8)]

# ── Entry Guards ───────────────────────────────────────────────────────────────
story += [h1("ENTRY GUARDS  (checked in order before every trade)")]
guard_data = [
    ["#", "CONDITION", "DEFAULT", "ACTION"],
    ["1", "Already traded this symbol today",          "—",        "BLOCK"],
    ["2", "Already in a position for this symbol",     "—",        "BLOCK"],
    ["3", "Too many open positions",                   "3 max",    "BLOCK"],
    ["4", "Daily loss limit hit",                      "-$300",    "BLOCK (circuit breaker)"],
    ["5", "After entry cutoff",                        "1:00 PM CT","BLOCK (POST_CUTOFF alert)"],
    ["6", "SPY down from prior close",                 "-1.5%",    "BLOCK (bear regime)"],
    ["7", "RVOL below setup floor (see RVOL section)", "varies",   "BLOCK"],
]
story += [tbl(guard_data, [0.3*inch, 3.0*inch, 1.1*inch, 2.4*inch]), sp(8)]

# ── RVOL ──────────────────────────────────────────────────────────────────────
story += [h1("RVOL RULES")]
rvol_data = [
    ["FEATURE", "DETAIL"],
    ["Scanner colors",    "Red < 1.5x (weak) · Yellow 1.5–2.5x (moderate) · Green ≥ 2.5x (strong)"],
    ["Setup floors",      "BREAKOUT 2.0x · CONTINUATION 1.75x · PULLBACK 1.5x"],
    ["Afternoon tighten", "After noon CT → floor = max(setup floor, 2.0x)"],
    ["Size boost",        "Linear ramp: 1.5x RVOL = no boost → 3.0x RVOL = +25% position size"],
    ["Collapse exit",     "After T1 hit (breakeven set): if RVOL drops below 1.0x → EXIT"],
    ["Candle type",       "PULLBACK: HAMMER = full size · NEUTRAL/BEARISH = -25% size · auto-detected from last 1-min bar"],
]
story += [tbl(rvol_data, [1.5*inch, 5.3*inch]), sp(8)]

# ── Scanner Table ──────────────────────────────────────────────────────────────
story += [h1("SCANNER TABLE COLUMNS")]
col_data = [
    ["COLUMN", "MEANING"],
    ["SYMBOL",    "Ticker (exchange prefix stripped for display)"],
    ["PRICE",     "Current price on scanner timeframe (15-min)"],
    ["RSI",       "RSI-14 on 15-min bars"],
    ["EMA DEV%",  "% distance from 21 EMA. Negative = below EMA, positive = above"],
    ["RVOL",      "Current bar volume ÷ 10-bar avg volume"],
    ["ORH/ORL",   "Opening Range High / Low — locks at 8:45 AM (15-min ORB)"],
    ["CUP",       "20-bar range < 6% AND price within 1% of 20-bar high"],
    ["SIGNAL",    "BREAKOUT · CONTINUATION · PULLBACK · --- (no signal)"],
]
story += [tbl(col_data, [1.1*inch, 5.7*inch]), sp(8)]

# ── Terminal Commands ──────────────────────────────────────────────────────────
story += [h1("TERMINAL COMMANDS")]
cmd_data = [
    ["COMMAND", "ACTION"],
    ["tricity",           "Start session: open TradingView + Claude, register all crons"],
    ["tricity-scan",      "Run premarket scanner manually"],
    ["tricity-status",    "Show all open positions"],
    ["tricity",           "Today's trade report (P&L, R-multiples)"],
    ["tricity-all",       "All-time performance report"],
]
story += [tbl(cmd_data, [1.6*inch, 5.2*inch]), sp(4)]
story += [
    body("<font color='#888888'>Dry-run (no order placed):</font>"),
    mono("python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py \\\n  --symbol NVDA --price 225.00 --orh 223.16 --orl 218.37 \\\n  --rsi 55.0 --ema_dev 0.85 --signal \"BREAKOUT\" --setup BREAKOUT --dry-run"),
    body("<font color='#888888'>Force EOD close now:</font>"),
    mono("python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod"),
    sp(8),
]

# ── .env Reference ─────────────────────────────────────────────────────────────
story += [h1(".ENV REFERENCE")]
env_data = [
    ["KEY", "DEFAULT", "NOTES"],
    ["ALPACA_PAPER",           "true",    "Set to false for live trading"],
    ["MIN_GAP_PCT",            "1.5",     "Minimum gap % to qualify as candidate"],
    ["FREE_RIDE_PCT",          "3",       "Profit % to activate free-ride stop"],
    ["T3_TRAIL_PCT",           "5",       "Trailing stop % for T3"],
    ["MAX_POSITIONS",          "3",       "Max concurrent open positions"],
    ["DAILY_LOSS_LIMIT",       "-300",    "Circuit breaker in dollars"],
    ["ORB_MINUTES",            "15",      "5, 15, or 30. Must match TV scanner timeframe"],
    ["BREAKOUT_MIN_RVOL",      "2.0",     "RVOL floor for BREAKOUT entries"],
    ["CONTINUATION_MIN_RVOL",  "1.75",    "RVOL floor for CONTINUATION entries"],
    ["PULLBACK_MIN_RVOL",      "1.5",     "RVOL floor for PULLBACK entries"],
    ["RVOL_SIZE_BOOST_MAX",    "1.25",    "Max position size multiplier at high RVOL"],
    ["RVOL_SIZE_BOOST_THRESH", "3.0",     "RVOL level that triggers max boost"],
    ["RVOL_EXIT_FLOOR",        "1.0",     "RVOL collapse exit threshold (post-T1 only)"],
    ["PM_MIN_RVOL",            "2.0",     "Afternoon RVOL floor (after noon CT)"],
    ["PULLBACK_FAIL_BUFFER",   "0.005",   "Fix B: 0.5% buffer below entry before exit fires"],
    ["EMA_STOP_BUFFER",        "0.30",    "Cents below EMA20 for PULLBACK stop (widened)"],
    ["RE_ENTRY_MAX_LOSS",      "0.50",    "Max $/share loss to allow re-entry after Fix B scratch"],
]
story += [tbl(env_data, [2.1*inch, 0.9*inch, 3.8*inch]), sp(8)]

# ── TradingView ────────────────────────────────────────────────────────────────
story += [h1("TRADINGVIEW")]
tv_data = [
    ["ITEM", "VALUE"],
    ["Layout",      "TRI CITY INATOR III  (ID 168250176)"],
    ["Indicator",   "Tri-City Inator  (Pine v5, shorttitle: Tri-City)"],
    ["Entity ID",   "ur9VaL  (used by symbol swap cron)"],
    ["Timeframe",   "15-min  (must match ORB_MINUTES=15)"],
    ["Symbol slots","in_7 through in_21  (15 symbols, auto-swapped at 8:30 AM)"],
]
story += [tbl(tv_data, [1.3*inch, 5.5*inch])]

doc.build(story,
    onFirstPage=lambda c, d: c.setFillColor(BG) or c.rect(0,0,letter[0],letter[1],fill=1,stroke=0),
    onLaterPages=lambda c, d: c.setFillColor(BG) or c.rect(0,0,letter[0],letter[1],fill=1,stroke=0))

print(f"PDF saved to: {OUT}")
