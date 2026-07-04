# bot.py (PRODUCTION FLATTRADE AUTHENTICATION RE-ENGINEERED)
import json
import os
import time
import logging
import requests
import pyotp
import hashlib  # Matrix crypto library for Flattrade
import pandas as pd
from datetime import datetime
import zoneinfo
from google import genai 
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt

import watchlist
from NorenRestApiPy.NorenApi import NorenApi

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==============================================================================
# ENVIRONMENT VARIABLES DUAL-USE PARSING SYSTEM
# ==============================================================================
# Priority 1: Check if running on Cloud Containers (Railway)
if os.environ.get("CLIENT_CODE"):
    CLIENT_CODE = os.environ.get("CLIENT_CODE")
    PASSWORD = os.environ.get("PASSWORD")
    API_KEY = os.environ.get("API_KEY")
    TOTP_TOKEN = os.environ.get("TOTP_TOKEN")
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    logging.info("☁️ RAILWAY CONTAINER METRICS: Loaded cleanly from platform variables vault.")
else:
    # Priority 2: Fall back to local config for debugging
    try:
        import config
        CLIENT_CODE = config.CLIENT_CODE
        PASSWORD = config.PASSWORD
        API_KEY = config.API_KEY
        TOTP_TOKEN = config.TOTP_TOKEN
        TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
        TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID
        GEMINI_API_KEY = config.GEMINI_API_KEY
        logging.info("💻 LOCAL ENVIRONMENT METRICS: config.py file parsed cleanly.")
    except ModuleNotFoundError:
        logging.error("❌ CRITICAL: Configuration variables not found on platform or local workspace.")
        raise RuntimeError("No environment credentials specified.")

TOKEN_TO_TICKER = {str(v): k for k, v in watchlist.WATCHLIST.items()}
DATA_CACHE = {}
STRATEGY_STATES = {ticker: "READY" for ticker in watchlist.WATCHLIST.keys()}

CASH_CAPITAL_PER_TRADE = 7000.0   
LEVERAGE_MULTIPLIER = 5.0          
VOLUME_MA_PERIOD = 20              

class FlattradeBotEngine(NorenApi):
    def __init__(self):
        # Initialize precisely with the verified PiConnect endpoints
        NorenApi.__init__(self, 
                          host='https://piconnect.flattrade.in/PiConnectAPI/', 
                          websocket='wss://piconnect.flattrade.in/PiConnectWSAPI/')

