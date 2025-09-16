import os, textwrap
from fastapi import FastAPI, Request, Header, HTTPException
import httpx
import google.generativeai as genai

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-exp')

app = FastAPI()

@app.get("/")
def health():
    return {"ok": True}

@app.post("/webhook/{path_secret}")
async def tg_webhook(request: Request,
                     path_secret: str,
                     x_telegram_bot_api_secret_token: str | None = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not (text and chat_id):
        return {"status": "ignored"}

    try:
        response = model.generate_content(text)
        reply = (response.text or "Пустой ответ").strip()
    except Exception as e:
        reply = f"Ошибка: {e}"

    if len(reply) > 4096:
        reply = reply[:4090] + "…"

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as http:
        await http.post(send_url, json={"chat_id": chat_id, "text": reply})

    return {"status": "sent"}
