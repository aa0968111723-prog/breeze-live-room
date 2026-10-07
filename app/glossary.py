"""Per-room glossary: parse, normalize, and locked-term checks.

The default table is empty. No club glossary is built in. Locked-term misses
are reported only. Replacing leftover Chinese inside `en` is not implemented.
"""

from __future__ import annotations

import unicodedata

SCHEMA_VERSION = 1
MAX_TERMS = 200
MAX_ALIASES = 8
MIN_ZH = 1
MAX_ZH = 20
MIN_ALIAS = 2
MAX_ALIAS = 20
MAX_EN = 80
MAX_CATEGORY = 20
MAX_NOTE = 80
PROMPT_LIMIT = 40
# Host PUT body. 200 terms of the field limits fit; anything larger is refused.
GLOSSARY_MAX_BODY = 256 * 1024
# Header only. Translations stay out until the user confirms them.
TEMPLATE_CSV = "zh,aliases,en,lock,category,note\n"

# Minimum traditional fold used only for matching. Output is always the canonical term.
FOLD = str.maketrans(
    "学会观经数开静禅语头话调务师处关习觉围",
    "學會觀經數開靜禪語頭話調務師處關習覺圍",
)
# Common words that must never be aliases. Matching ignores them even if stored.
ALIAS_STOP = frozenset({"開始", "法式", "師傅", "社科", "只觀", "工案", "開事", "一座", "產修"})


# Match keys are pure, and lectures repeat the same characters.
_FOLD_CACHE: dict[str, str] = {}


def _fold_char(ch: str) -> str:
    """One source character becomes one match character, so indexes stay aligned.

    NFKC and casefold are applied only when they stay one character. A compatibility
    form that expands (for example a ligature) keeps the traditional fold instead.
    """
    cached = _FOLD_CACHE.get(ch)
    if cached is not None:
        return cached
    mapped = _fold_one(ch)
    _FOLD_CACHE[ch] = mapped
    return mapped


def _fold_one(ch: str) -> str:
    nfkc = unicodedata.normalize("NFKC", ch)
    if len(nfkc) == 1:
        folded = nfkc.casefold()
        if len(folded) == 1:
            mapped = folded.translate(FOLD)
            if len(mapped) == 1:
                return mapped
    mapped = ch.translate(FOLD)
    return mapped if len(mapped) == 1 else ch


def _match_key(text: str) -> str:
    return "".join(_fold_char(ch) for ch in text)


_STOP_KEYS = frozenset(_match_key(word) for word in ALIAS_STOP)


def _is_stop_alias(alias: str) -> bool:
    return alias in ALIAS_STOP or _match_key(alias) in _STOP_KEYS


def is_locked(term: dict) -> bool:
    if "lock" in term and term["lock"] is not None:
        parsed = _parse_lock(term["lock"])
        return True if parsed is None else parsed
    if "locked" in term and term["locked"] is not None:
        parsed = _parse_lock(term["locked"])
        return True if parsed is None else parsed
    return True


def legacy_terms(rows: list[dict]) -> list[dict]:
    """Old `zh=en` rows become locked terms. Aliases are kept when the line had them."""
    terms = []
    for row in rows:
        terms.append({
            "zh": row["zh"],
            "aliases": list(row.get("aliases") or []),
            "en": row["en"],
            "lock": True,
            "category": "",
            "note": "",
        })
    return terms


