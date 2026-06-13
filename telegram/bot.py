"""
telegram/bot.py  (v2 — interactive keyboards + multi-trade)
============================================================
Full interactive Telegram bot for Varam-Dynamics.
Handles multi-trade selection, leverage picker, size picker,
hourly check-in, and trade closing — all via inline keyboards.

State stored in results/telegram_state.json
Processed by monitor workflow (hourly GitHub Actions run).
"""
from __future__ import annotations
import csv, json, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

API         = "https://api.telegram.org/bot{token}/{method}"
STATE_FILE  = Path(__file__).parent.parent / "results" / "telegram_state.json"
TRADE_LOG   = Path(__file__).parent.parent / "results" / "manual_trades" / "trades.csv"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)

MAX_TRADES   = 5
LEV_OPTIONS  = [2, 3, 5, 7, 10, 15, 20, 25, 50]
SIZE_PCT     = [25, 50, 75, 100]

# ── Duplicate-alert suppression (Option D) ──────────────────────────────────
ALERTED_TTL_DAYS = 35      # remember an alerted signal candle for this long
MAX_ALERTED      = 2000    # hard cap on remembered signal IDs (file size guard)
MAX_BATCHES      = 12       # how many recent alert messages stay button-tappable


# ── Core API ──────────────────────────────────────────────────────────────

def _tok(): return os.environ.get("TELEGRAM_BOT_TOKEN","")
def _cid(): return os.environ.get("TELEGRAM_CHAT_ID","")

def _post(method, payload):
    try:
        r = requests.post(API.format(token=_tok(), method=method),
                          json=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[tg] {method}: {e}"); return None

def send_message(text, markup=None, parse_mode="HTML"):
    p = {"chat_id":_cid(),"text":text,"parse_mode":parse_mode,
         "disable_web_page_preview":True}
    if markup: p["reply_markup"] = markup
    return _post("sendMessage", p)

def edit_message(mid, text, markup=None):
    p = {"chat_id":_cid(),"message_id":mid,"text":text,
         "parse_mode":"HTML","disable_web_page_preview":True}
    if markup: p["reply_markup"] = markup
    return _post("editMessageText", p)

def answer_cb(cb_id, text=""):
    _post("answerCallbackQuery",{"callback_query_id":cb_id,"text":text})

def get_updates(offset=0):
    r = _post("getUpdates",{"offset":offset,"timeout":3,"limit":20})
    return r.get("result",[]) if r else []


# ── Keyboards ─────────────────────────────────────────────────────────────

def _kb(rows): return {"inline_keyboard": rows}

def alert_kb():
    return _kb([[{"text":"✅ Select my trades","callback_data":"SELECT"},
                 {"text":"⏭ Skip all",        "callback_data":"SKIP"}]])

def multiselect_kb(sigs, selected):
    rows = []
    for s in sigs:
        tick = "✅" if s["symbol"] in selected else "☐"
        de   = "📈" if s["direction"]=="long" else "📉"
        rows.append([{"text":f"{tick} {s['symbol']} {de} [{s['score']:.1f}]",
                      "callback_data":f"TOGGLE_{s['symbol']}_{s['direction']}"}])
    n = len(selected)
    rows.append([{"text":f"✅ Done — {n} trade{'s' if n!=1 else ''}" if n
                  else "⏭ Done (nothing selected)",
                  "callback_data":"DONE"}])
    return _kb(rows)

def leverage_kb(sym, direction, suggested, max_lev):
    opts = [l for l in LEV_OPTIONS if l <= max_lev]
    rows, row = [], []
    for l in opts:
        star = "⭐" if abs(l-suggested)<0.6 else ""
        row.append({"text":f"{star}{l}x",
                    "callback_data":f"LEV_{sym}_{direction}_{l}"})
        if len(row)==4: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"✏️ Custom","callback_data":f"LEV_{sym}_{direction}_custom"},
                 {"text":"⬅️ Back",  "callback_data":"BACK"}])
    return _kb(rows)

def size_kb(sym, direction, lev, acct):
    rows, row = [], []
    for pct in SIZE_PCT:
        amt = round(acct*pct/100)
        row.append({"text":f"${amt} ({pct}%)",
                    "callback_data":f"SZ_{sym}_{direction}_{lev}_{amt}"})
        if len(row)==2: rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"✏️ Custom","callback_data":f"SZ_{sym}_{direction}_{lev}_custom"}])
    return _kb(rows)

