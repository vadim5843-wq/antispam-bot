# -*- coding: utf-8 -*-
"""
Антиспам-бот для Telegram-группы и группы комментариев канала.

Что делает:
  - удаляет сообщения со ссылками и рекламными словами от обычных участников;
  - банит новичков, которые сразу после входа начинают кидать ссылки/рекламу;
  - мьютит флудеров;
  - удаляет служебные сообщения «X вступил в группу»;
  - по команде /antispam показывает статистику (для админов).
"""
import asyncio
import base64
import json
import logging
import logging.handlers
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ----------------------------- НАСТРОЙКИ -----------------------------
DELETE_BAD_LINKS = True          # удалять ссылки на НЕдоверенные сайты (см. TRUSTED_DOMAINS)
DELETE_JOIN_MESSAGES = True      # удалять «X вступил в группу» / «X вышел»
MAT_FILTER = False               # удалять мат (сейчас выключено — чистим только спам)
MAT_MUTE_AFTER = 5               # после N удалённых матерных сообщений — мьют
MAT_MUTE_MINUTES = 10
NEW_MEMBER_WATCH_SECONDS = 300   # новичок считается «под наблюдением» 5 минут
BAN_NEWBIE_SPAMMER = True        # банить новичка за спам (иначе — просто удалять)
MUTE_NEW_MEMBERS_MINUTES = 0     # >0 — новички не могут писать N минут после входа
FLOOD_MAX_MESSAGES = 6           # не больше N сообщений...
FLOOD_WINDOW_SECONDS = 10        # ...за столько секунд, иначе мьют
FLOOD_MUTE_MINUTES = 15          # длительность мьюта за флуд
NOTIFY_SECONDS = 12              # через сколько удалять уведомления бота

# Ссылки на эти сайты НЕ считаются спамом (можно дополнять)
TRUSTED_DOMAINS = (
    "youtube.com", "youtu.be", "vk.com", "yandex.ru", "ya.ru", "google.com",
    "rutube.ru", "dzen.ru", "ok.ru", "wikipedia.org", "2gis.ru", "avito.ru",
    "wildberries.ru", "ozon.ru", "github.com", "hh.ru",
)

# Жёсткие стоп-фразы — удаляются сразу. Собраны по открытым базам спама
# (tg-spam / ClubDoorman) и по анализу реальных спам-сообщений.
SPAM_PHRASES = [
    # «пиши в личку» — главный маркер спама
    "пиши в лс", "пишите в лс", "писать в лс", "жду в лс", "в личку", "в личные",
    "личных сообщениях", "детали в лс", "подробности в лс", "подробности в личк",
    # заработок-лохотрон
    "заработок в интернете", "заработок в сети", "заработок на дому",
    "удаленный заработок", "удаленного заработка", "заработок без вложений",
    "быстрый заработок", "легкий заработок", "без вложений",
    "пассивный доход", "дополнительный доход", "высокий доход", "стабильный доход",
    "доход от ", "доходность", "ищу партнеров", "ищем партнеров",
    "партнерская программа", "сетевой маркетинг", "бизнес-предложение", "бизнес предложение",
    # крипта / инвестиции
    "usdt", "tether", "airdrop", "раздача крипты", "биткоин", "bitcoin",
    "трейдинг", "trading", "investment", "инвестиц",
    # ставки и казино
    "1xbet", "фрибет", "букмекер", "прогнозы на спорт", "прогноз на матч",
    "тотал больше", "тотал меньше", "точный счет", "бесплатный прогноз", "ставк",
    "казино", "casino", "вулкан",
    # займы и мошенничество
    "микрозайм", "займ без отказа", "кредит без отказа", "деньги в долг",
    "возврат средств", "chargeback", "обналич", "закладчик", "ищем дропов", "нужны дропы",
    # интим
    "onlyfans", "онлифанс", "интим", "индивидуалк", "проститутк", "вебкам", "webcam",
    "девочки ждут", "горячие фото",
    # услуги-спам
    "диплом на заказ", "курсовая на заказ", "курсовые на заказ", "реферат на заказ",
    "накрутка", "подписчиков бесплатно", "бесплатные деньги",
]

