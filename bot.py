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
# LIVE MARKET DEPTH SNAPSHOT EXTRACTOR
# ==============================================================================
# ==============================================================================
# LIVE MARKET DEPTH SNAPSHOT EXTRACTOR (HARDENED UPGRADE)
# ==============================================================================
def fetch_market_depth_ratio(smart_api_object, exchange, token, ticker):
    """
    Fetches the instantaneous market depth bid-ask pool distribution.
    Includes an automatic retry mechanism to insulate against temporary network drops.
    """
    # Enforce strict string sanitization for token inputs
    clean_token = str(token).strip()
    clean_exchange = str(exchange).strip().upper() # Ensure it's explicitly 'NSE'
    
    params = {
        "mode": "FULL",
        "exchangeTokens": {
            clean_exchange: [clean_token]
        }
    }
    
    # 3-pass loop to guard against temporary gateway timeouts
    for attempt in range(3):
        try:
            response = smart_api_object.getMarketData(params.get("mode"), params.get("exchangeTokens"))
            
            if response and response.get("status") is True:
                data_dict = response.get("data", {})
                fetched_list = data_dict.get("fetched", [])
                
                if fetched_list:
                    snap = fetched_list[0]
                    total_buy_qty = float(snap.get("totalBuyQuantity", 0))
                    total_sell_qty = float(snap.get("totalSellQuantity", 0))
                    
                    total_pool = total_buy_qty + total_sell_qty
                    if total_pool > 0:
                        buy_percent = round((total_buy_qty / total_pool) * 100, 1)
                        sell_percent = round((total_sell_qty / total_pool) * 100, 1)
                        logging.info(f"✅ Market Depth captured cleanly for {ticker} on attempt {attempt+1}")
                        return f"🟢 Buyers: {buy_percent}% vs 🔴 Sellers: {sell_percent}% (Total Volume Pool: {int(total_pool)})"
            
            # If the server returned an error format, back off slightly before retrying
            time.sleep(0.5)
            
        except Exception as e:
            logging.warning(f"⚠️ Market depth fetch try {attempt+1}/3 failed for {ticker}: {e}")
            time.sleep(0.5)
            
    # Final fallback if all attempts are exhausted
    logging.error(f"❌ Market Depth completely unreachable for {ticker} after 3 attempts.")
    return "Market Depth Pool Data Temporarily Unreachable from Exchange Endpoint."

