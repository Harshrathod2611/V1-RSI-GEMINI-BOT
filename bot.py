# bot.py
import os
import time
import logging
import requests
import pyotp
import pandas as pd
from datetime import datetime, timedelta
import zoneinfo
from google import genai 
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt

import watchlist
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# ==============================================================================
# ENVIRONMENT VARIABLE CONFIGURATION MAPPER
# ==============================================================================
try:
    import config
    API_KEY = config.API_KEY
    CLIENT_CODE = config.CLIENT_CODE
    PASSWORD = config.PASSWORD
    TOTP_TOKEN = config.TOTP_TOKEN
    TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID
    GEMINI_API_KEY = config.GEMINI_API_KEY
    logging.info("💻 Local config.py file loaded cleanly.")
except ModuleNotFoundError:
    API_KEY = os.environ.get("API_KEY")
    CLIENT_CODE = os.environ.get("CLIENT_CODE")
    PASSWORD = os.environ.get("PASSWORD")
    TOTP_TOKEN = os.environ.get("TOTP_TOKEN")
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    logging.info("☁️ Operating inside cloud container. Vault Variables loaded cleanly.")

TOKEN_TO_TICKER = {v: k for k, v in watchlist.WATCHLIST.items()}
DATA_CACHE = {}

# 🟢 FIXED (Issue #6): Strict tracking structures to suppress boundary fake whipsaws
STRATEGY_STATES = {ticker: "READY" for ticker in watchlist.WATCHLIST.keys()}

CASH_CAPITAL_PER_TRADE = 7000.0   
LEVERAGE_MULTIPLIER = 5.0          
PROFIT_TARGET_PERCENT = 0.005     
VOLUME_MA_PERIOD = 20              

