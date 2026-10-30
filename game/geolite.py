"""geolite.py — turn Met geography strings into scorable regions, offline.

The real game scores distance to a region polygon (0 km inside it), so every
object resolves to a `Place` whose geometry is one of:
  - modern country polygons (Natural Earth 50m), unioned for "Egypt or Syria"
  - a hand-placed circle for cultures that don't match a modern border (Moche)

Resolution order per object (see `resolve_object`):
  1. strong culture (Moche, Greek/Attic, Yoruba ...): beats the country field
  2. Country field, then Region field
  3. weak culture (demonyms: French, Italian, Roman ...)
  4. refine a whole-country answer using the Met's finer fields: FINE_PLACES (hand-placed
     sites and provinces), then game/cities.json. A match only counts if it lies in
     that country, so "Paris" in an American object can't land in France.
Matching is exact after normalization. Substring matching is deliberately absent:
it once mapped "French" to the French Southern and Antarctic Lands.
Anything unresolved, or alternatives spanning two world regions, returns None.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

COUNTRIES_PATH = Path(__file__).parent / "static" / "countries.json"
CITIES_PATH = Path(__file__).parent / "cities.json"
EARTH_KM = 6371.0

Polygon = list[list[list[float]]]  # rings of [lon, lat]; ring 0 is the shell


@dataclass(frozen=True)
class Place:
    key: str      # geometry key, e.g. "c:Egypt+c:Syria" or "o:35.2,24.8,250"
    label: str    # shown on reveal
    bucket: str   # balancing bucket for daily picks
    lat: float    # representative point
    lon: float
    tier: str     # A sub-national or culture region, B country, C inferred from a demonym


# ---------------------------------------------------------------- world regions

# Natural Earth SUBREGION -> balancing bucket
SUBREGIONS: dict[str, str] = {
    "Western Europe": "Europe", "Northern Europe": "Europe", "Southern Europe": "Europe",
    "Eastern Europe": "Europe", "Western Asia": "Near East", "Northern Africa": "Near East",
    "Central Asia": "South & Central Asia", "Southern Asia": "South & Central Asia",
    "Eastern Asia": "East Asia", "South-Eastern Asia": "Southeast Asia",
    "Western Africa": "Sub-Saharan Africa", "Middle Africa": "Sub-Saharan Africa",
    "Eastern Africa": "Sub-Saharan Africa", "Southern Africa": "Sub-Saharan Africa",
    "Northern America": "Americas", "Central America": "Americas", "Caribbean": "Americas",
    "South America": "Americas", "Melanesia": "Oceania", "Micronesia": "Oceania",
    "Polynesia": "Oceania", "Australia and New Zealand": "Oceania",
}
BUCKET_OVERRIDES: dict[str, str] = {
    "Iran": "Near East",
    "Afghanistan": "South & Central Asia",
    "Sudan": "Sub-Saharan Africa",
}

# Overseas parts are dropped, so a French object isn't "near" French Guiana.
TRIM_OVERSEAS_KM = {
    "France": 1500, "United States of America": 3000, "Netherlands": 1000,
    "Portugal": 1000, "Norway": 1500, "United Kingdom": 1500, "Spain": 2500,
    "Denmark": 1500, "Chile": 3000, "Ecuador": 1000,
}

# ---------------------------------------------------------------- name tables

COUNTRY_ALIASES: dict[str, list[str]] = {
    "united states": ["United States of America"],
    "usa": ["United States of America"],
    "america": ["United States of America"],
    "england": ["United Kingdom"],
    "scotland": ["United Kingdom"],
    "wales": ["United Kingdom"],
    "great britain": ["United Kingdom"],
    "britain": ["United Kingdom"],
    "cyprus": ["Cyprus", "Northern Cyprus"],
    "byzantine egypt": ["Egypt"],
    "roman egypt": ["Egypt"],
    "persia": ["Iran"],
    "mesopotamia": ["Iraq"],
    "anatolia": ["Turkey"],
    "asia minor": ["Turkey"],
    "turkiye": ["Turkey"],
    "palestine": ["Palestine", "Israel"],
    "israel": ["Israel", "Palestine"],
    "levant": ["Syria", "Lebanon", "Israel", "Palestine", "Jordan"],
    "holland": ["Netherlands"],
    "the netherlands": ["Netherlands"],
    "flanders": ["Belgium"],
    "bohemia": ["Czechia"],
    "czech republic": ["Czechia"],
    "serbia": ["Republic of Serbia"],
    "macedonia": ["North Macedonia"],
    "burma": ["Myanmar"],
    "siam": ["Thailand"],
    "ceylon": ["Sri Lanka"],
    "tanzania": ["United Republic of Tanzania"],
    "zaire": ["Democratic Republic of the Congo"],
    "democratic republic of congo": ["Democratic Republic of the Congo"],
    "congo": ["Democratic Republic of the Congo", "Republic of the Congo"],
    "cote d'ivoire": ["Ivory Coast"],
    "côte d'ivoire": ["Ivory Coast"],
    "swaziland": ["eSwatini"],
    "nubia": ["Sudan"],
    "bahamas": ["The Bahamas"],
    "korea": ["South Korea", "North Korea"],
    "tibet": ["China"],
    "micronesia": ["Federated States of Micronesia"],
    "new guinea": ["Papua New Guinea"],
    "polynesia": ["French Polynesia"],
    "sicily": ["Italy"],
    "crete": ["Greece"],
    "java": ["Indonesia"],
    "bali": ["Indonesia"],
    "sumatra": ["Indonesia"],
}

# Demonyms: only a guess at where it was made, so they rank below explicit fields.
WEAK_CULTURES: dict[str, list[str]] = {
    "american": ["United States of America"], "native american": ["United States of America"],
    "french": ["France"], "italian": ["Italy"], "german": ["Germany"], "british": ["United Kingdom"],
    "english": ["United Kingdom"], "scottish": ["United Kingdom"], "welsh": ["United Kingdom"],
    "irish": ["Ireland"], "spanish": ["Spain"], "catalan": ["Spain"], "portuguese": ["Portugal"],
    "dutch": ["Netherlands"], "flemish": ["Belgium"], "belgian": ["Belgium"],
    "netherlandish": ["Netherlands", "Belgium"], "south netherlandish": ["Belgium"],
    "north netherlandish": ["Netherlands"], "austrian": ["Austria"], "swiss": ["Switzerland"],
    "swedish": ["Sweden"], "danish": ["Denmark"], "norwegian": ["Norway"], "finnish": ["Finland"],
    "russian": ["Russia"], "polish": ["Poland"], "hungarian": ["Hungary"], "bohemian": ["Czechia"],
    "czech": ["Czechia"], "greek": ["Greece"], "roman": ["Italy"], "mexican": ["Mexico"],
    "peruvian": ["Peru"], "canadian": ["Canada"], "chinese": ["China"], "japanese": ["Japan"],
    "korean": ["South Korea", "North Korea"], "indian": ["India"], "iranian": ["Iran"],
    "persian": ["Iran"], "turkish": ["Turkey"], "ottoman": ["Turkey"], "egyptian": ["Egypt"],
    "syrian": ["Syria"], "iraqi": ["Iraq"], "thai": ["Thailand"], "vietnamese": ["Vietnam"],
    "burmese": ["Myanmar"], "nepalese": ["Nepal"], "nepali": ["Nepal"], "pakistani": ["Pakistan"],
    "afghan": ["Afghanistan"], "indonesian": ["Indonesia"], "javanese": ["Indonesia"],
    "balinese": ["Indonesia"], "filipino": ["Philippines"], "sri lankan": ["Sri Lanka"],
    "byzantine": ["Turkey"], "coptic": ["Egypt"],
}


def _countries(*names: str) -> tuple[str, list[str]]:
    return ("c", list(names))


def _circle(lat: float, lon: float, km: int) -> tuple[str, float, float, int]:
    return ("o", lat, lon, km)


# Cultures that say more than a modern border does.
# key -> (label, bucket, geometry). Radii are deliberately generous: the
# real game draws broad cultural regions, and a tiny circle punishes good guesses.
STRONG_CULTURES: dict[str, tuple[str, str, tuple]] = {
    # Aegean and Mediterranean
    "greek, attic": ("Attica, Greece", "Europe", _countries("Greece")),
    "greek, south italian": ("Magna Graecia, southern Italy", "Europe", _circle(39.8, 16.2, 300)),
    "greek, south italian, apulian": ("Apulia, southern Italy", "Europe", _circle(40.8, 16.8, 200)),
    "greek, east greek": ("Ionia, western Anatolia", "Near East", _circle(38.3, 27.2, 250)),
    "minoan": ("Minoan Crete", "Europe", _circle(35.2, 24.9, 200)),
    "mycenaean": ("Mycenaean Greece", "Europe", _countries("Greece")),
    "helladic": ("Bronze Age Greece", "Europe", _countries("Greece")),
    "cycladic": ("Cyclades, Greece", "Europe", _circle(37.0, 25.2, 150)),
    "cypriot": ("Cyprus", "Near East", _countries("Cyprus", "Northern Cyprus")),
    "roman, cypriot": ("Roman Cyprus", "Near East", _countries("Cyprus", "Northern Cyprus")),
    "etruscan": ("Etruria, central Italy", "Europe", _circle(42.8, 11.6, 200)),
    "villanovan": ("Etruria, central Italy", "Europe", _circle(43.0, 11.6, 250)),
    "phoenician": ("Phoenicia", "Near East", _circle(33.9, 35.6, 250)),
    "punic": ("Carthage", "Near East", _circle(36.8, 10.2, 300)),
    "frankish": ("Frankish lands", "Europe", _countries("France", "Germany", "Belgium")),
    "merovingian": ("Merovingian Gaul", "Europe", _countries("France", "Belgium")),
    "visigothic": ("Visigothic Spain", "Europe", _countries("Spain")),
    "langobardic": ("Lombard Italy", "Europe", _countries("Italy")),
    "lombard": ("Lombard Italy", "Europe", _countries("Italy")),
    "anglo-saxon": ("Anglo-Saxon England", "Europe", _countries("United Kingdom")),
    "celtic": ("Celtic Europe", "Europe", _countries("France", "Ireland", "United Kingdom")),
    "avar": ("Avar Khaganate", "Europe", _countries("Hungary", "Romania")),
    "scythian": ("Pontic steppe", "Europe", _circle(47.5, 34.5, 500)),
    # Ancient Near East
    "sumerian": ("Sumer, southern Mesopotamia", "Near East", _circle(31.3, 45.9, 300)),
    "babylonian": ("Babylonia, southern Mesopotamia", "Near East", _circle(32.5, 44.4, 350)),
    "assyrian": ("Assyria, northern Mesopotamia", "Near East", _circle(36.3, 43.2, 350)),
    "old assyrian trading colony": ("Cappadocia, Anatolia", "Near East", _circle(38.8, 35.5, 300)),
    "hittite": ("Hittite Anatolia", "Near East", _circle(40.0, 34.6, 400)),
    "phrygian": ("Phrygia, Anatolia", "Near East", _circle(39.5, 31.0, 300)),
    "lydian": ("Lydia, Anatolia", "Near East", _circle(38.5, 28.0, 200)),
    "urartian": ("Urartu", "Near East", _circle(39.0, 43.3, 300)),
    "elamite": ("Elam, southwest Iran", "Near East", _circle(32.0, 48.5, 300)),
    "achaemenid": ("Achaemenid Persia", "Near East", _countries("Iran")),
    "parthian": ("Parthian Persia", "Near East", _countries("Iran", "Iraq")),
    "sasanian": ("Sasanian Persia", "Near East", _countries("Iran", "Iraq")),
    "nabataean": ("Nabataea", "Near East", _circle(30.3, 35.4, 250)),
    "palmyrene": ("Palmyra", "Near East", _circle(34.6, 38.3, 250)),
    "israelite": ("Ancient Israel", "Near East", _countries("Israel", "Palestine")),
    "south arabian": ("South Arabia", "Near East", _countries("Yemen")),
    "bactria-margiana archaeological complex": ("Bactria-Margiana", "South & Central Asia",
                                                _countries("Turkmenistan", "Uzbekistan", "Afghanistan")),
    "bactrian": ("Bactria", "South & Central Asia", _countries("Afghanistan", "Uzbekistan", "Tajikistan")),
    "sogdian": ("Sogdiana", "South & Central Asia", _countries("Uzbekistan", "Tajikistan")),
    "kushan": ("Kushan Empire", "South & Central Asia", _countries("Afghanistan", "Pakistan")),
    "nubian": ("Nubia", "Sub-Saharan Africa", _circle(20.0, 31.5, 500)),
    "kushite": ("Kush, Nubia", "Sub-Saharan Africa", _circle(19.0, 32.5, 500)),
    # Asia
    "khmer": ("Khmer Empire", "Southeast Asia", _countries("Cambodia")),
    "cham": ("Champa, central Vietnam", "Southeast Asia", _circle(15.0, 108.5, 350)),
    "mongol": ("Mongolia", "East Asia", _countries("Mongolia")),
    "tibetan": ("Tibetan Plateau", "East Asia", _circle(31.0, 89.0, 800)),
    "mexica": ("Aztec Empire", "Americas", _circle(19.4, -99.1, 350)),
    "salinar": ("North coast of Peru", "Americas", _circle(-8.2, -78.9, 350)),
    "tibet": ("Tibetan Plateau", "East Asia", _circle(31.0, 89.0, 800)),
    # Mesoamerica and the Andes
    "olmec": ("Olmec heartland, Mexico", "Americas", _circle(18.0, -94.5, 300)),
    "maya": ("Maya region", "Americas", _circle(17.0, -89.8, 450)),
    "aztec": ("Aztec Empire", "Americas", _circle(19.4, -99.1, 350)),
    "mixtec": ("Oaxaca, Mexico", "Americas", _circle(17.1, -97.0, 250)),
    "zapotec": ("Oaxaca, Mexico", "Americas", _circle(17.1, -96.7, 250)),
    "teotihuacan": ("Teotihuacan, Mexico", "Americas", _circle(19.7, -98.8, 300)),
    "huastec": ("Huasteca, Mexico", "Americas", _circle(21.8, -98.5, 250)),
    "moche": ("North coast of Peru", "Americas", _circle(-8.2, -78.9, 350)),
    "chimú": ("North coast of Peru", "Americas", _circle(-8.1, -79.0, 350)),
    "chimu": ("North coast of Peru", "Americas", _circle(-8.1, -79.0, 350)),
    "vicús": ("Far north of Peru", "Americas", _circle(-5.2, -80.2, 250)),
    "nasca": ("South coast of Peru", "Americas", _circle(-14.8, -75.1, 300)),
    "nazca": ("South coast of Peru", "Americas", _circle(-14.8, -75.1, 300)),
    "paracas": ("South coast of Peru", "Americas", _circle(-13.9, -76.2, 300)),
    "chancay": ("Central coast of Peru", "Americas", _circle(-11.5, -77.2, 250)),
    "wari": ("Wari Empire, Peru", "Americas", _countries("Peru")),
    "inca": ("Inca Empire", "Americas", _countries("Peru", "Bolivia", "Ecuador")),
    "tiwanaku": ("Lake Titicaca basin", "Americas", _circle(-16.5, -68.7, 300)),
    "calima": ("Colombia", "Americas", _countries("Colombia")),
    "quimbaya": ("Colombia", "Americas", _countries("Colombia")),
    "muisca": ("Colombia", "Americas", _countries("Colombia")),
    "tairona": ("Colombia", "Americas", _countries("Colombia")),
    "jama-coaque": ("Coastal Ecuador", "Americas", _circle(-0.3, -80.0, 250)),
    "coclé": ("Panama", "Americas", _countries("Panama")),
    "taíno": ("Greater Antilles", "Americas", _countries("Dominican Republic", "Haiti", "Puerto Rico", "Cuba")),
    # North America
    "navajo": ("American Southwest", "Americas", _circle(36.0, -110.0, 500)),
    "hopi": ("American Southwest", "Americas", _circle(35.8, -110.5, 500)),
    "zuni": ("American Southwest", "Americas", _circle(35.0, -108.8, 500)),
    "lakota": ("Northern Plains", "Americas", _circle(44.5, -102.0, 600)),
    "haida": ("Pacific Northwest Coast", "Americas", _circle(54.0, -131.0, 600)),
    "tlingit": ("Pacific Northwest Coast", "Americas", _circle(58.0, -135.0, 600)),
    "anishinaabe": ("Great Lakes", "Americas", _circle(46.0, -85.0, 700)),
    # Sub-Saharan Africa
    "yoruba": ("Yorubaland", "Sub-Saharan Africa", _circle(7.6, 4.2, 300)),
    "edo": ("Kingdom of Benin, Nigeria", "Sub-Saharan Africa", _circle(6.3, 5.6, 200)),
    "benin": ("Kingdom of Benin, Nigeria", "Sub-Saharan Africa", _circle(6.3, 5.6, 200)),
    "igbo": ("Igboland, Nigeria", "Sub-Saharan Africa", _circle(5.9, 7.4, 200)),
    "akan": ("Akan lands", "Sub-Saharan Africa", _countries("Ghana", "Ivory Coast")),
    "asante": ("Asante, Ghana", "Sub-Saharan Africa", _countries("Ghana")),
    "bamana": ("Bamana lands, Mali", "Sub-Saharan Africa", _countries("Mali")),
    "dogon": ("Dogon country, Mali", "Sub-Saharan Africa", _countries("Mali")),
    "senufo": ("Senufo lands", "Sub-Saharan Africa", _circle(9.8, -5.5, 300)),
    "baule": ("Côte d'Ivoire", "Sub-Saharan Africa", _countries("Ivory Coast")),
    "dan": ("Dan lands", "Sub-Saharan Africa", _circle(7.3, -8.0, 250)),
    "mende": ("Sierra Leone", "Sub-Saharan Africa", _countries("Sierra Leone", "Liberia")),
    "sapi": ("Sierra Leone", "Sub-Saharan Africa", _countries("Sierra Leone", "Guinea")),
    "fang": ("Fang lands", "Sub-Saharan Africa", _countries("Gabon", "Equatorial Guinea")),
    "punu": ("Gabon", "Sub-Saharan Africa", _countries("Gabon")),
    "kota": ("Gabon", "Sub-Saharan Africa", _countries("Gabon")),
    "kongo": ("Kingdom of Kongo", "Sub-Saharan Africa", _circle(-5.8, 14.5, 450)),
    "luba": ("Luba lands, DR Congo", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "songye": ("DR Congo", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "kuba": ("Kuba Kingdom, DR Congo", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "lega": ("DR Congo", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "mangbetu": ("DR Congo", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "chokwe": ("Chokwe lands", "Sub-Saharan Africa", _countries("Angola")),
    "zulu": ("KwaZulu", "Sub-Saharan Africa", _countries("South Africa")),
    # Oceania
    "asmat": ("Asmat, New Guinea", "Oceania", _circle(-5.5, 138.5, 350)),
    "iatmul": ("Sepik River, New Guinea", "Oceania", _circle(-4.2, 143.3, 300)),
    "abelam": ("Sepik River, New Guinea", "Oceania", _circle(-3.8, 143.0, 300)),
    "maori": ("New Zealand", "Oceania", _countries("New Zealand")),
    "māori": ("New Zealand", "Oceania", _countries("New Zealand")),
    "hawaiian": ("Hawaiʻi", "Oceania", _circle(20.6, -157.5, 350)),
    "marquesan": ("Marquesas Islands", "Oceania", _circle(-9.5, -139.5, 300)),
    "rapa nui": ("Rapa Nui", "Oceania", _circle(-27.1, -109.4, 250)),
    "fijian": ("Fiji", "Oceania", _countries("Fiji")),
    "tongan": ("Tonga", "Oceania", _countries("Tonga")),
    "samoan": ("Samoa", "Oceania", _countries("Samoa")),
    "aboriginal": ("Australia", "Oceania", _countries("Australia")),
}


# Sub-national places named in the Met's fields, for refining a whole-country answer.
# name -> (label, lat, lon, radius km). Written from the most common strings in the catalogue.
_THEBES = ("Thebes, Egypt", 25.72, 32.61, 120)
_MEMPHITE = ("Memphite region, Egypt", 29.7, 31.2, 120)
_KHARGA = ("Kharga Oasis, Egypt", 25.45, 30.55, 150)
FINE_PLACES: dict[str, tuple[str, float, float, int]] = {
    # Egypt
    "thebes": _THEBES, "deir el-bahri": _THEBES, "asasif": _THEBES, "southern asasif": _THEBES,
    "malqata": _THEBES, "sheikh abd el-qurna": _THEBES, "valley of the kings": _THEBES,
    "dra abu el-naga": _THEBES, "khokha": _THEBES, "el-khokha": _THEBES, "deir el-medina": _THEBES,
    "wadi gabbanat el-qurud": _THEBES, "memphite region": _MEMPHITE, "lisht north": _MEMPHITE,
    "lisht south": _MEMPHITE, "lisht": _MEMPHITE, "saqqara": _MEMPHITE, "giza": _MEMPHITE,
    "memphis": _MEMPHITE, "dahshur": _MEMPHITE, "fayum entrance area": ("Fayum, Egypt", 29.3, 30.85, 100),
    "fayum": ("Fayum, Egypt", 29.3, 30.85, 100), "lahun": ("Fayum, Egypt", 29.3, 30.85, 100),
    "amarna": ("Amarna, Egypt", 27.65, 30.9, 100), "hermopolis": ("Hermopolis, Egypt", 27.78, 30.8, 100),
    "abydos": ("Abydos, Egypt", 26.18, 31.92, 100), "meir": ("Meir, Egypt", 27.42, 30.75, 100),
    "oxyrhynchus": ("Oxyrhynchus, Egypt", 28.53, 30.65, 100), "tell basta": ("Bubastis, Egypt", 30.57, 31.51, 100),
    "deir el-ballas": ("Deir el-Ballas, Egypt", 26.05, 32.75, 100), "kharga oasis": _KHARGA, "hibis": _KHARGA,
    "middle egypt": ("Middle Egypt", 27.8, 30.8, 250), "northern upper egypt": ("Upper Egypt", 26.5, 31.7, 200),
    "southern upper egypt": ("Upper Egypt", 25.0, 32.8, 200), "upper egypt": ("Upper Egypt", 25.8, 32.5, 300),
    "eastern delta": ("Nile Delta, Egypt", 30.8, 31.8, 120), "lower egypt": ("Nile Delta, Egypt", 30.8, 31.0, 180),
    "lower nubia": ("Lower Nubia", 22.5, 32.0, 250),
    # Near East and Iran
    "qasr-i abu nasr": ("Shiraz region, Iran", 29.6, 52.6, 120), "hasanlu": ("Hasanlu, northwest Iran", 37.0, 45.46, 120),
    "ctesiphon": ("Ctesiphon, Iraq", 33.09, 44.58, 120), "ziwiye": ("Ziwiye, northwest Iran", 36.2, 46.7, 120),
    "nippur": ("Nippur, Iraq", 32.13, 45.23, 120), "luristan": ("Luristan, Iran", 33.5, 48.0, 200),
    "western iran": ("Western Iran", 34.5, 47.0, 300), "northwestern iran": ("Northwestern Iran", 37.0, 46.5, 300),
    "lachish": ("Lachish", 31.56, 34.85, 80),
    # Central, South and East Asia
    "xinjiang": ("Xinjiang, China", 41.0, 85.0, 900), "xinjiang autonomous region": ("Xinjiang, China", 41.0, 85.0, 900),
    "tibetan": ("Tibetan Plateau", 31.0, 89.0, 800), "tibet": ("Tibetan Plateau", 31.0, 89.0, 800),
    "gandhara": ("Gandhara", 34.0, 72.0, 250), "kashmir": ("Kashmir", 34.0, 75.0, 200),
    "gujarat": ("Gujarat, India", 22.6, 71.5, 300), "rajasthan": ("Rajasthan, India", 26.5, 73.5, 350),
    "tamil nadu": ("Tamil Nadu, India", 11.0, 78.5, 300), "kerala": ("Kerala, India", 10.2, 76.4, 200),
    "deccan": ("Deccan, India", 17.0, 76.0, 500), "bengal": ("Bengal", 24.0, 89.0, 300),
    "punjab": ("Punjab", 31.0, 74.5, 300), "odisha": ("Odisha, India", 20.5, 84.5, 250),
    "orissa": ("Odisha, India", 20.5, 84.5, 250), "madhya pradesh": ("Madhya Pradesh, India", 23.5, 78.0, 400),
    "uttar pradesh": ("Uttar Pradesh, India", 27.0, 80.5, 350), "himachal pradesh": ("Himachal Pradesh, India", 32.0, 77.0, 150),
    "kathmandu valley": ("Kathmandu Valley, Nepal", 27.7, 85.3, 100),
    # Southeast Asia
    "java": ("Java, Indonesia", -7.4, 110.0, 400), "jawa": ("Java, Indonesia", -7.4, 110.0, 400),
    "central java": ("Central Java, Indonesia", -7.4, 110.0, 150), "east java": ("East Java, Indonesia", -7.9, 112.5, 150),
    "eastern java": ("East Java, Indonesia", -7.9, 112.5, 150), "west java": ("West Java, Indonesia", -6.9, 107.6, 150),
    "bali": ("Bali, Indonesia", -8.4, 115.1, 100), "sumatra": ("Sumatra, Indonesia", -0.5, 101.5, 500),
    # The Americas
    "guerrero": ("Guerrero, Mexico", 17.6, -99.9, 180), "veracruz": ("Veracruz, Mexico", 19.2, -96.5, 250),
    "colima": ("Colima, Mexico", 19.1, -103.9, 100), "michoacan": ("Michoacán, Mexico", 19.2, -101.8, 180),
    "nayarit": ("Nayarit, Mexico", 21.8, -105.0, 130), "jalisco": ("Jalisco, Mexico", 20.6, -103.4, 200),
    "oaxaca": ("Oaxaca, Mexico", 17.0, -96.7, 200), "north coast": ("North coast of Peru", -8.0, -79.0, 300),
    "central coast": ("Central coast of Peru", -12.0, -77.0, 250), "south coast": ("South coast of Peru", -14.5, -75.5, 250),
    "ica valley": ("Ica Valley, Peru", -14.1, -75.7, 120), "sierra nevada de santa marta": ("Santa Marta, Colombia", 10.9, -73.6, 80),
    "new england": ("New England", 43.0, -71.5, 350), "massachusetts": ("Massachusetts", 42.3, -71.8, 130),
    "pennsylvania": ("Pennsylvania", 40.9, -77.8, 220), "connecticut river valley": ("Connecticut River Valley", 42.5, -72.6, 180),
    "long island": ("Long Island, New York", 40.8, -73.2, 90), "british columbia": ("British Columbia, Canada", 54.0, -125.0, 500),
    # Europe
    "attic": ("Attica, Greece", 37.98, 23.73, 100), "laconian": ("Laconia, Greece", 37.1, 22.4, 80),
    "corinthian": ("Corinth, Greece", 37.9, 22.9, 80), "boeotian": ("Boeotia, Greece", 38.4, 23.2, 70),
    "bohemia": ("Bohemia", 50.0, 14.5, 150), "burgundy": ("Burgundy, France", 47.0, 4.5, 150),
    "normandy": ("Normandy, France", 49.0, 0.5, 130), "saxony": ("Saxony, Germany", 51.0, 13.2, 130),
    "tuscany": ("Tuscany, Italy", 43.4, 11.0, 120), "carinthia": ("Carinthia, Austria", 46.7, 14.0, 90),
    "meuse valley": ("Meuse Valley", 50.5, 5.3, 120), "staffordshire": ("Staffordshire, England", 52.9, -2.0, 60),
    "south staffordshire": ("Staffordshire, England", 52.6, -2.1, 60), "west midlands": ("West Midlands, England", 52.5, -2.0, 50),
    "west yorkshire": ("Yorkshire, England", 53.8, -1.6, 70), "south yorkshire": ("Yorkshire, England", 53.5, -1.3, 70),
    # Africa
    "court of benin": ("Kingdom of Benin, Nigeria", 6.3, 5.6, 200),
}
CITY_RADIUS_KM = 120
MAX_OUTSIDE_COUNTRY_KM = 250   # historical regions spill over modern borders, but not far
MAX_CITY_OUTSIDE_KM = 30       # cities are in the country or they aren't; this only covers coarse borders


# ---------------------------------------------------------------- natural earth

@lru_cache(maxsize=1)
def _ne() -> dict[str, dict]:
    data = json.loads(COUNTRIES_PATH.read_text(encoding="utf-8"))
    return {f["properties"]["ADMIN"]: f for f in data["features"] if f.get("geometry")}


def _ring_area_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """Planar shoelace in degrees: (|area|, lon, lat). Fine for picking a main part."""
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        c = x1 * y2 - x2 * y1
        a += c
        cx += (x1 + x2) * c
        cy += (y1 + y2) * c
    if abs(a) < 1e-9:
        return 0.0, ring[0][0], ring[0][1]
    return abs(a) / 2, cx / (3 * a), cy / (3 * a)


@lru_cache(maxsize=512)
def _country_polygons(name: str) -> tuple:
    """(polygons, (lat, lon) of main part), with far-flung parts trimmed if listed."""
    geom = _ne()[name]["geometry"]
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    measured = [(_ring_area_centroid(p[0]), p) for p in polys]
    (_, mlon, mlat), _ = max(measured, key=lambda m: m[0][0])
    limit = TRIM_OVERSEAS_KM.get(name)
    if limit:
        polys = [p for (_, lon, lat), p in measured if haversine_km(lat, lon, mlat, mlon) <= limit]
    return polys, (mlat, mlon)


def _circle_polygon(lat: float, lon: float, km: float, n: int = 48) -> Polygon:
    dlat = math.degrees(km / EARTH_KM)
    dlon = dlat / max(0.05, math.cos(math.radians(lat)))
    ring = [[round(lon + dlon * math.cos(t), 3), round(lat + dlat * math.sin(t), 3)]
            for t in (2 * math.pi * i / n for i in range(n))]
    return [ring + [ring[0]]]


@lru_cache(maxsize=4096)
def geometry(key: str) -> tuple:
    """Place.key -> polygons (GeoJSON MultiPolygon coordinates)."""
    polys: list = []
    for part in key.split("+"):
        kind, val = part.split(":", 1)
        if kind == "c":
            polys.extend(_country_polygons(val)[0])
        else:
            lat, lon, km = (float(v) for v in val.split(","))
            polys.append(_circle_polygon(lat, lon, km))
    return tuple(polys)


# ---------------------------------------------------------------- resolving

_QUALIFIERS = re.compile(
    r"^(?:possibly|probably|perhaps|likely|present-day|modern|the|ancient region of|ancient|attributed to|said to be from|from)\s+", re.I)
_DIRECTION = re.compile(r"^(?:north|south|east|west|central|upper|lower)(?:ern)?\s+", re.I)
_SPLIT = re.compile(r"\s*(?:\||/|\bor\b)\s*", re.I)


def _clean(s: str) -> str:
    s = s.strip().lower().replace("(?)", "").replace("?", "").strip(" ,.|")
    while _QUALIFIERS.match(s):
        s = _QUALIFIERS.sub("", s, count=1)
    return s.strip()


def _alternatives(raw: str) -> list[str]:
    """'Egypt or Syria' -> ['egypt', 'syria']; repeats collapse ('Iran|Iran')."""
    out: list[str] = []
    # A semicolon narrows rather than offers a choice: "Peru; north coast".
    for alt in _SPLIT.split((raw or "").replace(";", ",")):
        alt = _clean(alt)
        if alt and alt not in out:
            out.append(alt)
    return out


def _variants(alt: str) -> list[str]:
    """Most to least specific: 'Greek, South Italian, Apulian' -> itself, 'greek, south italian', 'greek'."""
    bare = re.sub(r"\s*\(.*?\)", "", alt).strip()
    parts = [p.strip() for p in bare.split(",")]
    return [", ".join(parts[:i]) for i in range(len(parts), 0, -1)]


def _country_names(alt: str) -> list[str] | None:
    lower_ne = _lower_ne()
    for v in _variants(alt):
        for candidate in (v, _DIRECTION.sub("", v)):
            if candidate in COUNTRY_ALIASES:
                return COUNTRY_ALIASES[candidate]
            if candidate in lower_ne:
                return [lower_ne[candidate]]
    return None


@lru_cache(maxsize=1)
def _lower_ne() -> dict[str, str]:
    return {name.lower(): name for name in _ne()}


def _bucket(name: str) -> str | None:
    return BUCKET_OVERRIDES.get(name) or SUBREGIONS.get(_ne()[name]["properties"]["SUBREGION"])


def _country_place(names: list[str], tier: str) -> Place | None:
    buckets = {_bucket(n) for n in names}
    if None in buckets or len(buckets) > 1:
        return None
    lat, lon = _country_polygons(names[0])[1]
    label = " or ".join(n.replace("United States of America", "United States") for n in names)
    return Place("+".join(f"c:{n}" for n in names), label, buckets.pop(), round(lat, 3), round(lon, 3), tier)


def _culture_place(entry: tuple) -> Place:
    label, bucket, geom = entry
    if geom[0] == "c":
        lat, lon = _country_polygons(geom[1][0])[1]
        key = "+".join(f"c:{n}" for n in geom[1])
    else:
        _, lat, lon, km = geom
        key = f"o:{lat},{lon},{km}"
    return Place(key, label, bucket, round(lat, 3), round(lon, 3), "A")


def _merge(places: list[Place]) -> Place | None:
    """Alternatives become one region, but not across world regions."""
    if not places or len({p.bucket for p in places}) > 1:
        return None
    if len({p.key for p in places}) == 1:
        return places[0]
    first = places[0]
    return Place("+".join(dict.fromkeys(p.key for p in places)),
                 " or ".join(dict.fromkeys(p.label for p in places)),
                 first.bucket, first.lat, first.lon, first.tier)


def _resolve_each(raw: str, lookup) -> Place | None:
    """Every alternative must resolve: half an answer isn't an answer."""
    places = []
    for alt in _alternatives(raw):
        place = lookup(alt)
        if place is None:
            return None
        places.append(place)
    return _merge(places)


