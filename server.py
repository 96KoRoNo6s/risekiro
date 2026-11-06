from __future__ import annotations

import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_CEILING
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse


SITE_DIR = Path(__file__).resolve().parent
WORK_DIR = SITE_DIR.parents[1]
VENDOR_DIR = WORK_DIR / "market_probe" / "vendor"
CARDINAL_DIR = Path(r"D:\кардинал))\FunPayCardinal")
TOKEN_DIR = CARDINAL_DIR / "configs" / "market_tokens"
ICONS_DIR = CARDINAL_DIR / "plugins" / "nft_fulfillment_state" / "charts" / "attributes" / "icons"
PORT = 8766
SELL_MARGIN_RATE = Decimal("0.075")
FUNPAY_FEE_RATE = Decimal("0.15071")
PRICE_STEP_RUB = Decimal("5")
MIN_TON_RUB_RATE = Decimal("99")
TRANSFER_STARS_COUNT = 25
STAR_PRICE_RUB = Decimal("2.0")
TON_RUB_CACHE_SECONDS = 300
_ton_rub_lock = threading.Lock()
_ton_rub_cache: tuple[float, float] | None = None

sys.path.insert(0, str(VENDOR_DIR))
from curl_cffi import requests  # noqa: E402
from tonnelmp import getGifts  # noqa: E402


MARKETS = {
    "portals": {
        "name": "Portals",
        "repo": "https://github.com/bleach-hub/portalsmp",
        "site": "https://portal-market.com/",
    },
    "tonnel": {
        "name": "Tonnel",
        "repo": "https://github.com/bleach-hub/tonnelmp",
        "site": "https://market.tonnel.network/",
    },
    "mrkt": {
        "name": "MRKT",
        "repo": "https://github.com/boostNT/MRKT-API",
        "site": "https://t.me/mrkt",
    },
}

# Активные маркетплейсы для подбора (Portals + MRKT):
ACTIVE_MARKETS = ["portals", "mrkt"]


class MarketFailure(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _ton_rub_rate() -> float:
    global _ton_rub_cache
    now = time.monotonic()
    if _ton_rub_cache and now < _ton_rub_cache[0]:
        return _ton_rub_cache[1]
    with _ton_rub_lock:
        now = time.monotonic()
        if _ton_rub_cache and now < _ton_rub_cache[0]:
            return _ton_rub_cache[1]
        try:
            response = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "the-open-network", "vs_currencies": "usd"},
                headers={"Accept": "application/json"},
                impersonate="chrome124",
                timeout=12,
            )
            if response.status_code == 200:
                ton_usd = float(response.json()["the-open-network"]["usd"])
                if math.isfinite(ton_usd) and ton_usd > 0:
                    # Базовый курс закупки 1 TON в рублях (не менее 99 ₽):
                    rate = float(max(MIN_TON_RUB_RATE, Decimal(str(ton_usd)) * Decimal("84.327")))
                    _ton_rub_cache = (time.monotonic() + TON_RUB_CACHE_SECONDS, rate)
                    return rate
        except Exception:
            pass
        if _ton_rub_cache:
            return _ton_rub_cache[1]
        default_rate = float(MIN_TON_RUB_RATE)
        _ton_rub_cache = (time.monotonic() + TON_RUB_CACHE_SECONDS, default_rate)
        return default_rate


