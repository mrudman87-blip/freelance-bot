"""Бот поиска заказов на фриланс-биржах → Telegram.

Один запуск = одна проверка: прочитать команды из Telegram, проверить сайты,
прислать новые подходящие заказы, запомнить, что уже видели.
В облаке его запускает GitHub Actions каждые ~10 минут.

  python bot.py            обычный запуск (нужны TELEGRAM_TOKEN и TELEGRAM_CHAT_ID)
  python bot.py --dry      проверка без Telegram: показать, какие заказы подходят под фильтр
  python bot.py --chat-id  показать chat id тех, кто написал боту (для настройки)
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request

import sources

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(ROOT, "settings.txt")
STATE = os.path.join(ROOT, "state", "state.json")
MSK = dt.timezone(dt.timedelta(hours=3))

SITES = {
    "kwork": ("Kwork", "🟢", sources.kwork),
    "flru": ("FL.ru", "🔵", sources.flru),
    "freelanceru": ("Freelance.ru", "🟠", sources.freelanceru),
}
KEEP_SEEN = 3000       # сколько id заказов помнить на каждый сайт
MAX_SEND = 40          # не больше стольких сообщений за одну проверку
FIRST_RUN_SAMPLES = 5  # при самом первом запуске прислать для примера столько последних заказов
FAIL_ALERT = 6         # после стольких неудачных проверок подряд (~1 час) — предупредить
MY_WORDS = "Мои слова"
STOP_SECTION = "Стоп-слова"
SETTINGS_SECTION = "Настройки"


def log(*a):
    print(*a, flush=True)


def now_msk():
    return dt.datetime.now(MSK)


# ======================================================== настройки (settings.txt)

def strip_comment(line):
    if line.lstrip().startswith("#"):
        return ""
    return re.split(r"\s#", line, maxsplit=1)[0]


def norm(s):
    return s.lower().replace("ё", "е")


def load_settings():
    cfg = {"min_budget": 0, "paused": False, "in_desc": False, "sites": list(SITES),
           "stop": [], "topics": {}}
    section = None
    with open(SETTINGS, encoding="utf-8-sig") as f:
        for raw in f:
            line = strip_comment(raw).strip()
            if not line:
                continue
            m = re.fullmatch(r"\[(.+)\]", line)
            if m:
                section = m.group(1).strip()
                continue
            if section is None:
                continue
            if norm(section) == norm(SETTINGS_SECTION):
                key, _, val = line.partition("=")
                key, val = norm(key.strip()), val.strip()
                if key == "мин_бюджет":
                    cfg["min_budget"] = sources.to_int(re.sub(r"\D", "", val) or 0) or 0
                elif key == "пауза":
                    cfg["paused"] = norm(val) in ("да", "yes", "true", "1", "вкл")
                elif key == "искать_в_описании":
                    cfg["in_desc"] = norm(val) in ("да", "yes", "true", "1", "вкл")
                elif key == "сайты":
                    cfg["sites"] = [s.strip().lower() for s in val.split(",") if s.strip()]
            elif norm(section) == norm(STOP_SECTION):
                cfg["stop"].append(line)
            else:
                cfg["topics"].setdefault(section, []).append(line)
    cfg["stop_re"] = [(w, compile_word(w)) for w in cfg["stop"]]
    cfg["topic_re"] = {t: [(w, compile_word(w)) for w in ws] for t, ws in cfg["topics"].items()}
    return cfg


def compile_word(word):
    """'оформлени документ' → найдёт 'оформление документов' (слово ищется с начала слова);
    "word" в кавычках → только целое слово (не найдёт 'WordPress')."""
    w = norm(word.strip())
    exact = len(w) > 2 and w[0] == w[-1] == '"'
    parts = w.strip('"').split()
    rx = r"(?<!\w)" + r"\w*\W+".join(re.escape(p) for p in parts)
    return re.compile(rx + (r"(?!\w)" if exact else ""))


def match(o, cfg):
    """Список (тема, слово) или [] — если заказ не подходит.
    Ищем в названии и категории (и в описании, если так задано в настройках);
    стоп-слова — только в названии и категории, чтобы «оставлю отзыв» в описании не мешало."""
    head = norm(o["title"] + " \n " + o["category"])
    for _, rx in cfg["stop_re"]:
        if rx.search(head):
            return []
    text = head + " \n " + norm(o["desc"]) if cfg["in_desc"] else head
    if cfg["min_budget"] and o["budget"] is not None and o["budget"] < cfg["min_budget"]:
        return []
    found = []
    for topic, words in cfg["topic_re"].items():
        for word, rx in words:
            m = rx.search(text)
            if m:
                # показываем найденное слово целиком: «мобильн приложени» → «мобильное приложение»
                tail = re.match(r"\w*", text[m.end():]).group(0)
                found.append((topic, text[m.start():m.end()] + tail))
                break
    return found


# --- правка settings.txt из Telegram (построчно, комментарии сохраняются)

def _read_lines():
    with open(SETTINGS, encoding="utf-8-sig") as f:
        return f.read().splitlines()


def _write_lines(lines):
    with open(SETTINGS, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def _section_range(lines, name):
    """(индекс заголовка, индекс конца секции) или None."""
    start = None
    for i, raw in enumerate(lines):
        m = re.fullmatch(r"\s*\[(.+)\]\s*", raw)
        if m:
            if start is not None:
                return start, i
            if norm(m.group(1).strip()) == norm(name):
                start = i
    return (start, len(lines)) if start is not None else None


def add_words(section, words):
    lines = _read_lines()
    rng = _section_range(lines, section)
    if rng is None:
        lines += ["", f"[{section}]"]
        rng = (len(lines) - 1, len(lines))
    start, end = rng
    have = {norm(strip_comment(l).strip()) for l in lines[start + 1:end]}
    new = [w for w in words if norm(w) not in have]
    pos = end
    while pos - 1 > start and not lines[pos - 1].strip():
        pos -= 1
    lines[pos:pos] = new
    _write_lines(lines)
    return new


def remove_words(words):
    targets = {norm(w) for w in words}
    lines, out, removed, section = _read_lines(), [], [], None
    for raw in lines:
        m = re.fullmatch(r"\s*\[(.+)\]\s*", raw)
        if m:
            section = m.group(1).strip()
        elif section and norm(section) != norm(SETTINGS_SECTION):
            w = strip_comment(raw).strip()
            if w and norm(w) in targets:
                removed.append(f"{w} ({section})")
                continue
        out.append(raw)
    _write_lines(out)
    return removed


def set_setting(key, value):
    lines = _read_lines()
    rng = _section_range(lines, SETTINGS_SECTION)
    if rng is None:
        lines[0:0] = [f"[{SETTINGS_SECTION}]", f"{key} = {value}", ""]
    else:
        start, end = rng
        for i in range(start + 1, end):
            if norm(lines[i].split("=")[0].strip()) == norm(key):
                cm = re.search(r"\s#.*$", lines[i])
                head = f"{key} = {value}"
                if cm:  # комментарий оставляем в той же колонке
                    comment = cm.group(0).lstrip()
                    col = len(lines[i]) - len(comment)
                    head = (head.ljust(col) if len(head) + 2 <= col else head + "  ") + comment
                lines[i] = head
                break
        else:
            lines.insert(start + 1, f"{key} = {value}")
    _write_lines(lines)


# ======================================================== память (state/state.json)

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("seen", {})
    st.setdefault("fails", {})
    st.setdefault("offset", 0)
    st.setdefault("stats", {})
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE)


# ======================================================== Telegram

def tg(method, **params):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except ValueError:
                body = {}
            if e.code == 429:
                time.sleep(int(body.get("parameters", {}).get("retry_after", 5)) + 1)
                continue
            log(f"Telegram {method}: ошибка {e.code} {body.get('description', '')}")
            return body
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"Telegram {method}: сеть ({e}), повтор")
            time.sleep(3)
    return {}


def send(chat_id, text, url=None):
    params = dict(chat_id=chat_id, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True})
    if url:
        params["reply_markup"] = {"inline_keyboard": [[{"text": "Открыть заказ ↗", "url": url}]]}
    res = tg("sendMessage", **params)
    time.sleep(1.1)  # Telegram: не больше ~1 сообщения в секунду в один чат
    return bool(res.get("ok"))


def esc(s):
    return html.escape(str(s), quote=False)


def shorten(s, n):
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def format_order(o, found):
    name, icon, _ = SITES[o["src"]]
    head = f"{icon} <b>{esc(name)}</b>  ·  💰 {esc(o['budget_text'])}"
    parts = [head, f"<b>{esc(o['title'])}</b>"]
    if o["desc"]:
        parts.append(esc(shorten(o["desc"], 450)))
    meta = ([o["category"]] if o["category"] else []) + list(o["extra"])
    tail = []
    if meta:
        tail.append("📂 " + esc(" · ".join(meta)))
    tail.append("🏷 " + esc(", ".join(f"{t} («{w}»)" for t, w in found)))
    parts.append("\n".join(tail))
    return "\n\n".join(parts)


HELP = """🤖 <b>Бот поиска заказов</b>