def validate_terms(raw) -> tuple[list[dict], list[dict]]:
    """Return (accepted, rejected). Accepted terms are not saved by this function.

    Over the term cap, nothing else is accepted. A term with any problem is
    left out of `accepted` and listed in `rejected` with a reason.
    """
    if not isinstance(raw, list):
        return [], [{"line": 0, "reason": "terms 必須是陣列"}]
    if len(raw) > MAX_TERMS:
        return [], [{"line": MAX_TERMS + 1, "reason": f"術語超過 {MAX_TERMS} 條"}]

    parsed: list[tuple[int, dict | None]] = []
    rejected: list[dict] = []
    canons: list[str] = []
    for index, item in enumerate(raw, start=1):
        term, problems = _parse_term(item)
        for reason in problems:
            rejected.append({"line": index, "reason": reason})
        parsed.append((index, term))
        if term is not None and not term.get("_bad"):
            canons.append(term["zh"])
    canon_set = set(canons)
    canon_keys = {_match_key(zh) for zh in canons}
    seen_zh: dict[str, int] = {}
    seen_keys: dict[str, int] = {}
    # Owners are keyed by the match fold, so 学社 and 學社 are the same alias.
    alias_owners: dict[str, list[tuple[int, str, str]]] = {}
    bad_lines: set[int] = set()
    for index, term in parsed:
        if term is None or term.get("_bad"):
            bad_lines.add(index)
            continue
        zh = term["zh"]
        zh_key = _match_key(zh)
        if zh in seen_zh or zh_key in seen_keys:
            rejected.append({"line": index, "reason": f"標準詞「{_clip(zh)}」重複"})
            bad_lines.add(index)
            continue
        seen_zh[zh] = index
        seen_keys[zh_key] = index
        for alias in term["aliases"]:
            if len(alias) < MIN_ALIAS:
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」少於 2 字"})
                bad_lines.add(index)
            if _is_stop_alias(alias):
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」是常用詞，不能當別名"})
                bad_lines.add(index)
            if alias in canon_set or _match_key(alias) in canon_keys:
                rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」與標準詞相同"})
                bad_lines.add(index)
            alias_owners.setdefault(_match_key(alias), []).append((index, zh, alias))
    for _key, owners in alias_owners.items():
        targets = {zh for _, zh, _alias in owners}
        if len(targets) < 2:
            continue
        named = "、".join(f"「{_clip(zh)}」" for zh in sorted(targets))
        for index, _zh, alias in owners:
            rejected.append({"line": index, "reason": f"別名「{_clip(alias)}」同時指向{named}"})
            bad_lines.add(index)
    accepted = []
    for index, term in parsed:
        if term is None or index in bad_lines or term.get("_bad"):
            continue
        accepted.append(_public_term(term))
    return accepted, rejected


def normalize(text: str, glossary) -> str:
    """Left-to-right longest match. A canonical span is consumed and not replaced again."""
    table = _dictionary(glossary)
    if not table or not text:
        return text or ""
    longest = max(len(key) for key in table)
    folded = _match_key(text)
    # A fold that changed the length would shift every later index. Leave the line alone.
    if len(folded) != len(text):
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        hit = None
        for size in range(min(longest, len(text) - index), 1, -1):
            key = folded[index:index + size]
            if key in table:
                hit = (size, table[key])
                break
        if hit:
            out.append(hit[1])
            index += hit[0]
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def matched_terms(zh: str, glossary) -> list[dict]:
    """Canonical hits in an already normalized line. Longest match, no overlap."""
    by_zh: dict[str, dict] = {}
    for term in _term_rows(glossary):
        by_zh[term["zh"]] = term
    keys = sorted(by_zh, key=len, reverse=True)
    found: list[dict] = []
    index = 0
    text = zh or ""
    while index < len(text):
        for key in keys:
            if text.startswith(key, index):
                found.append(by_zh[key])
                index += len(key)
                break
        else:
            index += 1
    return found


def prompt_terms(zh: str, glossary, context=None, limit: int = PROMPT_LIMIT) -> list[dict]:
    """Terms matched in this line, then in the previous lines. Cap is 40."""
    picked: list[dict] = []
    seen: set[str] = set()
    texts = [zh or ""]
    for line in list(context or [])[-4:]:
        texts.append(str(line or ""))
    for text in texts:
        for term in matched_terms(text, glossary):
            key = term["zh"]
            if key in seen:
                continue
            en = str(term.get("en") or "")
            if not en:
                continue
            seen.add(key)
            picked.append({"zh": key, "en": en, "locked": is_locked(term)})
            if len(picked) >= limit:
                return picked
    return picked


def missing_locked(zh: str, glossary, en: str) -> list[dict]:
    """Host-only flags. Does not change `en`."""
    flags = []
    for term in matched_terms(zh, glossary):
        if not is_locked(term):
            continue
        if term_hit(en, term):
            continue
        flags.append({"zh": term["zh"], "en": term.get("en") or "", "reason": "missing"})
    return flags


def term_hit(en: str, term: dict) -> bool:
    needle = _plain(str(term.get("en") or ""))
    if not needle:
        return False
    return needle in _plain(str(en or ""))


def _dictionary(glossary) -> dict[str, str]:
    table: dict[str, str] = {}
    rows = _term_rows(glossary)
    for term in rows:
        table[_match_key(term["zh"])] = term["zh"]
    for term in rows:
        for alias in term["aliases"]:
            if len(alias) < MIN_ALIAS or _is_stop_alias(alias):
                continue
            table.setdefault(_match_key(alias), term["zh"])
    return table


