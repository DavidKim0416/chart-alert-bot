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
        "text": text
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram Send Error: {e}")
        return str(e)

# 3. 보조지표 RSI 계산 함수 (14캔들 기준)
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
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

# 4. 실시간 가격 조회 및 대화형 Gemini 분석 함수 (RSI 지표 결합)
def analyze_requested_coin(ticker: str) -> str:
    user_query = ticker.strip().upper().replace("USDT", "")
    
    last_price = None
    target_symbol = ""
    source = ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    # [1단계] 바이낸스 선물 전체 종목 실시간 가격 API
    fapi_sources = [
        "https://fapi.binance.com/fapi/v1/ticker/price",
        "https://fapi.binance.vision/fapi/v1/ticker/price",
        "https://api.binance.com/api/v3/ticker/price"
    ]

    for endpoint in fapi_sources:
        try:
            res = requests.get(endpoint, headers=headers, timeout=4)
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
                        source = "바이낸스 선물"
                        break

                if not last_price:
                    for sym, pr in price_dict.items():
                        if sym.endswith("USDT") and user_query in sym:
                            clean_base = sym.replace("USDT", "").replace("1000000", "").replace("10000", "").replace("1000", "")
                            if clean_base == user_query:
                                last_price = float(pr)
                                target_symbol = sym
                                source = "바이낸스 선물"
                                break

            if last_price and last_price > 0:
                break
        except Exception:
            pass

    # [2단계] MEXC API (미상장 코인 백업)
    if not last_price:
        try:
            mexc_url = f"https://api.mexc.com/api/v3/ticker/price?symbol={user_query}USDT"
            m_res = requests.get(mexc_url, headers=headers, timeout=3)
            if m_res.status_code == 200:
                p = float(m_res.json().get("price", 0))
                if p > 0:
                    last_price = p
                    target_symbol = f"{user_query}USDT"
                    source = "MEXC 실시간"
        except Exception:
            pass

    # [3단계] CoinGecko 검색
    if not last_price:
        try:
            cg_res = requests.get(f"https://api.coingecko.com/api/v3/search?query={user_query}", headers=headers, timeout=3)
            if cg_res.status_code == 200:
                coins = cg_res.json().get("coins", [])
                if coins:
                    cid = coins[0]["id"]
                    p_res = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={cid}&vs_currencies=usd", headers=headers, timeout=3)
                    p_val = p_res.json().get(cid, {}).get("usd")
                    if p_val and float(p_val) > 0:
                        last_price = float(p_val)
                        target_symbol = f"{user_query}USDT"
                        source = "CoinGecko 피드"
        except Exception:
            pass

    if not last_price or last_price == 0:
        return f"⚠️ '{ticker}' 종목의 실시간 호가를 찾을 수 없습니다. 심볼명을 다시 확인해 주세요."

    # 1시간봉 캔들 기반 RSI 보조지표 추출
    rsi_str = "미제공"
    try:
        k_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={target_symbol}&interval=1h&limit=30"
        k_res = requests.get(k_url, headers=headers, timeout=3)
        if k_res.status_code == 200:
            closes = [float(k[4]) for k in k_res.json()]
            rsi_vals = calculate_rsi(closes, period=14)
            if rsi_vals:
                rsi_str = f"{rsi_vals[-1]:.1f}"
    except Exception:
        pass

    prompt = f"""
당신은 전문 가상자산 퀀트 트레이더입니다.
[필수 지침]
반드시 전달받은 실시간 기준 가격({last_price:,.8f} USDT)과 1시간봉 RSI 지표({rsi_str})를 종합적으로 고려하여 현재 차트 구조를 분석하고 지지선, 저항선, 진입가, 익절가, 손절가를 산출하세요.

- 분석 종목: {target_symbol} ({source})
- 실시간 현재가: {last_price:,.8f} USDT
- 1시간봉 RSI(14): {rsi_str}

아래 형식으로 명확하고 간결하게 한국어로 브리핑하세요:

[ {target_symbol} 실시간 전략 브리핑 ]
• 기준 체결가: {last_price:,.8f} USDT ({source})
• 1시간봉 RSI: {rsi_str}
1. RSI 및 시장 모멘텀 진단 (과매수/과매도/골든크로스 여부)
2. 단기 핵심 지지선 및 저항선
3. 추천 예상 눌림목 진입 구간 (Pullback Entry)
4. 목표 익절 구간 (1차 TP, 2차 TP)
5. 손절 기준가 (SL)
"""
    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI 분석 생성 중 오류: {str(e)}"

