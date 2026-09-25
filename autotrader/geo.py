"""Where a car actually is, and how far that is from where you are.

The site ignores the location and radius parameters a pasted link carries
(``prx``/``loc``), so the distance rule is enforced here instead.

Coordinates are good to about a kilometre, ample for a driving radius. The
tables cover the places Canadian listings name; a car that cannot be placed
is never excluded (see ``too_far``).
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

# Canadian postal codes start with a letter that names the region.
POSTAL_REGION = {
    "A": "NL", "B": "NS", "C": "PE", "E": "NB", "G": "QC", "H": "QC",
    "J": "QC", "K": "ON", "L": "ON", "M": "ON", "N": "ON", "P": "ON",
    "R": "MB", "S": "SK", "T": "AB", "V": "BC", "X": "NT", "Y": "YT",
}

# Where a postal code resolves to when we know nothing more precise than its
# first letter: the main centre of that region.
POSTAL_ANCHOR = {
    "A": (47.561, -52.712), "B": (44.649, -63.575), "C": (46.238, -63.129),
    "E": (46.088, -64.778), "G": (46.814, -71.208), "H": (45.502, -73.567),
    "J": (45.606, -73.712), "K": (45.421, -75.697), "L": (43.589, -79.644),
    "M": (43.653, -79.383), "N": (43.451, -80.493), "P": (46.492, -80.993),
    "R": (49.895, -97.138), "S": (52.134, -106.647), "T": (51.045, -114.058),
    "V": (49.283, -123.121), "X": (62.454, -114.372), "Y": (60.721, -135.057),
}

# Forward sortation areas worth pinning more precisely than their region.
FSA = {
    "V6C": (49.287, -123.117),   # Vancouver, downtown
    "M5V": (43.645, -79.395),    # Toronto, downtown
    "K1P": (45.423, -75.700),    # Ottawa, downtown
    "T2P": (51.047, -114.070),   # Calgary, downtown
    "T5J": (53.544, -113.494),   # Edmonton, downtown
    "H3B": (45.502, -73.570),    # Montreal, downtown
}

# City -> (lat, lon), keyed by province: the places listings name plus every
# town of any size, so an unknown town is the exception.
CITIES: dict[str, dict[str, tuple[float, float]]] = {
    "BC": {
        "vancouver": (49.283, -123.121), "richmond": (49.166, -123.134),
        "surrey": (49.104, -122.826), "burnaby": (49.248, -122.980),
        "coquitlam": (49.284, -122.792), "port coquitlam": (49.262, -122.781),
        "port moody": (49.283, -122.831), "langley": (49.104, -122.660),
        "abbotsford": (49.050, -122.304), "chilliwack": (49.158, -121.951),
        "delta": (49.084, -123.058), "new westminster": (49.207, -122.911),
        "north vancouver": (49.320, -123.073), "west vancouver": (49.328, -123.160),
        "maple ridge": (49.219, -122.602), "pitt meadows": (49.221, -122.690),
        "mission": (49.133, -122.315), "white rock": (49.026, -122.803),
        "squamish": (49.702, -123.156), "whistler": (50.116, -122.955),
        "victoria": (48.428, -123.366), "saanich": (48.484, -123.381),
        "sidney": (48.650, -123.399), "duncan": (48.779, -123.708),
        "nanaimo": (49.166, -123.940), "parksville": (49.319, -124.313),
        "courtenay": (49.687, -124.994), "campbell river": (50.024, -125.244),
        "port alberni": (49.234, -124.805), "powell river": (49.835, -124.523),
        "sechelt": (49.474, -123.760), "gibsons": (49.401, -123.505),
        "hope": (49.383, -121.441), "kelowna": (49.888, -119.496),
        "west kelowna": (49.863, -119.583), "vernon": (50.267, -119.272),
        "penticton": (49.491, -119.586), "kamloops": (50.675, -120.341),
        "salmon arm": (50.700, -119.284), "prince george": (53.917, -122.750),
        "nelson": (49.494, -117.297), "cranbrook": (49.512, -115.769),
        "kimberley": (49.670, -115.977), "castlegar": (49.324, -117.659),
        "trail": (49.096, -117.711), "revelstoke": (50.998, -118.196),
        "golden": (51.297, -116.965), "fort st john": (56.252, -120.846),
        "dawson creek": (55.760, -120.236), "terrace": (54.518, -128.603),
        "prince rupert": (54.312, -130.320), "quesnel": (52.978, -122.493),
        "williams lake": (52.129, -122.140), "osoyoos": (49.032, -119.466),
        "summerland": (49.600, -119.670), "langford": (48.450, -123.505),
        "colwood": (48.424, -123.485), "sooke": (48.374, -123.729),
    },
    "AB": {
        "calgary": (51.045, -114.058), "edmonton": (53.546, -113.494),
        "red deer": (52.268, -113.811), "lethbridge": (49.694, -112.833),
        "medicine hat": (50.041, -110.677), "airdrie": (51.292, -114.014),
        "grande prairie": (55.171, -118.795), "fort mcmurray": (56.727, -111.380),
        "st albert": (53.631, -113.626), "sherwood park": (53.542, -113.296),
        "spruce grove": (53.545, -113.911), "leduc": (53.264, -113.552),
        "okotoks": (50.725, -113.983), "cochrane": (51.189, -114.467),
        "camrose": (53.022, -112.833), "lloydminster": (53.277, -110.005),
        "canmore": (51.089, -115.359), "banff": (51.178, -115.571),
        "fort saskatchewan": (53.712, -113.213), "beaumont": (53.352, -113.415),
        "chestermere": (51.038, -113.819), "stony plain": (53.529, -114.006),
    },
    "SK": {
        "saskatoon": (52.134, -106.647), "regina": (50.445, -104.619),
        "warman": (52.322, -106.584), "moose jaw": (50.393, -105.552),
        "prince albert": (53.203, -105.753), "swift current": (50.285, -107.797),
        "yorkton": (51.214, -102.463), "martensville": (52.291, -106.667),
        "estevan": (49.139, -102.985), "north battleford": (52.777, -108.286),
    },
    "MB": {
        "winnipeg": (49.895, -97.138), "brandon": (49.848, -99.950),
        "steinbach": (49.526, -96.684), "winkler": (49.182, -97.941),
        "portage la prairie": (49.973, -98.292), "selkirk": (50.144, -96.884),
    },
    "ON": {
        "toronto": (43.653, -79.383), "north york": (43.767, -79.413),
        "scarborough": (43.773, -79.258), "etobicoke": (43.654, -79.567),
        "ottawa": (45.421, -75.697), "mississauga": (43.589, -79.644),
        "brampton": (43.731, -79.762), "hamilton": (43.256, -79.871),
        "london": (42.984, -81.245), "markham": (43.857, -79.337),
        "vaughan": (43.837, -79.508), "woodbridge": (43.777, -79.600),
        "concord": (43.800, -79.483), "thornhill": (43.815, -79.424),
        "richmond hill": (43.882, -79.440), "aurora": (44.000, -79.466),
        "newmarket": (44.057, -79.461), "stouffville": (43.971, -79.245),
        "kitchener": (43.451, -80.493), "waterloo": (43.464, -80.520),
        "cambridge": (43.360, -80.312), "guelph": (43.545, -80.248),
        "windsor": (42.317, -83.027), "oakville": (43.468, -79.687),
        "burlington": (43.325, -79.799), "milton": (43.518, -79.877),
        "georgetown": (43.650, -79.917), "barrie": (44.389, -79.690),
        "bradford": (44.115, -79.567), "oshawa": (43.897, -78.866),
        "whitby": (43.897, -78.943), "ajax": (43.851, -79.020),
        "pickering": (43.836, -79.090), "bowmanville": (43.913, -78.688),
        "st catharines": (43.159, -79.247), "niagara falls": (43.095, -79.076),
        "welland": (42.992, -79.248), "grimsby": (43.194, -79.560),
        "stoney creek": (43.217, -79.766), "ancaster": (43.219, -79.985),
        "brantford": (43.139, -80.265), "woodstock": (43.130, -80.747),
        "stratford": (43.370, -80.982), "sarnia": (42.975, -82.404),
        "chatham": (42.404, -82.191), "peterborough": (44.309, -78.320),
        "kingston": (44.231, -76.486), "belleville": (44.163, -77.383),
        "cornwall": (45.021, -74.730), "orillia": (44.609, -79.420),
        "collingwood": (44.501, -80.217), "orangeville": (43.919, -80.094),
        "caledon": (43.867, -79.867), "bolton": (43.874, -79.735),
        "sudbury": (46.492, -80.993), "north bay": (46.309, -79.461),
        "sault ste marie": (46.521, -84.334), "thunder bay": (48.380, -89.247),
        "timmins": (48.478, -81.330), "kanata": (45.308, -75.898),
        "nepean": (45.347, -75.735), "orleans": (45.463, -75.520),
        "gloucester": (45.383, -75.583), "mississauga east": (43.600, -79.600),
    },
    "QC": {
        "montreal": (45.502, -73.567), "laval": (45.606, -73.712),
        "quebec": (46.814, -71.208), "levis": (46.803, -71.177),
        "gatineau": (45.477, -75.702), "longueuil": (45.531, -73.518),
        "sherbrooke": (45.404, -71.888), "trois-rivieres": (46.343, -72.542),
        "terrebonne": (45.700, -73.646), "saint-jerome": (45.780, -74.003),
        "granby": (45.400, -72.733), "dorval": (45.450, -73.750),
        "brossard": (45.459, -73.465), "repentigny": (45.742, -73.450),
        "boisbriand": (45.611, -73.837), "blainville": (45.667, -73.883),
        "mirabel": (45.650, -74.083), "saint-hyacinthe": (45.630, -72.957),
        "drummondville": (45.883, -72.483), "vaudreuil-dorion": (45.400, -74.033),
        "saint-laurent": (45.500, -73.667), "anjou": (45.617, -73.550),
        "pointe-claire": (45.448, -73.817), "saint-eustache": (45.565, -73.905),
        "chicoutimi": (48.428, -71.058), "rimouski": (48.449, -68.523),
        "shawinigan": (46.566, -72.744), "victoriaville": (46.053, -71.966),
        "saint-jean-sur-richelieu": (45.307, -73.263), "magog": (45.267, -72.150),
        "boucherville": (45.594, -73.436), "candiac": (45.383, -73.517),
    },
    "NS": {
        "halifax": (44.649, -63.575), "dartmouth": (44.671, -63.577),
        "sydney": (46.136, -60.195), "truro": (45.367, -63.283),
        "new glasgow": (45.593, -62.645), "bridgewater": (44.378, -64.519),
    },
    "NB": {
        "moncton": (46.088, -64.778), "saint john": (45.273, -66.063),
        "fredericton": (45.964, -66.643), "dieppe": (46.098, -64.687),
        "bathurst": (47.618, -65.651),
    },
    "PE": {"charlottetown": (46.238, -63.129), "summerside": (46.394, -63.789)},
    "NL": {"st johns": (47.561, -52.712), "mount pearl": (47.517, -52.806),
           "corner brook": (48.950, -57.952)},
    "YT": {"whitehorse": (60.721, -135.057)},
    "NT": {"yellowknife": (62.454, -114.372)},
    "NU": {"iqaluit": (63.747, -68.517)},
}

_POSTAL_RE = re.compile(r"^\s*([A-Za-z]\d[A-Za-z])\s*\d?[A-Za-z]?\d?\s*$")
EARTH_RADIUS_KM = 6371.0


def _fold(text: str) -> str:
    """Normalise a place name: accents, punctuation and case all removed.

    "Montréal", "MONTREAL" and "Montreal" are one place; "St. Catharines" and
    "St Catharines" are one place.
    """
    stripped = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    stripped = stripped.lower().replace("&", " and ")
    stripped = re.sub(r"\bsaint\b", "st", stripped)
    stripped = re.sub(r"\bste\.?\b", "ste", stripped)
    stripped = re.sub(r"[^a-z0-9\- ]+", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km.

    Never longer than road distance, which is the safe direction for a
    filter: it never excludes a car that is actually within range.
    """
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def locate(city: str = "", province: str = "") -> tuple[float, float] | None:
    """Where a listing's city is, or None if we have never heard of it."""
    key = _fold(city)
    if not key:
        return None
    prov = (province or "").strip().upper()
    if prov in CITIES and key in CITIES[prov]:
        return CITIES[prov][key]
    # No province, or a province that does not know the name: a city name is
    # nearly unique across Canada, so a single match elsewhere is still right.
    hits = [coords for places in CITIES.values()
            for name, coords in places.items() if name == key]
    return hits[0] if len(hits) == 1 else None