def _pricing_breakdown(price_ton, ton_rub_rate: float) -> dict | None:
    try:
        ton_val = Decimal(str(price_ton))
        if not ton_val.is_finite() or ton_val <= 0:
            return None
        rate_val = Decimal(str(ton_rub_rate))
        base_ton_rub = ton_val * rate_val
        stars_rub = Decimal(TRANSFER_STARS_COUNT) * STAR_PRICE_RUB
        # Себестоимость: покупка подарка в TON (по курсу 99 ₽) + 25 звёзд Telegram на отправку с аккаунта
        cost = base_ton_rub + stars_rub
        # Сумма продавца на FunPay с учётом чистой маржи (7.5%)
        net = (cost * (Decimal(1) + SELL_MARGIN_RATE)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        # Итоговая цена для покупателя с учётом комиссии СБП (+15.071%), округление вверх до 5 ₽
        adjusted = net * (Decimal(1) + FUNPAY_FEE_RATE)
        buyer_rub = int((adjusted / PRICE_STEP_RUB).to_integral_value(rounding=ROUND_CEILING) * PRICE_STEP_RUB)
        return {
            "price_ton": float(ton_val),
            "ton_rub_rate": float(rate_val),
            "transfer_stars": TRANSFER_STARS_COUNT,
            "transfer_stars_rub": float(stars_rub),
            "cost_rub": float(cost.quantize(Decimal("0.01"), rounding=ROUND_CEILING)),
            "seller_price_rub": float(net),
            "price_rub": buyer_rub,
        }
    except Exception:
        return None


def _funpay_price_rub(price_ton, ton_rub_rate: float) -> int | None:
    info = _pricing_breakdown(price_ton, ton_rub_rate)
    return info["price_rub"] if info else None


def _token(name: str) -> str:
    try:
        return (TOKEN_DIR / f"{name}.txt").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _get_json(method: str, url: str, *, headers: dict | None = None, impersonate: str = "chrome", **kwargs):
    try:
        response = requests.request(
            method,
            url,
            headers=headers or {},
            impersonate=impersonate,
            timeout=22,
            allow_redirects=False,
            **kwargs,
        )
    except Exception as exc:
        raise MarketFailure(f"Сетевая ошибка: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise MarketFailure(f"HTTP {response.status_code}", response.status_code)
    try:
        return response.json()
    except ValueError:
        raise MarketFailure("Маркет вернул ответ не в JSON") from None


def _fold(value) -> str:
    return str(value or "").strip().casefold()


def _attributes(raw: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.get("attributes", []) if isinstance(raw.get("attributes"), list) else []:
        if not isinstance(item, dict):
            continue
        key = _fold(item.get("type"))
        if key:
            result[key] = str(item.get("value", ""))
    attrs = raw.get("attributes")
    if isinstance(attrs, dict):
        for key, value in attrs.items():
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or ""
            result.setdefault(_fold(key), str(value))
    return result


def _slug(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    return "".join(word[:1].upper() + word[1:] for word in words)


def _safe_image_url(value) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    allowed = {"cdn.tgmrkt.io", "portal-market.com", "gifts2.tonnel.network", "market.tonnel.network"}
    return candidate if parsed.scheme == "https" and parsed.hostname in allowed and not parsed.username and not parsed.password else ""


def _mrkt_asset_url(value) -> str:
    candidate = str(value or "").strip()
    if candidate.startswith("https://"):
        return _safe_image_url(candidate)
    path = candidate.lstrip("/")
    if not path or ".." in path or "\\" in path:
        return ""
    return "https://cdn.tgmrkt.io/" + path


def _hex_color(value) -> str:
    try:
        return "#%06x" % (int(value) & 0xFFFFFF)
    except (TypeError, ValueError):
        return ""


def _tg_link(collection: str, number) -> str | None:
    if number is None or not str(number).isdigit():
        return None
    slug = _slug(collection)
    return f"https://t.me/nft/{slug}-{number}" if slug else None


def _float_price(value):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _clean_trait(value) -> str:
    return re.sub(r"\s*\([^)]*%\)\s*$", "", str(value or "")).strip()


def _load_static_backdrops() -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}
    try:
        rows = json.loads((WORK_DIR / "xmas-backdrops.json").read_text(encoding="utf-8"))
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("name") or "").strip()
            hex_info = row.get("hex") or {}
            center = str(hex_info.get("centerColor") or _hex_color(row.get("centerColor")) or "")
            edge = str(hex_info.get("edgeColor") or _hex_color(row.get("edgeColor")) or "")
            if name and center and edge:
                result[name.casefold()] = (name, center, edge)
    except Exception:
        pass
    return result


def _load_static_symbols() -> list[str]:
    try:
        rows = json.loads((WORK_DIR / "xmas-symbols.json").read_text(encoding="utf-8"))
        return sorted({str(r.get("name") or "").strip() for r in rows if isinstance(r, dict) and r.get("name")}, key=str.casefold)
    except Exception:
        return []


_BACKDROPS_BY_KEY = _load_static_backdrops()
_DEFAULT_SYMBOLS = _load_static_symbols()


def _model_sticker_url(collection: str, model: str) -> str:
    c = str(collection or "").strip()
    m = _clean_trait(model)
    if not c or not m:
        return ""
    key = f"{c}_{m}".encode("utf-8").hex()
    return f"https://cdn.tgmrkt.io/gifts/stickers/thumbnails/{key}.webp"


def _symbol_icon_url(collection: str, symbol: str) -> str:
    s = _clean_trait(symbol)
    if not s:
        return ""
    c = str(collection or "").strip()
    return f"/api/symbol-icon?name={quote(s)}&collection={quote(c)}"


def _gift_slug(collection: str, number, tg_id: str = "") -> str:
    candidate = str(tg_id or "").strip()
    if re.fullmatch(r"[A-Za-z0-9]+-\d+", candidate):
        return candidate.lower()
    if number is not None and str(number).isdigit():
        slug = _slug(collection).lower()
        if slug:
            return f"{slug}-{number}"
    return ""


def _normalize_item(market_id: str, raw: dict, collection: str) -> dict:
    attrs = _attributes(raw)
    model_image = ""
    symbol_image = ""
    backdrop_center = ""
    backdrop_edge = ""
    tg_id = ""
    if market_id == "portals":
        gift_name = str(raw.get("name") or collection)
        tg_id = str(raw.get("tg_id") or "")
        match = re.search(r"-(\d+)$", tg_id)
        number = match.group(1) if match else raw.get("external_collection_number")
        name = tg_id or (f"{_slug(gift_name)}-{number}" if number is not None else gift_name)
        url = f"https://t.me/nft/{tg_id}" if re.fullmatch(r"[A-Za-z0-9]+-\d+", tg_id) else _tg_link(gift_name, number)
        photo = _safe_image_url(raw.get("photo_url"))
        price = raw.get("price")
        asset = "TON"
        item_id = raw.get("id")
        model = _clean_trait(attrs.get("model", ""))
        backdrop = _clean_trait(attrs.get("backdrop", ""))
        symbol = _clean_trait(attrs.get("symbol", ""))
    elif market_id == "tonnel":
        gift_name = str(raw.get("name") or raw.get("gift_name") or collection)
        number = raw.get("gift_num") or raw.get("number")
        name = str(raw.get("gift_num") and f"{_slug(gift_name)}-{raw.get('gift_num')}" or gift_name)
        url = _tg_link(gift_name, number)
        photo = _safe_image_url(raw.get("photo_url") or raw.get("image_url") or raw.get("preview"))
        price = raw.get("price")
        asset = str(raw.get("asset") or "TON").upper()
        item_id = raw.get("gift_id") or raw.get("id")
        model = _clean_trait(raw.get("model") or attrs.get("model", ""))
        backdrop = _clean_trait(raw.get("backdrop") or attrs.get("backdrop", ""))
        symbol = _clean_trait(raw.get("symbol") or raw.get("pattern") or attrs.get("symbol", ""))
    else:
        gift_name = str(raw.get("collectionName") or raw.get("giftName") or raw.get("name") or collection)
        number = raw.get("giftNum") or raw.get("number") or raw.get("gift_num")
        tg_id = str(raw.get("tg_id") or "")
        name = str(tg_id or raw.get("giftNameWithNumber") or (f"{_slug(gift_name)}-{number}" if number else gift_name))
        url = _tg_link(gift_name, number)
        photo = _safe_image_url(raw.get("photoUrl") or raw.get("imageUrl") or raw.get("image"))
        model_image = _mrkt_asset_url(raw.get("modelStickerThumbnailKey") or raw.get("modelStickerKey"))
        symbol_image = _mrkt_asset_url(raw.get("symbolStickerThumbnailKey") or raw.get("symbolStickerKey"))
        backdrop_center = _hex_color(raw.get("backdropColorsCenterColor"))
        backdrop_edge = _hex_color(raw.get("backdropColorsEdgeColor"))
        raw_price = raw.get("price")
        if raw_price is None:
            raw_price = raw.get("salePrice")
        if raw_price is not None and "salePrice" in raw:
            try:
                from decimal import Decimal
                price = format(Decimal(str(raw_price)) / Decimal("1000000000"), "f")
            except Exception:
                price = str(raw_price)
        else:
            price = raw_price
        asset = str(raw.get("asset") or raw.get("currency") or "TON").upper()
        item_id = raw.get("id") or raw.get("giftId")
        model = _clean_trait(raw.get("model") or raw.get("modelName") or attrs.get("model", ""))
        backdrop = _clean_trait(raw.get("backdrop") or raw.get("backdropName") or attrs.get("backdrop", ""))
        symbol = _clean_trait(raw.get("symbol") or raw.get("symbolName") or attrs.get("symbol", ""))

    if not model_image and gift_name and model:
        model_image = _model_sticker_url(gift_name, model)
    if symbol:
        symbol_image = _symbol_icon_url(gift_name, symbol)
    if backdrop and (not backdrop_center or not backdrop_edge):
        bd = _BACKDROPS_BY_KEY.get(backdrop.casefold())
        if bd:
            backdrop_center = bd[1]
            backdrop_edge = bd[2]

    slug = _gift_slug(gift_name, number, tg_id)
    poster_url = f"/api/gift-poster?slug={slug}" if slug else ""
    animation_url = f"/api/gift-animation?slug={slug}" if slug else ""

    return {
        "market": market_id,
        "market_name": MARKETS[market_id]["name"],
        "listing_id": str(item_id or name),
        "name": name,
        "slug": slug,
        "collection": gift_name,
        "number": str(number or ""),
        "price": str(price or ""),
        "price_value": _float_price(price),
        "asset": asset,
        "model": model,
        "backdrop": backdrop,
        "symbol": symbol,
        "photo": str(photo or poster_url or model_image or ""),
        "poster_url": poster_url,
        "animation_url": animation_url,
        "model_image": model_image,
        "symbol_image": symbol_image,
        "backdrop_center": backdrop_center,
        "backdrop_edge": backdrop_edge,
        "gift_url": url,
        "market_url": MARKETS[market_id]["site"],
    }


def _portals_collection_rows() -> list[dict]:
    token = _token("portals")
    if not token:
        raise MarketFailure("Portals unavailable")
    data = _get_json(
        "GET",
        "https://portal-market.com/api/collections",
        headers={"Authorization": token},
        params={"limit": 500},
    )
    raw_rows = data.get("collections", []) if isinstance(data, dict) else []
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        rows.append({
            "name": str(raw.get("name")),
            "floor_ton": _float_price(raw.get("floor_price")),
            "image": _safe_image_url(raw.get("photo_url")),
            "listed_count": raw.get("listed_count"),
        })
    return rows


def _mrkt_collection_rows() -> list[dict]:
    token = _token("mrkt")
    if not token:
        raise MarketFailure("MRKT unavailable")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Origin": "https://cdn.tgmrkt.io",
        "Pragma": "no-cache",
        "Referer": "https://cdn.tgmrkt.io/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Authorization": token,
        "Cookie": f"access_token={token}",
    }
    data = _get_json(
        "GET",
        "https://api.tgmrkt.io/api/v1/gifts/collections",
        headers=headers,
        impersonate="chrome124",
    )
    if not isinstance(data, list):
        raise MarketFailure("MRKT unavailable")
    rows = []
    for raw in data:
        if not isinstance(raw, dict) or not raw.get("name") or raw.get("isHidden"):
            continue
        image_key = raw.get("thumbnailImageKey") or raw.get("originalImageKey")
        rows.append({
            "name": str(raw.get("title") or raw.get("name")),
            "floor_ton": _float_price(raw.get("floorPriceNanoTons") / 1_000_000_000) if raw.get("floorPriceNanoTons") is not None else None,
            "image": f"https://cdn.tgmrkt.io/{image_key}" if image_key else "",
            "listed_count": raw.get("listedCount"),
        })
    return rows


def _tonnel_collection_rows() -> list[dict]:
    data = getGifts(
        gift_name=None,
        page=1,
        limit=30,
        sort="price_asc",
        asset="TON",
        authData=_token("tonnel"),
    )
    rows = []
    seen = set()
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("gift_name") or raw.get("name") or "").strip()
        key = _fold(name)
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append({
            "name": name,
            "floor_ton": _float_price(raw.get("price")),
            "image": _safe_image_url(raw.get("photo_url") or raw.get("image_url") or raw.get("preview")),
            "listed_count": None,
        })
    return rows


