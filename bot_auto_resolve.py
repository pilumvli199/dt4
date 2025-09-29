# bot_auto_resolve.py
import os
import time
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv

# try imports
try:
    from dhanhq import marketfeed
except Exception as e:
    logging.exception("dhanhq import error: %s", e)
    raise

try:
    from telegram import Bot
except Exception as e:
    logging.exception("python-telegram-bot import error: %s", e)
    raise

# optional local mapping file
try:
    from dhanhq_security_ids import INDICES_NSE, INDICES_BSE, NIFTY50_STOCKS
except Exception:
    INDICES_NSE = {"NIFTY 50": "13", "NIFTY BANK": "25"}
    INDICES_BSE = {"SENSEX": "51"}
    NIFTY50_STOCKS = {"TATAMOTORS": "3456", "RELIANCE": "2885", "TCS": "11536"}

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dhan-bot-async")

# env names: accept DHAN_ACCESS_TOKEN or DHAN_TOKEN
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

if not (DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN):
    log.warning("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN not set. Feed will likely fail until set in .env")

tg_bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# symbols to track
SYMBOLS = [
    ("NIFTY 50", "indices_nse"),
    ("NIFTY BANK", "indices_nse"),
    ("SENSEX", "indices_bse"),
    ("TATAMOTORS", "nifty50"),
    ("RELIANCE", "nifty50"),
    ("TCS", "nifty50"),
]

# ---------- resolver ----------
def resolve_symbols_list():
    instruments = []
    resolved_map = {}
    for name, kind in SYMBOLS:
        sid = None
        seg = None
        if kind == "indices_nse":
            sid = INDICES_NSE.get(name)
            seg = "NSE_INDEX"
        elif kind == "indices_bse":
            sid = INDICES_BSE.get(name)
            seg = "BSE_INDEX"
        else:
            sid = NIFTY50_STOCKS.get(name)
            seg = "NSE_EQ"
        resolved_map[name] = {"seg": seg, "sid": str(sid) if sid else None}
        if sid:
            instruments.append((seg, str(sid)))
            log.info("Resolved %s -> %s:%s", name, seg, sid)
        else:
            log.warning("Could not resolve %s", name)
    log.info("Final instruments: %s", instruments)
    return instruments, resolved_map

# ---------- LTP storage and helpers ----------
latest = {}  # name -> {"ltp": value, "raw": tick}
_last_sent = 0.0

def extract_ltp_from_tick(tick):
    if not isinstance(tick, dict):
        return None
    candidates = ["LTP", "ltp", "last_price", "lastPrice", "lastPriceTicks", "last", "lastTradedPrice"]
    for k in candidates:
        if k in tick and tick[k] is not None:
            return tick[k]
    # nested checks
    for parent in ("data", "instrument", "payload"):
        if parent in tick and isinstance(tick[parent], dict):
            for k in candidates:
                if k in tick[parent] and tick[parent][k] is not None:
                    return tick[parent][k]
    return None

