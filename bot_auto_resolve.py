# bot_auto_resolve.py
import os
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

# try imports
try:
    from dhanhq import marketfeed
except Exception as e:
    logging.exception("dhanhq import error (install dhanhq>=1.x/2.x): %s", e)
    raise

# telegram import
try:
    from telegram import Bot
except Exception as e:
    logging.exception("python-telegram-bot import error: %s", e)
    raise

# local security id map (ensure this file exists)
try:
    from dhanhq_security_ids import INDICES_NSE, INDICES_BSE, NIFTY50_STOCKS
except Exception:
    # fallback minimal map if file missing
    INDICES_NSE = {"NIFTY 50": "13", "NIFTY BANK": "25"}
    INDICES_BSE = {"SENSEX": "51"}
    NIFTY50_STOCKS = {"TATAMOTORS": "3456", "RELIANCE": "2885", "TCS": "11536"}

load_dotenv()

# ---------- config ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dhan-bot")

DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN") or os.getenv("DHAN_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

if not (DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN):
    log.error("Missing DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN in environment.")
    # do not exit here to allow dev to import module for inspection

tg_bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ---------- symbols (edit as needed) ----------
SYMBOLS = [
    ("NIFTY 50", "indices_nse"),
    ("NIFTY BANK", "indices_nse"),
    ("SENSEX", "indices_bse"),
    ("TATAMOTORS", "nifty50"),
    ("RELIANCE", "nifty50"),
    ("TCS", "nifty50"),
]

# ---------- resolve helpers ----------
def resolve_symbols_list():
    """
    Returns:
      instruments_raw: list of (seg, sid) strings as returned by scrip
      resolved_map: mapping display_name -> sid
    """
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
        if sid:
            instruments.append((seg, str(sid)))
            resolved_map[name] = {"seg": seg, "sid": str(sid)}
            log.info("Resolved %s -> %s:%s", name, seg, sid)
        else:
            resolved_map[name] = {"seg": None, "sid": None}
            log.warning("Could not resolve %s", name)
    return instruments, resolved_map

# ---------- LTP storage ----------
latest = {}  # display_name -> (ltp, change, pct, seg)
_last_sent = 0.0

# ---------- utility to extract LTP from tick with many possible keys ----------
def extract_ltp_from_tick(tick):
    # support many key names that different SDKs might use
    candidates = [
        "LTP", "ltp", "last_price", "lastPrice", "lastPriceTicks", "last"
    ]
    for k in candidates:
        if k in tick and tick[k] is not None:
            return tick[k]
    # sometimes data nested
    if "data" in tick and isinstance(tick["data"], dict):
        for k in candidates:
            if k in tick["data"] and tick["data"][k] is not None:
                return tick["data"][k]
    # fallback None
    return None

# ---------- robust feed creator (tries multiple SDK signatures) ----------
def create_feed(instruments):
    """
    Tries different constructor signatures and constants for dhanhq.marketfeed.DhanFeed.
    Returns constructed feed object or raises exception.
    """
    log.info("Creating DhanFeed for instruments: %s", instruments)

    # Try several instrument formats:
    # A) list of tuples like ("NSE_EQ", "1333")
    # B) list of dicts like {"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}
    inst_tuples = list(instruments)
    inst_dicts = [{"ExchangeSegment": seg, "SecurityId": sid} for seg, sid in instruments]

    # candidate kwargs patterns to try
    attempts = []

    # 1) older style: subscription_code
    attempts.append({"kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN,
                                "instruments": inst_tuples, "subscription_code": getattr(marketfeed, "Ticker", None)}})

    # 2) newer style: feed_type = marketfeed.FeedType.TICKER
    if hasattr(marketfeed, "FeedType"):
        ft = getattr(marketfeed, "FeedType")
        # try FeedType enum attribute names
        chosen = None
        for name in ("TICKER", "Ticker", "TickerFeed"):
            if hasattr(ft, name):
                chosen = getattr(ft, name)
                break
        if chosen is None:
            # maybe FeedType has .TICKER attribute directly
            chosen = getattr(ft, "TICKER", None)
        attempts.append({"kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN,
                                    "instruments": inst_tuples, "feed_type": chosen}})

    # 3) older style feed_type with string value
    attempts.append({"kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN,
                                "instruments": inst_tuples, "feed_type": "TICKER"}})

    # 4) try using inst_dicts and feed_type if SDK expects dict instruments
    attempts.append({"kwargs": {"client_id": DHAN_CLIENT_ID, "access_token": DHAN_ACCESS_TOKEN,
                                "instruments": inst_dicts, "feed_type": "TICKER"}})

    # 5) last resort: positional args (client_id, access_token, instruments)
    attempts.append({"args": (DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, inst_tuples), "kwargs": {}})

    last_exc = None
    for idx, a in enumerate(attempts):
        try:
            args = a.get("args", ())
            kwargs = a.get("kwargs", {})
            log.debug("Trying DhanFeed constructor attempt #%d: args=%s kwargs=%s", idx+1, args, {k: type(v).__name__ for k,v in kwargs.items()})
            feed = marketfeed.DhanFeed(*args, **kwargs)
            log.info("DhanFeed created with attempt #%d", idx+1)
            return feed
        except Exception as e:
            log.debug("Attempt #%d failed: %s", idx+1, e)
            last_exc = e
            continue
    # if all attempts failed raise last exception
    log.error("All DhanFeed constructor attempts failed. Last error: %s", last_exc)
    raise last_exc or RuntimeError("Unable to construct DhanFeed")

