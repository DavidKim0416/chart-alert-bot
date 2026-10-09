import os
import time
import threading
import requests
from fastapi import FastAPI
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
def send_telegram_message(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

# 3. Gemini 3.8 Flash 시장 분석 함수
def analyze_with_gemini(ticker: str, signal: str, price: str) -> str:
    prompt = f"""
    당신은 전문 가상자산 퀀트 트레이더입니다.
    현재 암호화폐 시장에서 다음 신호가 감지되었습니다:
    - 종목: {ticker}
    - 감지된 신호: {signal}
    - 현재가: {price}

    다음 형식에 맞춰 핵심만 한국어로 간결하게 브리핑해주세요:
    1. 시장 상태 요약 (급등/급락 원인 및 모멘텀)
    2. 단기 지지선 및 저항선 가격대 제시
    3. 추천 대응 전략 (돌파 추종 / 조정 매수 / 관망 등)
    4. 손절 기준 가격 제시
    """
    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"분석 생성 중 오류: {str(e)}"

# 4. 전체 시장 1시간 변동률 24시간 자동 감시 스레드 (Binance USDT 선물 기준)
ALERTED_COINS = {}  # 동일 코인 중복 알림 방지 캐시 (심볼: 마지막 알림 타임스탬프)

def market_scanner_loop():
    print("Market Scanner Thread Started...")
    while True:
        try:
            # 바이낸스 USDT 선물 전체 종목의 캔들 데이터 조회 (1시간 변동 감지)
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            res = requests.get(url, timeout=15)
            
            if res.status_code == 200:
                data = res.json()
                current_time = time.time()

                for item in data:
                    symbol = item.get("symbol", "")
                    if not symbol.endswith("USDT"):
                        continue

                    # 최근 1시간(60분) 변동률 정밀 계산 (1시간봉 시가 대비 현재가)
                    # 24hr 데이터 중 최근 급변 종목을 선별하여 1시간 캔들 상세 검증
                    last_price = float(item.get("lastPrice", 0))
                    
                    # 1시간봉 1개 조회
                    kline_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=1"
                    k_res = requests.get(kline_url, timeout=5)
                    if k_res.status_code != 200:
                        continue
                    
                    k_data = k_res.json()
                    if not k_data:
                        continue
                    
                    open_price = float(k_data[0][1])
                    if open_price == 0:
                        continue

                    pct_change = ((last_price - open_price) / open_price) * 100

                    # 15% 이상 급등 또는 -15% 이하 급락 확인
                    if abs(pct_change) >= 15.0:
                        # 동일 코인에 대해 2시간(7200초) 이내 재알림 방지
                        if symbol in ALERTED_COINS and (current_time - ALERTED_COINS[symbol]) < 7200:
                            continue

                        direction = "🚀 1시간 15% 이상 급등" if pct_change > 0 else "🩸 1시간 15% 이상 급락"
                        signal_text = f"{direction} (변동률: {pct_change:.2f}%)"

                        # Gemini 분석 실행
                        analysis = analyze_with_gemini(symbol, signal_text, str(last_price))

                        # 텔레그램 메시지 조립 및 발송
                        msg = (
                            f"🔔 *[전체 시장 변동성 긴급 감지]*\n"
                            f"• 종목: `{symbol}`\n"
                            f"• 상태: *{signal_text}*\n"
                            f"• 현재가: `{last_price}`\n\n"
                            f"📊 *Gemini AI 트레이딩 브리핑:*\n{analysis}"
                        )
                        send_telegram_message(msg)
                        ALERTED_COINS[symbol] = current_time
                        time.sleep(2)  # 연속 발송 딜레이

        except Exception as e:
            print(f"Scanner Loop Error: {e}")

        # 60초 대기 후 다음 전체 스캔
        time.sleep(60)

# 서버 시작 시 백그라운드에서 스캐너 자동 실행
@app.on_event("startup")
def startup_event():
    t = threading.Thread(target=market_scanner_loop, daemon=True)
    t.start()

# 5. 기존 트레이딩뷰 수동 웹훅 수신용 엔드포인트도 동시 유지
class AlertData(BaseModel):
    ticker: str
    signal: str
    price: str

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Market Scanner & Webhook Bot Running"}

@app.post("/webhook")
def receive_webhook(data: AlertData):
    analysis = analyze_with_gemini(data.ticker, data.signal, data.price)
    msg = f"🔔 *[트레이딩뷰 얼럿 감지]*\n• 종목: `{data.ticker}`\n• 신호: *{data.signal}*\n• 가격: `{data.price}`\n\n📊 *Gemini AI 분석:*\n{analysis}"
    send_telegram_message(msg)
    return {"status": "success"}
