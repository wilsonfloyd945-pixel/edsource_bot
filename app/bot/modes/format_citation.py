import asyncio
from typing import Optional, Dict, Any, List
from ..sessions import SESSIONS, ensure_session
from ..formatting import LINK_RE, first_formatted_line
from ..ui import SYSTEM_PROMPT_FORMATTER, menu_keyboard
from ...services.telegram_service import tg_send_message, tg_edit_message, tg_send_action
from ...config.settings import MODEL_PROVIDER
from ...services.zai_service import call_llm as call_zai
from ...services.deepseek_service import call_deepseek
from ...config.settings import MODEL_WATCHDOG_SECONDS
from ..tasks import fire_and_forget
from datetime import datetime
from ..splitter import split_sources
from ...services.amvera_service import amvera_chat


async def enter_mode(chat_id: int) -> None:
    SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}
    await tg_send_message(
        chat_id,
        "Режим оформления включён. Пришлите источник (название/журнал/год/том/стр/DOI) и гиперссылку. Можно по очереди.",
        reply_markup=menu_keyboard()
    )


async def handle_message(chat_id: int, text: str) -> None:
    sess = ensure_session(chat_id)
    parts = sess["parts"]
    txt = (text or "").strip()

    pairs = split_sources(txt)
    if len(pairs) > 1:
        # Если в сообщении сразу несколько источников — обработаем их последовательно одним воркером
        # Поставим небольшой “плейсхолдер”
        await tg_send_action(chat_id, "typing")
        placeholder_id = await tg_send_message(chat_id, f"Нашёл {len(pairs)} источника(ов). Обрабатываю по очереди…", reply_markup=menu_keyboard())

        # Последовательно прогоняем каждый (meta, link)
        for idx, (meta_i, link_i) in enumerate(pairs, start=1):
            # Собираем parts-формат под существующий воркер
            parts_i = {"meta": meta_i, "link": link_i}
            # Можно обновить плейсхолдер статусом
            if placeholder_id:
                await tg_edit_message(chat_id, placeholder_id, f"Обрабатываю {idx}/{len(pairs)}…")
            await _format_worker(chat_id, parts_i, None)  # последовательная обработка

        # Завершаем сообщением “готово” (опционально)
        if placeholder_id:
            await tg_edit_message(chat_id, placeholder_id, "Готово ✅")
        return

    urls = LINK_RE.findall(txt)
    if urls:
        if not parts.get("link"):
            parts["link"] = urls[0]
        meta_candidate = LINK_RE.sub("", txt).strip()
        if meta_candidate:
            if parts.get("meta"):
                parts["meta"] = (parts["meta"] + " " + meta_candidate).strip()
            else:
                parts["meta"] = meta_candidate
    else:
        if parts.get("meta"):
            parts["meta"] = (parts["meta"] + " " + txt).strip()
        else:
            parts["meta"] = txt

    if parts.get("link") and parts.get("meta"):
        await tg_send_action(chat_id, "typing")
        placeholder_id = await tg_send_message(chat_id, "Оформляю…", reply_markup=menu_keyboard())
        fire_and_forget(_format_worker(chat_id, parts.copy(), placeholder_id))
    else:
        if not parts.get("link"):
            await tg_send_message(chat_id, "Пришлите гиперссылку на источник (начинается с http/https).", reply_markup=menu_keyboard())
        elif not parts.get("meta"):
            await tg_send_message(chat_id, "Пришлите данные об источнике (название, журнал/место публикации, год, том/номер, страницы, DOI).", reply_markup=menu_keyboard())


async def _format_worker(chat_id: int, parts: Dict[str, Any], placeholder_id: Optional[int]) -> None:
    sess = ensure_session(chat_id)
    provider = (sess.get("llm") or MODEL_PROVIDER or "amvera").lower()  
    
    # сегодняшняя дата (для правовых источников нужно "Дата обращения")
    today = datetime.now().strftime("%d.%m.%Y")

    # формируем payload для модели: передаём TODAY, meta и ссылку
    user_payload = f"TODAY={today}\n{parts.get('meta','')}\n{parts.get('link','')}".strip()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_FORMATTER},
        {"role": "user", "content": user_payload},
    ]
    try:

        if provider == "amvera":
            res = await amvera_chat(user_payload, system_text=SYSTEM_PROMPT_FORMATTER)
            if res.get("ok"):
                raw = res["text"]
            else:
                raw = f"Ошибка Amvera: {res.get('error') or 'нет ответа'}"

        elif provider == "deepseek-chat":
            raw = await asyncio.wait_for(call_deepseek(messages), timeout=MODEL_WATCHDOG_SECONDS)


        else:  # 'zai' или что-то ещё
            raw = await asyncio.wait_for(call_zai(messages), timeout=MODEL_WATCHDOG_SECONDS)

        formatted = first_formatted_line(raw, fallback_link=parts.get("link"), fallback_meta=parts.get("meta"))
        if len(formatted) > 4096:
            formatted = formatted[:4090] + "…"
        out = formatted
    except asyncio.TimeoutError:
        out = "Сервис отвечает дольше обычного. Попробуйте ещё раз."
    except Exception:
        out = "Не удалось оформить источник. Попробуйте ещё раз."

    if placeholder_id:
        ok = await tg_edit_message(chat_id, placeholder_id, out)
        if not ok:
            await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
    else:
        await tg_send_message(chat_id, out, reply_markup=menu_keyboard())

    SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}