# ==============================================================================
# DEEP AI ADVISOR ENGINE WITH ANTI-503 RETRY LOOP
# ==============================================================================
def generate_ai_advisor_analysis(ticker_name, intraday_df, depth_summary):
    current_key = os.environ.get("GEMINI_API_KEY") if os.environ.get("GEMINI_API_KEY") else GEMINI_API_KEY
    if not current_key:
        return "⚠️ *AI ADVISOR VERDICT: REJECTED ENGINE*\n_Reason: Gemini API Key configuration missing on server._"

    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    today_str = datetime.now(ist_zone).strftime("%Y-%m-%d")
    today_candles = intraday_df[intraday_df['timestamp'].str.startswith(today_str)].tail(40)
    
    candle_history_text = ""
    for _, row in today_candles.iterrows():
        candle_history_text += (
            f"Time: {row['timestamp']} | O: {row['open']} | H: {row['high']} | "
            f"L: {row['low']} | C: {row['close']} | Vol: {row['volume']} | "
            f"RSI: {round(row.get('RSI', 0), 2)} | RSI_SMA: {round(row.get('RSI_SMA', 0), 2)} | "
            f"EMA_200: {round(row.get('EMA_200', 0), 2)}\n"
        )

    prompt = (
        f"You are an elite, high-conviction institutional Risk Officer and Trading Advisor for the Indian Stock Market.\n\n"
        f"DATA PACKAGE FOR {ticker_name}:\n"
        f"1. LIVE ORDER BOOK DEPTH PRESSURE:\n{depth_summary}\n\n"
        f"2. INTRADAY 5-MINUTE CANDLE TREND HISTORY (WITH STRATEGY METRICS):\n{candle_history_text}\n\n"
        f"YOUR INSTRUCTIONS:\n"
        f"- Analyze the overall chart trend using the candle history provided (look for higher highs, distribution blocks, distance from the 200 EMA).\n"
        f"- Factor in the Order Book Market Depth to evaluate real-time liquidity pressure.\n"
        f"- Dynamically retrieve the latest breaking news, corporate actions, or sector trends affecting {ticker_name} today.\n"
        f"- Provide a formal advisory verdict. You must decide whether you approve or advise against this entry based on conflicts or alignment between the indicators, volume, depth, and news.\n\n"
        f"OUTPUT FORMAT (Strictly return exactly this template format with no extra pleasantries):\n"
        f"🧠 *AI ADVISOR VERDICT:* [VALIDATED ENTRY] or [⚠️ ADVISE TO AVOID / LEAVE IT]\n"
        f"────────────────────────\n"
        f"📖 *ADVISORY ANALYSIS:* (Provide exactly 3-4 structural sentences detailing the breakdown of price action, order book layout, and today's news catalysts to back up your decision.)"
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
                
    return "⚠️ *AI ADVISOR VERDICT: SERVER TIMEOUT*\n_Reason: Google Free-Tier endpoints are busy. Execute manually using chart verification._"

# ==============================================================================
# TELEGRAM NOTIFICATION OUTBOUND PIPELINE
# ==============================================================================
def send_telegram_alert(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        response = requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"❌ Telegram pipeline fail: {e}")

# ==============================================================================
# STRATEGY ENGINE
# ==============================================================================
def check_for_signals(ticker, smart_api_object=None, token=None, is_live=False):
    if not is_live:
        return

    ist_zone = zoneinfo.ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist_zone)
    
    if not (now_ist.hour == 9 and now_ist.minute >= 25) and not (10 <= now_ist.hour < 13) and not (now_ist.hour == 13 and now_ist.minute <= 30):
        return

    df = DATA_CACHE.get(ticker)
    if df is None or len(df) < 200:
        return

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

    purchasing_power = CASH_CAPITAL_PER_TRADE * LEVERAGE_MULTIPLIER
    trade_quantity = int(purchasing_power // current_close)
    
    take_profit_price = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
    stop_loss_estimate = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)

    if current_rsi < 30 and current_state == "READY":
        if previous_bar['RSI'] <= previous_bar['RSI_SMA'] and latest_bar['RSI'] > latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_BUY"
            trend_status = "🔥 *HIGH PROBABILITY (UPTREND)*" if current_close >= current_ema200 else "⚠️ *COUNTER-TREND BUY (RISKY)*"
            
            depth_data = fetch_market_depth_ratio(smart_api_object, "NSE", token, ticker)
            ai_advisor_block = generate_ai_advisor_analysis(ticker, df, depth_data)

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
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {latest_bar['timestamp']} IST"
            )
            send_telegram_alert(alert_msg)

    elif current_rsi > 70 and current_state == "READY":
        if previous_bar['RSI'] >= previous_bar['RSI_SMA'] and latest_bar['RSI'] < latest_bar['RSI_SMA']:
            STRATEGY_STATES[ticker] = "LOCKED_SHORT"
            
            take_profit_short = round(current_close * (1.0 - PROFIT_TARGET_PERCENT), 2)
            stop_loss_short = round(current_close * (1.0 + PROFIT_TARGET_PERCENT), 2)
            trend_status = "🔥 *HIGH PROBABILITY SHORT (DOWNTREND)*" if current_close <= current_ema200 else "⚠️ *COUNTER-TREND SHORT (RISKY)*"
            
            depth_data = fetch_market_depth_ratio(smart_api_object, "NSE", token, ticker)
            ai_advisor_block = generate_ai_advisor_analysis(ticker, df, depth_data)

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
                f"{ai_advisor_block}\n"
                f"────────────────────────\n"
                f"🕒 *Candle Stamp:* {latest_bar['timestamp']} IST"
            )
            send_telegram_alert(alert_msg)

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
        # Double check cache initialization to completely prevent KeyError events
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
                    "fromdate": (now_ist - timedelta(days=30)).strftime("%Y-%m-%d %H:%M"),
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
        
        # 🟢 CRITICAL GUARD: If historical call fails completely or hits a rate-limit, 
        # initialize an empty DataFrame structural fallback so it NEVER throws a KeyError later.
        if not success:
            logging.warning(f"⚠️ Seeder rate-limited or failed for {ticker}. Initializing clean cache fallback.")
            DATA_CACHE[ticker] = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

def start_bot():
    smartApi = SmartConnect(api_key=API_KEY)
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
                # 🟢 Hardened Protection: Ensure ticker is registered in engines
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