def close_kb(sym):
    return _kb([[{"text":"✅ Win",        "callback_data":f"CL_{sym}_win"},
                 {"text":"❌ Loss",       "callback_data":f"CL_{sym}_loss"},
                 {"text":"↔️ Breakeven", "callback_data":f"CL_{sym}_be"}]])

def exitpx_kb(sym, result, tp, entry):
    half = round((tp+entry)/2, 8)
    return _kb([[{"text":f"🎯 {tp:.6g}",   "callback_data":f"EX_{sym}_{result}_{tp}"},
                 {"text":f"Half {half:.6g}","callback_data":f"EX_{sym}_{result}_{half}"}],
                [{"text":"✏️ Manual price","callback_data":f"EX_{sym}_{result}_manual"}]])


# ── State ─────────────────────────────────────────────────────────────────

def _ld():
    if STATE_FILE.exists():
        try: return json.loads(STATE_FILE.read_text())
        except: pass
    return {"offset":0,"sigs":[],"sel":[],"queue":[],
            "setup":None,"trades":{}}

def _sv(st): STATE_FILE.write_text(json.dumps(st,indent=2,default=str))


# ── Signal identity + state cleanup (Option D) ──────────────────────────────

def _signal_id(sig: dict) -> str:
    """Stable identity for a signal — same candle ⇒ same id.
    Price movement alone does NOT change this id; a new candle,
    new direction, or new timeframe does."""
    return (f"{sig.get('symbol')}_{sig.get('direction')}"
            f"_{sig.get('interval')}_{sig.get('bar_time')}")

def _prune_alerted(alerted: dict, now: datetime) -> dict:
    """Drop expired ids (older than TTL), then cap to the most recent N."""
    cutoff = now - timedelta(days=ALERTED_TTL_DAYS)
    kept = {}
    for sid, ts in alerted.items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                kept[sid] = ts
        except Exception:
            continue
    if len(kept) > MAX_ALERTED:
        newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_ALERTED]
        kept = dict(newest)
    return kept

def _prune_batches(batches: dict) -> dict:
    """Keep only the most recent MAX_BATCHES alert snapshots."""
    if len(batches) <= MAX_BATCHES:
        return batches
    newest = sorted(batches.items(),
                    key=lambda kv: kv[1].get("time", ""), reverse=True)[:MAX_BATCHES]
    return dict(newest)


# ── Trade log ─────────────────────────────────────────────────────────────

FIELDS = ["timestamp","symbol","direction","entry_price","exit_price",
          "leverage_suggested","leverage_used","size_usd","risk_usd",
          "sl_price","tp_price","result","pnl_pct","pnl_usd",
          "signal_score","exit_reason"]

def _log(trade):
    write_hdr = not TRADE_LOG.exists()
    with open(TRADE_LOG,"a",newline="") as f:
        w = csv.DictWriter(f,fieldnames=FIELDS,extrasaction="ignore")
        if write_hdr: w.writeheader()
        w.writerow({k:trade.get(k,"") for k in FIELDS})

def _stats():
    if not TRADE_LOG.exists(): return 0,0
    try:
        rows = list(csv.DictReader(open(TRADE_LOG)))
        wins = sum(1 for r in rows if r.get("result")=="win")
        return len(rows), wins
    except: return 0,0


# ── Alert sending ─────────────────────────────────────────────────────────

def _card(sb, acct):
    from scanner.assets import calc_leverage
    de   = "📈" if sb.direction=="long" else "📉"
    levi = calc_leverage(sb.entry_price, sb.sl_price, acct, 0.07, sb.symbol)
    tfs  = ", ".join(sb.agreeing_tfs) if sb.agreeing_tfs else sb.interval
    cap  = " ⚠️HL max" if levi.get("capped") else ""
    chg  = abs(sb.tp_price-sb.entry_price)/sb.entry_price*100
    chg_sign = "-" if sb.direction=="short" else "+"
    return (
        f"{'━'*32}\n"
        f"<b>{de} {sb.symbol} {sb.direction.upper()}</b>  "
        f"[<b>{sb.total_score:.1f}/10</b>] {sb.risk_emoji} {sb.risk_label}\n"
        f"  TF: {sb.interval} | Setup: Qualified\n"
        f"  🎯 Entry  <code>{sb.entry_price:.6g}</code>\n"
        f"  🟢 Target <code>{sb.tp_price:.6g}</code> ({chg_sign}{chg:.1f}%)\n"
        f"  🔴 Stop   <code>{sb.sl_price:.6g}</code> ({'+' if sb.direction=='short' else '-'}{sb.sl_pct:.2f}%)\n"
        f"  ⚖️ R:R {sb.rr_ratio:.2f}:1\n"
        f"  💡 Suggested: <b>{levi.get('leverage','?')}x</b>{cap} | "
        f"${levi.get('position_sz','?')} | {levi.get('qty','?')} {sb.symbol}\n"
        f"  TFs: {tfs}"
    )

