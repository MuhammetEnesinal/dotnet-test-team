"""Sanitisers for text that comes from a project (paths, names) and may end up in model-visible output.

clean_text keeps legitimate non-Latin text (Persian ZWNJ, Hindi conjuncts) and removes control, format and
direction characters. safe_ascii is the strict variant: everything outside a tiny ASCII set becomes '_'.
Neither makes text harmless; callers still keep project text out of free-form sentences.
"""
from __future__ import annotations

import re
import unicodedata

_STRIP_CATEGORIES = frozenset(("Cc", "Cs", "Co", "Cn", "Zl", "Zp"))
_ZWNJ = "\u200c"
_ZWJ = "\u200d"
_ALWAYS_STRIP = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)]      # bidi embeddings and overrides
    + [chr(c) for c in range(0x2066, 0x206A)]    # bidi isolates
    + [chr(c) for c in range(0x206A, 0x2070)]    # deprecated format characters
    + ["\u200e", "\u200f", "\u061c"]             # direction marks
    + ["\u200b", "\u2060", "\ufeff", "\u180e"]   # zero width space, word joiner, BOM, mongolian vowel separator
    + [chr(c) for c in range(0xE0000, 0xE0080)]  # tag characters
)

_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9 ._\-]", re.ASCII)


def clean_text(text, max_len: int = 200) -> str:
    """NFC, drop Cc/Cs/Co/Cn, line/paragraph separators, bidi controls, invisible characters and tag characters.

    ZWJ/ZWNJ survive only between two letters or marks. The length cap counts code points."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text))
    kept = []
    for ch in s:
        if ch in _ALWAYS_STRIP:
            continue
        if unicodedata.category(ch) in _STRIP_CATEGORIES:
            continue
        kept.append(ch)
    out = []
    n = len(kept)
    for i, ch in enumerate(kept):
        if ch == _ZWNJ or ch == _ZWJ:
            if 0 < i < n - 1:
                prev_c, next_c = kept[i - 1], kept[i + 1]
                if unicodedata.category(prev_c)[0] in "LM" and unicodedata.category(next_c)[0] in "LM":
                    out.append(ch)
            continue
        out.append(ch)
    result = unicodedata.normalize("NFC", "".join(out))
    if max_len >= 0 and len(result) > max_len:
        result = result[:max_len]
    return result


def safe_ascii(text, max_len: int = 200) -> str:
    """Everything outside [A-Za-z0-9 ._-] becomes '_' (one '_' per code point)."""
    if text is None:
        return ""
    clean = _ASCII_UNSAFE.sub("_", str(text))
    if len(clean) > max_len:
        clean = clean[:max_len]
    return clean


_STRICT_BAD = _ALWAYS_STRIP | frozenset((_ZWNJ, _ZWJ))


def has_bad_chars(text) -> bool:
    """True if the text contains any control, format, direction, zero-width, separator, private-use or unassigned character
    (the validate-scenarios rule: single-line printable text, no Trojan-Source style tricks; ZWJ/ZWNJ included)."""
    for ch in text:
        if ch in _STRICT_BAD or unicodedata.category(ch) in _STRIP_CATEGORIES:
            return True
    return False


def decode_source(data: bytes) -> str:
    """Source/text files: UTF-8 BOM, UTF-16/32 BOM, plain UTF-8, then cp1252 as the last resort; line endings become LF."""
    if data.startswith(b"\xef\xbb\xbf"):
        text = data[3:].decode("utf-8", errors="replace")
    elif data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text = data.decode("utf-32", errors="replace")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_VERBATIM_STRIP_CATEGORIES = frozenset(("Cc", "Cs", "Co", "Zl", "Zp"))
_VERBATIM_STRIP = _ALWAYS_STRIP        # bidi controls, zero width space, word joiner, BOM, direction marks, tag characters


def clean_verbatim(text) -> str:
    """For text that must stay the user's own words (the request text): no normalisation, ZWJ/ZWNJ/variation selectors and
    characters that this Python's Unicode tables do not know yet are kept (emoji, Persian, Indic scripts). Removed: control
    characters (except the line feed), line/paragraph separators, private-use and surrogate code points, bidi controls, zero
    width space, word joiner, BOM, tag characters. CRLF and CR become LF."""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in s:
        if ch == "\n":
            out.append(ch)
        elif ch in _VERBATIM_STRIP or unicodedata.category(ch) in _VERBATIM_STRIP_CATEGORIES:
            continue
        else:
            out.append(ch)
    return "".join(out)
