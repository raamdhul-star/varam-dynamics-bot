"""
scanner/scorer.py
=================
6-component signal scoring system (out of 10).

Components:
  1. Multi-TF Confluence  [3.0 pts] — how many TFs agree on direction
  2. CPR Width            [2.0 pts] — narrower = stronger magnet
  3. Volume               [1.5 pts] — how far above average
  4. Breakout Strength    [1.5 pts] — how far price closed beyond TC/BC
  5. Risk:Reward          [1.0 pts] — distance to target vs SL
  6. Asset Liquidity      [1.0 pts] — tier-based liquidity score

Bonus:
  Lower TF Support        [+0.5 pts] — 15m/30m micro-structure aligns

Risk classification:
  8.0 – 10.0  →  🟢 Low Risk
  6.0 –  7.9  →  🟡 Low-Medium Risk
  4.0 –  5.9  →  🟠 Medium Risk
  < 4.0       →  🔴 Skip (not sent in alert)

Minimum score to alert: 5.0
"""
from __future__ import annotations
from dataclasses import dataclass, field


MIN_ALERT_SCORE = 5.0


@dataclass
class ScoreBreakdown:
    symbol:      str
    interval:    str
    direction:   str   # 'long' or 'short'

    # Raw inputs
    tf_count:    int   = 0    # number of TFs that agree
    cpr_width:   float = 0.0  # CPR width as % of price
    vol_ratio:   float = 0.0  # volume / vol_ma
    breakout_pct:float = 0.0  # how far beyond TC/BC as % of price
    rr_ratio:    float = 0.0  # risk:reward
    liquidity:   float = 0.0  # 0.0–1.0 from tier
    lower_tf_ok: bool  = False # 15m/30m micro-structure supports trade

    # Computed scores
    score_tf:        float = 0.0
    score_cpr:       float = 0.0
    score_vol:       float = 0.0
    score_breakout:  float = 0.0
    score_rr:        float = 0.0
    score_liquidity: float = 0.0
    score_lower_tf:  float = 0.0

    total_score: float = 0.0
    risk_label:  str   = ""
    risk_emoji:  str   = ""
    should_alert:bool  = False

    # Signal metadata (passed through)
    entry_price: float = 0.0
    sl_price:    float = 0.0
    tp_price:    float = 0.0
    sl_pct:      float = 0.0
    cpr_type:    str   = ""
    is_narrow:   bool  = False
    bar_time:    object = None
    atr:         float = 0.0

    # Agreeing timeframes list
    agreeing_tfs: list = field(default_factory=list)