def send_alert(signals, scan_time, acct=200.0):
    if not signals:
        send_message(f"🔍 <b>VARAM-DYNAMICS</b>\n🕐 {scan_time}\nNo signals.")
        return None

    st  = _ld()
    now = datetime.now(timezone.utc)
    alerted = _prune_alerted(st.get("alerted", {}), now)

    # ── Option D dedup: skip any signal whose exact candle was already sent ──
    fresh = []
    for s in signals:
        sig = {"symbol":s.symbol, "direction":s.direction, "interval":s.interval,
               "score":s.total_score, "entry":s.entry_price,
               "sl":s.sl_price, "tp":s.tp_price,
               "sl_pct":s.sl_pct, "rr":s.rr_ratio,
               "bar_time":str(s.bar_time)}
        if _signal_id(sig) in alerted:
            print(f"[tg] SKIP duplicate alert — {_signal_id(sig)}")
            continue
        fresh.append((s, sig))

    if not fresh:
        st["alerted"] = alerted
        _sv(st)
        print("[tg] No new signals to alert (all already sent)")
        return None

    sbs  = [s   for s, _ in fresh]
    sigs = [sig for _, sig in fresh]

    n   = len(sbs)
    hdr = f"🔔 <b>VARAM-DYNAMICS</b> — {n} signal{'s' if n>1 else ''}\n🕐 {scan_time}\n\n"
    txt = hdr + "\n\n".join(_card(s,acct) for s in sbs)
    txt += "\n\n" + "━"*32 + "\nTap below to log your trades 👇"
    txt += "\n⚠️ Educational only · not financial advice · high-risk · DYOR"
    r   = send_message(txt, markup=alert_kb())
    mid = r["result"]["message_id"] if r and r.get("ok") else None

    # ── Remember these candles so we never alert the same one twice ──
    for sig in sigs:
        alerted[_signal_id(sig)] = now.isoformat()
    st["alerted"] = _prune_alerted(alerted, now)

    # ── Snapshot this batch so ITS buttons resolve to THESE prices ──
    if mid is not None:
        batches = st.get("batches", {})
        batches[str(mid)] = {"time": now.isoformat(), "sigs": sigs}
        st["batches"] = _prune_batches(batches)

    st["sigs"] = sigs    # latest batch (used by typed-command fallback flow)
    _sv(st)
    return mid


# ── Callback processor ────────────────────────────────────────────────────

def process_callbacks(acct=200.0):
    st   = _ld()
    upds = get_updates(st.get("offset",0))
    if not upds: return

    for upd in upds:
        st["offset"] = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if cb:
            answer_cb(cb["id"])
            _cb(cb.get("data",""), cb["message"]["message_id"], st, acct)
        msg = upd.get("message",{})
        txt = msg.get("text","").strip()
        if txt.startswith("/"): _cmd(txt, st, acct)

    _sv(st)


