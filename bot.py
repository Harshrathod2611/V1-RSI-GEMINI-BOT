# bot.py
import os
import time
import logging
import requests
import pyotp
import pandas as pd
from datetime import datetime, timedelta
import zoneinfo  # Explicit Indian Standard Time management
from google import genai 
import matplotlib
matplotlib.use('Agg') # Safe headless execution server environment configuration
import matplotlib.pyplot as plt

import watchlist
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# Configure clean logging format
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
STRATEGY_STATES = {ticker: "READY" for ticker in watchlist.WATCHLIST.keys()}

# User Core Configurations
CASH_CAPITAL_PER_TRADE = 7000.0   
LEVERAGE_MULTIPLIER = 5.0          
PROFIT_TARGET_PERCENT = 0.005     
VOLUME_MA_PERIOD = 20              

# ==============================================================================
# HEADLESS LIVE CHART GENERATOR
# ==============================================================================
def create_live_signal_chart(ticker, trade_type, full_df):
    """
    Generates a professional 2-panel chart showing the full day's activity
    up to the current live candle trigger point.
    """
    try:
        os.makedirs("charts", exist_ok=True)
        ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
        trade_day = datetime.now(ist_zone).strftime("%Y-%m-%d")
        
        # Filter dataframe for the current active trading day
        day_df = full_df[full_df['timestamp'].str.startswith(trade_day)].copy().reset_index()
        if day_df.empty or len(day_df) < 2:
            day_df = full_df.tail(40).copy().reset_index() # Fallback mechanism if grid is empty

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, 
                                       gridspec_kw={'height_ratios': [2.5, 1]})
        
        # PANEL 1: CANDLESTICKS ONLY
        for idx, row in day_df.iterrows():
            color = '#26a69a' if row['close'] >= row['open'] else '#ef5350'
            ax1.vlines(idx, row['low'], row['high'], colors=color, linewidth=1.2)
            body_line = ax1.vlines(idx, row['open'], row['close'], colors=color, linewidth=5)
            try: body_line.set_capstyle('round')
            except AttributeError: pass

        # Annotate Entry Signal Row Location
        signal_loc = len(day_df) - 1
        entry_row = day_df.iloc[signal_loc]
        marker_color = '#2ecc71' if trade_type == "BUY" else '#e74c3c'
        ax1.scatter(signal_loc, entry_row['close'], color=marker_color, s=150, zorder=5, marker='^' if trade_type == "BUY" else 'v')
        ax1.annotate(f"  {trade_type} Signal Trigger\n  INR {entry_row['close']}", (signal_loc, entry_row['close']), 
                     color='white', weight='bold', fontsize=8, bbox=dict(facecolor=marker_color, alpha=0.8, boxstyle='round,pad=0.3'))

        # 🟢 FIXED: Emojis removed from chart text attributes to prevent server font-missing warnings
        ax1.set_title(f"{ticker} - 5M LIVE SIGNAL ANALYSIS SNAPSHOT", color='white', fontsize=12, weight='bold', loc='left')
        ax1.set_ylabel("Stock Price (INR)", color='#b2b5be')
        ax1.grid(True, color='#2a2e39', linestyle=':', alpha=0.6)
        ax1.set_facecolor('#131722')

        # PANEL 2: RSI & RSI_SMA CONVERGENCE TRACE MATRIX
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

        # X-Axis Time Cleanups
        x_ticks = range(0, len(day_df), max(1, len(day_df)//6))
        ax2.set_xticks(x_ticks)
        
        formatted_labels = []
        for t in x_ticks:
            ts_str = str(day_df.iloc[t]['timestamp'])
            time_part = ts_str.split(' ')[1] if ' ' in ts_str else ts_str
            formatted_labels.append(time_part[:5])
        ax2.set_xticklabels(formatted_labels, color='#b2b5be', rotation=0)

        fig.patch.set_facecolor('#1c2030')
        ax1.tick_params(colors='#b2b5be', labelsize=9)
        ax2.tick_params(colors='#b2b5be', labelsize=9)
        
        file_path = f"charts/{ticker}_live_signal.png"
        plt.tight_layout()
        plt.savefig(file_path, facecolor=fig.get_facecolor(), edgecolor='none', dpi=120)
        plt.close(fig)
        
        # 🟢 FIXED: Tiny delay to allow the server operating system to finish writing and release the file handle
        time.sleep(0.5)
        return file_path
    except Exception as e:
        logging.error(f"❌ Headless chart compilation crashed: {str(e)}")
        return None

# ==============================================================================
# DEEP AI ADVISOR ENGINE (STABLE VOLUME INJECTION)
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
        f"You are an elite, high-conviction institutional Risk Officer and Trading Advisor for the Indian Stock Market.\n\n"
        f"DATA PACKAGE FOR {ticker_name}:\n"
        f"1. LIVE TRANSACTION VOLUME DISTRIBUTION METRICS:\n{volume_summary}\n\n"
        f"2. INTRADAY 5-MINUTE CANDLE TREND HISTORY:\n{candle_history_text}\n\n"
        f"YOUR INSTRUCTIONS:\n"
        f"- Analyze the overall chart trend using the candle history provided (look for breakdown blocks, structural support zones, or RSI exhaustion levels).\n"
        f"- Factor in the transactional volume status to confirm institutional buy/sell pressure.\n"
        f"- Dynamically retrieve the latest breaking news, corporate actions, or sector trends affecting {ticker_name} today.\n"
        f"- Provide a formal advisory verdict. You must decide whether you approve or advise against this entry based on conflicts or alignment between price action, volume spikes, and news.\n\n"
        f"OUTPUT FORMAT (Strictly return exactly this template format with no extra pleasantries):\n"
        f"🧠 *AI ADVISOR VERDICT:* [VALIDATED ENTRY] or [⚠️ ADVISE TO AVOID / LEAVE IT]\n"
        f"────────────────────────\n"
        f"📖 *ADVISORY ANALYSIS:* (Provide exactly 3-4 structural sentences detailing the breakdown of price action, volume confirmations, and today's news catalysts to back up your decision.)"
    )

    retry_delay = 2.0
    for attempt in range(3):
        try:
            local_ai_client = genai.Client(api_key=current_key)
            response = local_ai_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            if "503" in str(e) or "unavailable" in str(e).lower():
                logging.warning(f"⚠️ Gemini 503 hit on attempt {attempt+1}/3. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay *= 1.5
            else:
                return f"⚠️ *AI ADVISOR VERDICT: PIPELINE FAULT*\n_Error detailing: {str(e)}_"
                
    return "⚠️ *AI ADVISOR VERDICT: SERVER TIMEOUT*\n_Reason: Endpoints are congested. Confirm manually using technical chart verification._"

# ==============================================================================
# TELEGRAM MULTIMEDIA PHOTO TRANSPORT PIPELINE
# ==============================================================================
def send_telegram_multimedia_alert(text, image_path=None):
    """
    Transmits strategy metrics along with technical charts directly to your Telegram chat channel.
    """
    try:
        if image_path and os.path.exists(image_path):
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(image_path, 'rb') as photo_file:
                payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": text, "parse_mode": "Markdown"}
                files = {"photo": photo_file}
                response = requests.post(url, data=payload, files=files, timeout=15)
            if response.status_code == 200:
                logging.info("📸 Visual alert packet successfully dispatched to Telegram channels.")
                return
            else:
                logging.error(f"⚠️ Telegram API rejected sendPhoto request: Status {response.status_code}, Body: {response.text}")
        
        # Fallback to standard text channel execution if the chart asset generation fails
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"❌ Telegram pipeline exception fault: {e}")

# ==============================================================================
# LIVE MONITORING STRATEGY EVALUATION LOOP
# ==============================================================================
def check_for_signals(ticker, smart_api_object=None, token=None, is_live=False):
    if not is_live:
        return

    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist_zone)
    
    # Restrict execution engine to match standard live scanning windows
    if not (now_ist.hour == 9 and now_ist.minute >= 25) and not (10 <= now_ist.hour < 15):
        return

    df = DATA_CACHE.get(ticker)
    if df is None or len(df) < 60:
        return

    df['Vol_SMA'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()

    # Calculate indicators
    change = df['close'].diff()
    gain = change.mask(change < 0, 0)
    loss = -change.mask(change > 0, 0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI_SMA'] = df['RSI'].rolling(window=14).mean()
    
    if df['RSI_SMA'].isna().iloc[-1] or df['Vol_SMA'].isna().iloc[-1]:
        return

    latest_bar = df.iloc[-1]
    previous_bar = df.iloc[-2]
    
    current_close = latest_bar['close']
    current_rsi = latest_bar['RSI']
    
    current_state = STRATEGY_STATES.get(ticker, "READY")
    
    if 30 <= current_rsi <= 70:
        if current_state != "READY":
            STRATEGY_STATES[ticker] = "READY"
    elif current_rsi < 30 and current_state == "LOCKED_SHORT":
        STRATEGY_STATES[ticker] = "READY"
    elif current_rsi > 70 and current_state == "LOCKED_BUY":
        STRATEGY_STATES[ticker] = "READY"

    current_volume = latest_bar['volume']
    avg_volume = latest_bar['Vol_SMA'] if latest_bar['Vol_SMA'] > 0 else 1
    volume_ratio = round(current_volume / avg_volume, 2)
    volume_status = f"🔥 *SMART MONEY SPIKE ({volume_ratio}x)*" if volume_ratio >= 2.0 else f"📋 Standard Volume Activity ({volume_ratio}x)"
    volume_summary_string = f"Current Candle Volume: {int(current_volume)} shares vs 20-candle average benchmark: {int(avg_volume)} shares."

    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // current_close)
    
    take_profit_price = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
    stop_loss_estimate = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)

    # CRITICAL TRIGGER CHECK: RSI Crosses over RSI_SMA inside extreme boundaries
    if current_rsi < 30 and current_state == "READY":
        if previous_bar['RSI'] <= previous_bar['RSI_SMA'] and latest_bar['RSI'] > latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_BUY"
            
            # Fire headless chart generator engine
            saved_chart_file = create_live_signal_chart(ticker, "BUY", df)
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
                f"• *Take Profit (0.5%):* ₹{take_profit_price}\n"
                f"• *Risk Stop Guide:* ₹{stop_loss_estimate}\n"
                f"────────────────────────\n"
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {latest_bar['timestamp']} IST"
            )
            send_telegram_multimedia_alert(alert_msg, saved_chart_file)

    elif current_rsi > 70 and current_state == "READY":
        if previous_bar['RSI'] >= previous_bar['RSI_SMA'] and latest_bar['RSI'] < latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_SHORT"
            
            take_profit_short = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)
            stop_loss_short = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
            
            # Fire headless chart generator engine
            saved_chart_file = create_live_signal_chart(ticker, "SHORT", df)
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
                f"• *Take Profit (0.5%):* ₹{take_profit_short}\n"
                f"• *Risk Stop Guide:* ₹{stop_loss_short}\n"
                f"────────────────────────\n"
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {latest_bar['timestamp']} IST"
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

    def save_completed_candle(self):
        new_row = pd.DataFrame([{
            "timestamp": self.current_candle_time.strftime("%Y-%m-%d %H:%M"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume if self.volume > 0 else 1000
        }])
        
        DATA_CACHE[self.ticker] = pd.concat([DATA_CACHE[self.ticker], new_row]).drop_duplicates(subset=['timestamp']).tail(300)
        check_for_signals(self.ticker, smart_api_object=self.smart_api, token=self.token, is_live=True)

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
                    "exchange": "NSE",
                    "symboltoken": str(token),
                    "interval": "FIVE_MINUTE",
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
                elif response and "access rate" in response.get("message", "").lower():
                    time.sleep(12)
                    retry_attempts -= 1
                else:
                    break
            except Exception as e:
                time.sleep(5)
                retry_attempts -= 1
        
        if not success:
            logging.warning(f"⚠️ Seeder rate-limited or failed for {ticker}. Initializing clean cache fallback.")
            DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

def start_bot():
    smartApi = SmartConnect(api_key=API_KEY, timeout=15)
    generated_totp = pyotp.TOTP(TOTP_TOKEN).now()
    session = smartApi.generateSession(CLIENT_CODE, PASSWORD, generated_totp)
    
    if not session.get('status'):
        logging.error("❌ Session authentication fault. Terminating launch sequence.")
        return
        
    auth_token = session['data']['jwtToken']
    feed_token = smartApi.getfeedToken()
    
    bootstrap_history(smartApi)
    
    global LIVE_ENGINES
    LIVE_ENGINES = {ticker: CandleAggregator(ticker, smartApi, token) for ticker, token in watchlist.WATCHLIST.items()}
    
    sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)

    def on_data(wsapp, message):
        if isinstance(message, dict) and 'token' in message:
            token = message.get('token')
            ticker = TOKEN_TO_TICKER.get(token)
            if ticker:
                if ticker not in LIVE_ENGINES:
                    return
                
                raw_price = message.get('last_traded_price', 0)
                last_qty = message.get('last_traded_quantity', 0)
                live_price = raw_price / 100.0 if raw_price > 0 else 0
                if live_price > 0:
                    LIVE_ENGINES[ticker].handle_tick(live_price, last_qty)

    def on_open(wsapp):
        tokens_to_subscribe = list(watchlist.WATCHLIST.values())
        token_list = [{"exchangeType": 1, "tokens": tokens_to_subscribe}]
        sws.subscribe("fit_bot_stream", 1, token_list)
        logging.info("📡 WebSocket stream fully linked and processing live ticks.")

    sws.on_open = on_open
    sws.on_data = on_data
    sws.connect()

if __name__ == "__main__":
    start_bot()