# ---------- robust create_feed (tries multiple SDK signatures) ----------
def create_feed(instruments):
    """
    Try several constructor argument patterns for marketfeed.DhanFeed.
    Returns feed object. May be sync or async API.
    """
    attempts = []
    inst_tuples = list(instruments)
    inst_dicts = [{"ExchangeSegment": seg, "SecurityId": sid} for seg, sid in instruments]

    # attempt patterns
    attempts.append({"args": (), "kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN, "instruments": inst_tuples, "subscription_code": getattr(marketfeed, "Ticker", None)}})
    if hasattr(marketfeed, "FeedType"):
        try:
            ft = marketfeed.FeedType
            candidate = getattr(ft, "TICKER", getattr(ft, "Ticker", None))
            attempts.append({"args": (), "kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN, "instruments": inst_tuples, "feed_type": candidate}})
        except Exception:
            pass
    attempts.append({"args": (), "kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN, "instruments": inst_dicts, "feed_type": "TICKER"}})
    # positional fallback
    attempts.append({"args": (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, inst_tuples), "kwargs": {}})

    last_exc = None
    for idx, a in enumerate(attempts, start=1):
        try:
            args = a.get("args", ())
            kwargs = a.get("kwargs", {})
            log.debug("Trying DhanFeed ctor attempt #%d args=%s kwargs=%s", idx, args, {k: type(v).__name__ for k,v in kwargs.items()})
            feed = marketfeed.DhanFeed(*args, **kwargs)
            log.info("DhanFeed created with attempt #%d", idx)
            return feed
        except Exception as e:
            log.debug("Attempt #%d failed: %s", idx, e)
            last_exc = e
            continue
    raise last_exc or RuntimeError("Unable to construct DhanFeed")

# ---------- attach callback robustly ----------
def attach_callback(feed, callback):
    if hasattr(feed, "on_tick"):
        try:
            feed.on_tick = callback
            return True
        except Exception:
            pass
    # other possible attach methods:
    for name in ("register", "subscribe", "add_listener", "set_callback"):
        if hasattr(feed, name):
            try:
                getattr(feed, name)(callback)
                return True
            except Exception:
                pass
    # last resort: set attribute
    try:
        setattr(feed, "on_tick", callback)
        return True
    except Exception:
        return False

# ---------- callback factory ----------
def make_callback(resolved_map):
    def _cb(tick):
        try:
            # discover security id
            sid = None
            for k in ("SecurityId", "securityId", "sid", "security_id"):
                if isinstance(tick, dict) and k in tick and tick[k] is not None:
                    sid = str(tick[k]); break
            if sid is None:
                for parent in ("data", "instrument", "payload"):
                    if parent in tick and isinstance(tick[parent], dict):
                        for k in ("SecurityId", "securityId", "sid"):
                            if k in tick[parent]:
                                sid = str(tick[parent][k]); break
                        if sid: break
            if sid is None:
                return
            # find display name
            disp_name = None
            for name, info in resolved_map.items():
                if info.get("sid") == sid:
                    disp_name = name; break
            if not disp_name:
                # maybe resolved_map keys are like "NIFTY 50 (NSE_INDEX)": try prefix match
                for name, info in resolved_map.items():
                    if info.get("sid") == sid:
                        disp_name = name; break
            ltp = extract_ltp_from_tick(tick)
            latest[disp_name or sid] = {"ltp": ltp, "raw": tick}
            # throttle send below via periodic task; but allow immediate try
            return
        except Exception:
            log.exception("Error processing tick")
    return _cb

# ---------- periodic telegram sender (async) ----------
async def periodic_sender(resolved_map):
    global _last_sent
    while True:
        try:
            now = time.time()
            if now - _last_sent >= POLL_INTERVAL:
                _last_sent = now
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
                lines = [f"LTP Update • {ts}"]
                for name, info in resolved_map.items():
                    seg = info.get("seg") or ""
                    val = latest.get(name)
                    if val and val.get("ltp") is not None:
                        try:
                            ltp_f = float(val["ltp"])
                            lines.append(f"{name} ({seg}): {ltp_f:.2f}")
                        except Exception:
                            lines.append(f"{name} ({seg}): {val['ltp']}")
                    else:
                        lines.append(f"{name} ({seg}): (No Data)")
                text = "\n".join(lines)
                if tg_bot:
                    try:
                        tg_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
                        log.info("Sent Telegram update (%d lines)", len(lines))
                    except Exception:
                        log.exception("Telegram send failed")
                else:
                    log.info("No Telegram token configured — update:\n%s", text)
        except Exception:
            log.exception("Error in periodic sender")
        await asyncio.sleep(1)

# ---------- main async runner ----------
async def main_async():
    instruments, resolved_map = resolve_symbols_list()
    # create feed (may be sync or async construct; create_feed may raise)
    feed = create_feed(instruments)

    # attach callback
    cb = make_callback(resolved_map)
    ok = attach_callback(feed, cb)
    if not ok:
        log.warning("Could not attach callback to feed; proceeding but ticks may not be processed.")

    # If feed.connect is coroutine, await it and leave running;
    # otherwise if it is sync method returning None, run it in thread executor.
    connect_callable = getattr(feed, "connect", None)
    disconnect_callable = getattr(feed, "disconnect", None)

    # start periodic sender task
    sender_task = asyncio.create_task(periodic_sender(resolved_map))

    try:
        if asyncio.iscoroutinefunction(connect_callable):
            log.info("Using async feed.connect()")
            await connect_callable()
        else:
            # calling possibly synchronous connect function in thread
            log.info("Calling synchronous feed.connect() in executor")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, connect_callable)
        # at this point feed.connect likely blocks (keeps running). if it returns, continue loop.
        # keep the event loop alive until cancelled
        while True:
            await asyncio.sleep(1)
    finally:
        log.info("Shutting down feed and sender")
        try:
            if disconnect_callable:
                if asyncio.iscoroutinefunction(disconnect_callable):
                    await disconnect_callable()
                else:
                    await asyncio.get_running_loop().run_in_executor(None, disconnect_callable)
        except Exception:
            log.exception("Error during disconnect")
        try:
            sender_task.cancel()
            await sender_task
        except Exception:
            pass

# ---------- sync main wrapper ----------
def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — exiting")
    except Exception:
        log.exception("Unhandled exception in main()")

if __name__ == "__main__":
    main()