Каждые ~10 минут проверяю Kwork, FL.ru и Freelance.ru и присылаю новые заказы по твоим темам.

<b>Команды</b> (отвечаю при следующей проверке, обычно в течение 10–15 минут):
/list — темы, слова и настройки
/add слово — искать ещё и это слово (можно через запятую)
/del слово — больше не искать это слово
/ban слово — не присылать заказы с этим словом
/budget 3000 — не присылать заказы дешевле 3000 ₽ (/budget 0 — все)
/pause — замолчать · /resume — снова присылать
/status — как дела у бота

Слово можно писать началом: «приложени» найдёт «приложение», «приложения»…"""


def cmd_list(cfg):
    out = [f"⚙️ Мин. бюджет: <b>{cfg['min_budget'] or 'любой'}</b> · "
           f"пауза: <b>{'да' if cfg['paused'] else 'нет'}</b> · сайты: {esc(', '.join(cfg['sites']))}"]
    for topic, words in cfg["topics"].items():
        out.append(f"🏷 <b>{esc(topic)}</b>: {esc(', '.join(words)) or '—'}")
    out.append(f"🚫 <b>Стоп-слова</b>: {esc(', '.join(cfg['stop'])) or '—'}")
    text = "\n\n".join(out)
    if len(text) >= 4000:  # лимит Telegram — 4096 символов; режем без тегов, чтобы не сломать разметку
        text = re.sub(r"</?b>", "", text)[:3990] + "…"
    return text


def cmd_status(state, cfg):
    st = state.get("stats", {})
    lines = [f"🕒 Последняя проверка: {esc(state.get('last_check', '—'))} (МСК)",
             f"📨 Прислано сегодня: {st.get('sent', 0)}, проверок сегодня: {st.get('runs', 0)}",
             f"⏸ Пауза: {'да' if cfg['paused'] else 'нет'}"]
    for key in cfg["sites"]:
        if key in SITES:
            name = SITES[key][0]
            fails = state["fails"].get(key, 0)
            lines.append(f"{'✅' if fails == 0 else '⚠️'} {name}: "
                         + ("работает" if fails == 0 else f"не отвечает {fails} проверок подряд"))
    return "\n".join(lines)


def split_words(arg):
    return [w.strip() for w in re.split(r"[,;\n]", arg) if w.strip()]


def run_command(text, state):
    """Возвращает (ответ, менялись ли настройки)."""
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    cfg = load_settings()
    if cmd in ("/start", "/help"):
        return HELP, False
    if cmd == "/list":
        return cmd_list(cfg), False
    if cmd == "/status":
        return cmd_status(state, cfg), False
    if cmd == "/add":
        words = split_words(arg)
        if not words:
            return "Напиши слово после команды, например: /add figma", False
        new = add_words(MY_WORDS, words)
        return (f"✅ Добавил в «{MY_WORDS}»: {esc(', '.join(new))}" if new
                else "Эти слова уже есть."), bool(new)
    if cmd == "/del":
        words = split_words(arg)
        if not words:
            return "Напиши слово после команды, например: /del сайт", False
        removed = remove_words(words)
        return (f"🗑 Убрал: {esc(', '.join(removed))}" if removed
                else "Таких слов не нашёл. Список — /list"), bool(removed)
    if cmd == "/ban":
        words = split_words(arg)
        if not words:
            return "Напиши слово после команды, например: /ban курсовая", False
        new = add_words(STOP_SECTION, words)
        return (f"🚫 Теперь не присылаю заказы со словами: {esc(', '.join(new))}" if new
                else "Эти стоп-слова уже есть."), bool(new)
    if cmd == "/budget":
        digits = re.sub(r"\D", "", arg)
        if not digits:
            return "Напиши сумму, например: /budget 3000 (или /budget 0 — присылать все)", False
        set_setting("мин_бюджет", int(digits))
        return (f"💰 Ок, заказы дешевле {sources.rub(int(digits))} больше не присылаю "
                "(заказы без указанного бюджета — присылаю)." if int(digits)
                else "💰 Ок, присылаю заказы с любым бюджетом."), True
    if cmd in ("/pause", "/stop"):
        set_setting("пауза", "да")
        return "⏸ Пауза. Сайты проверяю, но молчу. Включить — /resume", True
    if cmd == "/resume":
        set_setting("пауза", "нет")
        return "▶️ Снова присылаю заказы.", True
    return "Не знаю такой команды 🤔 Список — /help", False


def handle_commands(state, chat_id):
    res = tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates=["message"])
    for upd in res.get("result", []):
        state["offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        cid = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if cid != chat_id:
            send(cid, "Это личный бот, он работает только для своего владельца 🙂")
            continue
        try:
            reply, _ = run_command(text, state) if text.startswith("/") else (
                "Я понимаю только команды 🙂 Список — /help", False)
        except Exception as e:
            log("Ошибка команды:", repr(e))
            reply = "Не получилось выполнить команду 😕"
        send(chat_id, reply)


# ======================================================== проверка сайтов

def check_sites(cfg, state, notify):
    """Возвращает список новых подходящих заказов [(order, found)]."""
    first_run = not state.get("started")
    result = []
    for key in cfg["sites"]:
        if key not in SITES:
            log(f"Неизвестный сайт в настройках: {key}")
            continue
        name, _, fn = SITES[key]
        try:
            orders = fn()
            if not orders:
                raise RuntimeError("не найдено ни одного заказа (возможно, сайт изменил разметку)")
        except Exception as e:
            fails = state["fails"].get(key, 0) + 1
            state["fails"][key] = fails
            log(f"{name}: ошибка #{fails}: {e!r}")
            if fails == FAIL_ALERT:
                notify(f"⚠️ <b>{name}</b>: не получается прочитать заказы уже около часа. "
                       "Возможно, сайт недоступен или изменился. Остальные сайты проверяю как обычно.")
            continue
        if state["fails"].get(key, 0) >= FAIL_ALERT:
            notify(f"✅ <b>{name}</b> снова работает.")
        state["fails"][key] = 0

        seen = state["seen"].setdefault(key, [])
        seen_set = set(seen)
        silent = not seen and not first_run  # новый сайт в списке — просто запоминаем текущие
        fresh = 0
        for o in orders:
            if o["id"] in seen_set:
                continue
            seen.append(o["id"])
            seen_set.add(o["id"])
            fresh += 1
            if silent:
                continue
            found = match(o, cfg)
            if found:
                result.append((o, found))
        state["seen"][key] = seen[-KEEP_SEEN:]
        log(f"{name}: заказов {len(orders)}, новых {fresh}")
    result.sort(key=lambda x: x[0]["ts"])
    return result


def load_dotenv():
    """Для запуска на своём компьютере: TELEGRAM_TOKEN=... и TELEGRAM_CHAT_ID=... в файле .env рядом."""
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                key, sep, val = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    os.environ.setdefault(key.strip(), val.strip().strip('"'))


def main():
    load_dotenv()
    args = sys.argv[1:]
    if "--chat-id" in args:
        res = tg("getUpdates", timeout=0)
        if not res.get("ok"):
            log("Не получилось. Проверь TELEGRAM_TOKEN:", res.get("description", res))
        for upd in res.get("result", []):
            chat = (upd.get("message") or {}).get("chat") or {}
            log(f"chat id: {chat.get('id')}  ({chat.get('first_name', '')} @{chat.get('username', '')})")
        if res.get("ok") and not res.get("result"):
            log("Сообщений нет. Напиши боту /start в Telegram и запусти ещё раз.")
        return

    if "--dry" in args:
        cfg = load_settings()
        state = {"seen": {}, "fails": {}, "started": True}
        for key in cfg["sites"]:
            state["seen"][key] = ["-"]  # чтобы не было «тихого» режима
        found = check_sites(cfg, state, notify=lambda t: log("[уведомление]", t))
        for o, f in found:
            log(f"\n[{SITES[o['src']][0]}] {o['budget_text']} | {o['title']}\n   {o['url']}\n   "
                + "; ".join(f"{t}: {w}" for t, w in f))
        log(f"\nВсего подходит: {len(found)}")
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not os.environ.get("TELEGRAM_TOKEN") or not chat_id:
        sys.exit("Нет TELEGRAM_TOKEN или TELEGRAM_CHAT_ID (секреты репозитория на GitHub).")

    state = load_state()
    try:
        handle_commands(state, chat_id)
        cfg = load_settings()
        notify = lambda text: send(chat_id, text)
        first_run = not state.get("started")
        found = check_sites(cfg, state, notify)

        today = now_msk().strftime("%Y-%m-%d")
        stats = state["stats"] if state["stats"].get("date") == today else {"date": today, "sent": 0, "runs": 0}
        stats["runs"] += 1

        if first_run:
            names = ", ".join(SITES[k][0] for k in cfg["sites"] if k in SITES)
            where = "в облаке GitHub" if os.environ.get("GITHUB_ACTIONS") else "на этом компьютере"
            # FIRST_SAMPLES (переменная репозитория) — сколько заказов прислать для примера при первом запуске
            samples = sources.to_int(os.environ.get("FIRST_SAMPLES") or FIRST_RUN_SAMPLES) or 0
            samples = min(len(found), max(samples, 0))
            notify(f"👋 Бот запущен {where}! Слежу за сайтами: {names}, проверяю каждые ~10 минут.\n"
                   + (f"Вот {samples} последних подходящих заказов для примера, дальше — только новые. "
                      if samples else "Буду присылать новые подходящие заказы. ")
                   + "Команды — /help")
            found = found[-samples:] if samples else []

        if cfg["paused"]:
            log(f"Пауза: не отправляю {len(found)} заказов")
            found = []
        skipped = max(0, len(found) - MAX_SEND)
        if skipped:
            notify(f"📦 Новых заказов очень много — присылаю последние {MAX_SEND}, ещё {skipped} пропускаю.")
            found = found[-MAX_SEND:]
        for o, f in found:
            if send(chat_id, format_order(o, f), o["url"]):
                stats["sent"] += 1
        log(f"Отправлено: {len(found)}")

        state["stats"] = stats
        state["started"] = True
        state["last_check"] = now_msk().strftime("%d.%m.%Y %H:%M")
    except Exception:
        traceback.print_exc()
        raise
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
