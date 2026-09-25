"""Which Pokemon are legendary or mythical, read off an encounter screen.

A GBL set can pay out a legendary -- a razr was handed a CP 2056 Terrakion on
24 Sep 2026 -- and a legendary is worth the Silver Pinap the ordinary reward
Rufflet is not.  The encounter's name plate is the only place the species is
written, so the check is a name match against OCR text.
"""
from __future__ import annotations

import re
from typing import Iterable

LEGENDARY = (
    "Articuno", "Zapdos", "Moltres", "Mewtwo",
    "Raikou", "Entei", "Suicune", "Lugia", "Ho-Oh",
    "Regirock", "Regice", "Registeel", "Latias", "Latios",
    "Kyogre", "Groudon", "Rayquaza",
    "Uxie", "Mesprit", "Azelf", "Dialga", "Palkia", "Heatran",
    "Regigigas", "Giratina", "Cresselia",
    "Cobalion", "Terrakion", "Virizion", "Tornadus", "Thundurus",
    "Reshiram", "Zekrom", "Landorus", "Kyurem",
    "Xerneas", "Yveltal", "Zygarde",
    "Type: Null", "Silvally", "Tapu Koko", "Tapu Lele", "Tapu Bulu",
    "Tapu Fini", "Cosmog", "Cosmoem", "Solgaleo", "Lunala", "Necrozma",
    "Zacian", "Zamazenta", "Eternatus", "Kubfu", "Urshifu",
    "Regieleki", "Regidrago", "Glastrier", "Spectrier", "Calyrex",
    "Enamorus",
    "Wo-Chien", "Chien-Pao", "Ting-Lu", "Chi-Yu",
    "Koraidon", "Miraidon", "Okidogi", "Munkidori", "Fezandipiti",
    "Ogerpon", "Terapagos",
)

MYTHICAL = (
    "Mew", "Celebi", "Jirachi", "Deoxys",
    "Phione", "Manaphy", "Darkrai", "Shaymin", "Arceus",
    "Victini", "Keldeo", "Meloetta", "Genesect",
    "Diancie", "Hoopa", "Volcanion",
    "Magearna", "Marshadow", "Zeraora", "Meltan", "Melmetal",
    "Zarude", "Pecharunt",
)


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


_NAMES = {_letters(name): name for name in LEGENDARY + MYTHICAL}
# A name this short turns up inside other words ("Mew" in "Mewtwo" is harmless,
# but OCR runs text together), so it has to be a whole word on the plate.
_WORD_ONLY = {key for key in _NAMES if len(key) < 5}


def legendary_named(texts: Iterable[str]) -> str | None:
    """The legendary or mythical Pokemon named in `texts`, if any."""
    for text in texts:
        words = {_letters(word) for word in re.split(r"[\s/|•·]+", text)}
        run = _letters(text)
        for key, name in _NAMES.items():
            if key in words or (key not in _WORD_ONLY and key in run):
                return name
    return None
