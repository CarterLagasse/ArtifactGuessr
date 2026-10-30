"""geolite.py — turn Met geography strings into scorable regions, offline.

The real game scores distance to a region polygon (0 km inside it), so every
object resolves to a `Place` whose geometry is one of:
  - modern country polygons (Natural Earth 50m), unioned for "Egypt or Syria"
  - a hand-placed circle for cultures that don't match a modern border (Moche)

Resolution order per object (see `resolve_object`):
  1. strong culture (Moche, Greek/Attic, Yoruba ...): beats the country field
  2. Country field, then Region field
  3. weak culture (demonyms: French, Italian, Roman ...)
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
EARTH_KM = 6371.0

Polygon = list[list[list[float]]]  # rings of [lon, lat]; ring 0 is the shell


@dataclass(frozen=True)
class Place:
    key: str      # geometry key, e.g. "c:Egypt+c:Syria" or "o:35.2,24.8,250"
    label: str    # shown on reveal
    hint: str     # world-region wording for the Place hint
    bucket: str   # balancing bucket for daily picks
    lat: float    # representative point
    lon: float
    tier: str     # A culture region, B country, C inferred from a demonym


# ---------------------------------------------------------------- world regions

# Natural Earth SUBREGION -> (hint wording, balancing bucket)
SUBREGIONS: dict[str, tuple[str, str]] = {
    "Western Europe": ("Western Europe", "Europe"),
    "Northern Europe": ("Northern Europe", "Europe"),
    "Southern Europe": ("Southern Europe", "Europe"),
    "Eastern Europe": ("Eastern Europe", "Europe"),
    "Western Asia": ("Near East", "Near East"),
    "Northern Africa": ("North Africa", "Near East"),
    "Central Asia": ("Central Asia", "South & Central Asia"),
    "Southern Asia": ("South Asia", "South & Central Asia"),
    "Eastern Asia": ("East Asia", "East Asia"),
    "South-Eastern Asia": ("Southeast Asia", "Southeast Asia"),
    "Western Africa": ("West Africa", "Sub-Saharan Africa"),
    "Middle Africa": ("Central Africa", "Sub-Saharan Africa"),
    "Eastern Africa": ("East Africa", "Sub-Saharan Africa"),
    "Southern Africa": ("Southern Africa", "Sub-Saharan Africa"),
    "Northern America": ("North America", "Americas"),
    "Central America": ("Central America", "Americas"),
    "Caribbean": ("Caribbean", "Americas"),
    "South America": ("South America", "Americas"),
    "Melanesia": ("Oceania", "Oceania"),
    "Micronesia": ("Oceania", "Oceania"),
    "Polynesia": ("Oceania", "Oceania"),
    "Australia and New Zealand": ("Oceania", "Oceania"),
}
HINT_OVERRIDES: dict[str, tuple[str, str]] = {
    "Iran": ("Near East", "Near East"),
    "Afghanistan": ("Central Asia", "South & Central Asia"),
    "Mexico": ("Mesoamerica", "Americas"),
    "Guatemala": ("Mesoamerica", "Americas"),
    "Belize": ("Mesoamerica", "Americas"),
    "El Salvador": ("Mesoamerica", "Americas"),
    "Honduras": ("Mesoamerica", "Americas"),
    "Peru": ("Andes", "Americas"),
    "Bolivia": ("Andes", "Americas"),
    "Ecuador": ("Andes", "Americas"),
    "Sudan": ("Northeast Africa", "Sub-Saharan Africa"),
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
# key -> (label, hint, bucket, geometry). Radii are deliberately generous: the
# real game draws broad cultural regions, and a tiny circle punishes good guesses.
STRONG_CULTURES: dict[str, tuple[str, str, str, tuple]] = {
    # Aegean and Mediterranean
    "greek, attic": ("Attica, Greece", "Southern Europe", "Europe", _countries("Greece")),
    "greek, south italian": ("Magna Graecia, southern Italy", "Southern Europe", "Europe", _circle(39.8, 16.2, 300)),
    "greek, south italian, apulian": ("Apulia, southern Italy", "Southern Europe", "Europe", _circle(40.8, 16.8, 200)),
    "greek, east greek": ("Ionia, western Anatolia", "Near East", "Near East", _circle(38.3, 27.2, 250)),
    "minoan": ("Minoan Crete", "Southern Europe", "Europe", _circle(35.2, 24.9, 200)),
    "mycenaean": ("Mycenaean Greece", "Southern Europe", "Europe", _countries("Greece")),
    "helladic": ("Bronze Age Greece", "Southern Europe", "Europe", _countries("Greece")),
    "cycladic": ("Cyclades, Greece", "Southern Europe", "Europe", _circle(37.0, 25.2, 150)),
    "cypriot": ("Cyprus", "Near East", "Near East", _countries("Cyprus", "Northern Cyprus")),
    "roman, cypriot": ("Roman Cyprus", "Near East", "Near East", _countries("Cyprus", "Northern Cyprus")),
    "etruscan": ("Etruria, central Italy", "Southern Europe", "Europe", _circle(42.8, 11.6, 200)),
    "villanovan": ("Etruria, central Italy", "Southern Europe", "Europe", _circle(43.0, 11.6, 250)),
    "phoenician": ("Phoenicia", "Near East", "Near East", _circle(33.9, 35.6, 250)),
    "punic": ("Carthage", "North Africa", "Near East", _circle(36.8, 10.2, 300)),
    "frankish": ("Frankish lands", "Western Europe", "Europe", _countries("France", "Germany", "Belgium")),
    "merovingian": ("Merovingian Gaul", "Western Europe", "Europe", _countries("France", "Belgium")),
    "visigothic": ("Visigothic Spain", "Southern Europe", "Europe", _countries("Spain")),
    "langobardic": ("Lombard Italy", "Southern Europe", "Europe", _countries("Italy")),
    "lombard": ("Lombard Italy", "Southern Europe", "Europe", _countries("Italy")),
    "anglo-saxon": ("Anglo-Saxon England", "Northern Europe", "Europe", _countries("United Kingdom")),
    "celtic": ("Celtic Europe", "Western Europe", "Europe", _countries("France", "Ireland", "United Kingdom")),
    "avar": ("Avar Khaganate", "Eastern Europe", "Europe", _countries("Hungary", "Romania")),
    "scythian": ("Pontic steppe", "Eastern Europe", "Europe", _circle(47.5, 34.5, 500)),
    # Ancient Near East
    "sumerian": ("Sumer, southern Mesopotamia", "Near East", "Near East", _circle(31.3, 45.9, 300)),
    "babylonian": ("Babylonia, southern Mesopotamia", "Near East", "Near East", _circle(32.5, 44.4, 350)),
    "assyrian": ("Assyria, northern Mesopotamia", "Near East", "Near East", _circle(36.3, 43.2, 350)),
    "old assyrian trading colony": ("Cappadocia, Anatolia", "Near East", "Near East", _circle(38.8, 35.5, 300)),
    "hittite": ("Hittite Anatolia", "Near East", "Near East", _circle(40.0, 34.6, 400)),
    "phrygian": ("Phrygia, Anatolia", "Near East", "Near East", _circle(39.5, 31.0, 300)),
    "lydian": ("Lydia, Anatolia", "Near East", "Near East", _circle(38.5, 28.0, 200)),
    "urartian": ("Urartu", "Near East", "Near East", _circle(39.0, 43.3, 300)),
    "elamite": ("Elam, southwest Iran", "Near East", "Near East", _circle(32.0, 48.5, 300)),
    "achaemenid": ("Achaemenid Persia", "Near East", "Near East", _countries("Iran")),
    "parthian": ("Parthian Persia", "Near East", "Near East", _countries("Iran", "Iraq")),
    "sasanian": ("Sasanian Persia", "Near East", "Near East", _countries("Iran", "Iraq")),
    "nabataean": ("Nabataea", "Near East", "Near East", _circle(30.3, 35.4, 250)),
    "palmyrene": ("Palmyra", "Near East", "Near East", _circle(34.6, 38.3, 250)),
    "israelite": ("Ancient Israel", "Near East", "Near East", _countries("Israel", "Palestine")),
    "south arabian": ("South Arabia", "Near East", "Near East", _countries("Yemen")),
    "bactria-margiana archaeological complex": ("Bactria-Margiana", "Central Asia", "South & Central Asia",
                                                _countries("Turkmenistan", "Uzbekistan", "Afghanistan")),
    "bactrian": ("Bactria", "Central Asia", "South & Central Asia", _countries("Afghanistan", "Uzbekistan", "Tajikistan")),
    "sogdian": ("Sogdiana", "Central Asia", "South & Central Asia", _countries("Uzbekistan", "Tajikistan")),
    "kushan": ("Kushan Empire", "South Asia", "South & Central Asia", _countries("Afghanistan", "Pakistan")),
    "nubian": ("Nubia", "Northeast Africa", "Sub-Saharan Africa", _circle(20.0, 31.5, 500)),
    "kushite": ("Kush, Nubia", "Northeast Africa", "Sub-Saharan Africa", _circle(19.0, 32.5, 500)),
    # Asia
    "khmer": ("Khmer Empire", "Southeast Asia", "Southeast Asia", _countries("Cambodia")),
    "cham": ("Champa, central Vietnam", "Southeast Asia", "Southeast Asia", _circle(15.0, 108.5, 350)),
    "mongol": ("Mongolia", "East Asia", "East Asia", _countries("Mongolia")),
    "tibetan": ("Tibetan Plateau", "East Asia", "East Asia", _circle(31.0, 89.0, 800)),
    "tibet": ("Tibetan Plateau", "East Asia", "East Asia", _circle(31.0, 89.0, 800)),
    # Mesoamerica and the Andes
    "olmec": ("Olmec heartland, Mexico", "Mesoamerica", "Americas", _circle(18.0, -94.5, 300)),
    "maya": ("Maya region", "Mesoamerica", "Americas", _circle(17.0, -89.8, 450)),
    "aztec": ("Aztec Empire", "Mesoamerica", "Americas", _circle(19.4, -99.1, 350)),
    "mixtec": ("Oaxaca, Mexico", "Mesoamerica", "Americas", _circle(17.1, -97.0, 250)),
    "zapotec": ("Oaxaca, Mexico", "Mesoamerica", "Americas", _circle(17.1, -96.7, 250)),
    "teotihuacan": ("Teotihuacan, Mexico", "Mesoamerica", "Americas", _circle(19.7, -98.8, 300)),
    "huastec": ("Huasteca, Mexico", "Mesoamerica", "Americas", _circle(21.8, -98.5, 250)),
    "moche": ("North coast of Peru", "Andes", "Americas", _circle(-8.2, -78.9, 350)),
    "chimú": ("North coast of Peru", "Andes", "Americas", _circle(-8.1, -79.0, 350)),
    "chimu": ("North coast of Peru", "Andes", "Americas", _circle(-8.1, -79.0, 350)),
    "vicús": ("Far north of Peru", "Andes", "Americas", _circle(-5.2, -80.2, 250)),
    "nasca": ("South coast of Peru", "Andes", "Americas", _circle(-14.8, -75.1, 300)),
    "nazca": ("South coast of Peru", "Andes", "Americas", _circle(-14.8, -75.1, 300)),
    "paracas": ("South coast of Peru", "Andes", "Americas", _circle(-13.9, -76.2, 300)),
    "chancay": ("Central coast of Peru", "Andes", "Americas", _circle(-11.5, -77.2, 250)),
    "wari": ("Wari Empire, Peru", "Andes", "Americas", _countries("Peru")),
    "inca": ("Inca Empire", "Andes", "Americas", _countries("Peru", "Bolivia", "Ecuador")),
    "tiwanaku": ("Lake Titicaca basin", "Andes", "Americas", _circle(-16.5, -68.7, 300)),
    "calima": ("Colombia", "Andes", "Americas", _countries("Colombia")),
    "quimbaya": ("Colombia", "Andes", "Americas", _countries("Colombia")),
    "muisca": ("Colombia", "Andes", "Americas", _countries("Colombia")),
    "tairona": ("Colombia", "Andes", "Americas", _countries("Colombia")),
    "jama-coaque": ("Coastal Ecuador", "Andes", "Americas", _circle(-0.3, -80.0, 250)),
    "coclé": ("Panama", "Central America", "Americas", _countries("Panama")),
    "taíno": ("Greater Antilles", "Caribbean", "Americas", _countries("Dominican Republic", "Haiti", "Puerto Rico", "Cuba")),
    # North America
    "navajo": ("American Southwest", "North America", "Americas", _circle(36.0, -110.0, 500)),
    "hopi": ("American Southwest", "North America", "Americas", _circle(35.8, -110.5, 500)),
    "zuni": ("American Southwest", "North America", "Americas", _circle(35.0, -108.8, 500)),
    "lakota": ("Northern Plains", "North America", "Americas", _circle(44.5, -102.0, 600)),
    "haida": ("Pacific Northwest Coast", "North America", "Americas", _circle(54.0, -131.0, 600)),
    "tlingit": ("Pacific Northwest Coast", "North America", "Americas", _circle(58.0, -135.0, 600)),
    "anishinaabe": ("Great Lakes", "North America", "Americas", _circle(46.0, -85.0, 700)),
    # Sub-Saharan Africa
    "yoruba": ("Yorubaland", "West Africa", "Sub-Saharan Africa", _circle(7.6, 4.2, 300)),
    "edo": ("Kingdom of Benin, Nigeria", "West Africa", "Sub-Saharan Africa", _circle(6.3, 5.6, 200)),
    "benin": ("Kingdom of Benin, Nigeria", "West Africa", "Sub-Saharan Africa", _circle(6.3, 5.6, 200)),
    "igbo": ("Igboland, Nigeria", "West Africa", "Sub-Saharan Africa", _circle(5.9, 7.4, 200)),
    "akan": ("Akan lands", "West Africa", "Sub-Saharan Africa", _countries("Ghana", "Ivory Coast")),
    "asante": ("Asante, Ghana", "West Africa", "Sub-Saharan Africa", _countries("Ghana")),
    "bamana": ("Bamana lands, Mali", "West Africa", "Sub-Saharan Africa", _countries("Mali")),
    "dogon": ("Dogon country, Mali", "West Africa", "Sub-Saharan Africa", _countries("Mali")),
    "senufo": ("Senufo lands", "West Africa", "Sub-Saharan Africa", _circle(9.8, -5.5, 300)),
    "baule": ("Côte d'Ivoire", "West Africa", "Sub-Saharan Africa", _countries("Ivory Coast")),
    "dan": ("Dan lands", "West Africa", "Sub-Saharan Africa", _circle(7.3, -8.0, 250)),
    "mende": ("Sierra Leone", "West Africa", "Sub-Saharan Africa", _countries("Sierra Leone", "Liberia")),
    "sapi": ("Sierra Leone", "West Africa", "Sub-Saharan Africa", _countries("Sierra Leone", "Guinea")),
    "fang": ("Fang lands", "Central Africa", "Sub-Saharan Africa", _countries("Gabon", "Equatorial Guinea")),
    "punu": ("Gabon", "Central Africa", "Sub-Saharan Africa", _countries("Gabon")),
    "kota": ("Gabon", "Central Africa", "Sub-Saharan Africa", _countries("Gabon")),
    "kongo": ("Kingdom of Kongo", "Central Africa", "Sub-Saharan Africa", _circle(-5.8, 14.5, 450)),
    "luba": ("Luba lands, DR Congo", "Central Africa", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "songye": ("DR Congo", "Central Africa", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "kuba": ("Kuba Kingdom, DR Congo", "Central Africa", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "lega": ("DR Congo", "Central Africa", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "mangbetu": ("DR Congo", "Central Africa", "Sub-Saharan Africa", _countries("Democratic Republic of the Congo")),
    "chokwe": ("Chokwe lands", "Central Africa", "Sub-Saharan Africa", _countries("Angola")),
    "zulu": ("KwaZulu", "Southern Africa", "Sub-Saharan Africa", _countries("South Africa")),
    # Oceania
    "asmat": ("Asmat, New Guinea", "Oceania", "Oceania", _circle(-5.5, 138.5, 350)),
    "iatmul": ("Sepik River, New Guinea", "Oceania", "Oceania", _circle(-4.2, 143.3, 300)),
    "abelam": ("Sepik River, New Guinea", "Oceania", "Oceania", _circle(-3.8, 143.0, 300)),
    "maori": ("New Zealand", "Oceania", "Oceania", _countries("New Zealand")),
    "māori": ("New Zealand", "Oceania", "Oceania", _countries("New Zealand")),
    "hawaiian": ("Hawaiʻi", "Oceania", "Oceania", _circle(20.6, -157.5, 350)),
    "marquesan": ("Marquesas Islands", "Oceania", "Oceania", _circle(-9.5, -139.5, 300)),
    "rapa nui": ("Rapa Nui", "Oceania", "Oceania", _circle(-27.1, -109.4, 250)),
    "fijian": ("Fiji", "Oceania", "Oceania", _countries("Fiji")),
    "tongan": ("Tonga", "Oceania", "Oceania", _countries("Tonga")),
    "samoan": ("Samoa", "Oceania", "Oceania", _countries("Samoa")),
    "aboriginal": ("Australia", "Oceania", "Oceania", _countries("Australia")),
}


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
    r"^(?:possibly|probably|perhaps|likely|present-day|modern|the|ancient|attributed to)\s+", re.I)
_DIRECTION = re.compile(r"^(?:north|south|east|west|central|upper|lower)(?:ern)?\s+", re.I)
_SPLIT = re.compile(r"\s*(?:\||;|/|\bor\b)\s*", re.I)


def _clean(s: str) -> str:
    s = s.strip().lower().replace("(?)", "").replace("?", "").strip(" ,.|")
    while _QUALIFIERS.match(s):
        s = _QUALIFIERS.sub("", s, count=1)
    return s.strip()


def _alternatives(raw: str) -> list[str]:
    """'Egypt or Syria' -> ['egypt', 'syria']; repeats collapse ('Iran|Iran')."""
    out: list[str] = []
    for alt in _SPLIT.split(raw or ""):
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


def _country_place(names: list[str], tier: str) -> Place | None:
    hints = {HINT_OVERRIDES.get(n) or SUBREGIONS.get(_ne()[n]["properties"]["SUBREGION"]) for n in names}
    hints.discard(None)
    if not hints or len({h[1] for h in hints}) > 1:
        return None
    hint, bucket = sorted(hints)[0]
    lat, lon = _country_polygons(names[0])[1]
    label = " or ".join(n.replace("United States of America", "United States") for n in names)
    return Place("+".join(f"c:{n}" for n in names), label, hint, bucket, round(lat, 3), round(lon, 3), tier)


def _culture_place(entry: tuple) -> Place:
    label, hint, bucket, geom = entry
    if geom[0] == "c":
        lat, lon = _country_polygons(geom[1][0])[1]
        key = "+".join(f"c:{n}" for n in geom[1])
    else:
        _, lat, lon, km = geom
        key = f"o:{lat},{lon},{km}"
    return Place(key, label, hint, bucket, round(lat, 3), round(lon, 3), "A")


def _merge(places: list[Place]) -> Place | None:
    """Alternatives become one region, but not across world regions."""
    if not places or len({p.bucket for p in places}) > 1:
        return None
    if len({p.key for p in places}) == 1:
        return places[0]
    first = places[0]
    return Place("+".join(dict.fromkeys(p.key for p in places)),
                 " or ".join(dict.fromkeys(p.label for p in places)),
                 first.hint, first.bucket, first.lat, first.lon, first.tier)


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
        if v in STRONG_CULTURES:
            return _culture_place(STRONG_CULTURES[v])
    return None


def _country(alt: str) -> Place | None:
    names = _country_names(alt)
    return _country_place(names, "B") if names else None


def _weak(alt: str) -> Place | None:
    for v in _variants(alt):
        if v in WEAK_CULTURES:
            return _country_place(WEAK_CULTURES[v], "C")
    return _country_place(n, "C") if (n := _country_names(alt)) else None


def resolve_object(culture: str = "", country: str = "", region: str = "") -> Place | None:
    """Best region for a Met object, or None to exclude it from the pool."""
    return (_resolve_each(culture, _strong)
            or _resolve_each(country, _country)
            or _resolve_each(region, _country)
            or _resolve_each(culture, _weak))


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
