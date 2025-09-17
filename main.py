import os, textwrap
from fastapi import FastAPI, Request, Header, HTTPException
import httpx

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
Z_AI_API_KEY = os.environ["Z_AI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "default_secret")

app = FastAPI()

@app.get("/")
def health():
    return {"ok": True}

@app.post("/webhook/{path_secret}")
async def tg_webhook(request: Request, path_secret: str):
    # Проверяем секрет из URL пути
    if path_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not (text and chat_id):
        return {"status": "ignored"}

    try:
        # Отправляем запрос к z.ai API
        zai_url = "https://api.z.ai/v1/chat/completions"
        api_key = Z_AI_API_KEY.strip().replace('\n', '').replace('\r', '')
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "zephyr-7b-beta",
            "messages": [
                {"role": "user", "content": text}
            ],
            "max_tokens": 1000,
            "temperature": 0.7
        }
        
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.post(zai_url, headers=headers, json=data)
            response.raise_for_status()
            result = response.json()
            reply = result["choices"][0]["message"]["content"].strip()
            
    except Exception as e:
        reply = f"Ошибка: {e}"

    if len(reply) > 4096:
        reply = reply[:4090] + "…"

    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as http:
        await http.post(send_url, json={"chat_id": chat_id, "text": reply})

    return {"status": "sent"}
