import os
import time
import threading
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
import google.generativeai as genai

app = FastAPI()

# 1. 환경 변수 로드
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)

# 2. 텔레그램 메시지 발송 함수
def send_telegram_message(text: str, target_chat_id: str = None):
    chat_id = target_chat_id or TG_CHAT_ID
    if not TG_TOKEN or not chat_id:
        return "Credentials missing"
    
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram Send Error: {e}")
        return str(e)

# 3. 보조지표 RSI 계산 함수 (Wilder's Smoothing 표준 공식)
def calculate_rsi(prices, period=14):
    if not prices or len(prices) < period + 1:
        return None
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_values = []
    for i in range(period, len(prices)):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period

        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_values.append(100.0 - (100.0 / (1.0 + rs)))

    return rsi_values

# 4. 실시간 가격 조회 및 대화형 Gemini 분석 함수 (RSI 결합)
def analyze_requested_coin(ticker: str) -> str:
    user_query = ticker.strip().upper().replace("USDT", "")
    
    last_price = None
    target_symbol = ""
    source = ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }

    # [1단계] 바이낸스 전용 가격 리스트 (403 우회 미러 엔드포인트)
    fapi_sources = [
        "https://data-api.binance.vision/api/v3/ticker/price",
        "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://api.binance.com/api/v3/ticker/price"
    ]

    for endpoint in fapi_sources:
        try:
            res = requests.get(endpoint, headers=headers, timeout=3)
            if res.status_code == 200:
                tickers_list = res.json()
                exact_candidates = [
                    f"{user_query}USDT",
                    f"1000{user_query}USDT",
                    f"10000{user_query}USDT",
                    f"1000000{user_query}USDT"
                ]
                price_dict = {item.get("symbol", ""): item.get("price") for item in tickers_list}
                
                for candidate in exact_candidates:
                    if candidate in price_dict:
                        last_price = float(price_dict[candidate])
                        target_symbol = candidate
                        source = "바이낸스"
                        break

                if not last_price:
                    for sym, pr in price_dict.items():
                        if sym.endswith("USDT") and user_query in sym:
                            clean_base = sym.replace("USDT", "").replace("1000000", "").replace("10000", "").replace("1000", "")
                            if clean_base == user_query:
                                last_price = float(pr)
                                target_symbol = sym
                                source = "바이낸스"
                                break

            if last_price and last_price > 0:
                break
        except Exception:
            pass

    # [2단계] MEXC API (바이낸스 미상장 코인)
    if not last_price:
        try:
            mexc_url = f"https://api.mexc.com/api/v3/ticker/price?symbol={user_query}USDT"
            m_res = requests.get(mexc_url, headers=headers, timeout=3)
            if m_res.status_code == 200:
                p = float(m_res.json().get("price", 0))
                if p > 0:
                    last_price = p
                    target_symbol = f"{user_query}USDT"
                    source = "MEXC"
        except Exception:
            pass

    if not last_price or last_price == 0:
        return f"⚠️ '{ticker}' 종목의 실시간 호가를 찾을 수 없습니다. 심볼명을 다시 확인해 주세요."

    # [3단계] 1시간봉 캔들 종가 수집 및 RSI 실시간 계산 (차단 우회 피드)
    rsi_str = "미제공"
    closes = []

    try:
        cand_url = f"https://data-api.binance.vision/api/v3/klines?symbol={target_symbol}&interval=1h&limit=30"
        c_res = requests.get(cand_url, headers=headers, timeout=3)
        if c_res.status_code == 200:
            closes = [float(k[4]) for k in c_res.json()]
    except Exception:
        pass

    if closes and len(closes) >= 15:
        rsi_vals = calculate_rsi(closes, period=14)
        if rsi_vals:
            rsi_str = f"{rsi_vals[-1]:.1f}"

    prompt = f"""
당신은 전문 가상자산 퀀트 트레이