_collection_cache_lock = threading.Lock()
_collection_cache: tuple[float, list[dict]] | None = None


def _collection_catalog() -> list[dict]:
    global _collection_cache
    now = time.monotonic()
    if _collection_cache and now < _collection_cache[0]:
        return _collection_cache[1]
    with _collection_cache_lock:
        now = time.monotonic()
        if _collection_cache and now < _collection_cache[0]:
            return _collection_cache[1]
        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=max(1, len(ACTIVE_MARKETS))) as pool:
            futures = []
            if "portals" in ACTIVE_MARKETS:
                futures.append(pool.submit(_portals_collection_rows))
            if "mrkt" in ACTIVE_MARKETS:
                futures.append(pool.submit(_mrkt_collection_rows))
            if "tonnel" in ACTIVE_MARKETS:
                futures.append(pool.submit(_tonnel_collection_rows))
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception:
                    continue
        merged: dict[str, dict] = {}
        for row in rows:
            key = _fold(row.get("name"))
            if not key:
                continue
            current = merged.get(key)
            floor = row.get("floor_ton")
            if current is None:
                merged[key] = {**row}
                continue
            current_floor = current.get("floor_ton")
            if floor is not None and (current_floor is None or floor < current_floor):
                current["floor_ton"] = floor
            if not current.get("image") and row.get("image"):
                current["image"] = row["image"]
            count = row.get("listed_count")
            if count is not None:
                try:
                    current["listed_count"] = max(int(current.get("listed_count") or 0), int(count))
                except (TypeError, ValueError):
                    pass
        if not merged:
            raise MarketFailure("Не удалось загрузить каталог коллекций")
        try:
            rate = _ton_rub_rate()
        except MarketFailure:
            rate = None
        catalog = []
        for row in merged.values():
            floor_rub = _funpay_price_rub(row.get("floor_ton"), rate) if rate and row.get("floor_ton") is not None else None
            catalog.append({
                "name": row["name"],
                "image": row.get("image") or "",
                "floor_price_rub": floor_rub,
                "listed_count": row.get("listed_count"),
            })
        catalog.sort(key=lambda item: item["name"].casefold())
        _collection_cache = (time.monotonic() + 600, catalog)
        return catalog