# ==============================================================================
# STRATEGY GRAPHIC ENGINE & ANALYSIS CORRIDORS
# ==============================================================================
def create_live_signal_chart(ticker, trade_type, full_df, timestamp_str):
    try:
        os.makedirs("static/chart_storage", exist_ok=True)
        day_df = full_df.tail(78).copy().reset_index(drop=True) if len(full_df) >= 78 else full_df.copy().reset_index(drop=True)
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={'height_ratios': [2.5, 1]})
        
        for idx, row in day_df.iterrows():
            color = '#26a69a' if row['close'] >= row['open'] else '#ef5350'
            ax1.vlines(idx, row['low'], row['high'], colors=color, linewidth=1.2)
            ax1.vlines(idx, row['open'], row['close'], colors=color, linewidth=5)

        signal_loc = len(day_df) - 1
        entry_row = day_df.iloc[signal_loc]
        marker_color = '#2ecc71' if trade_type == "BUY" else '#e74c3c'
        ax1.scatter(signal_loc, entry_row['close'], color=marker_color, s=150, zorder=5, marker='^' if trade_type == "BUY" else 'v')
        
        ax1.set_title(f"{ticker} - FLATTRADE 5M HORIZON", color='white', fontsize=12, weight='bold', loc='left')
        ax1.set_ylabel("Price (INR)", color='#b2b5be')
        ax1.grid(True, color='#2a2e39', linestyle=':', alpha=0.6)
        ax1.set_facecolor('#131722')

        ax2.plot(day_df.index, day_df['RSI'], color='#29b6f6', label='RSI (14)', linewidth=1.5)
        ax2.plot(day_df.index, day_df['RSI_SMA'], color='#ffeb3b', label='RSI SMA', linewidth=1.2)
        ax2.axhline(70, color='#ef5350', linestyle='--', alpha=0.5)
        ax2.axhline(30, color='#26a69a', linestyle='--', alpha=0.5)
        ax2.set_facecolor('#131722')
        ax2.set_ylim(10, 90)

        last_date_seen = None
        for idx, row in day_df.iterrows():
            current_date = str(row['timestamp']).split(' ')[0]
            if last_date_seen and current_date != last_date_seen:
                ax1.axvline(x=idx, color='#ffeb3b', linestyle='--', alpha=0.6)
                ax2.axvline(x=idx, color='#ffeb3b', linestyle='--', alpha=0.6)
            last_date_seen = current_date

        x_ticks = range(0, len(day_df), max(1, len(day_df)//6))
        ax2.set_xticks(x_ticks)
        ax2.set_xticklabels([str(day_df.iloc[t]['timestamp'])[5:] for t in x_ticks], color='#b2b5be', fontsize=8)

        fig.patch.set_facecolor('#1c2030')
        clean_time = timestamp_str.replace(":", "-")
        dashboard_path = f"static/chart_storage/{ticker.upper()}_{clean_time}.png"
        
        plt.tight_layout()
        plt.savefig(dashboard_path, facecolor=fig.get_facecolor(), edgecolor='none', dpi=120)
        plt.close(fig)
        return dashboard_path
    except Exception as e:
        logging.error(f"❌ Chart generation fault: {str(e)}")
        return None

def generate_ai_advisor_analysis(ticker_name, intraday_df, volume_summary):
    if not GEMINI_API_KEY: return "⚠️ *AI ADVISOR VERDICT: ACCESSIBILITY KEY UNSET*"
    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    today_str = datetime.now(ist_zone).strftime("%Y-%m-%d")
    today_candles = intraday_df[intraday_df['timestamp'].str.startswith(today_str)].tail(35)
    
    candle_history_text = ""
    for _, row in today_candles.iterrows():
        candle_history_text += f"T: {row['timestamp']} | C: {row['close']} | RSI: {round(row.get('RSI',0),2)} | SMA: {round(row.get('RSI_SMA',0),2)}\n"

    prompt = (
        f"You are an elite institutional risk officer. Analyze the technical layout for {ticker_name}.\n"
        f"Metrics: {volume_summary}\nData Matrix:\n{candle_history_text}\n"
        f"Provide exactly this template layout and nothing else:\n"
        f"🧠 *AI ADVISOR VERDICT:* [VALIDATED ENTRY] or [⚠️ ADVISE TO AVOID]\n"
        f"────────────────────────\n"
        f"📖 *ADVISORY ANALYSIS:*\n"
        f"• *Trend Context:* (1 short sentence mapping direction)\n"
        f"• *Volume Metrics:* (1 short sentence validating spike status)\n"
        f"• *Momentum Level:* (1 short sentence evaluating boundary exhaustion)"
    )
    try:
        local_ai_client = genai.Client(api_key=GEMINI_API_KEY)
        response = local_ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        return response.text.strip()
    except Exception as e:
        return f"⚠️ *AI ADVISOR VERDICT: TRANSCRIPTION FAULT* ({str(e)})"

def send_telegram_alert(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=5)
    except Exception as e: logging.error(f"Telegram alert delivery failure: {e}")

def check_for_signals(ticker, is_live=False, current_bid_depth=150000, current_ask_depth=120000):
    df = DATA_CACHE.get(ticker)
    if df is None or len(df) < 30: return

    df['Vol_SMA'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()
    change = df['close'].diff()
    gain = change.mask(change < 0, 0)
    loss = -change.mask(change > 0, 0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI_SMA'] = df['RSI'].rolling(window=14).mean()
    
    latest_bar = df.iloc[-1]
    previous_bar = df.iloc[-2]
    current_rsi = latest_bar['RSI']
    current_state = STRATEGY_STATES.get(ticker, "READY")
    
    if 30 <= current_rsi <= 70 and current_state != "READY":
        STRATEGY_STATES[ticker] = "READY"
        logging.info(f"🔄 {ticker} RSI returned to center zone. Strategy engine reset.")

    if not is_live: return

    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // latest_bar['close'])
    timestamp_str = latest_bar['timestamp']

    if current_rsi < 30 and current_state == "READY":
        if previous_bar['RSI'] <= previous_bar['RSI_SMA'] and latest_bar['RSI'] > latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_BUY"
            create_live_signal_chart(ticker, "BUY", df, timestamp_str)
            ai_block = generate_ai_advisor_analysis(ticker, df, f"Vol: {latest_bar['volume']}")
            
            payload = {
                "stock_name": ticker, "signal_type": "BUY", "entry_price": float(latest_bar['close']),
                "quantity": trade_quantity, "take_profit": round(latest_bar['close']*1.005, 2),
                "stop_loss": round(latest_bar['close']*0.995, 2), "timestamp": timestamp_str,
                "ai_verdict": "BUY_ENTRY", "ai_analysis": ai_block,
                "total_buy_qty": current_bid_depth, "total_sell_qty": current_ask_depth
            }
            requests.post("http://127.0.0.1:8000/api/webhook/alert", json=payload, timeout=5)
            send_telegram_alert(f"📊 *FLATTRADE ALERT DETECTED*\nAsset: {ticker}\nSignal: BUY ENTRY @ ₹{latest_bar['close']}")

    elif current_rsi > 70 and current_state == "READY":
        if previous_bar['RSI'] >= previous_bar['RSI_SMA'] and latest_bar['RSI'] < latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_SHORT"
            create_live_signal_chart(ticker, "SHORT", df, timestamp_str)
            ai_block = generate_ai_advisor_analysis(ticker, df, f"Vol: {latest_bar['volume']}")
            
            payload = {
                "stock_name": ticker, "signal_type": "SHORT", "entry_price": float(latest_bar['close']),
                "quantity": trade_quantity, "take_profit": round(latest_bar['close']*0.995, 2),
                "stop_loss": round(latest_bar['close']*1.005, 2), "timestamp": timestamp_str,
                "ai_verdict": "SHORT_ENTRY", "ai_analysis": ai_block,
                "total_buy_qty": current_bid_depth, "total_sell_qty": current_ask_depth
            }
            requests.post("http://127.0.0.1:8000/api/webhook/alert", json=payload, timeout=5)
            send_telegram_alert(f"📊 *FLATTRADE ALERT DETECTED*\nAsset: {ticker}\nSignal: SHORT SELL @ ₹{latest_bar['close']}")

class CandleAggregator:
    def __init__(self, ticker):
        self.ticker = ticker
        self.current_candle_time = None
        self.open, self.high, self.low, self.close = None, None, None, None
        self.volume = 0

    def handle_tick(self, price, volume_delta, bid_qty=150000, ask_qty=120000):
        if self.ticker not in DATA_CACHE:
            DATA_CACHE[self.ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist_zone)
        bracket_minute = (now_ist.minute // 5) * 5
        tick_candle_time = now_ist.replace(minute=bracket_minute, second=0, microsecond=0)

        if self.current_candle_time is None or tick_candle_time > self.current_candle_time:
            self.current_candle_time = tick_candle_time
            self.open = price
            self.high = price
            self.low = price
            self.volume = 0

        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume_delta

        new_row = {
            "timestamp": self.current_candle_time.strftime("%Y-%m-%d %H:%M"),
            "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume
        }
        
        df = DATA_CACHE[self.ticker]
        if not df.empty and df.iloc[-1]['timestamp'] == new_row['timestamp']:
            df.iloc[-1] = new_row
        else:
            DATA_CACHE[self.ticker] = pd.concat([df, pd.DataFrame([new_row])]).reset_index(drop=True)
            
        check_for_signals(self.ticker, is_live=True, current_bid_depth=bid_qty, current_ask_depth=ask_qty)

# ==============================================================================
# ENGINE MAIN EXECUTIVE FRAMEWORK
# ==============================================================================
def start_flattrade_system():
    # Instantiate the engine API globally at the start of the function scope
    api = FlattradeBotEngine()
    
    # 1. Generate dynamic 2FA TOTP token text string
    totp_generator = pyotp.TOTP(TOTP_TOKEN)
    raw_totp = str(totp_generator.now())  
    logging.info(f"🔑 Generating real-time direct handshake for client: {CLIENT_CODE}")
    
    # 2. Compute explicit SHA-256 Hashing required for Flattrade QuickAuth web validation
    hashed_password = hashlib.sha256(PASSWORD.encode('utf-8')).hexdigest()
    raw_secret_combo = f"{API_KEY}{CLIENT_CODE}"
    hashed_api_secret = hashlib.sha256(raw_secret_combo.encode('utf-8')).hexdigest()
    
    # 3. Construct raw dictionary parameters using specific internal keys
    payload = {
        "apkversion": "1.0.0",
        "uid": CLIENT_CODE,
        "pwd": hashed_password,       # Pre-hashed password for direct endpoint parsing
        "factor2": raw_totp,          
        "vc": "FTB2C",               
        "appkey": hashed_api_secret,
        "imei": "00-00-00-00-00-00",
        "source": "API"
    }
    
    url = "https://piconnect.flattrade.in/PiConnectAPI/QuickAuth"
    
    # 4. 🌟 MANUALLY FORMAT AN UNENCODED BODY STRING
    # This prevents the library from URL-encoding the JSON punctuation characters
    clean_json_string = json.dumps(payload, separators=(',', ':'))
    raw_unencoded_body = f"jData={clean_json_string}"
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    try:
        logging.info("📡 Firing raw string stream bypass request to Flattrade gatekeeper...")
        # Send the string directly as raw data bytes to preserve structural quotes and colons
        response = requests.post(url, data=raw_unencoded_body.encode('utf-8'), headers=headers, timeout=10)
        
        logging.info(f"💾 Raw Server HTTP Status Code: {response.status_code}")
        logging.info(f"📡 Raw Server Text Feedback: {response.text}")
        
        login_response = response.json()
    except Exception as err:
        logging.error(f"❌ Network channel connection failure: {str(err)}")
        return

    if login_response and login_response.get('stat') == 'Ok':
        logging.info("🚀 FLATTRADE DIRECT AUTHENTICATION SUCCESSFUL. SAVING MATRIX STATE.")
        # Initialize the session tokens directly inside the wrapper
        api.set_session(userid=CLIENT_CODE, token=login_response.get('susertoken'), user_data=login_response)
    else:
        error_msg = login_response.get('emsg') if isinstance(login_response, dict) else "Malformed payload data received."
        logging.error(f"❌ Flattrade gateway authentication rejected. Server feedback: {error_msg}")
        return

    # Seed baseline historical data sets 
    for ticker in watchlist.WATCHLIST.keys():
        DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        logging.info(f"📈 Seeding real-time metric arrays for tracking asset vector: {ticker}")

    global LIVE_ENGINES
    LIVE_ENGINES = {ticker: CandleAggregator(ticker) for ticker in watchlist.WATCHLIST.keys()}

    def event_handler_feed(msg):
        if msg.get('t') == 'tf': 
            token = msg.get('tk')
            ticker = TOKEN_TO_TICKER.get(str(token))
            if ticker and ticker in LIVE_ENGINES:
                price = float(msg.get('lp', 0))
                volume_delta = int(msg.get('v', 0))
                bid_depth = float(msg.get('tbq', 160000))
                ask_depth = float(msg.get('tsq', 130000))
                if price > 0:
                    LIVE_ENGINES[ticker].handle_tick(price, volume_delta, bid_depth, ask_depth)

    def open_callback():
        for ticker, token in watchlist.WATCHLIST.items():
            api.subscribe(f"NSE|{token}")
        logging.info("📡 Live WebSocket matrix pipeline successfully established.")

    api.start_websocket(subscribe_callback=event_handler_feed, socket_open_callback=open_callback)
    
    while True:
        time.sleep(1)
        
if __name__ == "__main__":
    start_flattrade_system()
