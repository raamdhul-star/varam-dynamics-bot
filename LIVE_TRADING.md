# Live trading lives elsewhere

Real-money execution moved to its own **private** repo:

> **`Rahul-Creator-005/varam-live`**

## Why it is not here

**This repo is public, so its Actions logs are public.** Live trading prints
account balances, position sizes and P&L. Splitting them keeps the expensive
scanner on unlimited free minutes while account detail stays private.

Keeping a second copy here was also a real hazard: while both existed, the same
bug had to be fixed in two places twice in one day. One home, no drift.

## How the two repos relate

```
   varam-dynamics-bot  (public, here)        varam-live  (private)
   ─────────────────────────────────         ──────────────────────
   scans the market                          reads those signals over https
   scores signals                     ──▶    sizes and places the orders
   sends Telegram alerts                     manages trailing stops
   paper trades all 3 exit styles            keeps balances/P&L private
   commits results/telegram_state.json
```

Live trading **never generates its own signals**. It reads
`results/telegram_state.json` from this repo, so live and paper always act on
identical calls — the only thing that makes comparing them honest.

**Nothing in this repo needs changing to support that.** The contract is just
that file, and its `batches` shape: each entry carries `time` plus `sigs` with
`symbol, direction, interval, score, entry, sl, tp, bar_time`. Live reads
`time` for freshness and `sl` for the stop. **Changing those field names breaks
live trading silently** — that exact mismatch once made every call skip.

## Paper trading stays here, deliberately unchanged

`paper/tracker.py` keeps all three exit styles and its +5% trailing trigger.
Live uses +3%. That difference is intentional: it makes the two a real A/B
rather than two copies of the same thing.