def _portals_search(collection: str, model: str, backdrop: str, symbol: str, offset: int, number: str = "") -> dict:
    token = _token("portals")
    if not token:
        raise MarketFailure("Локальный токен Portals не найден")
    base = "https://portal-market.com/api"
    headers = {"Authorization": token}
    params = {
        "offset": offset,
        "limit": 50,
        "sort_by": "price asc",
        "status": "listed",
        "exclude_bundled": "true",
        "premarket_status": "without_premarket",
    }
    if collection:
        found = _get_json("GET", base + "/collections", headers=headers, params={"search": collection, "limit": 100})
        collections = found.get("collections", []) if isinstance(found, dict) else []
        matches = [x for x in collections if _fold(x.get("name")) == _fold(collection)]
        if not matches:
            matches = [x for x in collections if _fold(collection) in _fold(x.get("name"))]
        if not matches:
            raise MarketFailure("Такой коллекции не нашёл Portals")
        if len(matches) > 1:
            exact_short = [x for x in matches if _fold(x.get("short_name")) == _fold(collection)]
            if exact_short:
                matches = exact_short
            else:
                raise MarketFailure("Найдено несколько коллекций; уточните название")
        params["collection_ids"] = matches[0]["id"]
    if model:
        params["filter_by_models"] = _clean_trait(model)
    if backdrop:
        params["filter_by_backdrops"] = _clean_trait(backdrop)
    if number and str(number).isdigit():
        params["external_collection_number"] = int(number)
    data = _get_json("GET", base + "/nfts/search", headers=headers, params=params)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise MarketFailure("Неожиданный формат каталога Portals")
    items = []
    for raw in data["results"]:
        attrs = _attributes(raw)
        if _fold(raw.get("status")) != "listed" or raw.get("bundle_id"):
            continue
        if model and _fold(_clean_trait(attrs.get("model"))) != _fold(_clean_trait(model)):
            continue
        if backdrop and _fold(_clean_trait(attrs.get("backdrop"))) != _fold(_clean_trait(backdrop)):
            continue
        if symbol and _fold(_clean_trait(attrs.get("symbol"))) != _fold(_clean_trait(symbol)):
            continue
        norm = _normalize_item("portals", raw, collection)
        if number and str(number).isdigit() and str(norm.get("number") or "") != str(int(number)):
            continue
        items.append(norm)
    return {
        "status": "ok",
        "message": "Есть предложения" if items else "По такому набору фильтров предложений нет",
        "count": len(items),
        "items": items,
        "next": offset + 50 if len(data["results"]) == 50 else None,
    }