# ---------- on_tick conversion (wraps feed callback) ----------
def make_on_tick_callback(resolved_map):
    def _on_tick(tick):
        try:
            # Support various tick shapes: dict keys and nested
            # Find security id from tick
            sid = None
            for k in ("SecurityId", "securityId", "SecurityID", "securityID", "security_id", "sid"):
                if k in tick and tick[k] is not None:
                    sid = str(tick[k])
                    break
            # sometimes nested under "instrument" or "data"
            if sid is None:
                if "instrument" in tick and isinstance(tick["instrument"], dict):
                    for k in ("securityId", "SecurityId", "sid"):
                        if k in tick["instrument"]:
                            sid = str(tick["instrument"][k])
                            break
            if sid is None and "data" in tick and isinstance(tick["data"], dict):
                for k in ("securityId", "SecurityId", "sid"):
                    if k in tick["data"]:
                        sid = str(tick["data"][k])
                        break

            if sid is None:
                # cannot identify security id
                return

            # find display name by sid
            disp = None
            for name, info in resolved_map.items():
                if info.get("sid") == sid:
                    disp = name
                    break

            if not disp:
                # maybe resolved_map stores names differently - try partial match
                for name, info in resolved_map.items():
                    if info.get("sid") == sid:
                        disp = name
                        break

            ltp = extract_ltp_from_tick(tick)

            if disp:
                if ltp is not None:
                    latest[disp] = {"ltp": ltp, "raw": tick}
                else:
                    latest[disp] = {"ltp": None, "raw": tick}
                # send throttled update
                _maybe_send_telegram_update()
        except Exception as e:
            log.exception("Error in tick callback: %s", e)
    return _on_tick

# ---------- telegram send (throttled) ----------
def _maybe_send_telegram_update():
    global _last_sent
    now = time.time()
    if now - _last_sent < max(10, POLL_INTERVAL):  # ensure at least POLL_INTERVAL between sends (POLL_INTERVAL default 60)
        return
    _last_sent = now

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    lines = [f"LTP Update • {ts}"]
    for name, info in resolved_map.items():
        # resolved_map keys are display names we used originally (like "NIFTY 50")
        sid = info.get("sid")
        seg = info.get("seg") or ""
        # find latest by display name (we stored by original name)
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
            log.info("Telegram update sent (%d lines).", len(lines))
        except Exception as e:
            log.exception("Failed to send Telegram message: %s", e)
    else:
        log.info("Telegram token not configured; skipping send. Message would be:\n%s", text)

# ---------- main runner ----------
def main():
    global resolved_map
    instruments, resolved_map = resolve_symbols_list()
    # build instruments list in a format to pass to SDK (we'll try multiple patterns in create_feed)
    try:
        feed = create_feed(instruments)
    except Exception as e:
        log.exception("Could not create feed: %s", e)
        raise

    # attach callback using robust wrapper
    callback = make_on_tick_callback(resolved_map)
    try:
        # many SDKs use feed.on_tick or feed.register or feed.set_on_tick
        if hasattr(feed, "on_tick"):
            feed.on_tick = callback
        elif hasattr(feed, "register"):
            feed.register("tick", callback)
        elif hasattr(feed, "subscribe"):
            feed.subscribe(callback)
        else:
            log.warning("Feed object has no known attach method; attempting attribute assignment 'on_tick'")
            try:
                setattr(feed, "on_tick", callback)
            except Exception:
                log.error("Couldn't attach callback to feed.")
                raise

        log.info("Connecting feed...")
        feed.connect()
        # block forever; feed will call our callback
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted, disconnecting feed...")
        try:
            feed.disconnect()
        except Exception:
            pass
    except Exception as e:
        log.exception("Feed runtime error: %s", e)
        try:
            feed.disconnect()
        except Exception:
            pass
        raise

# allow importing module without running
if __name__ == "__main__":
    main()
