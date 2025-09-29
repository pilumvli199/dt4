# bot_auto_resolve.py - WebSocket-based LTP bot with Telegram updates
import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

# dhanhq marketfeed import; ensure dhanhq==2.0.2 installed
try:
    from dhanhq import marketfeed
except Exception as e:
    logging.error("dhanhq SDK not found or failed to import: %s", e)
    raise

# telegram Bot
try:
    from telegram import Bot
except Exception as e:
    logging.error("python-telegram-bot not found or failed to import: %s", e)
    raise

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot_auto_resolve")

CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
ACCESS_TOKEN = os.getenv("DHAN_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

if not (CLIENT_ID and ACCESS_TOKEN and TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
    log.error("Missing required environment variables. Fill config.example.env -> .env")
    raise SystemExit(1)

tg_bot = Bot(token=TELEGRAM_TOKEN)

# symbols: map display name -> (segment, securityId)
SYMBOLS = {
    "NIFTY 50": ("NSE_INDEX", "13"),
    "NIFTY BANK": ("NSE_INDEX", "25"),
    "SENSEX": ("BSE_INDEX", "51"),
    "TATAMOTORS": ("NSE_EQ", "3456"),
    "RELIANCE": ("NSE_EQ", "2885"),
    "TCS": ("NSE_EQ", "11536"),
}

latest_data = {name: None for name in SYMBOLS.keys()}

def now_ist_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

def on_tick(tick):
    try:
        seg = tick.get("ExchangeSegment") or tick.get("Exchange")
        sid = str(tick.get("SecurityId") or tick.get("securityId") or "")
        # LTP fields may differ by SDK; try common keys
        ltp = tick.get("LTP") or tick.get("ltp") or tick.get("last_price")
        chg = tick.get("Change") or tick.get("change")
        pct = tick.get("PercentChange") or tick.get("percent_change") or tick.get("percentChange")
        for name, (s, i) in SYMBOLS.items():
            if s == seg and i == sid:
                latest_data[name] = (ltp, chg, pct, seg)
                break
    except Exception as e:
        log.error("Error processing tick: %s", e)

def send_update():
    now = now_ist_str()
    lines = [f"<b>LTP Update • {now}</b>"]
    for name, val in latest_data.items():
        if not val or val[0] is None:
            lines.append(f"{name}: (No Data)")
        else:
            ltp, chg, pct, seg = val
            try:
                ltp_s = f"{float(ltp):.2f}"
            except:
                ltp_s = str(ltp)
            try:
                chg_s = f"{float(chg):+.2f}"
            except:
                chg_s = str(chg) if chg is not None else "0.00"
            try:
                pct_s = f"{float(pct):+.2f}%"
            except:
                pct_s = str(pct)
            lines.append(f"{name} ({seg}): {ltp_s} ({chg_s}, {pct_s})")
    text = "\n".join(lines)
    try:
        tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")
        log.info("Telegram update sent.")
    except Exception as e:
        log.error("Failed to send Telegram message: %s", e)

def main():
    log.info("Starting WebSocket LTP Bot...")
    instruments = list(SYMBOLS.values())
    # DhanFeed expects list of tuples (ExchangeSegment, SecurityId)
    feed = marketfeed.DhanFeed(
        client_id=CLIENT_ID,
        access_token=ACCESS_TOKEN,
        instruments=instruments,
        subscription_code=marketfeed.Ticker,
    )
    feed.on_tick = on_tick
    feed.connect()
    try:
        while True:
            send_update()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        try:
            feed.disconnect()
        except:
            pass

if __name__ == "__main__":
    main()