def _tonnel_search(collection: str, model: str, backdrop: str, symbol: str, page: int, number: str = "") -> dict:
    token = _token("tonnel")
    try:
        data = getGifts(
            gift_name=collection or None,
            model=model or None,
            backdrop=backdrop or None,
            symbol=symbol or None,
            gift_num=int(number) if (number and str(number).isdigit()) else None,
            page=page,
            limit=30,
            sort="price_asc",
            asset="TON",
            authData=token,
        )
    except Exception as exc:
        message = str(exc)
        if "403" in message:
            raise MarketFailure("HTTP 403 — защита Tonnel отклонила запрос", 403) from None
        if "429" in message:
            raise MarketFailure("HTTP 429 — лимит запросов Tonnel", 429) from None
        if "401" in message:
            raise MarketFailure("HTTP 401 — Tonnel отклонил авторизацию", 401) from None
        raise MarketFailure(f"Ошибка запроса: {type(exc).__name__}") from None
    if not isinstance(data, list):
        raise MarketFailure("Неожиданный формат каталога Tonnel")
    items = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        norm = _normalize_item("tonnel", raw, collection)
        if number and str(number).isdigit() and str(norm.get("number") or "") != str(int(number)):
            continue
        items.append(norm)
    return {
        "status": "ok",
        "message": "Есть предложения" if items else "По такому набору фильтров предложений нет",
        "count": len(items),
        "items": items,
        "next": page + 1 if len(data) == 30 else None,
    }


def _mrkt_search(collection: str, model: str, backdrop: str, symbol: str, cursor: str, number: str = "") -> dict:
    token = _token("mrkt")
    if not token:
        raise MarketFailure("Локальный токен MRKT не найден")
    num_val = int(number) if (number and str(number).isdigit()) else None
    body = {
        "collectionNames": [collection] if collection else [],
        "modelNames": [model] if model else [],
        "backdropNames": [backdrop] if backdrop else [],
        "symbolNames": [symbol] if symbol else [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": num_val,
        "count": 20,
        "cursor": cursor,
        "query": None,
        "promotedFirst": False,
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Host": "api.tgmrkt.io",
        "Origin": "https://cdn.tgmrkt.io",
        "Pragma": "no-cache",
        "Priority": "u=3, i",
        "Referer": "https://cdn.tgmrkt.io/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Authorization": token,
        "Cookie": f"access_token={token}",
    }
    data = _get_json(
        "POST",
        "https://api.tgmrkt.io/api/v1/gifts/saling",
        headers=headers,
        impersonate="chrome124",
        json=body,
    )
    if not isinstance(data, dict) or not isinstance(data.get("gifts"), list):
        raise MarketFailure("Неожиданный формат каталога MRKT")
    items = []
    for raw in data["gifts"]:
        if not (isinstance(raw, dict) and raw.get("isOnSale", True) and not raw.get("isLockedForSale", False)):
            continue
        norm = _normalize_item("mrkt", raw, collection)
        if num_val is not None and str(norm.get("number") or "") != str(num_val):
            continue
        items.append(norm)
    return {
        "status": "ok",
        "message": "Есть предложения" if items else "По такому набору фильтров предложений нет",
        "count": len(items),
        "items": items,
        "next": data.get("cursor") or None,
    }


def _market_result(market_id: str, collection: str, model: str, backdrop: str, symbol: str, paging: dict, number: str = "") -> dict:
    meta = MARKETS[market_id]
    try:
        if market_id == "portals":
            result = _portals_search(collection, model, backdrop, symbol, paging["portals_offset"], number=number)
        elif market_id == "tonnel":
            result = _tonnel_search(collection, model, backdrop, symbol, paging["tonnel_page"], number=number)
        else:
            result = _mrkt_search(collection, model, backdrop, symbol, paging["mrkt_cursor"], number=number)
        result.update({"id": market_id, "name": meta["name"], "repo": meta["repo"], "site": meta["site"]})
        return result
    except MarketFailure as exc:
        return {
            "id": market_id,
            "name": meta["name"],
            "repo": meta["repo"],
            "site": meta["site"],
            "status": "unavailable",
            "message": str(exc),
            "http_status": exc.status_code,
            "count": 0,
            "items": [],
            "next": None,
        }
    except Exception as exc:
        return {
            "id": market_id,
            "name": meta["name"],
            "repo": meta["repo"],
            "site": meta["site"],
            "status": "unavailable",
            "message": f"Ошибка запроса: {type(exc).__name__}",
            "count": 0,
            "items": [],
            "next": None,
        }


_media_cache_lock = threading.Lock()
_media_cache: dict[str, bytes] = {}


def _fetch_gift_media(slug: str, ext: str, accept: str) -> bytes | None:
    if not slug or len(slug) > 160 or not re.fullmatch(r"[a-z0-9-]+", slug):
        return None
    cache_key = f"{slug}.{ext}"
    with _media_cache_lock:
        if cache_key in _media_cache:
            return _media_cache[cache_key]
    url = f"https://nft.fragment.com/gift/{slug}.{ext}"
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": accept,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
            impersonate="chrome124",
            timeout=15,
        )
        if resp.status_code != 200 or not resp.content:
            return None
        with _media_cache_lock:
            if len(_media_cache) > 500:
                _media_cache.pop(next(iter(_media_cache)), None)
            _media_cache[cache_key] = resp.content
        return resp.content
    except Exception:
        return None


