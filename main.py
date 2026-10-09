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

# 3. 실시간 가격 조회 및 대화형 Gemini 분석 함수
def analyze_requested_coin(ticker: str) -> str:
    raw_input = ticker.strip().upper().replace("USDT", "")
    
    variants = [raw_input]
    if raw_input.startswith("1000"):
        variants.append(raw_input.replace("1000", "", 1))
    else:
        variants.append(f"1000{raw_input}")

    last_price = None
    high_price = None
    low_price = None
    target_symbol = raw_input
    source = ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }

    # 1차: CryptoCompare (클라우드 IP 차단 없음)
    for v in variants:
        try:
            cc_url = f"https://min-api.cryptocompare.com/data/pricemultifull?fsyms={v}&tsyms=USD,USDT"
            r = requests.get(cc_url, headers=headers, timeout=3)
            if r.status_code == 200:
                res_data = r.json().get("RAW", {}).get(v, {})
                quote = res_data.get("USDT") or res_data.get("USD")
                if quote and quote.get("PRICE"):
                    last_price = float(quote.get("PRICE"))
                    high_price = float(quote.get("HIGHDAY", 0))
                    low_price = float(quote.get("LOWDAY", 0))
                    target_symbol = v
                    source = "글로벌 시세"
                    break
        except Exception:
            pass

    # 2차: Binance 글로벌 퍼블릭 Mirror API
    if not last_price:
        for v in variants:
            sym = f"{v}USDT"
            try:
                bn_url = f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={sym}"
                res = requests.get(bn_url, headers=headers, timeout=3)
                if res.status_code == 200:
                    d = res.json()
                    last_price = float(d.get("lastPrice", 0))
                    high_price = float(d.get("highPrice", 0))
                    low_price = float(d.get("lowPrice", 0))
                    target_symbol = v
                    source = "Binance"
                    break
            except Exception:
                pass

    if not last_price:
        return f"⚠️ '{ticker}' 종목의 실시간 호가를 가져올 수 없습니다. 심볼명을 다시 확인해 주세요."

    prompt = f"""
당신은 전문 가상자산 퀀트 트레이더입니다.
[필수 지침]
반드시 전달받은 실시간 기준 가격({last_price:,.6f} USDT)을 기준으로 현재 차트 구조를 분석하고 지지선, 저항선, 진입가, 익절가, 손절가를 산출하세요.

- 분석 종목: {target_symbol}/USDT ({source})
- 실시간 현재가: {last_price:,.6f} USDT (24h 고가: {high_price:,.6f} / 24h 저가: {low_price:,.6f})

아래 형식으로 명확하고 간결하게 한국어로 브리핑하세요:

[ {target_symbol} 실시간 전략 브리핑 ]
• 기준 체결가: {last_price:,.6f} USDT
1. 모멘텀 및 차트 구조 진단
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

# 4. 비동기 분석 실행 함수
def process_coin_analysis(ticker: str, chat_id: str):
    send_telegram_message(f"🔍 {ticker.upper()} 실시간 호가 및 퀀트 분석 중입니다...", target_chat_id=chat_id)
    report = analyze_requested_coin(ticker)
    send_telegram_message(report, target_chat_id=chat_id)

# 5. 텔레그램 채팅 수신 엔드포인트
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"].strip()

            if user_text.startswith("/start"):
                send_telegram_message("코인 심볼(예: BTC, ETH, SOL)을 입력하시면 실시간 지지/저항, 예상 눌림목, 목표 익절가를 분석해 드립니다.", target_chat_id=chat_id)
                return {"status": "ok"}

            ticker = user_text.replace("/분석", "").strip()
            if ticker:
                background_tasks.add_task(process_coin_analysis, ticker, chat_id)
    except Exception as e:
        print(f"Telegram webhook handling error: {e}")
    return {"status": "ok"}

# 6. 기존 24시간 자동 스캐너 백그라운드 스레드
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

                        msg = (
                            f"🔔 [24시 변동성 긴급 감지]\n"
                            f"• 종목: {symbol}\n"
                            f"• 신호: {signal_text}\n"
                            f"• 현재가: {last_price} (고가: {high_price} / 저가: {low_price})\n\n"
                            f"👉 상세 AI 퀀트 분석이 필요하시면 채팅방에 '{symbol.replace('USDT', '')}'를 입력하세요."
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

# 7. 기본 엔드포인트
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