# ==============================================================================
# HEADLESS LIVE CHART GENERATOR (Issue #10 Dashboard File Sync Built-in)
# ==============================================================================
def create_live_signal_chart(ticker, trade_type, full_df, timestamp_str):
    """
    Generates a professional 2-panel chart showing exactly 78 candles 
    and writes it directly to both local workspace and central dashboard storage.
    """
    try:
        # Ensure target asset paths exist globally across both execution spaces
        os.makedirs("charts", exist_ok=True)
        os.makedirs("static/chart_storage", exist_ok=True)
        
        if len(full_df) >= 78:
            day_df = full_df.tail(78).copy().reset_index(drop=True)
        else:
            day_df = full_df.copy().reset_index(drop=True)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, 
                                       gridspec_kw={'height_ratios': [2.5, 1]})
        
        # PANEL 1: CANDLESTICKS
        for idx, row in day_df.iterrows():
            color = '#26a69a' if row['close'] >= row['open'] else '#ef5350'
            ax1.vlines(idx, row['low'], row['high'], colors=color, linewidth=1.2)
            body_line = ax1.vlines(idx, row['open'], row['close'], colors=color, linewidth=5)
            try: body_line.set_capstyle('round')
            except AttributeError: pass

        signal_loc = len(day_df) - 1
        entry_row = day_df.iloc[signal_loc]
        marker_color = '#2ecc71' if trade_type == "BUY" else '#e74c3c'
        ax1.scatter(signal_loc, entry_row['close'], color=marker_color, s=150, zorder=5, marker='^' if trade_type == "BUY" else 'v')
        ax1.annotate(f"  {trade_type} Signal Trigger\n  INR {entry_row['close']}", (signal_loc, entry_row['close']), 
                     color='white', weight='bold', fontsize=8, bbox=dict(facecolor=marker_color, alpha=0.8, boxstyle='round,pad=0.3'))

        ax1.set_title(f"{ticker} - 5M LIVE SIGNAL SNAPSHOT (78-CANDLE HORIZON)", color='white', fontsize=12, weight='bold', loc='left')
        ax1.set_ylabel("Stock Price (INR)", color='#b2b5be')
        ax1.grid(True, color='#2a2e39', linestyle=':', alpha=0.6)
        ax1.set_facecolor('#131722')

        # PANEL 2: RSI MATRICES ONLY
        ax2.plot(day_df.index, day_df['RSI'], color='#29b6f6', label='RSI (14)', linewidth=1.5)
        ax2.plot(day_df.index, day_df['RSI_SMA'], color='#ffeb3b', label='RSI SMA', linewidth=1.2)
        
        ax2.axhline(70, color='#ef5350', linestyle='--', alpha=0.5, linewidth=1)
        ax2.axhline(30, color='#26a69a', linestyle='--', alpha=0.5, linewidth=1)
        ax2.fill_between(day_df.index, 30, 70, color='#29b6f6', alpha=0.03)

        ax2.set_ylabel("RSI Matrix", color='#b2b5be')
        ax2.set_ylim(10, 90)
        ax2.grid(True, color='#2a2e39', linestyle=':', alpha=0.6)
        ax2.set_facecolor('#131722')
        ax2.legend(loc='lower left', facecolor='#1c2030', edgecolor='none', labelcolor='white', fontsize=8)

        # Session Day Line Dividers
        last_date_seen = None
        for idx, row in day_df.iterrows():
            current_ts = str(row['timestamp'])
            current_date = current_ts.split(' ')[0] if ' ' in current_ts else current_ts
            if last_date_seen and current_date != last_date_seen:
                ax1.axvline(x=idx, color='#ffeb3b', linestyle='--', alpha=0.7, linewidth=1.5)
                ax2.axvline(x=idx, color='#ffeb3b', linestyle='--', alpha=0.7, linewidth=1.5)
            last_date_seen = current_date

        x_ticks = range(0, len(day_df), max(1, len(day_df)//6))
        ax2.set_xticks(x_ticks)
        
        formatted_labels = []
        for t in x_ticks:
            ts_str = str(day_df.iloc[t]['timestamp'])
            if ' ' in ts_str:
                date_part, time_part = ts_str.split(' ')
                formatted_labels.append(f"{date_part[5:]}\n{time_part[:5]}")
            else:
                formatted_labels.append(ts_str)
        ax2.set_xticklabels(formatted_labels, color='#b2b5be', rotation=0, fontsize=8)

        fig.patch.set_facecolor('#1c2030')
        ax1.tick_params(colors='#b2b5be', labelsize=9)
        ax2.tick_params(colors='#b2b5be', labelsize=9)
        
        # 🟢 FIXED (Issue #10): Save to standard layout plus active central dashboard tracking mirror
        local_path = f"charts/{ticker}_live_signal.png"
        clean_time = timestamp_str.replace(":", "-")
        dashboard_path = f"static/chart_storage/{ticker.upper()}_{clean_time}.png"
        
        plt.tight_layout()
        plt.savefig(local_path, facecolor=fig.get_facecolor(), edgecolor='none', dpi=120)
        plt.savefig(dashboard_path, facecolor=fig.get_facecolor(), edgecolor='none', dpi=120)
        plt.close(fig)
        
        time.sleep(0.2)
        return local_path
    except Exception as e:
        logging.error(f"❌ Headless chart compilation crashed: {str(e)}")
        return None

# ==============================================================================
# DEEP AI ADVISOR ENGINE
# ==============================================================================
def generate_ai_advisor_analysis(ticker_name, intraday_df, volume_summary):
    current_key = os.environ.get("GEMINI_API_KEY") if os.environ.get("GEMINI_API_KEY") else GEMINI_API_KEY
    if not current_key:
        return "⚠️ *AI ADVISOR VERDICT: REJECTED ENGINE*\n_Reason: Gemini API Key configuration missing on server._"

    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    today_str = datetime.now(ist_zone).strftime("%Y-%m-%d")
    today_candles = intraday_df[intraday_df['timestamp'].str.startswith(today_str)].tail(40)
    if today_candles.empty:
        today_candles = intraday_df.tail(40)
    
    candle_history_text = ""
    for _, row in today_candles.iterrows():
        candle_history_text += (
            f"Time: {row['timestamp']} | O: {row['open']} | H: {row['high']} | "
            f"L: {row['low']} | C: {row['close']} | Vol: {row['volume']} | "
            f"RSI: {round(row.get('RSI', 0), 2)} | RSI_SMA: {round(row.get('RSI_SMA', 0), 2)}\n"
        )

    prompt = (
        f"You are an elite institutional risk officer. Analyze the setup for {ticker_name}.\n"
        f"Metrics: {volume_summary}\nData:\n{candle_history_text}\n"
        f"Provide exactly this template layout and nothing else:\n"
        f"🧠 *AI ADVISOR VERDICT:* [VALIDATED ENTRY] or [⚠️ ADVISE TO AVOID]\n"
        f"────────────────────────\n"
        f"📖 *ADVISORY ANALYSIS:*\n"
        f"• *Trend Context:* (1 short sentence mapping direction)\n"
        f"• *Volume Metrics:* (1 short sentence validating spike status)\n"
        f"• *Momentum Level:* (1 short sentence evaluating boundary exhaustion)"
    )

    try:
        local_ai_client = genai.Client(api_key=current_key)
        response = local_ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        return f"⚠️ *AI ADVISOR VERDICT: PIPELINE FAULT*\n_Error: {str(e)}_"
    return "⚠️ *AI ADVISOR VERDICT: REJECTED ENGINE*"

# ==============================================================================
# TELEGRAM MULTIMEDIA PHOTO TRANSPORT PIPELINE
# ==============================================================================
def send_telegram_multimedia_alert(text, image_path=None):
    try:
        if image_path and os.path.exists(image_path):
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(image_path, 'rb') as photo_file:
                payload_photo = {"chat_id": TELEGRAM_CHAT_ID, "caption": "📊 Live Signal Horizon Analysis Snapshot Matrix", "parse_mode": "Markdown"}
                files_photo = {"photo": photo_file}
                requests.post(url_photo, data=payload_photo, files=files_photo, timeout=15)
        
        url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload_msg = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url_msg, json=payload_msg, timeout=5)
    except Exception as e:
        logging.error(f"❌ Telegram pipeline exception fault: {e}")

# ==============================================================================
# LIVE MONITORING STRATEGY EVALUATION LOOP (Issue #5 & #6 Real-Time Re-engineering)
# ==============================================================================
def check_for_signals(ticker, is_live=False):
    """
    Evaluates indicators immediately on every single incoming market tick 
    to remove processing delay entirely.
    """
    df = DATA_CACHE.get(ticker)
    if df is None or len(df) < 60:
        return

    # Indicator Matrix Calculations
    df['Vol_SMA'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()
    change = df['close'].diff()
    gain = change.mask(change < 0, 0)
    loss = -change.mask(change > 0, 0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI_SMA'] = df['RSI'].rolling(window=14).mean()
    
    if pd.isna(df['RSI_SMA'].iloc[-1]) or pd.isna(df['Vol_SMA'].iloc[-1]):
        return

    latest_bar = df.iloc[-1]
    previous_bar = df.iloc[-2]
    
    current_close = latest_bar['close']
    current_rsi = latest_bar['RSI']
    current_rsi_sma = latest_bar['RSI_SMA']
    prev_rsi = previous_bar['RSI']
    prev_rsi_sma = previous_bar['RSI_SMA']
    
    current_state = STRATEGY_STATES.get(ticker, "READY")
    
    # 🟢 FIXED (Issue #6): Strict Boundary Lock Reset State Management Engine
    if 30 <= current_rsi <= 70:
        if current_state != "READY":
            STRATEGY_STATES[ticker] = "READY"
            logging.info(f"🔄 {ticker} RSI returned to neutral zone ({round(current_rsi,2)}). Strategy is RESET to READY.")

    # Calculate volumes
    current_volume = latest_bar['volume']
    avg_volume = latest_bar['Vol_SMA'] if latest_bar['Vol_SMA'] > 0 else 1
    volume_ratio = round(current_volume / avg_volume, 2)
    volume_status = f"🔥 *SMART MONEY SPIKE ({volume_ratio}x)*" if volume_ratio >= 2.0 else f"📋 Standard Volume Activity ({volume_ratio}x)"
    volume_summary_string = f"Volume: {int(current_volume)} shares vs 20-MA: {int(avg_volume)}."

    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // current_close)
    
    if not is_live:
        return

    # 🟢 FIXED (Issue #6): Crosses exclusively inside overbought/oversold boundaries
    # OVERSOLD BUY LONG TRIGGER
    if current_rsi < 30 and current_state == "READY":
        if prev_rsi <= prev_rsi_sma and current_rsi > current_rsi_sma:
            STRATEGY_STATES[ticker] = "LOCKED_BUY" # Locks state to avoid fake duplicate signals in this zone
            
            timestamp_str = latest_bar['timestamp']
            saved_chart_file = create_live_signal_chart(ticker, "BUY", df, timestamp_str)
            ai_advisor_block = generate_ai_advisor_analysis(ticker, df, volume_summary_string)

            alert_msg = (
                f"📊 *STRATEGY SIGNAL DETECTED*\n"
                f"────────────────────────\n"
                f"• *Stock Name:* {ticker}\n"
                f"• *Signal Type:* BUY ENTRY\n"
                f"• *Volume Status:* {volume_status}\n"
                f"────────────────────────\n"
                f"💰 *EXECUTION METRICS:*\n"
                f"• *Entry Target:* ₹{current_close}\n"
                f"• *Allowed Quantity:* `{trade_quantity} Shares`\n"
                f"• *Take Profit (0.5%):* ₹{round(current_close * 1.005, 2)}\n"
                f"• *Risk Stop Guide:* ₹{round(current_close * 0.995, 2)}\n"
                f"────────────────────────\n"
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {timestamp_str} IST"
            )
            send_telegram_multimedia_alert(alert_msg, saved_chart_file)

    # OVERBOUGHT SHORT SELL TRIGGER
    elif current_rsi > 70 and current_state == "READY":
        if prev_rsi >= prev_rsi_sma and current_rsi < current_rsi_sma:
            STRATEGY_STATES[ticker] = "LOCKED_SHORT" # Locks state to avoid duplicate fake signals
            
            timestamp_str = latest_bar['timestamp']
            saved_chart_file = create_live_signal_chart(ticker, "SHORT", df, timestamp_str)
            ai_advisor_block = generate_ai_advisor_analysis(ticker, df, volume_summary_string)

            alert_msg = (
                f"📊 *STRATEGY SIGNAL DETECTED*\n"
                f"────────────────────────\n"
                f"• *Stock Name:* {ticker}\n"
                f"• *Signal Type:* SELL SHORT ENTRY\n"
                f"• *Volume Status:* {volume_status}\n"
                f"────────────────────────\n"
                f"💰 *EXECUTION METRICS:*\n"
                f"• *Short Entry:* ₹{current_close}\n"
                f"• *Allowed Quantity:* `{trade_quantity} Shares`\n"
                f"• *Take Profit (0.5%):* ₹{round(current_close * 0.995, 2)}\n"
                f"• *Risk Stop Guide:* ₹{round(current_close * 1.005, 2)}\n"
                f"────────────────────────\n"
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {timestamp_str} IST"
            )
            send_telegram_multimedia_alert(alert_msg, saved_chart_file)

# ==============================================================================
# LIVE DATA AGGREGATOR
# ==============================================================================
class CandleAggregator:
    def __init__(self, ticker, smart_api_object, token):
        self.ticker = ticker
        self.smart_api = smart_api_object
        self.token = token
        self.current_candle_time = None
        self.open = None
        self.high = None
        self.low = None
        self.close = None
        self.volume = 0

    def handle_tick(self, price, last_trade_qty=0):
        if self.ticker not in DATA_CACHE or DATA_CACHE[self.ticker] is None:
            DATA_CACHE[self.ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(ist_zone)
        
        bracket_minute = (now_ist.minute // 5) * 5
        tick_candle_time = now_ist.replace(minute=bracket_minute, second=0, microsecond=0)

        if self.current_candle_time and tick_candle_time > self.current_candle_time:
            self.save_completed_candle()
            
        if self.current_candle_time is None or tick_candle_time > self.current_candle_time:
            self.current_candle_time = tick_candle_time
            self.open = price
            self.high = price
            self.low = price
            self.volume = 0

        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += last_trade_qty

        # 🟢 FIXED (Issue #5): Update the current working dataframe row dynamically 
        # and evaluate signals instantly on the active live tick!
        new_row = {
            "timestamp": self.current_candle_time.strftime("%Y-%m-%d %H:%M"),
            "open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume
        }
        
        df = DATA_CACHE[self.ticker]
        if not df.empty and df.iloc[-1]['timestamp'] == new_row['timestamp']:
            df.iloc[-1] = new_row
        else:
            DATA_CACHE[self.ticker] = pd.concat([df, pd.DataFrame([new_row])]).reset_index(drop=True)
            
        # Fire structural evaluations instantly on live market data stream ticks
        check_for_signals(self.ticker, is_live=True)

    def save_completed_candle(self):
        logging.info(f"💾 Bar snapshot finalized on disk for asset: {self.ticker} | Close: {self.close}")

# ==============================================================================
# ENGINE STARTUP INITIALIZATIONS
# ==============================================================================
def bootstrap_history(smart_api_object):
    logging.info("📥 Seeding historical baseline arrays via 4.5s pacing...")
    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist_zone)
    
    for ticker, token in watchlist.WATCHLIST.items():
        time.sleep(4.5)  
        retry_attempts = 3
        success = False
        while retry_attempts > 0:
            try:
                params = {
                    "exchange": "NSE", "symboltoken": str(token), "interval": "FIVE_MINUTE",
                    "fromdate": (now_ist - timedelta(days=15)).strftime("%Y-%m-%d %H:%M"),
                    "todate": now_ist.strftime("%Y-%m-%d %H:%M")
                }
                response = smart_api_object.getCandleData(params)
                if response and response.get("status") is True:
                    df = pd.DataFrame(response["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
                    DATA_CACHE[ticker] = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                    check_for_signals(ticker, is_live=False)
                    success = True
                    break
                else:
                    time.sleep(5)
                    retry_attempts -= 1
            except Exception:
                time.sleep(5)
                retry_attempts -= 1
        
        if not success:
            DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

def start_bot():
    smartApi = SmartConnect(api_key=API_KEY, timeout=15)
    generated_totp = pyotp.TOTP(TOTP_TOKEN).now()
    session = smartApi.generateSession(CLIENT_CODE, PASSWORD, generated_totp)
    if not session.get('status'): return
        
    auth_token = session['data']['jwtToken']
    feed_token = smartApi.getfeedToken()
    bootstrap_history(smartApi)
    
    global LIVE_ENGINES
    LIVE_ENGINES = {ticker: CandleAggregator(ticker, smartApi, token) for ticker, token in watchlist.WATCHLIST.items()}
    sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)

    def on_data(wsapp, message):
        if isinstance(message, dict) and 'token' in message:
            ticker = TOKEN_TO_TICKER.get(message.get('token'))
            if ticker and ticker in LIVE_ENGINES:
                raw_price = message.get('last_traded_price', 0)
                last_qty = message.get('last_traded_quantity', 0)
                live_price = raw_price / 100.0 if raw_price > 0 else 0
                if live_price > 0:
                    LIVE_ENGINES[ticker].handle_tick(live_price, last_qty)

    def on_open(wsapp):
        sws.subscribe("fit_bot_stream", 1, [{"exchangeType": 1, "tokens": list(watchlist.WATCHLIST.values())}])
        logging.info("📡 WebSocket stream fully linked and processing live ticks.")

    sws.on_open = on_open
    sws.on_data = on_data
    sws.connect()

if __name__ == "__main__":
    start_bot()