def _fetch_symbol_icon(name: str, collection: str) -> bytes | None:
    clean = _clean_trait(name)
    if not clean or len(clean) > 120:
        return None
    cache_key = f"symbol:{clean.casefold()}"
    with _media_cache_lock:
        if cache_key in _media_cache:
            return _media_cache[cache_key]
    icon_file = ICONS_DIR / f"{clean.encode('utf-8').hex()}.png"
    try:
        if icon_file.is_file():
            data = icon_file.read_bytes()
            if data:
                with _media_cache_lock:
                    _media_cache[cache_key] = data
                return data
    except OSError:
        pass
    col = str(collection or "").strip() or "Xmas Stocking"
    url = f"https://api.changes.tg/symbol/{quote(col)}/{quote(clean)}.png?size=128"
    try:
        resp = requests.get(
            url,
            headers={"Accept": "image/png,image/*;q=0.8"},
            impersonate="chrome124",
            timeout=12,
        )
        if resp.status_code == 200 and resp.content:
            with _media_cache_lock:
                if len(_media_cache) > 500:
                    _media_cache.pop(next(iter(_media_cache)), None)
                _media_cache[cache_key] = resp.content
            return resp.content
    except Exception:
        pass
    return None


_attributes_cache_lock = threading.Lock()
_attributes_cache: dict[str, tuple[float, dict]] = {}