def _strong(alt: str) -> Place | None:
    for v in _variants(alt):
        for candidate in (v, _DIRECTION.sub("", v)):   # "Central Tibet" is still Tibet
            if candidate in STRONG_CULTURES:
                return _culture_place(STRONG_CULTURES[candidate])
    return None


def _country(alt: str) -> Place | None:
    names = _country_names(alt)
    return _country_place(names, "B") if names else None


def _weak(alt: str) -> Place | None:
    for v in _variants(alt):
        if v in WEAK_CULTURES:
            return _country_place(WEAK_CULTURES[v], "C")
    return _country_place(n, "C") if (n := _country_names(alt)) else None


@lru_cache(maxsize=1)
def _cities() -> dict[str, list]:
    return json.loads(CITIES_PATH.read_text(encoding="utf-8")) if CITIES_PATH.exists() else {}


def _fine_parts(alt: str) -> list[str]:
    """Most specific first: 'Italian, Venice (Murano)' -> murano, venice, italian."""
    inner = [p.strip() for m in re.findall(r"\((.*?)\)", alt) for p in m.split(",")]
    outer = [p.strip() for p in re.sub(r"\s*\(.*?\)", "", alt).split(",")]
    return [_clean(p) for p in inner + outer[::-1] if _clean(p)]