# 5. 비동기 분석 실행 함수
def process_coin_analysis(ticker: str, chat_id: str):
    send_telegram_message(f"🔍 {ticker.upper()} 실시간 호가 및 퀀트 분석 중입니다...", target_chat_id=chat_id)
    report = analyze_requested_coin(ticker)
    send_telegram_message(report, target_chat_id=chat_id)

# 6. 텔레그램 채팅 수신 엔드포인트
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"].strip()

            if user_text.startswith("/start"):
                send_telegram_message("코인 심볼(예: BTC, BTW, VELVET, BULLA, AKE)을 입력하시면 RSI 지표가 포함된 실시간 퀀트 분석을 제공해 드립니다.", target_chat_id=chat_id)
                return {"status": "ok"}

            ticker = user_text.replace("/분석", "").strip()
            if ticker:
                background_tasks.add_task(process_coin_analysis, ticker, chat_id)
    except Exception as e:
        print(f"Telegram webhook handling error: {e}")
    return {"status": "ok"}

# 7. 24시간 자동 스캐너 (10% 변동성 + RSI 골든크로스 감지)
ALERTED_COINS = {}

def market_scanner_loop():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    while True:
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            res = requests.get(url, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                current_time = time.time()
                for item in data:
                    symbol = item.get("symbol", "")
                    if not symbol.endswith("USDT"):
                        continue
                    last_price = float(item.get("lastPrice", 0))

                    kline_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=20"
                    k_res = requests.get(kline_url, headers=headers, timeout=5)
                    if k_res.status_code != 200:
                        continue
                    k_data = k_res.json()
                    if len(k_data) < 16:
                        continue

                    open_price = float(k_data[-1][1])
                    high_price = float(k_data[-1][2])
                    low_price = float(k_data[-1][3])
                    if open_price == 0 or low_price == 0:
                        continue

                    net_change = ((last_price - open_price) / open_price) * 100
                    volatility = ((high_price - low_price) / low_price) * 100

                    # RSI 계산 및 골든크로스 판정
                    closes = [float(k[4]) for k in k_data]
                    rsi_vals = calculate_rsi(closes, period=14)
                    rsi_gc = False
                    current_rsi = 0
                    if rsi_vals and len(rsi_vals) >= 2:
                        prev_rsi = rsi_vals[-2]
                        current_rsi = rsi_vals[-1]
                        # 직전 봉에서 과매도(30 이하)였다가 현재 봉에서 30선을 상향 돌파한 경우
                        if prev_rsi <= 30.0 and current_rsi > 30.0:
                            rsi_gc = True

                    trigger = False
                    signal_text = ""

                    # 감지 조건: RSI 과매도 탈출 골든크로스 OR 10% 이상 변동
                    if rsi_gc:
                        trigger = True
                        signal_text = f"📈 RSI 과매도 탈출 골든크로스 (RSI: {current_rsi:.1f})"
                    elif abs(net_change) >= 10.0:
                        trigger = True
                        direction = "🚀 1시간 급등" if net_change > 0 else "🩸 1시간 급락"
                        signal_text = f"{direction} ({net_change:+.2f}%)"
                    elif volatility >= 10.0:
                        trigger = True
                        signal_text = f"⚡ 거대 스파이크 (고저폭: {volatility:.2f}%, 변동: {net_change:+.2f}%)"

                    if trigger:
                        if symbol in ALERTED_COINS and (current_time - ALERTED_COINS[symbol]) < 7200:
                            continue

                        clean_name = symbol.replace("USDT", "")
                        msg = (
                            f"🔔 [24시 시장 긴급 감지]\n"
                            f"• 종목: `{symbol}`\n"
                            f"• 신호: *{signal_text}*\n"
                            f"• 현재가: `{last_price}` (고가: {high_price} / 저가: {low_price})\n\n"
                            f"👉 상세 AI 퀀트 분석이 필요하시면 채팅방에 `{clean_name}`를 입력하세요."
                        )
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

# 8. 트레이딩뷰 웹훅 연동 엔드포인트
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