def score_signal(
    symbol:       str,
    interval:     str,
    direction:    str,
    signal_data:  dict,
    all_tf_signals: dict,       # {interval: signal_dict or None}
    liquidity_score: float,     # from assets.liquidity_score()
    lower_tf_ok:  bool = False,
) -> ScoreBreakdown:
    """
    Score a signal across all 6 components.

    signal_data     — the signal dict from cpr_engine.get_latest_signal()
    all_tf_signals  — signals (or None) for each scanned interval
    liquidity_score — tier-based liquidity score 0.0–1.0
    lower_tf_ok     — True if 15m/30m microstructure supports the trade
    """
    sb = ScoreBreakdown(
        symbol=symbol, interval=interval, direction=direction,
        entry_price=signal_data.get("entry_price", 0),
        sl_price=signal_data.get("sl_price", 0),
        tp_price=signal_data.get("tp_price", 0),
        sl_pct=signal_data.get("sl_pct", 0),
        cpr_type=signal_data.get("cpr_type", ""),
        is_narrow=signal_data.get("is_narrow", False),
        bar_time=signal_data.get("bar_time"),
        atr=signal_data.get("atr", 0),
        liquidity=liquidity_score,
        lower_tf_ok=lower_tf_ok,
    )

    # ── Component 1: Multi-TF Confluence [3.0 pts] ──────────────────────
    # Check how many timeframes show the same direction signal
    agreeing = []
    tf_order = ["15m", "30m", "1h", "4h", "1d", "1w"]
    for tf, sig in all_tf_signals.items():
        if sig and sig.get("signal") == direction:
            agreeing.append(tf)

    # Weight by timeframe importance
    tf_weights = {"1h": 1.0, "4h": 1.2, "1d": 1.4, "1w": 1.6,
                  "15m": 0.5, "30m": 0.7}
    weighted_score = sum(tf_weights.get(tf, 1.0) for tf in agreeing)
    max_weighted   = sum(tf_weights.get(tf, 1.0) for tf in tf_order
                         if tf in all_tf_signals)

    sb.tf_count    = len(agreeing)
    sb.agreeing_tfs = agreeing
    sb.score_tf    = round(min(3.0, (weighted_score / max(max_weighted, 1)) * 3.0), 2) \
                     if max_weighted > 0 else 0.0

    # ── Component 2: CPR Width [2.0 pts] ────────────────────────────────
    # Narrower CPR = stronger price magnet = higher score
    width = signal_data.get("cpr_width", 5.0)
    sb.cpr_width = width
    if width < 0.2:
        sb.score_cpr = 2.0   # extremely narrow — maximum score
    elif width < 0.5:
        sb.score_cpr = 1.7   # narrow
    elif width < 1.0:
        sb.score_cpr = 1.3   # moderate
    elif width < 2.0:
        sb.score_cpr = 0.8   # wide
    else:
        sb.score_cpr = 0.3   # very wide — weak magnet

    # ── Component 3: Volume [1.5 pts] ───────────────────────────────────
    vol_ratio = signal_data.get("vol_ratio", 1.0)
    sb.vol_ratio = vol_ratio
    if vol_ratio >= 3.0:
        sb.score_vol = 1.5   # exceptional volume — high conviction
    elif vol_ratio >= 2.0:
        sb.score_vol = 1.2
    elif vol_ratio >= 1.5:
        sb.score_vol = 1.0
    elif vol_ratio >= 1.2:
        sb.score_vol = 0.7
    elif vol_ratio >= 1.0:
        sb.score_vol = 0.4
    else:
        sb.score_vol = 0.1   # below average — weak confirmation

    # ── Component 4: Breakout Strength [1.5 pts] ────────────────────────
    # How far did price close beyond TC (long) or BC (short)?
    entry  = signal_data.get("entry_price", 0)
    tc     = signal_data.get("tc", entry)
    bc     = signal_data.get("bc", entry)

    if direction == "long" and tc > 0:
        breakout_dist = (entry - tc) / tc * 100
    elif direction == "short" and bc > 0:
        breakout_dist = (bc - entry) / bc * 100
    else:
        breakout_dist = 0.0

    sb.breakout_pct = round(breakout_dist, 3)
    if breakout_dist >= 1.0:
        sb.score_breakout = 1.5   # strong conviction close
    elif breakout_dist >= 0.5:
        sb.score_breakout = 1.2
    elif breakout_dist >= 0.2:
        sb.score_breakout = 0.9
    elif breakout_dist >= 0.05:
        sb.score_breakout = 0.6
    else:
        sb.score_breakout = 0.2   # marginal close — barely above TC

    # ── Component 5: Risk:Reward [1.0 pts] ──────────────────────────────
    rr = signal_data.get("rr_ratio", 0.0)
    sb.rr_ratio = rr
    if rr >= 3.0:
        sb.score_rr = 1.0
    elif rr >= 2.0:
        sb.score_rr = 0.8
    elif rr >= 1.5:
        sb.score_rr = 0.6
    elif rr >= 1.0:
        sb.score_rr = 0.4
    else:
        sb.score_rr = 0.1   # R:R below 1:1 — poor setup

    # ── Component 6: Asset Liquidity [1.0 pts] ──────────────────────────
    sb.score_liquidity = round(liquidity_score * 1.0, 2)

    # ── Bonus: Lower TF Support [+0.5 pts] ──────────────────────────────
    sb.score_lower_tf = 0.5 if lower_tf_ok else 0.0

    # ── Total ────────────────────────────────────────────────────────────
    raw_total = (
        sb.score_tf + sb.score_cpr + sb.score_vol +
        sb.score_breakout + sb.score_rr + sb.score_liquidity +
        sb.score_lower_tf
    )
    sb.total_score = round(min(10.0, raw_total), 2)

    # ── Risk classification ──────────────────────────────────────────────
    if sb.total_score >= 8.0:
        sb.risk_label = "Low Risk"
        sb.risk_emoji = "🟢"
    elif sb.total_score >= 6.0:
        sb.risk_label = "Low-Medium Risk"
        sb.risk_emoji = "🟡"
    elif sb.total_score >= 4.0:
        sb.risk_label = "Medium Risk"
        sb.risk_emoji = "🟠"
    else:
        sb.risk_label = "High Risk — Skip"
        sb.risk_emoji = "🔴"

    sb.should_alert = sb.total_score >= MIN_ALERT_SCORE

    return sb


