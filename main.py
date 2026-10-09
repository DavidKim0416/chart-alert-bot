import os
import time
import threading
import requests
from fastapi import FastAPI, Request
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
        "text": text
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram Send Error: {e}")
        return str(e)

# 3. 실시간 가격 조회 및 대화형 Gemini 분석 함수
def analyze_requested_coin(ticker: str) -> str:
    symbol = ticker.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    # 바이낸스 선물 시세 및 24시간 통계 조회
    price_info = "시세 조회 실패 (기준가 불명)"
    try:
        ticker_url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
        res = requests.get(ticker_url, timeout=5)
        if res.status_code == 200:
            d = res.json()
            last_price = d.get("lastPrice")
            high_price = d.get("highPrice")
            low_price = d.get("lowPrice")
            vol = d.get("quoteVolume")
            price_info = f"현재가: {last_price} USDT / 24h 고가: {high_price} / 24h 저가: {low_price} / 24h 거래대금: {float(vol):,.0f} USDT"
    except Exception as e:
        print(f"Price fetch error: {e}")

    prompt = f"""
    당신은 전문 가상자산 퀀트 트레이더입니다.
    사용자가 종목 분석을 요청했습니다:
    - 종목: {symbol}
    - 실시간 시장 데이터: {price_info}

    아래 포맷에 맞추어 한국어로 명확하고 간결하게 브리핑해 주세요:

    [ {symbol} 실시간 전략 브리핑 ]
    1. 현재 시장 흐름 및 모멘텀 진단
    2. 단기 핵심 지지선 및 저항선
    3. 추천 예상 눌림목 진입 구간 (Pullback Entry)
    4. 목표 익절 구간 (1차 TP, 2차 TP)
    5. 칼손절 기준가 (Stop-Loss)
    """
    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"분석 오류 발생: {str(e)}"

# 4. 텔레그램 채팅 수신 엔드포인트 (사용자 입력 처리)
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"].strip()

            # /start 명령어 처리
            if user_text.startswith("/start"):
                send_telegram_message("코인 심볼(예: BTC, ETH, SOL)을 입력하시면 실시간 지지/저항, 예상 눌림목, 목표 익절가를 분석해 드립니다.", target_chat_id=chat_id)
                return {"status": "ok"}

            # /분석 BTC 또는 단순 BTC 입력 대응
            ticker = user_text.replace("/분석", "").strip()
            if ticker:
                send_telegram_message(f"🔍 {ticker.upper()} 실시간 데이터 및 호가 분석 중입니다. 잠시만 기다려주세요...", target_chat_id=chat_id)
                report = analyze_requested_coin(ticker)
                send_telegram_message(report, target_chat_id=chat_id)
    except Exception as e:
        print(f"Telegram webhook handling error: {e}")
    return {"status": "ok"}

# 5. 기존 24시간 자동 스캐너 백그라운드 스레드
ALERTED_COINS = {}

def market_scanner_loop():
    while True:
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            res = requests.get(url, timeout=15)
            if res.status_code == 200:
                data = res.json()
                current_time = time.time()
                for item in data:
                    symbol = item.get("symbol", "")
                    if not symbol.endswith("USDT"):
                        continue
                    last_price = float(item.get("lastPrice", 0))

                    kline_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=1"
                    k_res = requests.get(kline_url, timeout=5)
                    if k_res.status_code != 200:
                        continue
                    k_data = k_res.json()
                    if not k_data:
                        continue

                    open_price = float(k_data[0][1])
                    high_price = float(k_data[0][2])
                    low_price = float(k_data[0][3])
                    if open_price == 0 or low_price == 0:
                        continue

                    net_change = ((last_price - open_price) / open_price) * 100
                    volatility = ((high_price - low_price) / low_price) * 100

                    trigger = False
                    signal_text = ""
                    if abs(net_change) >= 15.0:
                        trigger = True
                        direction = "🚀 1시간 급등" if net_change > 0 else "🩸 1시간 급락"
                        signal_text = f"{direction} ({net_change:+.2f}%)"
                    elif volatility >= 15.0:
                        trigger = True
                        signal_text = f"⚡ 거대 스파이크 (고저폭: {volatility:.2f}%, 변동: {net_change:+.2f}%)"

                    if trigger:
                        if symbol in ALERTED_COINS and (current_time - ALERTED_COINS[symbol]) < 7200:
                            continue
                        report = analyze_requested_coin(symbol)
                        msg = f"🔔 [변동성 긴급 감지]\n• 종목: {symbol}\n• 신호: {signal_text}\n\n{report}"
                        send_telegram_message(msg)
                        ALERTED_COINS[symbol] = current_time
                        time.sleep(2)
        except Exception as e:
            print(f"Scanner Loop Error: {e}")
        time.sleep(60)

@app.on_event("startup")
def startup_event():
    t = threading.Thread(target=market_scanner_loop, daemon=True)
    t.start()

# 6. 트레이딩뷰 웹훅 연동 엔드포인트
class AlertData(BaseModel):
    ticker: str
    signal: str
    price: str

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Bot is running"}

@app.post("/webhook")
def receive_webhook(data: AlertData):
    report = analyze_requested_coin(data.ticker)
    msg = f"🔔 [트레이딩뷰 얼럿 감지]\n• 종목: {data.ticker}\n• 신호: {data.signal}\n• 가격: {data.price}\n\n{report}"
    send_telegram_message(msg)
    return {"status": "success"}