def _is_broad_name(part: str) -> bool:
    return part in COUNTRY_ALIASES or part in _lower_ne() or part in WEAK_CULTURES or part in STRONG_CULTURES


def _fine_place(part: str, base: Place) -> tuple[str, float, float, int] | None:
    """A named place inside `base`, as (label, lat, lon, radius)."""
    def within(lat: float, lon: float, km: float) -> bool:
        return distance_to_region_km(lat, lon, base.key) <= km

    if part in FINE_PLACES:
        hit = FINE_PLACES[part]
        return hit if within(hit[1], hit[2], MAX_OUTSIDE_COUNTRY_KM) else None
    if _is_broad_name(part):
        return None
    for _, lat, lon in _cities().get(part, []):
        if within(lat, lon, MAX_CITY_OUTSIDE_KM):
            # The Met's spelling reads better than the gazetteer's (Nishapur, not Neyshabur).
            name = part.title()
            return (name if " or " in base.label else f"{name}, {base.label}"), lat, lon, CITY_RADIUS_KM
    return None


def _refine(place: Place, fields: tuple[str, ...]) -> Place:
    """Narrow a whole-country answer using the first field that names a place within it."""
    if not place.key.startswith("c:"):
        return place
    for value in fields:
        hits = []
        for alt in _alternatives(value):
            hit = next((h for p in _fine_parts(alt) if (h := _fine_place(p, place))), None)
            if hit is None:
                hits = []    # "Hermopolis or somewhere unknown" doesn't narrow anything
                break
            if hit not in hits:
                hits.append(hit)
        if hits:
            key = "+".join(f"o:{h[1]},{h[2]},{h[3]}" for h in hits)
            label = " or ".join(dict.fromkeys(h[0] for h in hits))
            return Place(key, label, place.bucket, hits[0][1], hits[0][2], "A")
    return place


