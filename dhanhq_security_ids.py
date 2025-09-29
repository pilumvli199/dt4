# dhanhq_security_ids.py - reference mapping (sample)
INDICES_NSE = {
    "NIFTY 50": "13",
    "NIFTY BANK": "25",
    "BANKNIFTY": "25",
}
INDICES_BSE = {
    "SENSEX": "51",
}
NIFTY50_STOCKS = {
    "TATAMOTORS": "3456",
    "RELIANCE": "2885",
    "TCS": "11536",
}
def get_security_id(symbol: str):
    s = symbol.upper()
    return NIFTY50_STOCKS.get(s) or INDICES_NSE.get(s) or INDICES_BSE.get(s)
