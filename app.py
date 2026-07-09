from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = BASE_DIR / "outputs"

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

PLACE_FIELDS = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.googleMapsUri",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.businessStatus",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
BAD_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
EXPORT_FIELDS = ["query", "name", "phone", "address", "website", "emails", "google_maps_url", "distance_km", "rating", "reviews", "business_status", "place_id"]
EXPORT_HEADERS = ["Arama", "Firma", "Telefon", "Adres", "Web", "E-posta", "Harita", "Km", "Puan", "Yorum", "Durum", "ID"]


class AppError(Exception):
    pass


class RateLimitError(AppError):
    pass


@dataclass
class SearchConfig:
    source: str
    google_api_key: str
    default_location: str
    default_radius_km: float
    default_queries: list[str]
    language_code: str
    region_code: str
    page_size: int
    max_pages_per_query: int
    fetch_emails_from_websites: bool
    email_pages: list[str]
    request_timeout_seconds: int
    overpass_limit: int
    overpass_timeout_seconds: int
    overpass_retry_delay_seconds: int
    gosom_binary: str
    gosom_use_docker: bool
    gosom_depth: int
    gosom_fast_mode: bool
    gosom_exit_on_inactivity: str


def load_config() -> SearchConfig:
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    api_key = os.getenv("GOOGLE_MAPS_API_KEY") or data.get("google_api_key") or ""
    api_key = api_key.strip()
    if not api_key or api_key == "GOOGLE_MAPS_API_KEY_BURAYA":
        api_key = ""

    return SearchConfig(
        source=str(data.get("source", "osm")).lower(),
        google_api_key=api_key,
        default_location=str(data.get("default_location", "10115 Berlin, Germany")),
        default_radius_km=float(data.get("default_radius_km", 10)),
        default_queries=list(data.get("default_queries", ["kfz werkstatt"])),
        language_code=str(data.get("language_code", "de")),
        region_code=str(data.get("region_code", "DE")),
        page_size=max(1, min(int(data.get("page_size", 20)), 20)),
        max_pages_per_query=max(1, min(int(data.get("max_pages_per_query", 3)), 3)),
        fetch_emails_from_websites=bool(data.get("fetch_emails_from_websites", True)),
        email_pages=list(data.get("email_pages", ["/", "/contact", "/kontakt", "/impressum"])),
        request_timeout_seconds=max(3, int(data.get("request_timeout_seconds", 12))),
        overpass_limit=max(10, min(int(data.get("overpass_limit", 250)), 1000)),
        overpass_timeout_seconds=max(20, min(int(data.get("overpass_timeout_seconds", 45)), 180)),
        overpass_retry_delay_seconds=max(0, min(int(data.get("overpass_retry_delay_seconds", 3)), 30)),
        gosom_binary=str(data.get("gosom_binary", "")).strip(),
        gosom_use_docker=bool(data.get("gosom_use_docker", True)),
        gosom_depth=max(1, min(int(data.get("gosom_depth", 1)), 10)),
        gosom_fast_mode=bool(data.get("gosom_fast_mode", True)),
        gosom_exit_on_inactivity=str(data.get("gosom_exit_on_inactivity", "90s")),
    )


def http_json(url: str, method: str = "GET", headers: dict[str, str] | None = None, body: Any = None, timeout: int = 12) -> Any:
    payload = None
    request_headers = headers or {}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **request_headers}

    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 429:
            raise RateLimitError("OpenStreetMap Overpass su anda yogun ve istekleri sinirliyor.") from exc
        raise AppError(f"HTTP {exc.code}: {detail[:600]}") from exc
    except URLError as exc:
        raise AppError(f"Baglanti hatasi: {exc.reason}") from exc
    except socket.timeout as exc:
        raise AppError("Istek zaman asimina ugradi.") from exc

    return json.loads(raw)