def resolve_object(culture: str = "", country: str = "", region: str = "", subregion: str = "",
                   city: str = "", county: str = "", state: str = "") -> Place | None:
    """Best region for a Met object, or None to exclude it from the pool."""
    place = (_resolve_each(culture, _strong)
             or _resolve_each(country, _country)
             or _resolve_each(region, _country)
             or _resolve_each(culture, _weak))
    if place is None:
        return None
    return _refine(place, (subregion, city, culture, county, state, region, country))


# ---------------------------------------------------------------- distances

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def _in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _in_polygon(lon: float, lat: float, polygon: Polygon) -> bool:
    return _in_ring(lon, lat, polygon[0]) and not any(_in_ring(lon, lat, h) for h in polygon[1:])


def distance_to_region_km(lat: float, lon: float, key: str) -> float:
    """0 inside the region, else great-circle km to the nearest point on its border.

    The nearest point is found in lon/lat space (as the real game does), then
    measured with haversine.
    """
    lon = ((lon + 180) % 360 + 360) % 360 - 180
    polygons = geometry(key)
    if any(_in_polygon(lon, lat, p) for p in polygons):
        return 0.0
    best = math.inf
    for polygon in polygons:
        for ring in polygon:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                dx, dy = x2 - x1, y2 - y1
                seg = dx * dx + dy * dy
                t = 0.0 if seg == 0 else max(0.0, min(1.0, ((lon - x1) * dx + (lat - y1) * dy) / seg))
                best = min(best, haversine_km(lat, lon, y1 + t * dy, x1 + t * dx))
    return best
