import re
from typing import Optional

LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def is_link(text: str) -> bool:
    return bool(LINK_RE.search(text))

def force_parenthesized(link: Optional[str], meta: Optional[str], model_text: str) -> str:
    txt = (model_text or "").strip()
    if txt.startswith("(") and txt.endswith(")") and "'" in txt:
        return txt  # уже ок

    lnk = (link or "").strip()
    mt  = (meta or "").strip()

    found_link = LINK_RE.search(txt)
    if not lnk and found_link:
        lnk = found_link.group(0)

    if not mt:
        mt = txt

    safe_meta = mt.replace("’", "'").replace("`", "'")
    safe_meta = safe_meta.replace("'", "’")  # апострофы внутрь

    if lnk:
        return f"({lnk} '{safe_meta}')"
    return f"({safe_meta})"

def first_formatted_line(model_text: str, fallback_link: Optional[str], fallback_meta: Optional[str]) -> str:
    out = force_parenthesized(fallback_link, fallback_meta, model_text)
    return out.replace("\r", " ").replace("\n", " ").strip()
