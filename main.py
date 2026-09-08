#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
САНЧОУС — Telegram-бот и веб-сервер магазина в одном процессе.

Что делает:
  * раздаёт сайт из этой же папки (index.html, css, js, img);
  * поднимает бота на long polling;
  * на /start присылает кнопку «Открыть магазин» (Telegram Mini App) и ссылку;
  * принимает заказ из мини-приложения и отвечает составом заказа.

Переменные окружения:
  BOT_TOKEN   — токен бота от @BotFather (обязательно)
  PUBLIC_URL  — публичный HTTPS-адрес сайта, например https://sanchous.hostbot.ru
                (без него кнопка Mini App не работает — Telegram требует HTTPS)
  PORT        — порт веб-сервера, по умолчанию 8080
  ADMIN_ID    — необязательно: id чата, куда дублировать заказы
"""

import json
import os
import posixpath
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- настройки
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN") or "").strip()


def _first_env(*names):
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _as_https(v):
    """Адрес хостинга приходит по-разному: с https, без схемы, со слэшем."""
    v = v.strip().rstrip("/")
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v
    return "https://" + v


PUBLIC_URL = _as_https(_first_env(
    "PUBLIC_URL", "WEBAPP_URL", "APP_URL", "SITE_URL", "DOMAIN", "RENDER_EXTERNAL_URL"))
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()


def _ports():
    """Порты, на которых слушаем сайт.

    Ловушка хостинга: системная переменная PORT перекрывает
    пользовательскую, а обратный прокси всё равно стучится на 3000.
    Процесс с живым сайтом на «своём» порту выглядит снаружи как 502.
    Поэтому занимаем несколько портов сразу — какой-нибудь да совпадёт.
    """
    out = []
    for v in (os.environ.get("PORT"), os.environ.get("APP_PORT"), "3000", "8080"):
        try:
            p = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if 0 < p < 65536 and p not in out:
            out.append(p)
    return out or [8080]


PORTS = _ports()
PORT = PORTS[0]

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, "{}")

SHOP_NAME = "Санчоус"


def log(*parts):
    print("[{}]".format(time.strftime("%H:%M:%S")), *parts, flush=True)


# ---------------------------------------------------------------- веб-сервер
class SiteHandler(SimpleHTTPRequestHandler):
    """Отдаёт статику сайта из папки проекта."""

    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map)
    extensions_map.update({
        ".webp": "image/webp",
        ".webmanifest": "application/manifest+json",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".js": "text/javascript",
        ".css": "text/css",
        ".woff2": "font/woff2",
    })

    def translate_path(self, path):
        path = urllib.parse.urlsplit(path).path
        path = urllib.parse.unquote(path)
        parts = [p for p in path.split("/") if p and p not in (".", "..")]
        return os.path.join(ROOT, *parts) if parts else os.path.join(ROOT, "index.html")

    def do_GET(self):
        if self.path.split("?")[0] in ("/health", "/healthz", "/ping"):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return SimpleHTTPRequestHandler.do_GET(self)

    def end_headers(self):
        # мини-приложение открывается во фрейме Telegram
        self.send_header("Access-Control-Allow-Origin", "*")
        p = self.path.split("?")[0]
        if p.startswith("/img/") or p.startswith("/css/") or p.startswith("/js/"):
            self.send_header("Cache-Control", "public, max-age=604800")
        else:
            self.send_header("Cache-Control", "no-cache")
        SimpleHTTPRequestHandler.end_headers(self)

    def log_message(self, fmt, *args):
        pass          # не засоряем вывод обращениями к статике


def serve_on(port):
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), SiteHandler)
    except OSError as e:
        log("порт {} занят или недоступен: {}".format(port, e))
        return False
    log("слушаю 0.0.0.0:{}".format(port))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return True


def serve_site():
    """Поднимаем сайт на всех портах из PORTS — хотя бы один совпадёт
    с тем, куда ходит прокси хостинга."""
    live = [p for p in PORTS if serve_on(p)]
    if not live:
        log("!! сайт не поднялся ни на одном порту из {}".format(PORTS))
    else:
        log("сайт раздаётся из папки {}".format(ROOT))
    return live


# ---------------------------------------------------------------- Telegram API
def api(method, **params):
    """Вызов Telegram Bot API. Возвращает result или None."""
    data = {}
    for k, v in params.items():
        if v is None:
            continue
        data[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(API.format(method), data=body)
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            detail = str(e)
        log("!! {}: {}".format(method, detail))
        return None
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return None
    except Exception as e:
        log("!! {}: {}".format(method, e))
        return None

    if not payload.get("ok"):
        log("!! {}: {}".format(method, payload.get("description")))
        return None
    return payload.get("result")


# ---------------------------------------------------------------- тексты
def site_url():
    return PUBLIC_URL or "http://localhost:{}".format(PORT)


def mini_app_ok():
    """Кнопка Mini App живёт только на https — таково требование Telegram."""
    return site_url().startswith("https://")


def start_keyboard():
    """Первая кнопка — во всю ширину и открывает магазин прямо в Telegram.
    Ссылкой на сайт подстраховываемся только тогда, когда Mini App
    недоступен: две кнопки, ведущие в одно место, только путают."""
    url = site_url()
    rows = []
    if mini_app_ok():
        rows.append([{"text": "🛒  Открыть магазин", "web_app": {"url": url}}])
    else:
        rows.append([{"text": "🌐  Открыть сайт", "url": url}])
    rows.append([
        {"text": "🔥 Каталог", "callback_data": "catalog"},
        {"text": "🚚 Доставка", "callback_data": "delivery"},
    ])
    return {"inline_keyboard": rows}


WELCOME = (
    "🌶 <b>САНЧОУС</b>\n"
    "<i>Соус есть? Санчоус.</i>\n\n"
    "Четыре соуса, которые хочется повторить:\n"
    "🧀 Сырный · 🍅 Кетчуп\n"
    "👑 Фирменный · 🌶 Медово-чили\n\n"
    "От <b>99 ₽</b> · доставка по всей стране\n"
    "Бесплатно от 1 500 ₽\n\n"
    "Жмите кнопку ниже — соберём заказ в пару касаний 👇"
)

CATALOG = (
    "<b>Наши соусы</b>\n\n"
    "🧀 <b>Сырный соус</b> — 250 мл — 99 ₽\n"
    "Нежный, сливочный, сырный.\n\n"
    "🍅 <b>Кетчуп</b> — 340 мл — 99 ₽\n"
    "Насыщенный, томатный, идеальный.\n\n"
    "👑 <b>Фирменный соус Санчоус</b> — 290 мл — 149 ₽\n"
    "Уникальный вкус, который запомнится.\n\n"
    "🌶 <b>Медово-чили</b> — 250 мл — 149 ₽\n"
    "Сладость мёда и острота перца.\n\n"
    "Открой магазин, чтобы заказать 👇"
)

DELIVERY = (
    "<b>Доставка и оплата</b>\n\n"
    "🚚 Доставка от 1 дня по всей стране\n"
    "📦 Бесплатно при заказе от 1 500 ₽\n"
    "💳 Оплата картой или при получении\n\n"
    "Вопросы: 8 800 000-00-00, ежедневно с 9:00 до 21:00"
)


# ---------------------------------------------------------------- обработчики
def send(chat_id, text, keyboard=None):
    return api("sendMessage", chat_id=chat_id, text=text,
               parse_mode="HTML", disable_web_page_preview=True,
               reply_markup=keyboard)


def send_start(chat_id):
    """Приветствие карточкой: баннер, текст под ним и кнопка магазина.

    Картинку Telegram забирает по ссылке сам, поэтому она появляется
    только когда сайт уже виден снаружи. Не получилось — отправляем
    тем же текстом без картинки: человек всё равно получает кнопку.
    """
    if mini_app_ok():
        photo = site_url() + "/img/banner-1400.jpg"
        if api("sendPhoto", chat_id=chat_id, photo=photo,
               caption=WELCOME, parse_mode="HTML",
               reply_markup=start_keyboard()):
            return
    send(chat_id, WELCOME + "\n\n🌐 {}".format(site_url()), start_keyboard())


def handle_order(chat_id, raw):
    """Заказ, отправленный из мини-приложения через Telegram.WebApp.sendData."""
    try:
        order = json.loads(raw)
    except Exception:
        send(chat_id, "Не получилось прочитать заказ. Попробуй ещё раз 🙏")
        return

    items = order.get("items") or []
    if not items:
        send(chat_id, "Корзина пустая — добавь соусы и повтори заказ 🙂")
        return

    lines = []
    for i in items:
        lines.append("• {} ({}) × {} — {} ₽".format(
            i.get("name", "?"), i.get("volume", ""), i.get("qty", 1),
            int(i.get("price", 0)) * int(i.get("qty", 1))))

    total = order.get("total") or sum(int(i.get("price", 0)) * int(i.get("qty", 1)) for i in items)
    text = ("<b>Заказ принят!</b> 🎉\n\n" + "\n".join(lines) +
            "\n\n<b>Итого: {} ₽</b>\n\n"
            "Скоро свяжемся с тобой для подтверждения.".format(total))
    send(chat_id, text)

    if ADMIN_ID:
        send(ADMIN_ID, "🔔 Новый заказ от <code>{}</code>\n\n".format(chat_id) +
                       "\n".join(lines) + "\n\nИтого: {} ₽".format(total))


def handle_update(u):
    msg = u.get("message") or u.get("edited_message")
    cb = u.get("callback_query")

    if cb:
        data = cb.get("data")
        chat_id = cb["message"]["chat"]["id"]
        api("answerCallbackQuery", callback_query_id=cb["id"])
        if data == "catalog":
            send(chat_id, CATALOG, start_keyboard())
        elif data == "delivery":
            send(chat_id, DELIVERY, start_keyboard())
        return

    if not msg:
        return

    chat_id = msg["chat"]["id"]

    if "web_app_data" in msg:
        handle_order(chat_id, msg["web_app_data"].get("data", ""))
        return

    text = (msg.get("text") or "").strip().lower()

    if text.startswith("/start"):
        send_start(chat_id)
    elif text.startswith("/shop") or text.startswith("/site") or "сайт" in text:
        send(chat_id, "Магазин «{}»: {}".format(SHOP_NAME, site_url()), start_keyboard())
    elif text.startswith("/catalog") or "каталог" in text:
        send(chat_id, CATALOG, start_keyboard())
    elif text.startswith("/delivery") or "доставка" in text:
        send(chat_id, DELIVERY, start_keyboard())
    elif text.startswith("/help"):
        send(chat_id, "Команды:\n/start — открыть магазин\n/catalog — список соусов\n"
                      "/delivery — доставка и оплата", start_keyboard())
    else:
        send(chat_id, "Открывай магазин — там все соусы 👇", start_keyboard())


# ---------------------------------------------------------------- запуск
def setup_bot():
    me = api("getMe")
    if not me:
        log("!! токен не принят Telegram. Проверь переменную BOT_TOKEN")
        return False
    log("бот запущен: @{}".format(me.get("username")))

    api("setMyCommands", commands=[
        {"command": "start", "description": "Открыть магазин"},
        {"command": "catalog", "description": "Наши соусы"},
        {"command": "delivery", "description": "Доставка и оплата"},
    ])

    url = site_url()
    if url.startswith("https://"):
        api("setChatMenuButton", menu_button={
            "type": "web_app",
            "text": "Магазин",
            "web_app": {"url": url},
        })
        log("кнопка меню ведёт на мини-приложение: {}".format(url))
    else:
        log("!! PUBLIC_URL не задан — магазин не откроется кнопкой внутри Telegram.")
        log("!! Впишите в переменные окружения хостинга адрес сайта, например:")
        log("!!   PUBLIC_URL=https://bot-XXXX.bothost.tech")
        log("!! Пока в сообщениях будет обычная ссылка: {}".format(url))
    return True


def poll():
    offset = None
    fails = 0
    while True:
        updates = api("getUpdates", offset=offset, timeout=50,
                      allowed_updates=["message", "edited_message", "callback_query"])
        if updates is None:
            fails += 1
            time.sleep(min(30, 2 ** min(fails, 4)))
            continue
        fails = 0
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:
                log("!! ошибка обработки:", e)


def main():
    serve_site()

    if not BOT_TOKEN:
        log("!! BOT_TOKEN не задан — работает только сайт на порту {}".format(PORT))
        while True:
            time.sleep(3600)

    if not setup_bot():
        # сайт всё равно должен остаться доступным
        while True:
            time.sleep(3600)

    poll()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("остановлено")
        sys.exit(0)
