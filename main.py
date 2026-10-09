import os
import time
import threading
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from pydantic import BaseModel
import google.generativeai as genai

app = FastAPI()

# 환경 변수 로드 및 유효성 검사
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not GEMINI_KEY:
    print("경고: GEMINI_API_KEY 환경 변수가 없습니다.")
else:
    genai.configure(api_key=GEMINI_KEY)

if not TG_TOKEN or not TG_CHAT_ID:
    print("경고: TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 환경 변수가 없습니다.")

# 텔레그램 메시지 발송 함수 (기본)
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

# 보조지표 RSI 계산 함수 (Wilder's Smoothing 표준 공식)
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

# 실시간 가격 조회 및 대화형 Gemini 분석 함수 (RSI 결합)
# 낮은 파이썬 버전 호환을 위해 f-string을 .format()으로 교체
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
                    user_query + "USDT",
                    "1000" + user_query + "USDT",
                    "10000" + user_query + "USDT",
                    "1000000" + user_query + "USDT"
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
            mexc_url = "https://api.mexc.com/api/v3/ticker/price?symbol=" + user_query + "USDT"
            m_res = requests.get(mexc_url, headers=headers, timeout=3)
            if m_res.status_code == 200:
                p = float(m_res.json().get("price", 0))
                if p > 0:
                    last_price = p
                    target_symbol = user_query + "USDT"
                    source = "MEXC"
        except Exception:
            pass

    if not last_price or last_price == 0:
        return "⚠️ '{}' 종목의 실시간 호가를 찾을 수 없습니다. 심볼명을 다시 확인해 주세요.".format(ticker)

    # [3단계] 1시간봉 캔들 종가 수집 및 RSI 실시간 계산 (차단 우회 피드)
    rsi_str = "미제공"
    closes = []

    try:
        cand_url = "https://data-api.binance.vision/api/v3/klines?symbol=" + target_symbol + "&interval=1h&limit=30"
        c_res = requests.get(cand_url, headers=headers, timeout=3)
        if c_res.status_code == 200:
            closes = [float(k[4]) for k in c_res.json()]
    except Exception:
        pass

    if not closes or len(closes) < 15:
        try:
            clean_sym = target_symbol.replace("USDT", "")
            cc_kline = "https://min-api.cryptocompare.com/data/v2/histohour?fsym=" + clean_sym + "&tsym=USDT&limit=30"
            cc_res = requests.get(cc_kline, headers=headers, timeout=3)
            if cc_res.status_code == 200:
                data_list = cc_res.json().get("Data", {}).get("Data", [])
                if data_list:
                    closes = [float(item["close"]) for item in data_list]
        except Exception:
            pass

    if closes and len(closes) >= 15:
        rsi_vals = calculate_rsi(closes, period=14)
        if rsi_vals:
            rsi_str = "{:.1f}".format(rsi_vals[-1])

    # 구형 파이썬 버전 호환을 위해 f-string 해제
    prompt = """
당신은 전문 가상자산 퀀트 트레이더입니다.
[필수 지침]
반드시 전달받은 실시간 기준 가격({:.8f} USDT)과 1시간봉 RSI 지표({})를 종합적으로 고려하여 현재 차트 구조를 분석하고 지지선, 저항선, 진입가, 익절가, 손절가를 산출하세요.

- 분석 종목: {} ({})
- 실시간 현재가: {:.8f} USDT
- 1시간봉 RSI(14): {}

아래 형식으로 명확하고 간결하게 한국어로 브리핑하세요:

[ {} 실시간 전략 브리핑 ]
• 기준 체결가: {:.8f} USDT ({})
• 1시간봉 RSI: {}
1. RSI 및 시장 모멘텀 진단 (과매수/과매도/골든크로스 여부)
2. 단기 핵심 지지선 및 저항선
3. 추천 예상 눌림목 진입 구간 (Pullback Entry)
4. 목표 익절 구간 (1차 TP, 2차 TP)
5. 손절 기준가 (SL)
""".format(last_price, rsi_str, target_symbol, source, last_price, rsi_str, target_symbol, last_price, source, rsi_str)
    
    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return "AI 분석 생성 중 오류: {}".format(str(e))

# 비동기 분석 실행 함수
def process_coin_analysis(ticker: str, chat_id: str):
    send_telegram_message("🔍 {} 실시간 호가 및 퀀트 분석 중입니다...".format(ticker.upper()), target_chat_id=chat_id)
    report = analyze_requested_coin(ticker)
    send_telegram_message(report, target_chat_id=chat_id)