def format_score_card(sb: ScoreBreakdown) -> str:
    """Format a ScoreBreakdown into a readable text block for the alert."""
    direction_emoji = "📈" if sb.direction == "long" else "📉"
    tfs = ", ".join(sb.agreeing_tfs) if sb.agreeing_tfs else "none"

    lines = [
        f"{direction_emoji} {sb.symbol}  {sb.direction.upper()}  "
        f"[{sb.total_score:.1f}/10]  {sb.risk_emoji} {sb.risk_label}",
        f"  Timeframe : {sb.interval}",
        f"  Entry     : {sb.entry_price:.6g}",
        f"  Target    : {sb.tp_price:.6g}",
        f"  Stop Loss : {sb.sl_price:.6g}  ({sb.sl_pct:.2f}% risk)",
        f"  R:R Ratio : {sb.rr_ratio:.2f}:1",
        f"  CPR Type  : {sb.cpr_type}  (width {sb.cpr_width:.3f}%)",
        "",
        f"  Score breakdown:",
        f"    TF Confluence  {sb.score_tf:>4.1f}/3.0  "
        f"({sb.tf_count} TFs agree: {tfs})",
        f"    CPR Width      {sb.score_cpr:>4.1f}/2.0  "
        f"({'Narrow ✅' if sb.is_narrow else 'Wide ⚠️'})",
        f"    Volume         {sb.score_vol:>4.1f}/1.5  "
        f"({sb.vol_ratio:.1f}× average)",
        f"    Breakout       {sb.score_breakout:>4.1f}/1.5  "
        f"({sb.breakout_pct:.3f}% beyond CPR)",
        f"    Risk:Reward    {sb.score_rr:>4.1f}/1.0",
        f"    Liquidity      {sb.score_liquidity:>4.1f}/1.0",
    ]
    if sb.score_lower_tf > 0:
        lines.append(f"    Lower TF ✅    {sb.score_lower_tf:>4.1f}/0.5  "
                     f"(15m/30m supports)")
    lines.append(f"    {'─'*30}")
    lines.append(f"    TOTAL          {sb.total_score:>4.1f}/10.0")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test scorer
    mock_signal = {
        "signal": "long",
        "entry_price": 2316.0,
        "sl_price": 2280.0,
        "tp_price": 2420.0,
        "sl_pct": 1.55,
        "rr_ratio": 2.89,
        "cpr_type": "Narrow",
        "cpr_width": 0.18,
        "is_narrow": True,
        "vol_ratio": 2.3,
        "tc": 2314.0,
        "bc": 2310.0,
        "atr": 35.0,
        "bar_time": "2026-05-11T08:00:00Z",
    }

    mock_all_tfs = {
        "1h":  {"signal": "long"},
        "4h":  {"signal": "long"},
        "1d":  {"signal": "long"},
        "1w":  {"signal": None},
    }

    sb = score_signal(
        symbol="ETH",
        interval="1h",
        direction="long",
        signal_data=mock_signal,
        all_tf_signals=mock_all_tfs,
        liquidity_score=1.0,   # Tier 1
        lower_tf_ok=True,
    )

    print(format_score_card(sb))
    print(f"\nShould alert: {sb.should_alert}")