def locate_reference(text: str) -> tuple[float, float] | None:
    """Where a postal code, "City, PR" or a bare city name is."""
    raw = str(text or "").strip()
    if not raw:
        return None
    match = _POSTAL_RE.match(raw)
    if match:
        fsa = match.group(1).upper()
        return FSA.get(fsa) or POSTAL_ANCHOR.get(fsa[0])
    if "," in raw:
        city, _, prov = raw.rpartition(",")
        found = locate(city, prov.strip())
        if found:
            return found
    return locate(raw)


def region_of(text: str) -> str:
    """The province a reference point is in, when it names one."""
    raw = str(text or "").strip()
    match = _POSTAL_RE.match(raw)
    if match:
        return POSTAL_REGION.get(match.group(1)[0].upper(), "")
    if "," in raw:
        prov = raw.rpartition(",")[2].strip().upper()
        if prov in CITIES:
            return prov
    key = _fold(raw)
    for prov, places in CITIES.items():
        if key in places:
            return prov
    return ""


def nearest_in(province: str, point: tuple[float, float]) -> float | None:
    """How close the nearest place we know of in a province gets to a point.

    Lets an unknown town still be excluded honestly: if every known place in
    its province is beyond the radius, so is the town.
    """
    places = CITIES.get((province or "").strip().upper())
    if not places:
        return None
    return min(distance_km(point, coords) for coords in places.values())


def too_far(city: str, province: str, reference: tuple[float, float],
            radius_km: float) -> tuple[bool, float | None]:
    """Is this car outside the radius?  Returns (too far, distance if known).

    A car that cannot be placed is never excluded: the table is the likeliest
    thing to be incomplete, and hiding a real match is the worst failure.
    """
    if radius_km <= 0:
        return False, None
    here = locate(city, province)
    if here is not None:
        away = distance_km(reference, here)
        return away > radius_km, round(away)
    floor = nearest_in(province, reference)
    if floor is not None and floor > radius_km:
        # Nothing in that province is within range, so this town is not either.
        return True, None
    return False, None


def describe(reference: str, radius_km: float) -> str:
    """A phrase for the dashboard and the search's rule chips."""
    where = str(reference or "").strip() or "your area"
    return f"within {int(radius_km):,} km of {where}"


def summary(reference: str, radius_km: float) -> dict[str, Any]:
    point = locate_reference(reference)
    return {
        "reference": str(reference or "").strip(),
        "radius_km": int(radius_km or 0),
        "resolved": bool(point),
        "lat": round(point[0], 3) if point else None,
        "lon": round(point[1], 3) if point else None,
        "province": region_of(reference),
        "text": describe(reference, radius_km),
    }
