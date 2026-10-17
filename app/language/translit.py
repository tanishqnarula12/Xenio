"""Devanagari -> Latin, for matching spoken app names ("डिस्कॉर्ड" -> "diskord" ~ Discord). Not a general
transliterator: just enough to compare names by sound (``sound_key``)."""

from __future__ import annotations

import re

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f",
}
_VOWEL_SIGNS = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o",
                "ौ": "au", "ॉ": "o", "ॅ": "e", "ं": "n", "ँ": "n", "ः": "h"}
_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
           "ऑ": "o", "ऋ": "ri", "ॐ": "om"}
_VIRAMA, _NUKTA = "्", "़"
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def transliterate(text: str) -> str:
    out, i, chars = [], 0, list(text or "")
    while i < len(chars):
        ch = chars[i]
        if i + 1 < len(chars) and chars[i + 1] == _NUKTA and ch + _NUKTA in _CONSONANTS:
            ch, i = ch + _NUKTA, i + 1
        if ch in _CONSONANTS:
            out.append(_CONSONANTS[ch])
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt in _VOWEL_SIGNS:
                out.append(_VOWEL_SIGNS[nxt])
                i += 1
            elif nxt == _VIRAMA:
                i += 1
            elif nxt and (nxt in _CONSONANTS or nxt in _VOWELS):
                out.append("a")  # inherent vowel inside a word (dropped at the end: schwa deletion)
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _VOWEL_SIGNS:
            out.append(_VOWEL_SIGNS[ch])
        elif ch not in (_VIRAMA, _NUKTA, "।"):
            out.append(ch)
        i += 1
    return "".join(out)


def sound_key(text: str) -> str:
    """A rough phonetic key so "krom" ~ "chrome", "diskord" ~ "discord", "vatsep" ~ "whatsapp"."""
    t = transliterate(text).lower() if has_devanagari(text) else (text or "").lower()
    t = re.sub(r"[^a-z0-9]", "", t)
    for a, b in (("ph", "f"), ("ch", "k"), ("ck", "k"), ("c", "k"), ("q", "k"), ("w", "v"), ("x", "ks"), ("z", "s"),
                 ("ee", "i"), ("oo", "u"), ("y", "i")):
        t = t.replace(a, b)
    t = re.sub(r"(?<=[kgtdpbsv])h", "", t)  # aspiration: kh/gh/th/dh/bh/sh -> plain
    t = re.sub(r"([a-z])\1+", r"\1", t)  # doubled letters
    return re.sub(r"(?<=.)[aeiou]", "", t)  # vowels after the first letter carry little weight across scripts
