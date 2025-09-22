# app/services/amvera_service.py
from typing import List, Dict, Any, Optional
from . import http_client
from ..config.settings import AMVERA_TOKEN, AMVERA_BASE, AMVERA_MODEL, AMVERA_TIMEOUT, logger

AMVERA_ENDPOINT = f"{AMVERA_BASE}/models/llama"

def _norm_messages(system: Optional[str], user: str) -> List[Dict[str, Any]]:
    msgs: List[Dict[str, Any]] = []
    if system:
        msgs.append({"role": "system", "text": system})
    msgs.append({"role": "user", "text": user})
    return msgs

async def amvera_chat(
    user_text: str,
    system_text: Optional[str] = None,
    model: Optional[str] = None,
    json_mode: bool = False,
    json_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Возвращает dict:
      {
        "ok": bool,
        "text": str,        # ответ модели (или "")
        "raw": dict | None, # сырой ответ
        "error": str | None # описание ошибки
      }
    """
    if http_client.client is None:
        logger.error("HTTP client is not initialized")
        return {"ok": False, "text": "", "raw": None, "error": "http client not initialized"}

    if not AMVERA_TOKEN:
        return {"ok": False, "text": "", "raw": None, "error": "AMVERA_TOKEN is empty"}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Auth-Token": f"Bearer {AMVERA_TOKEN}",
    }

    payload: Dict[str, Any] = {
        "model": model or AMVERA_MODEL,   # "llama8b"
        "messages": _norm_messages(system_text, user_text),
    }
    if json_mode:
        payload["jsonObject"] = True
        if json_schema:
            payload["jsonSchema"] = {"schema": json_schema}

    try:
        r = await http_client.client.post(
            AMVERA_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=AMVERA_TIMEOUT,
        )
        data = r.json()
        if r.status_code >= 400:
            desc = data.get("message") or data.get("description") or r.text
            logger.error(f"Amvera llama error {r.status_code}: {desc}")
            return {"ok": False, "text": "", "raw": data, "error": desc}

        # ожидаем:
        # { "alternatives": [ { "message": { "text": "...", "role": "assistant", ... }, "status": "..." } ], "usage": {...} }
        alts = (data or {}).get("alternatives") or []
        text = ""
        if alts and isinstance(alts, list):
            msg = (alts[0] or {}).get("message") or {}
            text = (msg.get("text") or "").strip()

        if not text:
            logger.info("Amvera returned empty text")
            return {"ok": False, "text": "", "raw": data, "error": "empty completion"}

        return {"ok": True, "text": text, "raw": data, "error": None}

    except Exception as e:
        logger.exception(f"Amvera llama request failed: {e}")
        return {"ok": False, "text": "", "raw": None, "error": str(e)}
