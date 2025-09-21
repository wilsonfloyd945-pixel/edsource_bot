from typing import Dict, Any

SESSIONS: Dict[int, Dict[str, Any]] = {}
LAST_USED_AT: Dict[int, float] = {}

def ensure_session(chat_id: int) -> Dict[str, Any]:
    s = SESSIONS.get(chat_id)
    if not s:
        s = {"mode": "menu", "parts": {"link": None, "meta": ""}}
        SESSIONS[chat_id] = s
    if "parts" not in s:
        s["parts"] = {"link": None, "meta": ""}
    return s