def http_form_json(url: str, fields: dict[str, str], headers: dict[str, str] | None = None, timeout: int = 12) -> Any:
    payload = urlencode(fields).encode("utf-8")
    request_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    request = Request(url, data=payload, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AppError(f"HTTP {exc.code}: {detail[:600]}") from exc
    except URLError as exc:
        raise AppError(f"Baglanti hatasi: {exc.reason}") from exc
    except socket.timeout as exc:
        raise AppError("Istek zaman asimina ugradi.") from exc

    return json.loads(raw)


def overpass_json(query: str, config: SearchConfig) -> Any:
    errors = []
    timeout = max(config.overpass_timeout_seconds, config.request_timeout_seconds)
    for index, url in enumerate(OVERPASS_URLS):
        try:
            return http_form_json(
                url,
                {"data": query},
                headers={"User-Agent": "LeadFinder/1.0 local tool"},
                timeout=timeout + 8,
            )
        except RateLimitError as exc:
            errors.append(f"{urlparse(url).netloc}: 429 rate limit")
            if index < len(OVERPASS_URLS) - 1 and config.overpass_retry_delay_seconds:
                time.sleep(config.overpass_retry_delay_seconds)
            continue
        except AppError as exc:
            errors.append(f"{urlparse(url).netloc}: {exc}")
    raise AppError("OpenStreetMap Overpass su anda yogun veya sorgu cok genis. Yaricapi 1-3 km yapin, daha net kriter girin ve biraz sonra tekrar deneyin. Detay: " + " | ".join(errors))


def geocode_location(location: str, config: SearchConfig) -> dict[str, float]:
    query = f"{GEOCODE_URL}?address={quote(location)}&key={quote(config.google_api_key)}"
    data = http_json(query, timeout=config.request_timeout_seconds)
    if data.get("status") != "OK" or not data.get("results"):
        raise AppError(f"Konum bulunamadi: {location}. Google status: {data.get('status')}")
    point = data["results"][0]["geometry"]["location"]
    return {"latitude": float(point["lat"]), "longitude": float(point["lng"])}


def osm_geocode_location(location: str, config: SearchConfig) -> dict[str, float]:
    params = urlencode({"q": location, "format": "jsonv2", "limit": "1"})
    headers = {"User-Agent": "LeadFinder/1.0 local tool"}
    data = http_json(f"{NOMINATIM_URL}?{params}", headers=headers, timeout=config.request_timeout_seconds)
    if not data:
        raise AppError(f"Konum bulunamadi: {location}")
    return {"latitude": float(data[0]["lat"]), "longitude": float(data[0]["lon"])}


def radius_viewport(center: dict[str, float], radius_km: float) -> dict[str, Any]:
    lat = center["latitude"]
    lng = center["longitude"]
    lat_delta = radius_km / 111.32
    lng_delta = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return {
        "low": {"latitude": lat - lat_delta, "longitude": lng - lng_delta},
        "high": {"latitude": lat + lat_delta, "longitude": lng + lng_delta},
    }


def distance_km(a: dict[str, float], b: dict[str, float]) -> float:
    radius = 6371.0088
    lat1, lon1 = math.radians(a["latitude"]), math.radians(a["longitude"])
    lat2, lon2 = math.radians(b["latitude"]), math.radians(b["longitude"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def search_places(query: str, center: dict[str, float], radius_km: float, config: SearchConfig) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    token = ""
    viewport = radius_viewport(center, radius_km)

    for page in range(config.max_pages_per_query):
        body: dict[str, Any] = {
            "textQuery": query,
            "pageSize": config.page_size,
            "languageCode": config.language_code,
            "regionCode": config.region_code,
            "locationRestriction": {"rectangle": viewport},
        }
        if token:
            body["pageToken"] = token

        data = http_json(
            TEXT_SEARCH_URL,
            method="POST",
            headers={"X-Goog-Api-Key": config.google_api_key, "X-Goog-FieldMask": PLACE_FIELDS},
            body=body,
            timeout=config.request_timeout_seconds,
        )

        for place in data.get("places", []):
            loc = place.get("location") or {}
            if "latitude" in loc and "longitude" in loc:
                place_distance = distance_km(center, {"latitude": float(loc["latitude"]), "longitude": float(loc["longitude"])})
                if place_distance > radius_km:
                    continue
                place["_distanceKm"] = round(place_distance, 2)
            place["_query"] = query
            results.append(place)

        token = data.get("nextPageToken") or ""
        if not token:
            break
        if page < config.max_pages_per_query - 1:
            time.sleep(2)

    return results


OSM_KEYWORD_FILTERS = {
    "autoteile": [('shop', 'car_parts')],
    "oto yedek": [('shop', 'car_parts')],
    "kfz": [('shop', 'car_repair'), ('craft', 'mechanic')],
    "werkstatt": [('shop', 'car_repair'), ('craft', 'mechanic')],
    "tamir": [('shop', 'car_repair'), ('craft', 'mechanic')],
    "servis": [('shop', 'car_repair'), ('craft', 'mechanic')],
    "reifen": [('shop', 'tyres')],
    "lastik": [('shop', 'tyres')],
    "restaurant": [('amenity', 'restaurant')],
    "cafe": [('amenity', 'cafe')],
    "eczane": [('amenity', 'pharmacy')],
    "apotheke": [('amenity', 'pharmacy')],
    "doktor": [('amenity', 'doctors'), ('healthcare', 'doctor')],
    "hotel": [('tourism', 'hotel')],
}


def overpass_regex(query: str) -> str:
    words = [re.escape(word) for word in re.findall(r"[\w\-]+", query, flags=re.UNICODE)]
    return ".*".join(words) if words else re.escape(query)


def osm_filters_for_query(query: str) -> list[tuple[str, str]]:
    lowered = query.lower()
    filters: list[tuple[str, str]] = []
    for keyword, mapped in OSM_KEYWORD_FILTERS.items():
        if keyword in lowered:
            filters.extend(mapped)
    return sorted(set(filters))


def search_osm_places(query: str, center: dict[str, float], radius_km: float, config: SearchConfig) -> list[dict[str, Any]]:
    radius_m = int(radius_km * 1000)
    lat = center["latitude"]
    lon = center["longitude"]
    regex = overpass_regex(query)
    filters = osm_filters_for_query(query)
    statements = []
    if not filters or radius_km <= 3:
        statements.extend(
            [
                f'nwr(around:{radius_m},{lat},{lon})["name"~"{regex}",i];',
                f'nwr(around:{radius_m},{lat},{lon})["brand"~"{regex}",i];',
                f'nwr(around:{radius_m},{lat},{lon})["operator"~"{regex}",i];',
            ]
        )
    for key, value in filters:
        statements.append(f'nwr(around:{radius_m},{lat},{lon})["{key}"="{value}"];')

    overpass_query = f"""
[out:json][timeout:{config.overpass_timeout_seconds}];
(
  {' '.join(statements)}
);
out center tags {config.overpass_limit};
"""
    data = overpass_json(overpass_query, config)

    results = []
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        loc = element.get("center") or {"lat": element.get("lat"), "lon": element.get("lon")}
        if loc.get("lat") is None or loc.get("lon") is None:
            continue
        point = {"latitude": float(loc["lat"]), "longitude": float(loc["lon"])}
        place_distance = distance_km(center, point)
        if place_distance > radius_km:
            continue
        element["_query"] = query
        element["_distanceKm"] = round(place_distance, 2)
        element["_location"] = point
        if tags.get("name") or tags.get("phone") or tags.get("contact:phone") or tags.get("website") or tags.get("contact:website"):
            results.append(element)
    return results


def decode_cloudflare_email(hex_value: str) -> str:
    key = int(hex_value[:2], 16)
    chars = [chr(int(hex_value[i : i + 2], 16) ^ key) for i in range(2, len(hex_value), 2)]
    return "".join(chars)


def extract_emails(page_text: str) -> set[str]:
    text = html.unescape(page_text)
    emails = set(match.group(0).strip(".,;:()[]{}<>\"'") for match in EMAIL_RE.finditer(text))
    for encoded in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', text):
        try:
            emails.add(decode_cloudflare_email(encoded))
        except ValueError:
            pass
    return {email for email in emails if not email.lower().endswith(BAD_EMAIL_SUFFIXES)}


def same_site_url(base_url: str, path: str) -> str:
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    root = f"{parsed.scheme}://{parsed.netloc}"
    return urljoin(root, path)


def fetch_website_emails(website: str, config: SearchConfig) -> list[str]:
    if not website:
        return []

    found: set[str] = set()
    headers = {"User-Agent": "LeadFinder/1.0 (+local business research tool)"}
    for path in config.email_pages[:8]:
        url = same_site_url(website, path)
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=config.request_timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "")
                if "text/html" not in content_type and "text/plain" not in content_type:
                    continue
                body = response.read(500_000).decode("utf-8", errors="ignore")
        except Exception:
            continue
        found.update(extract_emails(body))
    return sorted(found)


def normalize_place(place: dict[str, Any], config: SearchConfig) -> dict[str, Any]:
    name = (place.get("displayName") or {}).get("text", "")
    phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber") or ""
    website = place.get("websiteUri") or ""
    emails = fetch_website_emails(website, config) if config.fetch_emails_from_websites else []
    return {
        "query": place.get("_query", ""),
        "name": name,
        "phone": phone,
        "address": place.get("formattedAddress", ""),
        "website": website,
        "emails": ", ".join(emails),
        "google_maps_url": place.get("googleMapsUri", ""),
        "distance_km": place.get("_distanceKm", ""),
        "rating": place.get("rating", ""),
        "reviews": place.get("userRatingCount", ""),
        "business_status": place.get("businessStatus", ""),
        "place_id": place.get("id", ""),
    }


def osm_address(tags: dict[str, str]) -> str:
    street = " ".join(part for part in [tags.get("addr:street", ""), tags.get("addr:housenumber", "")] if part).strip()
    city = " ".join(part for part in [tags.get("addr:postcode", ""), tags.get("addr:city", "")] if part).strip()
    parts = [part for part in [street, city, tags.get("addr:country", "")] if part]
    return ", ".join(parts)


def normalize_osm_place(place: dict[str, Any], config: SearchConfig) -> dict[str, Any]:
    tags = place.get("tags") or {}
    loc = place.get("_location") or {}
    website = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
    phone = tags.get("phone") or tags.get("contact:phone") or tags.get("mobile") or tags.get("contact:mobile") or ""
    email = tags.get("email") or tags.get("contact:email") or ""
    web_emails = fetch_website_emails(website, config) if config.fetch_emails_from_websites else []
    emails = sorted(set([item for item in [email] if item] + web_emails))
    osm_url = f"https://www.openstreetmap.org/{place.get('type')}/{place.get('id')}"
    if loc:
        osm_url = f"https://www.openstreetmap.org/?mlat={loc['latitude']}&mlon={loc['longitude']}#map=18/{loc['latitude']}/{loc['longitude']}"
    return {
        "query": place.get("_query", ""),
        "name": tags.get("name") or tags.get("brand") or tags.get("operator") or "(isimsiz kayit)",
        "phone": phone,
        "address": osm_address(tags),
        "website": website,
        "emails": ", ".join(emails),
        "google_maps_url": osm_url,
        "distance_km": place.get("_distanceKm", ""),
        "rating": "",
        "reviews": "",
        "business_status": tags.get("opening_hours", ""),
        "place_id": f"osm:{place.get('type')}:{place.get('id')}",
    }


def clean_gosom_emails(value: str) -> str:
    if not value:
        return ""
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return ", ".join(str(item) for item in decoded if item)
    except json.JSONDecodeError:
        pass
    return value.replace("|", ", ")


def normalize_gosom_row(row: dict[str, str], query: str, center: dict[str, float]) -> dict[str, Any]:
    latitude = row.get("latitude") or row.get("lat") or ""
    longitude = row.get("longitude") or row.get("lng") or ""
    distance = ""
    if latitude and longitude:
        try:
            distance = round(distance_km(center, {"latitude": float(latitude), "longitude": float(longitude)}), 2)
        except ValueError:
            distance = ""
    return {
        "query": row.get("input_id") or query,
        "name": row.get("title") or row.get("name") or "",
        "phone": row.get("phone") or "",
        "address": row.get("address") or row.get("complete_address") or "",
        "website": row.get("website") or "",
        "emails": clean_gosom_emails(row.get("emails") or ""),
        "google_maps_url": row.get("link") or row.get("google_maps_url") or row.get("url") or row.get("place_link") or "",
        "distance_km": distance,
        "rating": row.get("review_rating") or "",
        "reviews": row.get("review_count") or "",
        "business_status": row.get("status") or "",
        "place_id": row.get("place_id") or row.get("cid") or row.get("data_id") or "",
    }


def gosom_command(config: SearchConfig, work_dir: Path, center: dict[str, float], radius_km: float, include_email: bool) -> list[str]:
    input_path = work_dir / "queries.txt"
    output_path = work_dir / "results.csv"
    radius_m = int(radius_km * 1000)
    common_args = [
        "-input",
        str(input_path),
        "-results",
        str(output_path),
        "-depth",
        str(config.gosom_depth),
        "-exit-on-inactivity",
        config.gosom_exit_on_inactivity,
        "-lang",
        config.language_code,
        "-geo",
        f"{center['latitude']},{center['longitude']}",
        "-radius",
        str(radius_m),
        "-c",
        "1",
    ]
    if include_email:
        common_args.append("-email")
    if config.gosom_fast_mode:
        common_args.append("-fast-mode")

    if config.gosom_binary:
        binary_path = Path(config.gosom_binary)
        if not binary_path.is_absolute():
            binary_path = BASE_DIR / binary_path
        return [str(binary_path), *common_args]

    local_binary = shutil.which("google-maps-scraper")
    if local_binary:
        return [local_binary, *common_args]

    if config.gosom_use_docker:
        if not shutil.which("docker"):
            raise AppError("gosom modu icin Docker bulunamadi. Docker Desktop kurun veya config.json icinde gosom_binary yolunu verin.")
        docker_work = "/work"
        docker_args = [
            "-input",
            f"{docker_work}/queries.txt",
            "-results",
            f"{docker_work}/results.csv",
            "-depth",
            str(config.gosom_depth),
            "-exit-on-inactivity",
            config.gosom_exit_on_inactivity,
            "-lang",
            config.language_code,
            "-geo",
            f"{center['latitude']},{center['longitude']}",
            "-radius",
            str(radius_m),
            "-c",
            "1",
        ]
        if include_email:
            docker_args.append("-email")
        if config.gosom_fast_mode:
            docker_args.append("-fast-mode")
        return ["docker", "run", "--rm", "-v", f"{work_dir}:{docker_work}", "gosom/google-maps-scraper", *docker_args]

    raise AppError("gosom modu icin google-maps-scraper binary bulunamadi.")


def run_gosom_search(queries: list[str], center: dict[str, float], radius_km: float, include_email: bool, config: SearchConfig) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="maps_lead_gosom_") as temp_name:
        work_dir = Path(temp_name)
        queries_path = work_dir / "queries.txt"
        results_path = work_dir / "results.csv"
        queries_path.write_text("\n".join(queries) + "\n", encoding="utf-8")
        command = gosom_command(config, work_dir, center, radius_km, include_email)
        try:
            completed = subprocess.run(command, cwd=str(BASE_DIR), capture_output=True, text=True, timeout=max(180, config.overpass_timeout_seconds * 4), check=False)
        except subprocess.TimeoutExpired as exc:
            raise AppError("gosom google-maps-scraper zaman asimina ugradi. Yaricapi/detay derinligini azaltin veya proxy kullanin.") from exc
        except OSError as exc:
            raise AppError(f"gosom google-maps-scraper calistirilamadi: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise AppError(f"gosom google-maps-scraper hata verdi: {detail[:900]}")
        if not results_path.exists():
            return []

        with results_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [normalize_gosom_row(row, row.get("input_id") or "", center) for row in reader]
        return rows


def run_search(payload: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    source = str(payload.get("source") or config.source or "osm").lower()
    if source == "google_scraper":
        source = "gosom"
    if source == "google" and not config.google_api_key:
        raise AppError("Google API anahtari yok. config.json icine google_api_key ekleyin veya GOOGLE_MAPS_API_KEY ortam degiskenini ayarlayin.")

    location = str(payload.get("location") or config.default_location).strip()
    radius_km = float(payload.get("radius_km") or config.default_radius_km)
    queries_raw = payload.get("queries") or config.default_queries
    if isinstance(queries_raw, str):
        queries = [line.strip() for line in queries_raw.replace(",", "\n").splitlines() if line.strip()]
    else:
        queries = [str(item).strip() for item in queries_raw if str(item).strip()]
    if not queries:
        raise AppError("En az bir arama kriteri girilmeli.")

    include_email = bool(payload.get("fetch_emails", config.fetch_emails_from_websites))
    config.fetch_emails_from_websites = include_email
    center = geocode_location(location, config) if source == "google" else osm_geocode_location(location, config)

    if source == "gosom":
        rows = run_gosom_search(queries, center, radius_km, include_email, config)
        rows = sorted(rows, key=lambda row: (row.get("distance_km") == "", row.get("distance_km") or 9999, row.get("name", "")))
        return save_search_result(rows, center, source)

    unique: dict[str, dict[str, Any]] = {}
    for query in queries:
        places = search_places(query, center, radius_km, config) if source == "google" else search_osm_places(query, center, radius_km, config)
        for place in places:
            place_id = place.get("id") or f"osm:{place.get('type')}:{place.get('id')}"
            if place_id not in unique:
                unique[place_id] = normalize_place(place, config) if source == "google" else normalize_osm_place(place, config)
            elif query not in unique[place_id]["query"]:
                unique[place_id]["query"] += f", {query}"

    rows = sorted(unique.values(), key=lambda row: (row.get("distance_km") == "", row.get("distance_km") or 9999, row.get("name", "")))
    return save_search_result(rows, center, source)


def save_search_result(rows: list[dict[str, Any]], center: dict[str, float], source: str) -> dict[str, Any]:
    rows = dedupe_rows(rows)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    OUTPUT_DIR.mkdir(exist_ok=True)
    json_path = OUTPUT_DIR / f"leads-{run_id}.json"
    csv_path = OUTPUT_DIR / f"leads-{run_id}.csv"
    xlsx_path = OUTPUT_DIR / f"leads-{run_id}.xlsx"
    pdf_path = OUTPUT_DIR / f"leads-{run_id}.pdf"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, rows)
    write_xlsx(xlsx_path, rows)
    write_pdf(pdf_path, rows, source)

    return {
        "center": center,
        "source": source,
        "count": len(rows),
        "rows": rows,
        "files": {
            "json": f"/download/{json_path.name}",
            "csv": f"/download/{csv_path.name}",
            "xlsx": f"/download/{xlsx_path.name}",
            "pdf": f"/download/{pdf_path.name}",
        },
    }


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = dedupe_key(row)
        if not key:
            key = f"row:{len(order)}"
        if key not in unique:
            unique[key] = dict(row)
            order.append(key)
        else:
            unique[key] = merge_duplicate_row(unique[key], row)
    return [unique[key] for key in order]


def dedupe_key(row: dict[str, Any]) -> str:
    place_id = clean_key_part(row.get("place_id", ""))
    if place_id:
        return f"id:{place_id}"
    website = normalize_website(row.get("website", ""))
    if website:
        return f"web:{website}"
    phone = clean_key_part(row.get("phone", ""))
    name = clean_key_part(row.get("name", ""))
    address = clean_key_part(row.get("address", ""))
    if phone and name:
        return f"phone:{phone}|{name}"
    if name and address:
        return f"nameaddr:{name}|{address}"
    return name


def clean_key_part(value: Any) -> str:
    text = str(value or "").casefold().strip()
    return re.sub(r"[^a-z0-9]+", "", text)


def normalize_website(value: Any) -> str:
    text = str(value or "").casefold().strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc.removeprefix("www.")
    path = parsed.path.strip("/")
    return f"{host}/{path}".rstrip("/")


def merge_duplicate_row(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for field in EXPORT_FIELDS:
        if not merged.get(field) and incoming.get(field):
            merged[field] = incoming[field]
    merged["query"] = merge_list_text(existing.get("query", ""), incoming.get("query", ""))
    merged["emails"] = merge_list_text(existing.get("emails", ""), incoming.get("emails", ""))
    merged["distance_km"] = closest_distance(existing.get("distance_km", ""), incoming.get("distance_km", ""))
    return merged


def merge_list_text(left: Any, right: Any) -> str:
    values: list[str] = []
    for value in [left, right]:
        for part in str(value or "").split(","):
            item = part.strip()
            if item and item not in values:
                values.append(item)
    return ", ".join(values)


def closest_distance(left: Any, right: Any) -> Any:
    try:
        if left == "":
            return right
        if right == "":
            return left
        return min(float(left), float(right))
    except (TypeError, ValueError):
        return left or right


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_xlsx(path: Path, rows: list[dict[str, Any]]) -> None:
    sheet_rows = [EXPORT_HEADERS]
    sheet_rows.extend([[str(row.get(field, "")) for field in EXPORT_FIELDS] for row in rows])
    worksheet = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetData>',
    ]
    for row_index, values in enumerate(sheet_rows, start=1):
        worksheet.append(f'<row r="{row_index}">')
        for col_index, value in enumerate(values, start=1):
            cell_ref = f"{xlsx_col(col_index)}{row_index}"
            worksheet.append(f'<c r="{cell_ref}" t="inlineStr"><is><t>{xml_escape(value)}</t></is></c>')
        worksheet.append("</row>")
    worksheet.extend(["</sheetData>", "</worksheet>"])

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""")
        archive.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        archive.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Leads" sheetId="1" r:id="rId1"/></sheets>
</workbook>""")
        archive.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""")
        archive.writestr("xl/worksheets/sheet1.xml", "\n".join(worksheet))


def xlsx_col(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def write_pdf(path: Path, rows: list[dict[str, Any]], source: str) -> None:
    build_table_pdf(path, rows, source)


def wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:width]
    if current:
        lines.append(current)
    return lines


def build_table_pdf(path: Path, rows: list[dict[str, Any]], source: str) -> None:
    table_rows = rows[:300]
    page_rows = [table_rows[index : index + 22] for index in range(0, len(table_rows), 22)] or [[]]
    objects: list[bytes] = []
    page_refs = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_index, page in enumerate(page_rows, start=1):
        stream, links = pdf_table_stream(page, page_index, len(page_rows), len(rows), source)
        content_id = len(objects) + 1
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream")
        annots = ""
        if links:
            annot_refs = []
            for link in links:
                annot_id = len(objects) + 1
                annot_refs.append(f"{annot_id} 0 R")
                objects.append(pdf_link_annotation(link))
            annots = f" /Annots [{' '.join(annot_refs)}]"
        page_id = len(objects) + 1
        page_refs.append(f"{page_id} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R{annots} >>".encode("ascii"))
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>".encode("ascii")

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_id, content in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(content)
        output.extend(b"\nendobj\n")
    xref_at = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii"))
    path.write_bytes(bytes(output))


def pdf_table_stream(rows: list[dict[str, Any]], page_index: int, page_count: int, total_count: int, source: str) -> tuple[bytes, list[dict[str, Any]]]:
    parts = [
        "0.96 0.97 0.98 rg",
        "0 806 595 36 re f",
        "0 g",
        "BT /F1 15 Tf 34 820 Td (Maps Lead Finder) Tj ET",
        f"BT /F1 8 Tf 430 820 Td ({pdf_escape(f'{source} | {total_count} kayit | Sayfa {page_index}/{page_count}')}) Tj ET",
    ]
    columns = [
        ("Firma", 34, 125),
        ("Telefon", 159, 62),
        ("Adres", 221, 140),
        ("Web", 361, 56),
        ("E-posta", 417, 86),
        ("Km", 503, 28),
        ("Puan", 531, 30),
    ]
    row_h = 28
    top = 780
    links: list[dict[str, Any]] = []
    parts.extend(["0.12 0.45 0.42 rg", f"34 {top} 527 18 re f", "1 1 1 rg"])
    for title, x, width in columns:
        parts.append(f"BT /F1 7 Tf {x + 3} {top + 6} Td ({pdf_escape(title)}) Tj ET")
    parts.append("0 g")

    y = top - row_h
    for row in rows:
        parts.extend(["0.86 0.88 0.89 RG", f"34 {y} 527 {row_h} re S"])
        values = [
            str(row.get("name", "")),
            str(row.get("phone", "")),
            str(row.get("address", "")),
            "Site" if row.get("website") else "",
            str(row.get("emails", "")),
            str(row.get("distance_km", "")),
            str(row.get("rating", "")),
        ]
        for value, (_, x, width) in zip(values, columns):
            text = trim_for_pdf(value, max(4, int(width / 4.2)))
            parts.append(f"BT /F1 6.5 Tf {x + 3} {y + 17} Td ({pdf_escape(text)}) Tj ET")
        if row.get("website"):
            links.append({"url": str(row["website"]), "rect": [364, y + 13, 412, y + 24]})
            parts.extend(["0 0.32 0.75 rg", f"BT /F1 6.5 Tf 364 {y + 17} Td (Site) Tj ET", "0 g"])
        if row.get("google_maps_url"):
            links.append({"url": str(row["google_maps_url"]), "rect": [37, y + 3, 100, y + 13]})
            parts.extend(["0 0.32 0.75 rg", f"BT /F1 5.5 Tf 37 {y + 5} Td (Haritada ac) Tj ET", "0 g"])
        y -= row_h

    if total_count > 300 and page_index == page_count:
        parts.append(f"BT /F1 7 Tf 34 34 Td ({pdf_escape('PDF ilk 300 kaydi icerir. Tum kayitlar icin Excel dosyasini kullanin.')}) Tj ET")
    return "\n".join(parts).encode("latin-1", errors="replace"), links


def trim_for_pdf(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 3)] + "..."


def pdf_link_annotation(link: dict[str, Any]) -> bytes:
    x1, y1, x2, y2 = link["rect"]
    url = pdf_escape_url(link["url"])
    return f"<< /Type /Annot /Subtype /Link /Rect [{x1} {y1} {x2} {y2}] /Border [0 0 0] /A << /S /URI /URI ({url}) >> >>".encode("latin-1", errors="replace")


def pdf_escape_url(text: str) -> str:
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_escape(text: str) -> str:
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadFinderHTTP/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_static("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/config":
            config = load_config()
            self.send_json(
                {
                    "location": config.default_location,
                    "radius_km": config.default_radius_km,
                    "queries": "\n".join(config.default_queries),
                    "fetch_emails": config.fetch_emails_from_websites,
                    "source": config.source,
                    "has_api_key": bool(config.google_api_key),
                }
            )
        elif parsed.path.startswith("/download/"):
            name = Path(parsed.path).name
            path = OUTPUT_DIR / name
            if not path.exists():
                self.send_error(404, "Dosya bulunamadi")
                return
            content_type = download_content_type(path)
            inline = path.suffix == ".pdf"
            self.send_bytes(path.read_bytes(), content_type, attachment=name, inline=inline)
        elif parsed.path.startswith("/static/"):
            name = Path(parsed.path).name
            content_type = "text/css; charset=utf-8" if name.endswith(".css") else "application/javascript; charset=utf-8"
            self.send_static(name, content_type)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/search":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body)
            result = run_search(payload)
            self.send_json(result)
        except AppError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.send_json({"error": f"Beklenmeyen hata: {exc}"}, status=500)

    def send_static(self, name: str, content_type: str) -> None:
        path = STATIC_DIR / name
        if not path.exists():
            self.send_error(404)
            return
        self.send_bytes(path.read_bytes(), content_type)

    def send_json(self, data: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, attachment: str | None = None, inline: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if attachment:
            disposition = "inline" if inline else "attachment"
            self.send_header("Content-Disposition", f'{disposition}; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def download_content_type(path: Path) -> str:
    if path.suffix == ".csv":
        return "text/csv; charset=utf-8"
    if path.suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if path.suffix == ".pdf":
        return "application/pdf"
    return "application/json; charset=utf-8"


def main() -> None:
    port = int(os.getenv("PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Lead Finder running: http://127.0.0.1:{port}")
    print(f"Config file: {CONFIG_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
