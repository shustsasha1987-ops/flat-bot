"""
Бот-мониторинг аренды квартир в Минске: Kufar, Onliner, Realt -> Telegram.
Запускается по расписанию (GitHub Actions), за один запуск:
  1) забирает свежие объявления с трёх сайтов,
  2) фильтрует по цене, комнатам и районам (по координатам),
  3) отправляет новые в Telegram,
  4) запоминает отправленные в state.json.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# ====================== НАСТРОЙКИ ПОИСКА ======================
PRICE_MIN_USD = 500
PRICE_MAX_USD = 1000
ROOMS = {1, 2, 3}

# Районы: многоугольники (долгота, широта). Порядок важен: первый совпавший.
ZONES = {
    "Лебяжий": [  # Тимирязева – Нарочанская – пр. Победителей – МКАД
        (27.4795, 53.9302), (27.4728, 53.9311), (27.4645, 53.9319),
        (27.4563, 53.9336), (27.4523, 53.9352), (27.4469, 53.9376),
        (27.4420, 53.9400),                      # Тимирязева x МКАД
        (27.4485, 53.9500),                      # МКАД x Победителей
        (27.4551, 53.9470), (27.4596, 53.9450), (27.4708, 53.9401),
        (27.4773, 53.9380), (27.4795, 53.9377),  # Победителей x Нарочанская
    ],
    "Левада": [(27.516, 53.929), (27.542, 53.929), (27.542, 53.942), (27.516, 53.942)],
    "Дрозды": [  # к северу от пр. Победителей, вокруг водохранилища
        (27.4485, 53.9500), (27.4551, 53.9470), (27.4596, 53.9450),
        (27.4708, 53.9401), (27.4773, 53.9380), (27.4900, 53.9373),
        (27.4900, 53.9680), (27.4400, 53.9680), (27.4400, 53.9510),
    ],
    "Северный берег": [(27.548, 53.952), (27.582, 53.952), (27.582, 53.970), (27.548, 53.970)],
}
# Общая рамка всех зон (для запроса к Onliner)
BBOX = (53.905, 27.435, 53.972, 27.585)  # lat_min, lon_min, lat_max, lon_max
# ==============================================================

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "ru-RU,ru;q=0.9"}
KEEP_DAYS = 45  # сколько дней помнить отправленные объявления


def in_polygon(lon, lat, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def zone_of(lon, lat):
    if lon is None or lat is None:
        return None
    for name, poly in ZONES.items():
        if in_polygon(lon, lat, poly):
            return name
    return None


def fmt_floor(floor, floors):
    first = lambda v: v[0] if isinstance(v, list) and v else v
    floor, floors = first(floor), first(floors)
    if not floor:
        return None
    return f"{floor}/{floors}" if floors else str(floor)


# ---------------------------- ИСТОЧНИКИ ----------------------------
def fetch_kufar():
    url = ("https://api.kufar.by/search-api/v2/search/rendered-paginated"
           "?cat=1010&typ=let&rgn=7&size=200&lang=ru&sort=lst.d&rms=v.or:1,2,3")
    data = requests.get(url, headers=UA, timeout=30).json()
    out = []
    for a in data.get("ads", []):
        p = {x["p"]: x for x in a.get("ad_parameters", [])}
        acc = {x["p"]: x for x in a.get("account_parameters", [])}
        coords = p.get("coordinates", {}).get("v") or [None, None]
        try:
            price = int(a.get("price_usd") or 0) / 100
            rooms = int(p.get("rooms", {}).get("v") or 0)
        except ValueError:
            continue
        floor = p.get("floor", {}).get("v")
        floors = p.get("re_number_floors", {}).get("v")
        imgs = a.get("images") or []
        photo = None
        if imgs and imgs[0].get("path"):
            photo = "https://rms.kufar.by/v1/gallery/" + imgs[0]["path"]
        out.append(dict(
            source="Kufar", id=str(a["ad_id"]), url=a.get("ad_link"),
            price=price, rooms=rooms, lon=coords[0], lat=coords[1],
            address=acc.get("address", {}).get("v") or "",
            district=p.get("re_district", {}).get("vl"),
            area=p.get("size", {}).get("v"),
            floor=fmt_floor(floor, floors),
            owner=not a.get("company_ad", False),
            agency=None, title=a.get("subject"), photo=photo,
        ))
    return out


def fetch_onliner():
    lat1, lon1, lat2, lon2 = BBOX
    params = [("rent_type[]", f"{r}_room" if r == 1 else f"{r}_rooms") for r in sorted(ROOMS)]
    params += [("price[min]", PRICE_MIN_USD), ("price[max]", PRICE_MAX_USD), ("currency", "usd"),
               ("bounds[lb][lat]", lat1), ("bounds[lb][long]", lon1),
               ("bounds[rt][lat]", lat2), ("bounds[rt][long]", lon2),
               ("order", "created_at:desc"), ("limit", 50)]
    out = []
    for page in (1, 2):
        data = requests.get("https://r.onliner.by/sdapi/ak.api/search/apartments",
                            params=params + [("page", page)], headers=UA, timeout=30).json()
        for a in data.get("apartments", []):
            loc = a.get("location", {})
            m = re.match(r"(\d+)_room", a.get("rent_type", ""))
            out.append(dict(
                source="Onliner", id=str(a["id"]), url=a.get("url"),
                price=float(a["price"]["converted"]["USD"]["amount"]),
                rooms=int(m.group(1)) if m else 0,
                lon=loc.get("longitude"), lat=loc.get("latitude"),
                address=loc.get("user_address") or loc.get("address") or "",
                district=None, area=None, floor=None,
                owner=bool(a.get("contact", {}).get("owner")),
                agency=None, title=None, photo=a.get("photo"),
            ))
        if data.get("page", {}).get("last", 1) <= page:
            break
    return out


def fetch_realt():
    out = []
    for page in (1, 2):
        r = requests.get(f"https://realt.by/rent/flat-for-long/?sortType=createdAt&page={page}",
                         headers=UA, timeout=30)
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError("Realt: не найден __NEXT_DATA__ (сайт поменял вёрстку?)")
        objs = json.loads(m.group(1))["props"]["pageProps"].get("objects", [])
        for o in objs:
            loc = o.get("location") or [None, None]
            price = (o.get("priceRates") or {}).get("840")
            agency = o.get("agencyName") or o.get("companyName")
            out.append(dict(
                source="Realt", id=str(o["code"]),
                url=f"https://realt.by/rent-flat-for-long/object/{o['code']}/",
                price=float(price) if price else 0, rooms=o.get("rooms") or 0,
                lon=loc[0], lat=loc[1], address=(o.get("address") or "").replace("Минск ", ""),
                district=None, area=o.get("areaTotal"),
                floor=fmt_floor(o.get("storey"), o.get("storeys")),
                owner=not (agency or o.get("agencyUuid") or o.get("companyUuid")),
                agency=agency, title=o.get("headline"), photo=None,
            ))
        time.sleep(1)
    return out


SOURCES = {"Kufar": fetch_kufar, "Onliner": fetch_onliner, "Realt": fetch_realt}


# ---------------------------- TELEGRAM ----------------------------
def tg(method, **payload):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, timeout=30)
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, timeout=30)
    return r.ok


def card(ad, also=None):
    e = html.escape
    who = "👤 Собственник" if ad["owner"] else "🏢 Агентство" + (f" ({e(ad['agency'])})" if ad.get("agency") else "")
    lines = [
        f"<b>{ad['rooms']}-комн. · ${ad['price']:.0f}</b> · {e(ad['zone'])}",
        f"📍 {e(ad['address'] or ad.get('district') or '—')}",
    ]
    extra = []
    if ad.get("area"):
        extra.append(f"{ad['area']} м²")
    if ad.get("floor"):
        extra.append(f"этаж {ad['floor']}")
    if extra:
        lines.append("📐 " + ", ".join(extra))
    lines.append(who)
    if ad.get("title"):
        lines.append(f"<i>{e(ad['title'][:200])}</i>")
    lines.append(f"🔗 <a href=\"{e(ad['url'])}\">{ad['source']}</a>")
    if also:
        lines.append("Также на: " + ", ".join(also))
    return "\n".join(lines)


def send(ad):
    text = card(ad)
    if ad.get("photo") and len(text) < 1000:
        if tg("sendPhoto", chat_id=TG_CHAT, photo=ad["photo"], caption=text, parse_mode="HTML"):
            return True
    return tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML",
              disable_web_page_preview="false")


# ---------------------------- ОСНОВНОЙ ЦИКЛ ----------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": {}, "fingerprints": {}, "errors": {}, "initialized": False}


def save_state(st):
    now = time.time()
    for key in ("seen", "fingerprints"):
        st[key] = {k: v for k, v in st[key].items() if now - v < KEEP_DAYS * 86400}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0, sort_keys=True)


def fingerprint(ad):
    # одна и та же квартира на разных сайтах: координаты (~100 м) + комнаты + цена (±$10)
    if ad["lat"] is None:
        return None
    return f"{round(ad['lat'], 3)}:{round(ad['lon'], 3)}:{ad['rooms']}:{round(ad['price'] / 20)}"


def main():
    dry = "--dry" in sys.argv
    if not dry and not (TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    st = load_state()
    st.setdefault("errors", {})
    now = time.time()
    matched = []
    for name, fn in SOURCES.items():
        try:
            ads = fn()
            print(f"{name}: получено {len(ads)}")
            st["errors"][name] = 0
        except Exception as ex:  # один упавший сайт не ломает остальные
            print(f"{name}: ОШИБКА {ex!r}")
            st["errors"][name] = st["errors"].get(name, 0) + 1
            if st["errors"][name] == 6 and not dry:  # ~час подряд не работает
                tg("sendMessage", chat_id=TG_CHAT,
                   text=f"⚠️ Бот не может получить объявления с {name} уже около часа. Возможно, сайт поменялся.")
            continue
        for ad in ads:
            if not (PRICE_MIN_USD <= ad["price"] <= PRICE_MAX_USD and ad["rooms"] in ROOMS):
                continue
            ad["zone"] = zone_of(ad["lon"], ad["lat"])
            if ad["zone"]:
                matched.append(ad)

    first_run = not st.get("initialized")
    new = [a for a in matched if f"{a['source']}:{a['id']}" not in st["seen"]]
    print(f"Подходит под фильтр: {len(matched)}, новых: {len(new)}")

    if dry:
        for a in new:
            print("-" * 40); print(card(a))
        return

    if first_run:
        tg("sendMessage", chat_id=TG_CHAT, parse_mode="HTML",
           text="✅ Бот запущен. Присылаю всё, что сейчас подходит под фильтр (без дублей между сайтами). "
                "Дальше буду присылать только новые.")
    sent = 0
    for ad in new:
        key = f"{ad['source']}:{ad['id']}"
        fp = fingerprint(ad)
        if fp and fp in st["fingerprints"]:
            st["seen"][key] = now  # дубль с другого сайта — не шлём повторно
            continue
        if send(ad):
            sent += 1
            st["seen"][key] = now
            if fp:
                st["fingerprints"][fp] = now
            time.sleep(1.1)
    st["initialized"] = True
    st["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(st)
    print(f"Отправлено: {sent}")


if __name__ == "__main__":
    main()
