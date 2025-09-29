import os
import logging
import time
from datetime import datetime
from dhanhq import marketfeed
from telegram import Bot
from dhanhq_security_ids import INDICES_NSE, INDICES_BSE, NIFTY50_STOCKS

# --------------------------------
# Logging Setup
# --------------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

# --------------------------------
# Env Vars
# --------------------------------
client_id = os.getenv("DHAN_CLIENT_ID")
access_token = os.getenv("DHAN_ACCESS_TOKEN")
telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=telegram_token)

# --------------------------------
# Symbols to track (user-defined)
# --------------------------------
SYMBOLS = [
    ("NIFTY 50", "indices_nse"),
    ("NIFTY BANK", "indices_nse"),
    ("SENSEX", "indices_bse"),
    ("TATAMOTORS", "nifty50"),
    ("RELIANCE", "nifty50"),
    ("TCS", "nifty50"),
]

# --------------------------------
# Helper - Resolve Security IDs
# --------------------------------
def resolve_symbols():
    payload = []
    resolved_map = {}

    for symbol, stype in SYMBOLS:
        try:
            if stype == "indices_nse":
                sid = INDICES_NSE.get(symbol)
                seg = "NSE_INDEX"
            elif stype == "indices_bse":
                sid = INDICES_BSE.get(symbol)
                seg = "BSE_INDEX"
            else:  # default nifty50
                sid = NIFTY50_STOCKS.get(symbol)
                seg = "NSE_EQ"

            if sid:
                payload.append((seg, sid))
                resolved_map[f"{symbol} ({seg})"] = sid
                logging.info(f"Resolved {symbol} -> {seg}:{sid}")
            else:
                resolved_map[f"{symbol}"] = None
                logging.warning(f"{symbol} not found in dict")
        except Exception as e:
            logging.error(f"Error resolving {symbol}: {e}")
            resolved_map[f"{symbol}"] = None

    logging.info(f"Final Instruments: {payload}")
    return payload, resolved_map

# --------------------------------
# WebSocket Callback
# --------------------------------
ltp_data = {}
resolved_payload, resolved_map = resolve_symbols()

def on_tick(tick):
    try:
        sid = str(tick.get("securityId"))
        ltp = tick.get("lastPrice")

        # Find symbol name
        symbol_name = None
        for k, v in resolved_map.items():
            if v == sid:
                symbol_name = k
                break

        if not symbol_name:
            return

        if ltp is not None:
            ltp_data[symbol_name] = f"{ltp}"
        else:
            ltp_data[symbol_name] = "(No Data)"

        # Format & Send update every tick
        send_update()
    except Exception as e:
        logging.error(f"Tick error: {e}")

# --------------------------------
# Telegram Send
# --------------------------------
last_sent = 0
def send_update():
    global last_sent
    now = time.time()

    # Limit updates: send every ~60s
    if now - last_sent < 60:
        return
    last_sent = now

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    msg_lines = [f"LTP Update • {ts}"]

    for symbol, _ in SYMBOLS:
        match = [k for k in ltp_data.keys() if k.startswith(symbol)]
        if match:
            msg_lines.append(f"{match[0]}: {ltp_data.get(match[0], '(No Data)')}")
        else:
            seg = "?"  # unknown
            msg_lines.append(f"{symbol} ({seg}): (No Data)")

    msg = "\n".join(msg_lines)
    logging.info(f"Telegram Msg:\n{msg}")

    try:
        bot.send_message(chat_id=telegram_chat_id, text=msg)
    except Exception as e:
        logging.error(f"Telegram send failed: {e}")

# --------------------------------
# WebSocket Runner
# --------------------------------
def run_feed():
    instruments = [(1, sid) for seg, sid in resolved_payload]  # 1 = NSE_EQ, etc.
    feed = marketfeed.DhanFeed(
        client_id=client_id,
        access_token=access_token,
        instruments=instruments,
        feed_type=marketfeed.FeedType.TICKER
    )
    feed.on_tick = on_tick
    logging.info("Starting WebSocket LTP Bot...")
    feed.connect()

if __name__ == "__main__":
    while True:
        try:
            run_feed()
        except Exception as e:
            logging.error(f"WebSocket crashed: {e}, retrying in 5s...")
            time.sleep(5)
