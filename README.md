# Varam-Dynamics

CPR breakout scanner for top 50 Hyperliquid assets.
Sends ranked Telegram alerts with 6-component scoring system.
Automatically paper trades all signals across 3 exit styles.

## Setup (15 minutes)

### 1. Create Telegram Bot
1. Open Telegram → search **@BotFather**
2. Send `/newbot` → follow prompts → copy the **BOT_TOKEN**
3. Send `/start` to your new bot
4. Open `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
5. Copy your **chat_id** from the response

### 2. Create GitHub Repository
```
New repo: varam-dynamics-bot
Visibility: Public (unlimited free Actions minutes)
```

### 3. Add GitHub Secrets
Go to: Settings → Secrets and variables → Actions → New secret

| Secret | Value |
|--------|-------|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat ID from getUpdates |
| `ACCOUNT_SIZE` | `200` (your account size in USD) |

### 4. Push code
```powershell
cd "D:\Varam Dynamics"
git init
git remote add origin https://github.com/YOUR_USERNAME/varam-dynamics-bot.git
git add .
git commit -m "Initial: Varam-Dynamics scanner"
git push -u origin main
```

### 5. Test Telegram
```
Actions → Run workflow → select "Varam-Dynamics Scanner" → Run
```
Or locally (with secrets set as env vars):
```powershell
$env:TELEGRAM_BOT_TOKEN="your_token"
$env:TELEGRAM_CHAT_ID="your_chat_id"
$env:ACCOUNT_SIZE="200"
python main.py setup
```

## How it works

**Every 2 hours:**
1. Fetches candles for 50 assets × 6 timeframes from Hyperliquid
2. Calculates CPR levels and detects TC/BC breakouts
3. Scores each signal (6 components, out of 10)
4. Sends top signals to Telegram with full breakdown
5. Opens paper positions (3 exit styles per signal)

**Every hour:**
1. Updates open paper positions with current prices
2. Closes positions that hit SL/TP
3. Sends Telegram notification on close

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/trade SYMBOL DIR ENTRY SL LEV SIZE` | Log your manual trade |
| `/close SYMBOL EXIT_PRICE RESULT` | Record trade outcome |
| `/status` | Show open paper positions |
| `/skip` | Log that you skipped the signals |
| `/help` | Show all commands |

**Example:**
```
/trade DOGE long 0.0943 0.0921 15 50
/close DOGE 0.0978 win
```

## Scoring System (out of 10)

| Component | Max | What it measures |
|-----------|-----|-----------------|
| TF Confluence | 3.0 | How many timeframes agree |
| CPR Width | 2.0 | Narrower = stronger magnet |
| Volume | 1.5 | Above average = real breakout |
| Breakout Strength | 1.5 | How far beyond TC/BC |
| Risk:Reward | 1.0 | Distance to target vs SL |
| Liquidity | 1.0 | Asset tier quality |
| Lower TF Bonus | +0.5 | 15m/30m supports trade |

**Risk levels:**
- 🟢 8.0–10.0 → Low Risk
- 🟡 6.0–7.9  → Low-Medium Risk
- 🟠 4.0–5.9  → Medium Risk
- 🔴 < 4.0    → Not sent

## Paper Trade Exit Styles

Three parallel paper positions per signal:

| Style | Logic |
|-------|-------|
| Fixed % | Exit at +10%, stop at -7% |
| CPR Target | Exit at R1/S1 (green line) |
| Trailing | Move SL to entry at +5%, trail 2% |

After 30+ signals, the data shows which exit style works best.

---
*Part of the Varam trading system. Sister project to Varam CPR 4h bot.*