# Фейковые вакансии: срабатывает только СВЯЗКА «работа» + «крючок» (ЛС/сумма),
# чтобы не удалять обычные разговоры про работу.
WORK_WORDS = (
    "работ", "подработк", "ваканси", "удаленн", "заработ", "зарабат", "доход",
    "требу", "ищем", "набор сотрудников", "без опыта",
)
HOOK_WORDS = (
    "в лс", "личк", "личные сообщения", "в телеграм", "в телегу",
    "ватсап", "вотсап", "whatsapp", "@",
)
MONEY_RE = re.compile(
    r"\d[\d\s]*\s*(?:р\.|руб|₽|\$|€|доллар|бакс)|рублей\s*в\s*(?:день|неделю|час)",
    re.IGNORECASE,
)
SPACED_LETTERS_RE = re.compile(r"(?:\b[^\s]\s){6,}")  # «з а р а б о т о к» — обход фильтров

DOMAIN_RE = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?:/[^\s]*)?", re.IGNORECASE)
_MAT_PREFIX = r"(?:а|на|по|про|пре|пере|о|об|обо|за|у|вы|с|съ|со|рас|раз|до|недо|под|при|от|отъ|из|вз|воз)?"
MAT_RE = re.compile(
    r"\b" + _MAT_PREFIX +
    r"(?:ху[йёяеи]\w*|хули\b|п[иеё]зд\w*|еб[аеёиу]\w*|ёб\w*|бля\w*|сук[аио]\w*|"
    r"муда\w*|залуп\w*|шлюх\w*|пид[оа]р\w*|гандон\w*|дроч\w*|манда(?:вош\w*)?\b)\b",
    re.IGNORECASE,
)

# Латинские двойники кириллицы («зapaбoтoк») и цифры-замены приводим к кириллице,
# чтобы спамеры не могли обойти фильтр подменой букв.
_HOMOGLYPHS = str.maketrans({
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
    "h": "н", "k": "к", "m": "м", "t": "т", "b": "в", "n": "п", "r": "г",
    "u": "и", "d": "д", "l": "л", "i": "і", "0": "о", "3": "з", "6": "б",
})

# (?<!\w) — фраза должна начинаться с границы слова, чтобы «доставка»
# не ловилась по «ставк», «указ» по «каз» и т.п.
_SPAM_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(k) for k in sorted(SPAM_PHRASES, key=len, reverse=True)) + ")"
)


def normalize_text(text: str) -> str:
    return text.lower().translate(_HOMOGLYPHS).replace("ё", "е")


# ----------------------------- ОБУЧЕНИЕ -----------------------------
# Админ отвечает на спам командой /spam — бот удаляет сообщение, банит автора
# и навсегда запоминает текст. /ham — откат (ответом на сообщение).
# Запомненные фразы хранятся в learned.json и дублируются в GitHub-репозиторий,
# чтобы переживать перезапуски хостинга.
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "")
LEARNED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learned.json")
learned_phrases: list = []