def _cb(data, mid, st, acct):
    from scanner.assets import calc_leverage, max_leverage as ml

    if data == "SKIP":
        edit_message(mid,"⏭ Signals skipped. Next scan ~2h.")

    elif data == "SELECT":
        # Resolve to the signal data from the alert that was actually tapped,
        # NOT whatever the latest scan produced.
        batch = st.get("batches", {}).get(str(mid))
        if batch is None:
            edit_message(mid, "⚠️ This alert is old. Please use the latest signal message.")
            return
        st["sigs"] = batch["sigs"]
        st["sel"]  = []
        edit_message(mid,"Which trades did you take?\nTap to select, then Done ✅",
                     markup=multiselect_kb(st.get("sigs",[]),set()))

    elif data.startswith("TOGGLE_"):
        _,sym,_ = data.split("_",2)
        sel = st.get("sel",[])
        if sym in sel: sel.remove(sym)
        elif len(sel) < MAX_TRADES: sel.append(sym)
        st["sel"] = sel
        edit_message(mid,"Which trades did you take?\nTap to select, then Done ✅",
                     markup=multiselect_kb(st.get("sigs",[]),set(sel)))

    elif data == "DONE":
        sel = st.get("sel",[])
        if not sel:
            edit_message(mid,"⏭ None selected."); return
        smap   = {s["symbol"]:s for s in st.get("sigs",[])}
        st["queue"] = [smap[s] for s in sel if s in smap]
        _next_setup(st, acct)

    elif data.startswith("LEV_"):
        _,sym,direction,lv = data.split("_",3)
        if lv == "custom":
            send_message(f"Enter leverage:\n/setlev {sym} {direction} <number>")
            return
        sig  = {s["symbol"]:s for s in st.get("sigs",[])}.get(sym,{})
        lev  = float(lv)
        st["setup"] = {"symbol":sym,"direction":direction,"leverage":lev,"sig":sig}
        send_message(f"<b>{sym} {direction.upper()}</b> — {lev}x leverage\nSize?",
                     markup=size_kb(sym,direction,lev,acct))

    elif data.startswith("SZ_"):
        _,sym,direction,lv,amt = data.split("_",4)
        if amt == "custom":
            send_message(f"Enter size:\n/setsize {sym} {direction} {lv} <USD>")
            return
        _confirm(sym,direction,float(lv),float(amt),st,acct)

    elif data.startswith("CL_"):
        _,sym,result = data.split("_",2)
        tr = st.get("trades",{}).get(sym)
        if not tr:
            send_message(f"No open trade for {sym}."); return
        send_message(f"<b>{sym}</b> closing as {result.upper()}\nExit price?",
                     markup=exitpx_kb(sym,result,tr.get("tp",0),tr.get("entry",0)))

    elif data.startswith("EX_"):
        _,sym,result,px = data.split("_",3)
        if px == "manual":
            send_message(f"Enter exit price:\n/exitpx {sym} {result} <price>"); return
        _close(sym,result,float(px),st)

    elif data == "BACK":
        send_message("Select trades again:", markup=multiselect_kb(
            st.get("sigs",[]),set(st.get("sel",[]))))


def _next_setup(st, acct):
    from scanner.assets import calc_leverage, max_leverage as ml
    q = st.get("queue",[])
    if not q:
        send_message("✅ All trades logged! Checking in every hour 👀"); return
    sig  = q.pop(0); st["queue"] = q
    sym  = sig["symbol"]; direction = sig["direction"]
    levi = calc_leverage(sig.get("entry",0),sig.get("sl",0),acct,0.07,sym)
    rem  = f" ({len(q)} more after)" if q else " (last one)"
    send_message(
        f"<b>{sym} {direction.upper()}</b>{rem}\n"
        f"Entry:{sig.get('entry',0):.6g}  SL:{sig.get('sl',0):.6g}  "
        f"TP:{sig.get('tp',0):.6g}\n"
        f"⭐ Suggested: <b>{levi.get('leverage','?')}x</b>\nYour leverage?",
        markup=leverage_kb(sym,direction,levi.get("leverage",3),ml(sym)))


def _confirm(sym, direction, lev, size, st, acct):
    sig   = {s["symbol"]:s for s in st.get("sigs",[])}.get(sym,{})
    entry = sig.get("entry",0); sl = sig.get("sl",0)
    risk  = size * abs(entry-sl)/entry if entry else 0
    risk_pct = risk/acct*100
    from scanner.assets import calc_leverage
    sug = calc_leverage(entry,sl,acct,0.07,sym).get("leverage",0)
    note= f"\n⚠️ You used {lev}x vs suggested {sug}x" if abs(lev-sug)>0.5 else ""

    st.setdefault("trades",{})[sym] = {
        "symbol":sym,"direction":direction,"entry":entry,
        "sl":sl,"tp":sig.get("tp",0),
        "leverage_suggested":sug,"leverage_used":lev,
        "size_usd":size,"risk_usd":round(risk,2),
        "signal_score":sig.get("score",0),
        "open_time":datetime.now(timezone.utc).isoformat(),
    }
    send_message(
        f"✅ <b>Logged — {sym} {direction.upper()}</b>{note}\n\n"
        f"  Entry: {entry:.6g}  Lev: <b>{lev}x</b>  Size: ${size}\n"
        f"  SL: {sl:.6g}  TP: {sig.get('tp',0):.6g}\n"
        f"  At risk: ${risk:.2f} ({risk_pct:.1f}%)\n\n"
        f"To close: /close {sym}"
    )
    _next_setup(st, acct)


