# bot.py
import time
import logging
import requests
import pyotp
import pandas as pd
from datetime import datetime, timedelta
import google.generativeai as genai  # Google Gemini SDK

# Import modular configuration files
import config
import watchlist

from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# Configure clean console logging formats
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

TOKEN_TO_TICKER = {v: k for k, v in watchlist.WATCHLIST.items()}
DATA_CACHE = {}

# 🟢 STRATEGY STATE TRACKER: Remembers cycles to prevent repetitive signal spam
STRATEGY_STATES = {ticker: "READY" for ticker in watchlist.WATCHLIST.keys()}

# User Risk Management Configurations
CASH_CAPITAL_PER_TRADE = 7000.0   # Your personal cash allocation
LEVERAGE_MULTIPLIER = 5.0          # Zerodha intraday MIS margin factor
PROFIT_TARGET_PERCENT = 0.005     # 0.5% Take Profit threshold
VOLUME_MA_PERIOD = 20              # Period used to baseline average volume

# Configure Gemini Client Connection
genai.configure(api_key=config.GEMINI_API_KEY)

# ==============================================================================
# ISOLATED AI REAL-TIME NEWS PIPELINE (GEMINI)
# ==============================================================================
def fetch_gemini_news_sentiment(ticker_name):
    """
    Leverages Gemini's live data capabilities to pull global and national news
    concerning the asset at the exact millisecond of the execution alert.
    """
    try:
        logging.info(f"🤖 Querying Gemini for real-time news updates on {ticker_name}...")
        prompt = (
            f"You are an elite financial research assistant checking real-time market data on the Indian stock market. "
            f"Provide a brief, hyper-concise summary (max 3 sentences) of the most recent, relevant breaking news, "
            f"corporate actions, earnings results, or global macro developments affecting {ticker_name} today. "
            f"If there is no major breaking news today, simply state 'No major corporate news or global catalyst detected today. Trading purely on structural chart momentum.'"
        )
        
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        
        if response and response.text:
            return response.text.strip()
        else:
            return "Unable to parse AI response framework at this second."
            
    except Exception as e:
        logging.error(f"❌ Gemini News Pipeline Error: {e}")
        return "AI News Stream Temporarily Unavailable."

# ==============================================================================
# TELEGRAM NOTIFICATION PIPELINE
# ==============================================================================
def send_telegram_alert(text):
    """Pushes automated alert messages directly to your Telegram chat interface."""
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logging.info("📨 Telegram notification delivered successfully.")
        else:
            logging.error(f"❌ Telegram gateway error response: {response.text}")
    except Exception as e:
        logging.error(f"❌ Failed to reach Telegram API: {e}")

