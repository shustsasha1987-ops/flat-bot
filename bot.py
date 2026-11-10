"""
Бот-мониторинг аренды квартир в Минске: Kufar, Onliner, Realt -> Telegram.
Запускается по расписанию (GitHub Actions), за один запуск:
  1) забирает свежие объявления с трёх сайтов,
  2) фильтрует по цене, комнатам и районам (по координатам),
  3) отправляет новые в Telegram с кнопками «Думаю / Альтернатива / Отказ»,
  4) обрабатывает нажатия кнопок (отказ — сообщение удаляется),
  5) запоминает всё в state.json.
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
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "0"))  # сколько секунд слушать кнопки за один запуск
RECHECK_SECONDS = 300  # повторная проверка сайтов внутри запуска


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
STATUS = {
    "new": "",
    "think": "🤔 ДУМАЮ",
    "alt": "🔁 АЛЬТЕРНАТИВА",
}
BUTTONS = {  # какие кнопки показывать в каждом статусе
    "new": [("🤔 Думаю", "think"), ("🔁 Альтернатива", "alt"), ("❌ Отказ", "reject")],
    "think": [("🔁 В альтернативу", "alt"), ("❌ Отказ", "reject")],
    "alt": [("🤔 В думаю", "think"), ("❌ Отказ", "reject")],
}
TOAST = {"think": "🤔 Думаю", "alt": "🔁 Альтернатива", "reject": "❌ Отказ — удалено"}


def tg(method, **payload):
    """Вызов Telegram API. Возвращает result или None."""
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload,
                              timeout=40)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        j = r.json()
        return j.get("result") if j.get("ok") else None
    return None


def keyboard(key, status):
    rows = [[{"text": t, "callback_data": f"s|{code}|{key}"} for t, code in BUTTONS[status]]]
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def card(ad):
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
    return "\n".join(lines)


def with_status(text, status):
    label = STATUS.get(status, "")
    return f"<b>{label}</b>\n{text}" if label else text


def send(key, ad):
    """Отправляет карточку с кнопками, возвращает запись для state или None."""
    text = card(ad)
    kb = keyboard(key, "new")
    if ad.get("photo") and len(text) < 950:
        res = tg("sendPhoto", chat_id=TG_CHAT, photo=ad["photo"], caption=text,
                 parse_mode="HTML", reply_markup=kb)
        if res:
            return {"msg": res["message_id"], "photo": True, "text": text, "status": "new"}
    res = tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML", reply_markup=kb)
    if res:
        return {"msg": res["message_id"], "photo": False, "text": text, "status": "new"}
    return None


def edit(rec, key):
    body = with_status(rec["text"], rec["status"])
    common = dict(chat_id=TG_CHAT, message_id=rec["msg"], parse_mode="HTML",
                  reply_markup=keyboard(key, rec["status"]))
    if rec["photo"]:
        return tg("editMessageCaption", caption=body, **common)
    return tg("editMessageText", text=body, **common)


def reject(rec):
    if tg("deleteMessage", chat_id=TG_CHAT, message_id=rec["msg"]):
        return
    # Telegram не даёт удалить (например, сообщение старше 48 ч) — сворачиваем
    first = rec["text"].split("\n")[0]
    body = f"❌ <s>Отказ</s> · {first}"
    common = dict(chat_id=TG_CHAT, message_id=rec["msg"], parse_mode="HTML",
                  reply_markup=json.dumps({"inline_keyboard": []}))
    if rec["photo"]:
        tg("editMessageCaption", caption=body, **common)
    else:
        tg("editMessageText", text=body, **common)


def send_list(st):
    groups = {"think": [], "alt": []}
    for key, rec in st["ads"].items():
        if rec.get("status") in groups:
            first = re.sub(r"<[^>]+>", "", rec["text"].split("\n")[0])
            addr = re.sub(r"<[^>]+>", "", rec["text"].split("\n")[1]) if "\n" in rec["text"] else ""
            groups[rec["status"]].append(f"• {first} — {addr.replace('📍 ', '')} "
                                         f"<a href=\"{html.escape(rec.get('url', ''))}\">ссылка</a>")
    parts = []
    for status, title in (("think", "🤔 Думаю"), ("alt", "🔁 Альтернатива")):
        items = groups[status]
        parts.append(f"<b>{title} ({len(items)})</b>\n" + ("\n".join(items) if items else "— пусто"))
    tg("sendMessage", chat_id=TG_CHAT, text="\n\n".join(parts), parse_mode="HTML",
       disable_web_page_preview="true")


HELP = ("Кнопки под объявлением:\n"
        "🤔 Думаю — звоню и рассматриваю в первую очередь\n"
        "🔁 Альтернатива — во вторую очередь\n"
        "❌ Отказ — объявление удаляется из чата и больше не появится\n\n"
        "Статус можно менять в любой момент кнопками под объявлением.\n"
        "/list — список «Думаю» и «Альтернатива»")


def process_updates(st, wait=0):
    """Читает нажатия кнопок и команды. wait — сколько секунд ждать новых событий."""
    res = tg("getUpdates", offset=st.get("offset", 0), timeout=wait,
             allowed_updates=json.dumps(["callback_query", "message"]))
    if res is None and wait:
        time.sleep(5)  # Telegram недоступен — не долбим API
    if not res:
        return
    for u in res:
        st["offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:
            if str(cq.get("from", {}).get("id")) != str(TG_CHAT):
                continue
            try:
                _, code, key = cq.get("data", "").split("|", 2)
            except ValueError:
                continue
            rec = st["ads"].get(key)
            if not rec:
                tg("answerCallbackQuery", callback_query_id=cq["id"], text="Объявление не найдено")
                continue
            if code == "reject":
                reject(rec)
                rec["status"] = "reject"
            elif code in ("think", "alt"):
                rec["status"] = code
                edit(rec, key)
            rec["changed"] = time.time()
            tg("answerCallbackQuery", callback_query_id=cq["id"], text=TOAST.get(code, "Готово"))
            continue
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/list"):
            send_list(st)
        elif text.startswith("/start") or text.startswith("/help"):
            tg("sendMessage", chat_id=TG_CHAT, text=HELP)


# ---------------------------- ОСНОВНОЙ ЦИКЛ ----------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for k, v in (("seen", {}), ("fingerprints", {}), ("errors", {}), ("ads", {})):
        st.setdefault(k, v)
    return st


def save_state(st):
    now = time.time()
    for key in ("seen", "fingerprints"):
        st[key] = {k: v for k, v in st[key].items() if now - v < KEEP_DAYS * 86400}
    # «Думаю» и «Альтернатива» храним всегда, остальное — KEEP_DAYS
    st["ads"] = {k: v for k, v in st["ads"].items()
                 if v.get("status") in ("think", "alt") or k in st["seen"]}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0, sort_keys=True)


def fingerprint(ad):
    # одна и та же квартира на разных сайтах: координаты (~100 м) + комнаты + цена (±$10)
    if ad["lat"] is None:
        return None
    return f"{round(ad['lat'], 3)}:{round(ad['lon'], 3)}:{ad['rooms']}:{round(ad['price'] / 20)}"


def collect(st, dry=False):
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
    return matched


def check_sites(st):
    now = time.time()
    matched = collect(st)
    new = [a for a in matched if f"{a['source']}:{a['id']}" not in st["seen"]]
    print(f"Подходит под фильтр: {len(matched)}, новых: {len(new)}")
    sent = 0
    for ad in new:
        key = f"{ad['source']}:{ad['id']}"
        fp = fingerprint(ad)
        if fp and fp in st["fingerprints"]:
            st["seen"][key] = now  # дубль с другого сайта — не шлём повторно
            continue
        rec = send(key, ad)
        if rec:
            rec["url"] = ad["url"]
            st["ads"][key] = rec
            st["seen"][key] = now
            if fp:
                st["fingerprints"][fp] = now
            sent += 1
            time.sleep(1.1)
    print(f"Отправлено: {sent}")


def migrate(st):
    """Переход на версию с кнопками: убираем старые сообщения без кнопок и шлём всё заново."""
    if st.get("version", 1) >= 2:
        return
    probe = tg("sendMessage", chat_id=TG_CHAT, text="🔄 Обновляю бота…")
    if probe:
        last = probe["message_id"]
        for mid in range(last, max(0, last - 300), -1):
            tg("deleteMessage", chat_id=TG_CHAT, message_id=mid)
    st["seen"], st["fingerprints"], st["ads"] = {}, {}, {}
    st["version"] = 2
    tg("sendMessage", chat_id=TG_CHAT,
       text="✅ Бот обновлён: теперь под каждым объявлением есть кнопки. Присылаю заново всё актуальное.\n\n" + HELP)


def main():
    if "--dry" in sys.argv:
        st = load_state()
        for a in collect(st, dry=True):
            print("-" * 40); print(card(a))
        return
    if not (TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
    start = time.time()
    st = load_state()
    process_updates(st)            # нажатия, накопившиеся между запусками
    migrate(st)
    check_sites(st)
    next_check = start + RECHECK_SECONDS
    # слушаем кнопки до конца отведённого времени, по пути ещё раз проверяем сайты
    while time.time() - start < RUN_SECONDS - 5:
        wait = int(min(25, RUN_SECONDS - (time.time() - start) - 3))
        if wait <= 0:
            break
        process_updates(st, wait=wait)
        if time.time() >= next_check:
            check_sites(st)
            next_check += RECHECK_SECONDS
    st["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(st)


if __name__ == "__main__":
    main()