def load_learned() -> None:
    global learned_phrases
    try:
        if os.path.exists(LEARNED_FILE):
            with open(LEARNED_FILE, encoding="utf-8") as f:
                learned_phrases = json.load(f)
    except Exception as exc:
        log.warning("Не смог прочитать learned.json: %s", exc)
    if GH_TOKEN and GH_REPO:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{GH_REPO}/contents/learned.json",
                headers={"Authorization": f"Bearer {GH_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.load(r)
            learned_phrases = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            log.info("Загружено %d обученных фраз с GitHub", len(learned_phrases))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.warning("GitHub load: %s", e)
        except Exception as exc:
            log.warning("GitHub load: %s", exc)


def save_learned() -> None:
    try:
        with open(LEARNED_FILE, "w", encoding="utf-8") as f:
            json.dump(learned_phrases, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        log.warning("Не смог сохранить learned.json: %s", exc)
    if not (GH_TOKEN and GH_REPO):
        return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/learned.json"
        headers = {"Authorization": f"Bearer {GH_TOKEN}"}
        sha = None
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
                sha = json.load(r).get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        payload = {
            "message": "learn: update spam phrases",
            "content": base64.b64encode(
                json.dumps(learned_phrases, ensure_ascii=False, indent=1).encode()
            ).decode(),
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(url, method="PUT", headers=headers,
                                     data=json.dumps(payload).encode())
        urllib.request.urlopen(req, timeout=15).read()
        log.info("Обученные фразы сохранены на GitHub")
    except Exception as exc:
        log.warning("GitHub save: %s", exc)

MUTE_PERMS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
# Дублируем лог в файл, чтобы смотреть его при работе через планировщик (без консоли)
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
    maxBytes=2_000_000, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(_file_handler)
log = logging.getLogger("antispam")

join_times = {}                  # (chat_id, user_id) -> время входа
msg_times = defaultdict(list)    # (chat_id, user_id) -> [время сообщений]
mat_counts = defaultdict(int)    # (chat_id, user_id) -> сколько раз удалён мат
deleted_stats = defaultdict(int) # chat_id -> сколько спама удалено
# ----------------------------------------------------------------------


async def is_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def _is_trusted(domain: str) -> bool:
    return any(domain == t or domain.endswith("." + t) for t in TRUSTED_DOMAINS)


def has_bad_link(msg) -> bool:
    """True, если в сообщении есть ссылка на сайт не из TRUSTED_DOMAINS."""
    domains = []
    text = msg.text or msg.caption or ""
    for ent in (msg.entities or []) + (msg.caption_entities or []):
        if ent.type == "text_link" and ent.url:
            m = DOMAIN_RE.search(ent.url)
            if m:
                domains.append(m.group(1).lower())
    domains.extend(m.group(1).lower() for m in DOMAIN_RE.finditer(text))
    if not domains:
        return False
    return any(not _is_trusted(d) for d in domains)


def has_mat(msg) -> bool:
    return bool(MAT_RE.search(msg.text or msg.caption or ""))


def looks_like_spam(msg) -> bool:
    raw = msg.text or msg.caption or ""
    low = raw.lower().replace("ё", "е")
    norm = normalize_text(raw)

    if _SPAM_RE.search(norm) or _SPAM_RE.search(low):
        return True
    # обученные фразы (полные тексты спама, пойманного ранее)
    for entry in learned_phrases:
        if entry and (entry in norm or (len(norm) > 25 and norm in entry)):
            return True
    if SPACED_LETTERS_RE.search(raw):
        return True
    if DELETE_BAD_LINKS and has_bad_link(msg):
        return True

    # фейковые вакансии: «работа» + крючок (личка/телеграм/сумма)
    if any(w in norm for w in WORK_WORDS):
        if any(h in norm for h in HOOK_WORDS) or MONEY_RE.search(low):
            return True
    return False


def notify_later(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    async def _job():
        try:
            sent = await context.bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, disable_notification=True
            )
            await asyncio.sleep(NOTIFY_SECONDS)
            await sent.delete()
        except Exception:
            pass

    asyncio.create_task(_job())


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    for user in msg.new_chat_members:
        if user.id == context.bot.id:
            continue
        join_times[(msg.chat_id, user.id)] = time.time()
        if MUTE_NEW_MEMBERS_MINUTES > 0:
            try:
                await msg.chat.restrict_member(
                    user.id,
                    MUTE_PERMS,
                    until_date=datetime.now(timezone.utc) + timedelta(minutes=MUTE_NEW_MEMBERS_MINUTES),
                )
            except Exception as exc:
                log.warning("Не удалось замьютить новичка %s: %s", user.id, exc)
    if DELETE_JOIN_MESSAGES:
        try:
            await msg.delete()
        except Exception:
            pass


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    join_times.pop((msg.chat_id, msg.left_chat_member.id), None)
    if DELETE_JOIN_MESSAGES:
        try:
            await msg.delete()
        except Exception:
            pass


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = msg.from_user
    if user is None:  # сообщение от имени канала / анонимного админа
        return
    if user.id == context.bot.id:
        return
    if await is_admin(msg.chat_id, user.id, context):
        return

    key = (msg.chat_id, user.id)
    now = time.time()
    log.info(
        "Сообщение: чат=%s (%s) юзер=%s: %s",
        msg.chat_id, msg.chat.title, user.id, (msg.text or msg.caption or "<медиа>")[:60],
    )
    joined_at = join_times.get(key)
    is_newbie = joined_at is not None and (now - joined_at) < NEW_MEMBER_WATCH_SECONDS

    if looks_like_spam(msg):
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("Не удалось удалить сообщение (нужны права админа): %s", exc)
            return
        deleted_stats[msg.chat_id] += 1
        log.info("Удалён спам от %s (%s) в чате %s", user.id, user.username, msg.chat_id)
        if is_newbie and BAN_NEWBIE_SPAMMER:
            try:
                await msg.chat.ban_member(user.id, revoke_messages=True)
                join_times.pop(key, None)
                notify_later(context, msg.chat_id, f"🚫 Спамер {user.mention_html()} забанен.")
            except Exception as exc:
                log.warning("Не удалось забанить %s: %s", user.id, exc)
        return

    if MAT_FILTER and has_mat(msg):
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("Не удалось удалить мат (нужны права админа): %s", exc)
            return
        deleted_stats[msg.chat_id] += 1
        mat_counts[key] += 1
        log.info("Удалён мат от %s (%s) в чате %s", user.id, user.username, msg.chat_id)
        if mat_counts[key] >= MAT_MUTE_AFTER:
            mat_counts[key] = 0
            try:
                await msg.chat.restrict_member(
                    user.id,
                    MUTE_PERMS,
                    until_date=datetime.now(timezone.utc) + timedelta(minutes=MAT_MUTE_MINUTES),
                )
                notify_later(
                    context, msg.chat_id,
                    f"🔇 {user.mention_html()} замьючен на {MAT_MUTE_MINUTES} мин за мат.",
                )
            except Exception as exc:
                log.warning("Не удалось замьютить %s: %s", user.id, exc)
        return

    # антифлуд
    times = msg_times[key]
    times.append(now)
    while times and times[0] < now - FLOOD_WINDOW_SECONDS:
        times.pop(0)
    if len(times) > FLOOD_MAX_MESSAGES:
        times.clear()
        try:
            await msg.chat.restrict_member(
                user.id,
                MUTE_PERMS,
                until_date=datetime.now(timezone.utc) + timedelta(minutes=FLOOD_MUTE_MINUTES),
            )
            await msg.delete()
            notify_later(
                context, msg.chat_id,
                f"🔇 {user.mention_html()} замьючен на {FLOOD_MUTE_MINUTES} мин за флуд.",
            )
        except Exception as exc:
            log.warning("Не удалось замьютить флудера %s: %s", user.id, exc)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or not await is_admin(chat.id, user.id, context):
        return
    await update.message.reply_text(
        f"🛡 Антиспам работает.\n"
        f"Удалено спам-сообщений за сессию: {deleted_stats.get(chat.id, 0)}\n"
        f"Новичков под наблюдением: {sum(1 for (c, _) in join_times if c == chat.id)}"
    )


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if user is None or not await is_admin(msg.chat_id, user.id, context):
        return
    target = msg.reply_to_message
    if target is None:
        await msg.reply_text("Отправь /spam ответом на спам-сообщение.")
        return
    spammer = target.from_user
    text = (target.text or target.caption or "").strip()
    try:
        await target.delete()
    except Exception as exc:
        log.warning("cmd_spam delete: %s", exc)
    banned = False
    if spammer and spammer.id != context.bot.id:
        try:
            await msg.chat.ban_member(spammer.id, revoke_messages=True)
            banned = True
        except Exception as exc:
            log.warning("cmd_spam ban: %s", exc)
    norm = normalize_text(text)
    learned = False
    if len(norm) >= 10 and norm not in learned_phrases:
        learned_phrases.append(norm)
        await asyncio.to_thread(save_learned)
        learned = True
    parts = ["удалил"]
    if banned:
        parts.append("забанил")
    if learned:
        parts.append("запомнил")
    reply = await msg.reply_text("✅ " + ", ".join(parts) + ".")
    log.info("cmd_spam от %s: banned=%s learned=%s", user.id, banned, learned)
    await asyncio.sleep(10)
    for m in (reply, msg):
        try:
            await m.delete()
        except Exception:
            pass


async def cmd_ham(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    user = update.effective_user
    if user is None or not await is_admin(msg.chat_id, user.id, context):
        return
    target = msg.reply_to_message
    removed = False
    if target is not None:
        norm = normalize_text(target.text or target.caption or "")
        if norm in learned_phrases:
            learned_phrases.remove(norm)
            await asyncio.to_thread(save_learned)
            removed = True
        if target.from_user:
            try:
                await msg.chat.unban_member(target.from_user.id, only_if_banned=True)
            except Exception:
                pass
    await msg.reply_text(
        "Ок, убрал из спам-листа и разбанил." if removed
        else "Такого текста нет в спам-листе."
    )


def load_token() -> str:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.txt")
        if os.path.exists(token_file):
            with open(token_file, encoding="utf-8") as f:
                token = f.read().strip()
    if not token:
        raise SystemExit(
            "Токен не найден. Укажите его в переменной окружения BOT_TOKEN "
            "или в файле token.txt рядом с bot.py"
        )
    return token


def start_health_server() -> None:
    """Мини-HTTP-сервер для health-check хостинга (Render, ClawCloud и т.п.).
    Запускается, только если задана переменная окружения PORT."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), Handler).serve_forever(),
        daemon=True,
    ).start()
    log.info("Health-check сервер слушает порт %s", port)


def main() -> None:
    start_health_server()
    app = Application.builder().token(load_token()).build()

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(CommandHandler("antispam", cmd_status, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("spam", cmd_spam, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("ham", cmd_ham, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND, moderate
    ))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE, moderate))

    log.info("Антиспам-бот запущен.")
    load_learned()
    app.run_polling(allowed_updates=["message", "edited_message", "chat_member"])


if __name__ == "__main__":
    main()