# ==============================================================================
# STRATEGY ENGINE (With Memory Crossover Reset Locks)
# ==============================================================================
def check_for_signals(ticker):
# 🟢 NEW: Time Gate. Only run the strategy between 9:25 AM and 1:30 PM IST
    now = datetime.now()
    if not (now.hour == 9 and now.minute >= 25) and not (10 <= now.hour < 13) and not (now.hour == 13 and now.minute <= 30):
        return  # Silently bypass everything if it's afternoon or night

    df = DATA_CACHE[ticker]
    
    if len(df) < 200:
        return

    # Calculate Technical Indicators
    df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['Vol_SMA'] = df['volume'].rolling(window=VOLUME_MA_PERIOD).mean()

    change = df['close'].diff()
    gain = change.mask(change < 0, 0)
    loss = -change.mask(change > 0, 0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 0.00001)
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI_SMA'] = df['RSI'].rolling(window=14).mean()
    
    if df['RSI_SMA'].isna().iloc[-1] or df['EMA_200'].isna().iloc[-1] or df['Vol_SMA'].isna().iloc[-1]:
        return

    latest_bar = df.iloc[-1]
    previous_bar = df.iloc[-2]
    
    current_close = latest_bar['close']
    current_ema200 = latest_bar['EMA_200']
    current_rsi = latest_bar['RSI']
    
    # 🟢 DYNAMIC RESET MECHANISM: Releases the locks if the stock returns to normal bounds
    current_state = STRATEGY_STATES[ticker]
    
    if 30 <= current_rsi <= 70:
        if current_state != "READY":
            STRATEGY_STATES[ticker] = "READY"
            logging.info(f"🔄 State Reset: {ticker} RSI returned to normal bounds ({round(current_rsi, 2)}). Strategy is un-locked.")
    elif current_rsi < 30 and current_state == "LOCKED_SHORT":
        # If it was locked on an overbought short, but crashes straight down into oversold, reset it
        STRATEGY_STATES[ticker] = "READY"
    elif current_rsi > 70 and current_state == "LOCKED_BUY":
        # If it was locked on an oversold buy, but rockets straight up into overbought, reset it
        STRATEGY_STATES[ticker] = "READY"

    # Volume Analysis
    current_volume = latest_bar['volume']
    avg_volume = latest_bar['Vol_SMA'] if latest_bar['Vol_SMA'] > 0 else 1
    volume_ratio = round(current_volume / avg_volume, 2)
    volume_status = f"🔥 *SMART MONEY SPIKE ({volume_ratio}x)*" if volume_ratio >= 2.0 else f"📋 Standard Market Activity ({volume_ratio}x)"

    # Execution Sizing Metrics
    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // current_close)
    
    take_profit_price = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
    stop_loss_estimate = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)

    # 🟢 BUY TRIGGER (Only if state is READY and not already locked in oversold zone)
    if current_rsi < 30:
        if STRATEGY_STATES[ticker] == "READY":
            if previous_bar['RSI'] <= previous_bar['RSI_SMA'] and latest_bar['RSI'] > latest_bar['RSI_SMA']:
                
                # Turn on the memory lock immediately so it can't spam while staying under 30
                STRATEGY_STATES[ticker] = "LOCKED_BUY"
                
                trend_status = "🔥 *HIGH PROBABILITY (UPTREND)*" if current_close >= current_ema200 else "⚠️ *COUNTER-TREND BUY (RISKY)*"
                ai_news_brief = fetch_gemini_news_sentiment(ticker)

                alert_msg = (
                    f"📊 *STRATEGY SIGNAL DETECTED*\n"
                    f"────────────────────────\n"
                    f"• *Stock Name:* {ticker}\n"
                    f"• *Signal Type:* BUY ENTRY\n"
                    f"• *Trend Status:* {trend_status}\n"
                    f"• *Volume Status:* {volume_status}\n"
                    f"────────────────────────\n"
                    f"💰 *EXECUTION METRICS (Zerodha MIS Input):*\n"
                    f"• *Entry Target:* ₹{current_close}\n"
                    f"• *Allowed Quantity:* `{trade_quantity} Shares` _(Using ₹{int(CASH_CAPITAL_PER_TRADE)} Cash @ 5x)_\n"
                    f"• *Take Profit Target (0.5%):* ₹{take_profit_price}\n"
                    f"• *Risk Stop Guide (0.5%):* ₹{stop_loss_estimate}\n"
                    f"────────────────────────\n"
                    f"🤖 *GEMINI REAL-TIME NEWS INTELLIGENCE:*\n"
                    f"_{ai_news_brief}_\n"
                    f"────────────────────────\n"
                    f"🕒 *Candle Stamp:* {latest_bar['timestamp']}"
                )
                
                logging.info(f"🚀 BUY SIGNAL DETECTED FOR: {ticker} at ₹{current_close}. Cycle locked.")
                send_telegram_alert(alert_msg)

    # 🟢 SELL SHORT TRIGGER (Only if state is READY and not already locked in overbought zone)
    elif current_rsi > 70:
        if STRATEGY_STATES[ticker] == "READY":
            if previous_bar['RSI'] >= previous_bar['RSI_SMA'] and latest_bar['RSI'] < latest_bar['RSI_SMA']:
                
                # Turn on the memory lock immediately so it can't spam while staying over 70
                STRATEGY_STATES[ticker] = "LOCKED_SHORT"
                
                take_profit_short = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)
                stop_loss_short = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
                
                trend_status = "🔥 *HIGH PROBABILITY SHORT (DOWNTREND)*" if current_close <= current_ema200 else "⚠️ *COUNTER-TREND SHORT (RISKY)*"
                ai_news_brief = fetch_gemini_news_sentiment(ticker)

                alert_msg = (
                    f"📊 *STRATEGY SIGNAL DETECTED*\n"
                    f"────────────────────────\n"
                    f"• *Stock Name:* {ticker}\n"
                    f"• *Signal Type:* SELL SHORT ENTRY\n"
                    f"• *Trend Status:* {trend_status}\n"
                    f"• *Volume Status:* {volume_status}\n"
                    f"────────────────────────\n"
                    f"💰 *EXECUTION METRICS (Zerodha MIS Input):*\n"
                    f"• *Short Entry:* ₹{current_close}\n"
                    f"• *Allowed Quantity:* `{trade_quantity} Shares` _(Using ₹{int(CASH_CAPITAL_PER_TRADE)} Cash @ 5x)_\n"
                    f"• *Take Profit Target (0.5%):* ₹{take_profit_short}\n"
                    f"• *Risk Stop Guide (0.5%):* ₹{stop_loss_short}\n"
                    f"────────────────────────\n"
                    f"🤖 *GEMINI REAL-TIME NEWS INTELLIGENCE:*\n"
                    f"_{ai_news_brief}_\n"
                    f"────────────────────────\n"
                    f"🕒 *Candle Stamp:* {latest_bar['timestamp']}"
                )
                
                logging.info(f"⚠️ SELL SHORT SIGNAL DETECTED FOR: {ticker} at ₹{current_close}. Cycle locked.")
                send_telegram_alert(alert_msg)