def _close(sym, result, exit_px, st):
    tr = st.get("trades",{}).pop(sym,None)
    if not tr: send_message(f"No open trade for {sym}."); return
    entry = tr.get("entry",exit_px); size = tr.get("size_usd",0)
    lev   = tr.get("leverage_used",1); direction = tr.get("direction","long")
    pnl   = (exit_px-entry)/entry*100 if direction=="long" else (entry-exit_px)/entry*100
    pusd  = round(size*pnl/100,2)
    total, wins = _stats()
    if result=="win": wins+=1
    wr = f"{wins}/{total+1} ({wins/(total+1)*100:.0f}%)"
    emoji = "🎉" if result=="win" else "😞" if result=="loss" else "↔️"
    send_message(
        f"{emoji} <b>{sym} {direction.upper()} Closed</b>\n\n"
        f"  Entry: {entry:.6g}  Exit: {exit_px:.6g}\n"
        f"  P&L: <b>{pnl:+.2f}%</b>  (${pusd:+.2f})\n"
        f"  At {lev}x: <b>{pnl*lev:+.2f}%</b> on margin\n\n"
        f"Win rate: {wr} ✅"
    )
    _log({**tr,"exit_price":exit_px,"result":result,
           "pnl_pct":round(pnl,3),"pnl_usd":pusd,
           "exit_reason":"manual",
           "timestamp":datetime.now(timezone.utc).isoformat()})


def _cmd(txt, st, acct):
    parts = txt.split(); cmd = parts[0].lower()
    if cmd=="/close" and len(parts)>=2:
        sym=parts[1].upper(); tr=st.get("trades",{}).get(sym)
        if tr: send_message(f"Closing {sym}:",markup=close_kb(sym))
        else:  send_message(f"No open trade for {sym}.")
    elif cmd=="/closeall":
        for sym in list(st.get("trades",{}).keys()):
            send_message(f"Closing {sym}:",markup=close_kb(sym))
    elif cmd=="/status":
        trs=st.get("trades",{})
        if not trs: send_message("No open trades."); return
        lines=["📋 <b>Open Trades</b>"]
        for sym,t in trs.items():
            de="📈" if t["direction"]=="long" else "📉"
            lines.append(f"{de} <b>{sym}</b> {t['direction'].upper()} "
                         f"@ {t['entry']:.6g}  {t['leverage_used']}x  "
                         f"${t['size_usd']}")
        send_message("\n".join(lines))
    elif cmd=="/setlev" and len(parts)>=4:
        sym,d,lv=parts[1].upper(),parts[2].lower(),float(parts[3])
        st["setup"]={"symbol":sym,"direction":d,"leverage":lv}
        send_message(f"{sym} lev={lv}x. Size?",markup=size_kb(sym,d,lv,acct))
    elif cmd=="/setsize" and len(parts)>=5:
        sym,d,lv,amt=parts[1].upper(),parts[2].lower(),float(parts[3]),float(parts[4])
        _confirm(sym,d,lv,amt,st,acct)
    elif cmd=="/exitpx" and len(parts)>=4:
        sym,res,px=parts[1].upper(),parts[2],float(parts[3])
        _close(sym,res,px,st)
    elif cmd=="/help":
        send_message("<b>Commands</b>\n/close SYMBOL\n/closeall\n/status\n/help")


def send_checkin(open_trades, prices):
    if not open_trades: return
    lines=[f"👀 <b>Check-in</b> — {len(open_trades)} open\n"]
    for sym,t in open_trades.items():
        px=prices.get(sym)
        if not px: continue
        entry=t.get("entry",px); tp=t.get("tp",px); d=t.get("direction","long")
        pnl=(px-entry)/entry*100 if d=="long" else (entry-px)/entry*100
        prog=max(0,min(100,(px-entry)/(tp-entry)*100 if d=="long" and tp!=entry
                       else (entry-px)/(entry-tp)*100 if tp!=entry else 0))
        bar="▓"*int(prog/10)+"░"*(10-int(prog/10))
        de="📈" if d=="long" else "📉"
        lines.append(f"{'✅' if pnl>=0 else '⚠️'} <b>{sym}</b> {de} "
                     f"{pnl:+.2f}%\n   {bar} {prog:.0f}% to target")
    lines.append("\n/close SYMBOL to exit")
    send_message("\n".join(lines))


if __name__=="__main__":
    st=_ld(); print("State:",list(st.keys()))
    print("Log:",TRADE_LOG)
