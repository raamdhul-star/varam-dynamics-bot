"""
cleanup_positions.py
====================
One-time script to fix the 118 duplicate open positions.
Keeps only the BEST (highest score) set of 3 per asset.
Closes all duplicates as 'cancelled' in history.

Run once:
    python cleanup_positions.py
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

STATE_FILE   = Path("results/paper_trades/open_positions.json")
HISTORY_FILE = Path("results/paper_trades/trade_history.csv")

if not STATE_FILE.exists():
    print("No state file found.")
    exit()

positions = json.loads(STATE_FILE.read_text())
open_pos   = [p for p in positions if p.get("status") == "open"]
print(f"Before cleanup: {len(open_pos)} open positions")

# Group by symbol + direction + exit_style, keep highest score
# Strategy: for each symbol, keep the 3 positions with the highest score
# (one per exit style)
best_per_asset: dict[str, list] = defaultdict(list)
for p in open_pos:
    sym = p["symbol"]
    best_per_asset[sym].append(p)

kept     = []
cancelled = []

for sym, group in best_per_asset.items():
    # Sort by score descending, then by open_time descending (newest first)
    group.sort(key=lambda x: (x.get("score", 0), x.get("open_time", "")),
               reverse=True)

    # Keep one set of 3 (one per exit style) — pick from highest scored
    styles_kept = set()
    for p in group:
        style = p["exit_style"]
        if style not in styles_kept:
            styles_kept.add(style)
            kept.append(p)
        else:
            # Mark as cancelled
            cancelled.append({
                **p,
                "status":      "cancelled",
                "exit_time":   datetime.now(timezone.utc).isoformat(),
                "exit_reason": "cleanup_dedup",
                "pnl_pct":     p.get("current_pnl_pct", 0.0),
                "result":      "cancelled",
            })

print(f"After cleanup:  {len(kept)} open positions kept")
print(f"Cancelled:      {len(cancelled)} duplicate positions")
print()
print("Assets kept:")
shown = set()
for p in kept:
    key = f"{p['symbol']} {p['direction']}"
    if key not in shown:
        shown.add(key)
        score = p.get("score", 0)
        pnl   = p.get("current_pnl_pct", 0.0)
        print(f"  {p['symbol']:<8} {p['direction']:<6} score={score:.1f}  pnl={pnl:+.2f}%")

# Save cleaned state
STATE_FILE.write_text(json.dumps(kept, indent=2, default=str))
print(f"\nState file updated: {len(kept)} open positions")

# Append cancelled to history
if cancelled:
    import pandas as pd
    df_cancel = pd.DataFrame(cancelled)
    if HISTORY_FILE.exists():
        existing = pd.read_csv(HISTORY_FILE)
        pd.concat([existing, df_cancel], ignore_index=True).to_csv(HISTORY_FILE, index=False)
    else:
        df_cancel.to_csv(HISTORY_FILE, index=False)
    print(f"History updated: {len(cancelled)} cancelled positions logged")

print("\nDone. Run 'python main.py status' to verify.")