# ==============================================================================
# LIVE TICK-TO-CANDLE DATA AGGREGATOR
# ==============================================================================
class CandleAggregator:
    def __init__(self, ticker):
        self.ticker = ticker
        self.current_candle_time = None
        self.open = None
        self.high = None
        self.low = None
        self.close = None
        self.volume = 0

    def handle_tick(self, price, last_trade_qty=0):
        now = datetime.now()
        bracket_minute = (now.minute // 5) * 5
        tick_candle_time = now.replace(minute=bracket_minute, second=0, microsecond=0)

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
        check_for_signals(self.ticker)

LIVE_ENGINES = {ticker: CandleAggregator(ticker) for ticker in watchlist.WATCHLIST.keys()}

# ==============================================================================
# SEEDER PIPELINE
# ==============================================================================
def bootstrap_history(smart_api_object):
    logging.info("📥 Seeding historical baseline arrays (Drawing 30-day lookback frames via 4.5s pacing)...")
    
    for ticker, token in watchlist.WATCHLIST.items():
        time.sleep(4.5)  
        retry_attempts = 3
        while retry_attempts > 0:
            try:
                params = {
                    "exchange": "NSE",
                    "symboltoken": str(token),
                    "interval": "FIVE_MINUTE",
                    "fromdate": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M"),
                    "todate": datetime.now().strftime("%Y-%m-%d %H:%M")
                }
                response = smart_api_object.getCandleData(params)
                
                if response and response.get("status") is True:
                    df = pd.DataFrame(response["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
                    DATA_CACHE[ticker] = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                    logging.info(f"✅ Baseline synced for {ticker}: {len(df)} lines loaded.")
                    break
                elif response and "access rate" in response.get("message", "").lower():
                    logging.warning(f"⚠️ Rate limit hit for {ticker}. Backing off for 12 seconds...")
                    time.sleep(12)
                    retry_attempts -= 1
                else:
                    DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
                    break
            except Exception as e:
                logging.error(f"❌ Network error on {ticker}: {e}. Retrying in 5 seconds...")
                time.sleep(5)
                retry_attempts -= 1
                
        if retry_attempts == 0:
            DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

def start_bot():
    smartApi = SmartConnect(api_key=config.API_KEY)
    generated_totp = pyotp.TOTP(config.TOTP_TOKEN).now()
    session = smartApi.generateSession(config.CLIENT_CODE, config.PASSWORD, generated_totp)
    
    if not session.get('status'):
        logging.error("❌ Login Session Failure. Check credentials in config.py.")
        return
        
    logging.info("🔐 Logged in to Angel One successfully.")
    auth_token = session['data']['jwtToken']
    feed_token = smartApi.getfeedToken()
    
    bootstrap_history(smartApi)
    
    sws = SmartWebSocketV2(auth_token, config.API_KEY, config.CLIENT_CODE, feed_token)

    def on_data(wsapp, message):
        if isinstance(message, dict) and 'token' in message:
            token = message.get('token')
            ticker = TOKEN_TO_TICKER.get(token)
            if ticker:
                raw_price = message.get('last_traded_price', 0)
                last_qty = message.get('last_traded_quantity', 0)
                live_price = raw_price / 100.0 if raw_price > 0 else 0
                if live_price > 0:
                    LIVE_ENGINES[ticker].handle_tick(live_price, last_qty)

    def on_open(wsapp):
        logging.info("🔌 Pipeline connected. Subscribing watchlist tokens...")
        tokens_to_subscribe = list(watchlist.WATCHLIST.values())
        token_list = [{"exchangeType": 1, "tokens": tokens_to_subscribe}]
        sws.subscribe("fit_bot_stream", 1, token_list)
        logging.info(f"📡 WebSocket streaming running actively for {len(tokens_to_subscribe)} tickers.")

    def on_error(wsapp, error):
        logging.error(f"⚠️ Stream Error: {error}")

    def on_close(wsapp, code, reason):
        logging.warning("🔌 Connection closed. Reconnecting pipe automatically...")

    sws.on_open = on_open
    sws.on_data = on_data
    sws.on_error = on_error
    sws.on_close = on_close

    sws.connect()

if __name__ == "__main__":
    start_bot()