import re
from typing import List, Tuple

LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def split_sources(raw: str) -> List[Tuple[str, str]]:
    """
    Находит все ссылки в тексте и пытается сопоставить им “ближайшее” описание (meta).
    Простой, но практичный подход:
      - если одна ссылка — вернём один элемент [(meta_всё, ссылка)] как раньше
      - если ссылок несколько — режем по ссылкам и берём текст между ними как meta
    Возвращает список [(meta, link), ...] по порядку.
    """
    text = (raw or "").strip()
    links = list(LINK_RE.finditer(text))
    if not links:
        return []

    if len(links) == 1:
        return [(LINK_RE.sub("", text).strip(), links[0].group(0))]

    # Несколько ссылок: распределяем куски текста между ними
    result: List[Tuple[str, str]] = []
    last_end = 0
    for i, m in enumerate(links):
        link = m.group(0)
        start = m.start()
        # meta — это текст с предыдущего конца до начала текущей ссылки (без других ссылок)
        meta_chunk = text[last_end:start].strip()
        # уберём саму ссылку из meta на всякий
        meta_chunk = LINK_RE.sub("", meta_chunk).strip()
        result.append((meta_chunk, link))
        last_end = m.end()

    # хвост после последней ссылки добавим к последнему meta
    tail = text[last_end:].strip()
    if tail and result:
        m_last, l_last = result[-1]
        tail = LINK_RE.sub("", tail).strip()
        result[-1] = ((m_last + " " + tail).strip(), l_last)

    return result