# 텔레그램 채팅 수신 엔드포인트
@app.post("/telegram-webhook")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        data = await request.json()
        if "message" in data and "text" in data["message"]:
            chat_id = str(data["message"]["chat"]["id"])
            user_text = data["message"]["text"].strip()

            if user_text.startswith("/start"):
                send_telegram_message("코인 심볼(예: BTC, ETH, BULLA)을 입력하시면 실시간 전략 분석을 제공해 드립니다.", target_chat_id=chat_id)
                return {"status": "ok"}

            ticker = user_text.replace("/분석", "").strip()
            if ticker:
                background_tasks.add_task(process_coin_analysis, ticker, chat_id)
    except Exception as e:
        print("Telegram webhook handling error: {}".format(e))
    return {"status": "ok"}

# 24시간 자동 스캐너 (30% 변동성 + 바이낸스 선물 필터링 + RSI 감지)
ALERTED_COINS = {}

def market_scanner_loop():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
    }
    while True:
        try:
            # 1단계: 실시간 바이낸스 '선물' 가격 목록 (미러)
            mirror_fapi = "https://fapi.binance.vision/fapi/v1/ticker/price"
            f_res = requests.get(mirror_fapi, headers=headers, timeout=15)
            if f_res.status_code != 200:
                time.sleep(10)
                continue
            
            futures_list = f_res.json()
            futures_price_map = {item['symbol']: float(item['price']) for item in futures_list}
            
            # 2단계: 24시간 변동성 전체 데이터 수집 (스캔용)
            scanner_feed = "https://data-api.binance.vision/api/v3/ticker/24hr"
            res = requests.get(scanner_feed, headers=headers, timeout=15)
            if res.status_code == 200:
                data = res.json()
                current_time = time.time()
                
                for item in data:
                    symbol = item.get("symbol", "")
                    if not symbol.endswith("USDT"):
                        continue
                    
                    last_price = float(item.get("lastPrice", 0))

                    # [핵심] 바이낸스 선물 필터링
                    if symbol not in futures_price_map:
                        continue

                    # 가격 오류 차단
                    if last_price <= 0:
                        continue

                    # 3단계: 1시간봉 캔들 데이터 수집
                    kline_url = "https://data-api.binance.vision/api/v3/klines?symbol=" + symbol + "&interval=1h&limit=20"
                    k_res = requests.get(kline_url, headers=headers, timeout=4)
                    if k_res.status_code != 200:
                        continue
                    k_data = k_res.json()
                    if len(k_data) < 16:
                        continue

                    open_price = float(k_data[-1][1])
                    high_price = float(k_data[-1][2])
                    low_price = float(k_data[-1][3])
                    if open_price <= 0:
                        continue

                    net_change = ((last_price - open_price) / open_price) * 100
                    volatility = ((high_price - low_price) / low_price) * 100

                    closes = [float(k[4]) for k in k_data]
                    rsi_vals = calculate_rsi(closes, period=14)
                    rsi_gc = False
                    current_rsi = 0
                    if rsi_vals and len(rsi_vals) >= 2:
                        prev_rsi = rsi_vals[-2]
                        current_rsi = rsi_vals[-1]
                        if prev_rsi <= 20.0 and current_rsi > 20.0:
                            rsi_gc = True

                    trigger = False
                    signal_text = ""

                    if rsi_gc:
                        trigger = True
                        signal_text = "📈 RSI 극과매도 탈출 골든크로스 (RSI: {})".format("{:.1f}".format(current_rsi))
                    elif abs(net_change) >= 30.0:
                        trigger = True
                        direction = "🚀 1시간 초급등" if net_change > 0 else "🩸 1시간 초급락"
                        signal_text = "{} ({:+.2f}%)".format(direction, net_change)
                    elif volatility >= 30.0:
                        trigger = True
                        signal_text = "⚡ 메가 스파이크 (고저폭: {:.2f}%)".format(volatility)

                    if trigger:
                        if symbol in ALERTED_COINS and (current_time - ALERTED_COINS[symbol]) < 7200:
                            continue

                        clean_name = symbol.replace("USDT", "")
                        # 가독성을 위해 f-string 해제
                        msg = """🔔 *[24시 시장 긴급 감지]*
• 종목: `{}` (바이낸스 선물 상장)
• 신호: *{}*
• 현재가: `{}` (고가: {} / 저가: {})

👉 상세 AI 퀀트 분석이 필요하시거나 포지션 수립을 원하시면 채팅방에 `{}`를 입력하세요.""".format(symbol, signal_text, last_price, high_price, low_price, clean_name)
                        send_telegram_message(msg)
                        ALERTED_COINS[symbol] = current_time
                        time.sleep(2)
        except Exception as e:
            print("Scanner Loop Error: {}".format(e))
        time.sleep(60)

@app.on_event("startup")
def startup_event():
    t = threading.Thread(target=market_scanner_loop, daemon=True)
    t.start()

# 트레이딩뷰 웹훅 연동 엔드포인트
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
