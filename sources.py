"""Чтение заказов с фриланс-бирж.

Каждая функция возвращает список заказов (словарей) с полями:
  src, id, title, desc, url, budget (число в рублях или None),
  budget_text, category, extra (список коротких строк), ts (unix-время).
Если сайт не ответил или поменял разметку — функция бросает исключение.
"""
import datetime as dt
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT = 25
MSK = dt.timezone(dt.timedelta(hours=3))


def http(url, data=None, headers=None):
    """GET (или POST, если есть data) с одним повтором. Возвращает bytes."""
    h = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}
    if headers:
        h.update(headers)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # сеть, таймаут, 5xx
            last = e
            if attempt == 0:
                time.sleep(3)
    raise last


def clean(s):
    """HTML → обычный текст в одну строку."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</li>", " ", str(s), flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(html.unescape(s))
    return re.sub(r"\s+", " ", s).strip()


def to_int(s):
    try:
        return int(float(str(s).replace(" ", "").replace("\u00a0", "")))
    except (TypeError, ValueError):
        return None


def rub(n):
    return f"{n:,}".replace(",", " ") + " ₽"


def money_from_text(s):
    """'3 000 ₽ / заказ' → 3000 (только рубли)."""
    if "₽" not in s and "руб" not in s.lower():
        return None
    m = re.search(r"\d[\d\s\u00a0]*", s)
    return to_int(m.group(0)) if m else None


def order(**kw):
    base = dict(budget=None, budget_text="не указан", category="", extra=[], ts=0.0, desc="")
    base.update(kw)
    return base


# ---------------------------------------------------------------- Kwork

# Рубрики Kwork (id → название), взяты со страницы kwork.ru/projects 11.11.2026.
KWORK_CATS = {
    "44": "SEO аудиты, консультации", "43": "Внутренняя оптимизация", "273": "Продвижение сайта в топ",
    "71": "Семантическое ядро", "59": "Ссылки", "56": "Статистика и аналитика", "72": "Трафик",
    "20": "Аудиозапись и озвучка", "76": "Видеоролики", "78": "Видеосъемка и монтаж",
    "300": "ИИ-генерация видео", "77": "Интро и анимация логотипа", "23": "Музыка и песни",
    "106": "Редактирование аудио", "64": "Бухгалтерия и налоги", "262": "Обзвоны и продажи",
    "55": "Обучение и консалтинг", "84": "Персональный помощник", "265": "Подбор персонала",
    "114": "Продажа сайтов и групп", "65": "Стройка и ремонт", "63": "Юридическая помощь",
    "28": "Арт и иллюстрации", "24": "Веб и мобильный дизайн", "306": "ИИ-генерация изображений",
    "90": "Интерьер и экстерьер", "25": "Логотип и брендинг", "286": "Маркетплейсы и соцсети",
    "272": "Наружная реклама", "68": "Обработка и редактирование", "27": "Полиграфия",
    "270": "Презентации и инфографика", "250": "Промышленный дизайн", "79": "Верстка",
    "80": "Десктоп программирование", "38": "Доработка и настройка сайта", "40": "Игры",
    "39": "Мобильные приложения", "255": "Сервера и хостинг", "41": "Скрипты, боты и mini apps",
    "37": "Создание сайта", "81": "Юзабилити, тесты и помощь", "108": "E-mail маркетинг и рассылки",
    "113": "Базы данных и клиентов", "49": "Контекстная реклама", "47": "Маркетинг и PR",
    "112": "Маркетплейсы и доски объявлений", "46": "Соцсети и SMM", "303": "ИИ-тексты",
    "75": "Набор текста", "35": "Переводы", "74": "Продающие и бизнес-тексты",
    "235": "Резюме и вакансии", "73": "Тексты и наполнение сайта",
}


def kwork(pages=3):
    """Биржа проектов Kwork. Новые и поднятые заказы идут первыми, по 12 на страницу."""
    out = []
    for page in range(1, pages + 1):
        raw = http("https://kwork.ru/projects", data={"page": page},
                   headers={"X-Requested-With": "XMLHttpRequest",
                            "Accept": "application/json"})
        items = json.loads(raw.decode("utf-8"))["data"]["pagination"]["data"]
        for w in items:
            price = to_int(w.get("priceLimit"))
            max_price = to_int(w.get("possiblePriceLimit"))
            if price:
                budget_text = rub(price)
                if max_price and max_price > price:
                    budget_text += f" (допустимо до {rub(max_price)})"
            else:
                budget_text = "не указан"
            extra = []
            if w.get("kwork_count") is not None:
                extra.append(f"предложений: {w['kwork_count']}")
            if w.get("timeLeft"):
                extra.append(f"осталось {w['timeLeft']}")
            ts = 0.0
            stamp = w.get("date_active") or w.get("date_create")
            if stamp:
                try:
                    ts = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).timestamp()
                except ValueError:
                    pass
            out.append(order(
                src="kwork", id=str(w["id"]), title=clean(w.get("name")),
                desc=clean(w.get("description")),
                url=f"https://kwork.ru/projects/{w['id']}/view",
                category=KWORK_CATS.get(str(w.get("category_id", "")), ""),
                budget=price, budget_text=budget_text, extra=extra, ts=ts))
        if len(items) < 12:
            break
        time.sleep(1)
    return out


# ---------------------------------------------------------------- FL.ru

def flru():
    """FL.ru — официальная RSS-лента всех заказов (около 60 последних)."""
    root = ET.fromstring(http("https://www.fl.ru/rss/all.xml"))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        m = re.search(r"/projects/(\d+)", link)
        if not m:
            continue
        title = clean(it.findtext("title"))
        budget, budget_text = None, "не указан"
        b = re.search(r"\s*\(Бюджет:\s*([^)]*)\)\s*$", title)
        if b:
            budget_text = re.sub(r"\s+", " ", b.group(1)).strip()
            budget = money_from_text(budget_text)
            title = title[:b.start()].strip()
        title = re.sub(r"\s*\(для всех\)\s*$", "", title)
        ts = 0.0
        pub = it.findtext("pubDate")
        if pub:
            try:
                ts = dt.datetime.strptime(pub.strip(), "%a, %d %b %Y %H:%M:%S GMT").replace(
                    tzinfo=dt.timezone.utc).timestamp()
            except ValueError:
                pass
        out.append(order(
            src="flru", id=m.group(1), title=title, desc=clean(it.findtext("description")),
            url=link, budget=budget, budget_text=budget_text,
            category=clean(it.findtext("category")), ts=ts))
    return out


# ---------------------------------------------------------------- Freelance.ru

def _first(pattern, text):
    m = re.search(pattern, text, re.S)
    return clean(m.group(1)) if m else ""


def freelanceru():
    """Freelance.ru — лента заданий freelance.ru/task (25 последних)."""
    page = http("https://freelance.ru/task").decode("utf-8", errors="replace")
    out = []
    for card in page.split('<article class="task-card')[1:]:
        card = card.split("</article>")[0]
        m = re.search(r'href="/task/view/(\d+)"', card)
        if not m:
            continue
        tid = m.group(1)
        title = _first(r'class="task-card__title-link"[^>]*>(.*?)</a>', card)
        budget_text = _first(r'class="task-card__budget(?: [^"]*)?">(.*?)</div>', card) or "не указан"
        budget_text = budget_text.replace("/ заказ", "за заказ")
        extra = []
        term = _first(r"(Срок:[^<]*)", card)
        if term:
            extra.append(term.lower())
        ts = 0.0
        when = re.search(r'title="(\d\d\.\d\d\.\d{4} \d\d:\d\d)"', card)
        if when:
            ts = dt.datetime.strptime(when.group(1), "%d.%m.%Y %H:%M").replace(tzinfo=MSK).timestamp()
        out.append(order(
            src="freelanceru", id=tid, title=title,
            desc=_first(r'class="task-card__desc">(.*?)</p>', card),
            url=f"https://freelance.ru/task/view/{tid}",
            budget=money_from_text(budget_text), budget_text=budget_text,
            category=_first(r'task-chip--cat">(.*?)</span>', card), extra=extra, ts=ts))
    return out
