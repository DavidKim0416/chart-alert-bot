import os
import requests
from fastapi import FastAPI, Request
import google.generativeai as genai

app = FastAPI()

# 환경 변수 설정
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

@app.get("/")
def health_check():
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        return {"status": "ok", "available_models": models}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
async def tradingview_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}

    ticker = data.get("ticker", "미지정 종목")
    signal = data.get("signal", "조건 만족 신호 발생")
    price = data.get("price", "현재가 정보 없음")

    prompt = f"""
    코인 티커: {ticker}
    현재 가격: {price}
    발생 신호: {signal}

    위 신호를 바탕으로 다음 항목을 텔레그램 메시지용으로 짧고 명확하게 분석해줘:
    1. 핵심 지지 및 저항선
    2. 추천 포지션 (롱 또는 숏) 및 근거
    3. 손절 라인(SL)과 1차 목표가(TP)
    """

    try:
        model = genai.GenerativeModel("gemini-3.8-flash")
        response = model.generate_content(prompt)
        analysis_result = response.text

        # 텔레그램 전송
        message = f"🚨 *[{ticker}] 차트 자동 분석 알림*\n\n{analysis_result}"
        send_telegram(message)
        return {"status": "success"}

    except Exception as e:
        send_telegram(f"❌ 분석 중 오류 발생: {str(e)}")
        return {"status": "error", "message": str(e)}