def _term_rows(glossary) -> list[dict]:
    rows = []
    for item in glossary or []:
        if not isinstance(item, dict):
            continue
        zh = item.get("zh")
        if not isinstance(zh, str) or not zh:
            continue
        aliases = item.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [part.strip() for part in aliases.split("|") if part.strip()]
        cleaned = []
        for alias in aliases:
            if isinstance(alias, str) and alias:
                cleaned.append(alias)
        rows.append({"zh": zh, "aliases": cleaned, "en": item.get("en") or "", "lock": item.get("lock", item.get("locked", True)), "category": item.get("category") or "", "note": item.get("note") or ""})
    return rows


def _parse_term(item) -> tuple[dict | None, list[str]]:
    problems: list[str] = []
    if not isinstance(item, dict):
        return None, ["術語必須是物件"]
    zh = item.get("zh")
    en = item.get("en")
    if not isinstance(zh, str):
        problems.append("中文必須是 1 到 20 字")
        zh_text = ""
    else:
        zh_text = zh.strip()
        if _has_control(zh_text):
            problems.append("中文含有控制字元")
        elif not MIN_ZH <= len(zh_text) <= MAX_ZH:
            problems.append("中文必須是 1 到 20 字")
    if not isinstance(en, str):
        problems.append("英文必須是 1 到 80 字")
        en_text = ""
    else:
        en_text = en.strip()
        if _has_control(en) or _has_control(en_text):
            problems.append("英文含有控制字元或換行")
        elif not 1 <= len(en_text) <= MAX_EN:
            problems.append("英文必須是 1 到 80 字")
    aliases_raw = item.get("aliases", [])
    aliases: list[str] = []
    if aliases_raw is None:
        aliases_raw = []
    if not isinstance(aliases_raw, list):
        problems.append("別名必須是陣列")
    elif len(aliases_raw) > MAX_ALIASES:
        problems.append(f"別名最多 {MAX_ALIASES} 個")
    else:
        for alias in aliases_raw:
            if not isinstance(alias, str):
                problems.append("別名必須是文字")
                continue
            text = alias.strip()
            if not text:
                continue
            if _has_control(text):
                problems.append(f"別名「{_clip(text)}」含有控制字元")
                continue
            if len(text) > MAX_ALIAS:
                problems.append(f"別名「{_clip(text)}」超過 {MAX_ALIAS} 字")
                continue
            if text not in aliases:
                aliases.append(text)
    category = item.get("category", "")
    note = item.get("note", "")
    if category is None:
        category = ""
    if note is None:
        note = ""
    if not isinstance(category, str):
        problems.append("分類必須是文字")
        category = ""
    else:
        category = category.strip()
        if _has_control(category):
            problems.append("分類含有控制字元或換行")
        elif len(category) > MAX_CATEGORY:
            problems.append(f"分類超過 {MAX_CATEGORY} 字")
    if not isinstance(note, str):
        problems.append("備註必須是文字")
        note = ""
    else:
        note = note.strip()
        if _has_control(note):
            problems.append("備註含有控制字元或換行")
        elif len(note) > MAX_NOTE:
            problems.append(f"備註超過 {MAX_NOTE} 字")
    lock_value = True
    if "lock" in item and item["lock"] is not None:
        parsed = _parse_lock(item["lock"])
        if parsed is None:
            problems.append("鎖定必須是布林值")
        else:
            lock_value = parsed
    elif "locked" in item and item["locked"] is not None:
        parsed = _parse_lock(item["locked"])
        if parsed is None:
            problems.append("鎖定必須是布林值")
        else:
            lock_value = parsed
    if problems or not zh_text or not en_text:
        term = {"_bad": True, "zh": zh_text, "aliases": aliases, "en": en_text, "lock": lock_value, "category": category, "note": note}
        return term, problems
    return {
        "zh": zh_text,
        "aliases": aliases,
        "en": en_text,
        "lock": lock_value,
        "category": category,
        "note": note,
    }, []


def _public_term(term: dict) -> dict:
    return {
        "zh": term["zh"],
        "aliases": list(term.get("aliases") or []),
        "en": term["en"],
        "lock": bool(term.get("lock", True)),
        "category": term.get("category") or "",
        "note": term.get("note") or "",
    }


def _parse_lock(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y"}:
            return True
        if text in {"0", "false", "no", "n"}:
            return False
    return None


def _has_control(text: str) -> bool:
    for char in text:
        if char in "\r\n\t":
            return True
        if unicodedata.category(char).startswith("C"):
            return True
    return False


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.replace("-", " ").casefold().split())


def _clip(text: str, limit: int = 20) -> str:
    flat = "".join(" " if _has_control(char) or char.isspace() and char != " " else char for char in text)
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"