def _collection_attributes(collection: str) -> dict:
    col = str(collection or "").strip()
    key = col.casefold()
    now = time.monotonic()
    with _attributes_cache_lock:
        cached = _attributes_cache.get(key)
        if cached and now < cached[0]:
            return cached[1]

    backdrops = [
        {"name": name, "center": center, "edge": edge}
        for _, (name, center, edge) in sorted(_BACKDROPS_BY_KEY.items(), key=lambda kv: kv[1][0].casefold())
    ]

    model_names: list[str] = []
    symbol_names: list[str] = []
    if col:
        try:
            resp = requests.get(
                f"https://api.changes.tg/models/{quote(col)}",
                headers={"Accept": "application/json"},
                impersonate="chrome124",
                timeout=10,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    seen_m = set()
                    for row in rows:
                        if isinstance(row, dict):
                            m = _clean_trait(row.get("name"))
                            if m and m.casefold() not in seen_m:
                                seen_m.add(m.casefold())
                                model_names.append(m)
        except Exception:
            pass
        try:
            resp = requests.get(
                f"https://api.changes.tg/symbols/{quote(col)}",
                headers={"Accept": "application/json"},
                impersonate="chrome124",
                timeout=10,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    seen_s = set()
                    for row in rows:
                        if isinstance(row, dict):
                            s = _clean_trait(row.get("name"))
                            if s and s.casefold() not in seen_s:
                                seen_s.add(s.casefold())
                                symbol_names.append(s)
        except Exception:
            pass

    if not symbol_names:
        symbol_names = list(_DEFAULT_SYMBOLS)

    model_names.sort(key=str.casefold)
    symbol_names.sort(key=str.casefold)

    payload = {
        "collection": col,
        "models": [{"name": m, "image": _model_sticker_url(col, m)} for m in model_names],
        "backdrops": backdrops,
        "symbols": [{"name": s, "image": _symbol_icon_url(col, s)} for s in symbol_names],
    }
    with _attributes_cache_lock:
        _attributes_cache[key] = (time.monotonic() + 1800, payload)
    return payload


def _load_static_collections() -> list[str]:
    try:
        rows = json.loads((WORK_DIR / "nft-collections.json").read_text(encoding="utf-8"))
        if isinstance(rows, list):
            return [str(r.get("name") or r) for r in rows if (isinstance(r, dict) and r.get("name")) or isinstance(r, str)]
    except Exception:
        pass
    return []


_STATIC_COLLECTIONS = _load_static_collections()


def _resolve_collection_by_slug(slug_prefix: str) -> str:
    target = re.sub(r"[^a-z0-9]", "", str(slug_prefix or "").lower())
    if not target:
        return ""
    names: list[str] = []
    try:
        names.extend(row["name"] for row in _collection_catalog() if row.get("name"))
    except Exception:
        pass
    names.extend(_STATIC_COLLECTIONS)
    for name in names:
        if _slug(name).lower() == target:
            return name
    return ""


def _parse_gift_reference(raw_text: str, fallback_collection: str = "") -> tuple[str, str]:
    text = str(raw_text or "").strip()
    if not text:
        return fallback_collection, ""
    m = re.search(r"([A-Za-z][A-Za-z0-9]*)-(\d{1,10})", text)
    if m:
        col = _resolve_collection_by_slug(m.group(1)) or fallback_collection
        return col, str(int(m.group(2)))
    m_num = re.fullmatch(r"#?\s*(\d{1,10})", text)
    if m_num:
        return fallback_collection, str(int(m_num.group(1)))
    return fallback_collection, ""


def _cardinal_quote(raw_query: str, fallback_collection: str = "") -> dict:
    collection, number = _parse_gift_reference(raw_query, fallback_collection)
    if not number:
        return {
            "ok": False,
            "found": False,
            "error": "Не удалось распознать код или номер подарка (ожидается формат GiftName-12345).",
        }
    if not collection:
        return {
            "ok": False,
            "found": False,
            "number": number,
            "error": "Не удалось определить коллекцию подарка по коду.",
        }
    paging = {"portals_offset": 0, "tonnel_page": 1, "mrkt_cursor": ""}
    selected = ACTIVE_MARKETS
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        futures = {
            pool.submit(_market_result, market_id, collection, "", "", "", paging, number): market_id
            for market_id in selected
        }
        for future in as_completed(futures):
            market_id = futures[future]
            results[market_id] = future.result()

    try:
        ton_rub_rate = _ton_rub_rate()
    except MarketFailure:
        ton_rub_rate = float(MIN_TON_RUB_RATE)

    offers = []
    matched_items = []
    for market_id in selected:
        for item in results.get(market_id, {}).get("items", []):
            if str(item.get("number") or "") != str(int(number)):
                continue
            pricing = _pricing_breakdown(item.get("price_value"), ton_rub_rate)
            if not pricing:
                continue
            matched_items.append((pricing["price_ton"], item, pricing))
            offers.append({
                "market": item.get("market"),
                "market_name": item.get("market_name"),
                "listing_id": item.get("listing_id"),
                "price_ton": pricing["price_ton"],
                "asset": item.get("asset") or "TON",
                "cost_rub": pricing["cost_rub"],
                "seller_price_rub": pricing["seller_price_rub"],
                "price_rub": pricing["price_rub"],
            })

    if not matched_items:
        canonical = f"{_slug(collection)}-{number}"
        return {
            "ok": True,
            "found": False,
            "name": canonical,
            "slug": canonical.lower(),
            "collection": collection,
            "number": number,
            "error": "Подарок сейчас не выставлен на продажу или уже куплен.",
            "verified_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    matched_items.sort(key=lambda row: row[0])
    offers.sort(key=lambda row: row["price_ton"])
    _, best_item, best_pricing = matched_items[0]
    canonical_name = best_item.get("name") or f"{_slug(collection)}-{number}"
    return {
        "ok": True,
        "found": True,
        "name": canonical_name,
        "slug": best_item.get("slug") or canonical_name.lower(),
        "collection": best_item.get("collection") or collection,
        "number": str(number),
        "model": best_item.get("model") or "",
        "backdrop": best_item.get("backdrop") or "",
        "symbol": best_item.get("symbol") or "",
        "gift_url": best_item.get("gift_url") or _tg_link(collection, number),
        "poster_url": best_item.get("poster_url") or "",
        "animation_url": best_item.get("animation_url") or "",
        "price_rub": best_pricing["price_rub"],
        "seller_price_rub": best_pricing["seller_price_rub"],
        "cost_rub": best_pricing["cost_rub"],
        "price_ton": best_pricing["price_ton"],
        "ton_rub_rate": best_pricing["ton_rub_rate"],
        "transfer_stars": best_pricing["transfer_stars"],
        "transfer_stars_rub": best_pricing["transfer_stars_rub"],
        "command": f"Я хочу купить подарок {canonical_name}",
        "best_offer": offers[0],
        "offers": offers,
        "offers_count": len(offers),
        "verified_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GiftCatalog/1.0"

    def log_message(self, fmt: str, *args):
        # Keep request URLs and query text out of the local console log.
        print(f"[catalog] {args[1] if len(args) > 1 else ''}")

    def _send(self, status: int, payload: bytes, content_type: str, cache_control: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/cardinal/quote":
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw_body = self.rfile.read(min(length, 16384)).decode("utf-8") if length > 0 else "{}"
                data = json.loads(raw_body) if raw_body else {}
            except Exception:
                data = {}
            q_text = str(data.get("query") or data.get("gift") or data.get("text") or "").strip()
            col = str(data.get("collection") or "").strip()
            payload = _cardinal_quote(q_text, col)
            status = 200 if payload.get("ok") else 400
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/logo.png", "/favicon.png", "/favicon.ico"):
            logo_path = SITE_DIR / "logo.png"
            if logo_path.is_file():
                self._send(200, logo_path.read_bytes(), "image/png", "public, max-age=86400")
            else:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        if parsed.path == "/api/gift-poster":
            slug = parse_qs(parsed.query).get("slug", [""])[0].strip().lower()
            data = _fetch_gift_media(slug, "webp", "image/webp,image/*;q=0.8")
            if data is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
            else:
                self._send(200, data, "image/webp", "public, max-age=86400")
            return
        if parsed.path == "/api/gift-animation":
            slug = parse_qs(parsed.query).get("slug", [""])[0].strip().lower()
            data = _fetch_gift_media(slug, "lottie.json", "application/json")
            if data is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
            else:
                self._send(200, data, "application/json; charset=utf-8", "public, max-age=86400")
            return
        if parsed.path == "/api/symbol-icon":
            q = parse_qs(parsed.query)
            name = q.get("name", [""])[0].strip()
            col = q.get("collection", [""])[0].strip()
            data = _fetch_symbol_icon(name, col)
            if data is None:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
            else:
                self._send(200, data, "image/png", "public, max-age=86400")
            return
        if parsed.path == "/api/attributes":
            q = parse_qs(parsed.query)
            col = q.get("collection", [""])[0].strip()
            if col.casefold() in {"all", "all collections", "все", "все коллекции"}:
                col = ""
            payload = _collection_attributes(col)
            self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/collections":
            try:
                payload = {"collections": _collection_catalog()}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            except MarketFailure as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(503, body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/cardinal/quote":
            q = parse_qs(parsed.query)
            raw_query = (q.get("query") or q.get("gift") or q.get("text") or [""])[0].strip()
            col = q.get("collection", [""])[0].strip()
            payload = _cardinal_quote(raw_query, col)
            status = 200 if payload.get("ok") else 400
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/search":
            self._search(parse_qs(parsed.query))
            return
        if parsed.path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        if parsed.path == "/" or parsed.path == "/index.html":
            file_path = SITE_DIR / "index.html"
            self._send(200, file_path.read_bytes(), "text/html; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _search(self, query: dict[str, list[str]]):
        def get(name: str, default: str = "") -> str:
            return query.get(name, [default])[0].strip()[:100]

        collection = get("collection")
        if collection.casefold() in {"all", "all collections", "все", "все коллекции"}:
            collection = ""
        model, backdrop, symbol = get("model"), get("backdrop"), get("symbol")
        raw_number = get("number") or get("gift")
        number = ""
        if raw_number:
            resolved_col, resolved_num = _parse_gift_reference(raw_number, collection)
            if resolved_col and not collection:
                collection = resolved_col
            if resolved_num:
                number = resolved_num
        try:
            paging = {
                "portals_offset": max(0, min(int(get("page0", "0")), 2_000_000)),
                "tonnel_page": max(1, min(int(get("page1", "1")), 100_000)),
                "mrkt_cursor": get("page2"),
            }
            min_rub = float(get("min_rub")) if get("min_rub") else None
            max_rub = float(get("max_rub")) if get("max_rub") else None
            if (min_rub is not None and min_rub < 0) or (max_rub is not None and max_rub < 0):
                raise ValueError
        except ValueError:
            body = json.dumps({"error": "Некорректная страница"}, ensure_ascii=False).encode("utf-8")
            self._send(400, body, "application/json; charset=utf-8")
            return
        selected = ACTIVE_MARKETS

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            futures = {
                pool.submit(_market_result, market_id, collection, model, backdrop, symbol, paging, number): market_id
                for market_id in selected
            }
            for future in as_completed(futures):
                market_id = futures[future]
                results[market_id] = future.result()

        all_items = []
        next_pages = {}
        for market_id in ACTIVE_MARKETS:
            if market_id in results:
                result = results[market_id]
            else:
                meta = MARKETS[market_id]
                result = {
                    "id": market_id,
                    "name": meta["name"],
                    "repo": meta["repo"],
                    "site": meta["site"],
                    "status": "not_requested",
                    "message": "Не выбран в этом запросе",
                    "count": 0,
                    "items": [],
                    "next": None,
                }
            all_items.extend(result.get("items", []))
            next_pages[market_id] = result.get("next")
        try:
            ton_rub_rate = _ton_rub_rate()
        except MarketFailure:
            ton_rub_rate = None
        visible_items = []
        for item in all_items:
            price_rub = _funpay_price_rub(item.get("price_value"), ton_rub_rate) if ton_rub_rate else None
            item["price_rub"] = price_rub
            if min_rub is not None and (price_rub is None or price_rub < min_rub):
                continue
            if max_rub is not None and (price_rub is None or price_rub > max_rub):
                continue
            for key in ("market", "market_name", "market_url", "listing_id", "price", "price_value", "asset"):
                item.pop(key, None)
            visible_items.append(item)
        visible_items.sort(key=lambda item: (item["price_rub"] is None, item["price_rub"] or 0))
        payload = {
            "items": visible_items,
            "resolved_collection": collection,
            "next": [next_pages.get(m) for m in ACTIVE_MARKETS],
            "verified_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="minutes"),
        }
        self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


if __name__ == "__main__":
    print(f"Каталог слушает http://127.0.0.1:{PORT}")
    print("Токены читаются с диска только в запросах к API и не возвращаются браузеру.")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
