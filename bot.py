# bot.py
import os
import time
import logging
import requests
import pyotp
import pandas as pd
from datetime import datetime, timedelta
import zoneinfo  # 🟢 Fixed: Natively maps explicit Indian Standard Time
from google import genai 

import watchlist
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

try:
    import config
    API_KEY = config.API_KEY
    CLIENT_CODE = config.CLIENT_CODE
    PASSWORD = config.PASSWORD
    TOTP_TOKEN = config.TOTP_TOKEN
    TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID = config.TELEGRAM_CHAT_ID
    GEMINI_API_KEY = config.GEMINI_API_KEY
except ModuleNotFoundError:
    API_KEY = os.environ.get("API_KEY")
    CLIENT_CODE = os.environ.get("CLIENT_CODE")
    PASSWORD = os.environ.get("PASSWORD")
    TOTP_TOKEN = os.environ.get("TOTP_TOKEN")
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TOKEN_TO_TICKER = {v: k for k, v in watchlist.WATCHLIST.items()}
DATA_CACHE = {}
STRATEGY_STATES = {ticker: "READY" for ticker in watchlist.WATCHLIST.keys()}

CASH_CAPITAL_PER_TRADE = 7000.0   
LEVERAGE_MULTIPLIER = 5.0          
PROFIT_TARGET_PERCENT = 0.005     
VOLUME_MA_PERIOD = 20              

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# ==============================================================================
# REAL-TIME NEWS PIPELINE (DYNAMIC KEY MAPPING)
# ==============================================================================
def fetch_gemini_news_sentiment(ticker_name):
    try:
        logging.info(f"🤖 Querying Gemini for real-time news updates on {ticker_name}...")
        current_key = os.environ.get("GEMINI_API_KEY") if os.environ.get("GEMINI_API_KEY") else GEMINI_API_KEY
        
        if not current_key:
            return "Skipping AI analysis (Missing API Key configuration)."
            
        local_ai_client = genai.Client(api_key=current_key)
        prompt = (
            f"You are an elite financial research assistant checking real-time market data on the Indian stock market. "
            f"Provide a brief, hyper-concise summary (max 3 sentences) of the most recent, relevant breaking news, "
            f"corporate actions, earnings results, or global macro developments affecting {ticker_name} today. "
            f"If there is no major breaking news today, simply state 'No major corporate news or global catalyst detected today. Trading purely on structural chart momentum.'"
        )
        
        response = local_ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text.strip() if response and response.text else "No news parsed."
    except Exception as e:
        return f"AI News Stream Temporarily Unavailable. (Error: {str(e)})"

def send_telegram_alert(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"❌ Telegram pipeline fail: {e}")

# ==============================================================================
# STRATEGY ENGINE (TIMEZONE LOCKED)
# ==============================================================================
def check_for_signals(ticker, is_live=False):
    # 🟢 1. TIMEZONE LOCK: Force checking exact time in India right now
    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist_zone)
    
    # 🟢 2. SEEDER PROTECTION: Block historical loader loop from firing alert notifications
    if not is_live:
        return

    # Strict Market Session Window: 9:25 AM to 1:30 PM IST
    if not (now_ist.hour == 9 and now_ist.minute >= 25) and not (10 <= now_ist.hour < 13) and not (now_ist.hour == 13 and now_ist.minute <= 30):
        return

    df = DATA_CACHE[ticker]
    if len(df) < 200:
        return

    # Math processing functions completely untouched
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
    
    current_state = STRATEGY_STATES[ticker]
    
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
    volume_status = f"🔥 *SMART MONEY SPIKE ({volume_ratio}x)*" if volume_ratio >= 2.0 else f"📋 Standard Market Activity ({volume_ratio}x)"

    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // current_close)
    
    take_profit_price = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
    stop_loss_estimate = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)

    # BUY TRIGGER
    if current_rsi < 30 and current_state == "READY":
        if previous_bar['RSI'] <= previous_bar['RSI_SMA'] and latest_bar['RSI'] > latest_bar['RSI_SMA']:
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
            send_telegram_alert(alert_msg)

    # SELL SHORT TRIGGER
    elif current_rsi > 70 and current_state == "READY":
        if previous_bar['RSI'] >= previous_bar['RSI_SMA'] and latest_bar['RSI'] < latest_bar['RSI_SMA']:
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
            send_telegram_alert(alert_msg)

# ==============================================================================
# LIVE DATA AGGREGATOR (IST CLOCK SYNCHRONIZED)
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
        # 🟢 3. LIVE STREAM CLOCK FIX: Explicitly match India Time for incoming packets
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
        # Pass is_live=True so the bot knows this candle came directly from the active market
        check_for_signals(self.ticker, is_live=True)

LIVE_ENGINES = {ticker: CandleAggregator(ticker) for ticker in watchlist.WATCHLIST.keys()}

def bootstrap_history(smart_api_object):
    logging.info("📥 Seeding historical baseline arrays via 4.5s pacing...")
    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist_zone)
    
    for ticker, token in watchlist.WATCHLIST.items():
        time.sleep(4.5)  
        retry_attempts = 3
        while retry_attempts > 0:
            try:
                params = {
                    "exchange": "NSE",
                    "symboltoken": str(token),
                    "interval": "FIVE_MINUTE",
                    "fromdate": (now_ist - timedelta(days=30)).strftime("%Y-%m-%d %H:%M"),
                    "todate": now_ist.strftime("%Y-%m-%d %H:%M")
                }
                response = smart_api_object.getCandleData(params)
                
                if response and response.get("status") is True:
                    df = pd.DataFrame(response["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
                    DATA_CACHE[ticker] = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                    # Pass is_live=False so historical loading won't issue notifications
                    check_for_signals(ticker, is_live=False)
                    break
                elif response and "access rate" in response.get("message", "").lower():
                    time.sleep(12)
                    retry_attempts -= 1
                else:
                    DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
                    break
            except Exception as e:
                time.sleep(5)
                retry_attempts -= 1

def start_bot():
    smartApi = SmartConnect(api_key=API_KEY)
    generated_totp = pyotp.TOTP(TOTP_TOKEN).now()
    session = smartApi.generateSession(CLIENT_CODE, PASSWORD, generated_totp)
    
    if not session.get('status'):
        return
        
    auth_token = session['data']['jwtToken']
    feed_token = smartApi.getfeedToken()
    
    bootstrap_history(smartApi)
    
    sws = SmartWebSocketV2(auth_token, API_KEY, CLIENT_CODE, feed_token)

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
        tokens_to_subscribe = list(watchlist.WATCHLIST.values())
        token_list = [{"exchangeType": 1, "tokens": tokens_to_subscribe}]
        sws.subscribe("fit_bot_stream", 1, token_list)

    sws.on_open = on_open
    sws.on_data = on_data
    sws.connect()

if __name__ == "__main__":
    start_bot()
