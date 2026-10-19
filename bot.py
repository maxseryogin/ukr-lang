"""Private two-person Telegram learning bot. Python 3.11+."""
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("learning_bot")


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class APIError(Exception):
    def __init__(self, code, retry_after=30):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API error {code}")


class Telegram:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **data):
        request = Request(self.url + method, json.dumps(data).encode(),
                          {"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=25) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.loads(error.read())
            except (ValueError, OSError):
                raise APIError(error.code) from None
        except (URLError, TimeoutError, OSError):
            raise APIError(0) from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0),
                           result.get("parameters", {}).get("retry_after", 30))
        return result["result"]


def keyboard(rows):
    return {"inline_keyboard": [[{"text": text, "callback_data": data}
                                 for text, data in row] for row in rows]}


def actions(lesson_id):
    return keyboard([[("✅ Точно прошёл тему", f"done:{lesson_id}")],
                     [("⏳ Ещё не прошёл", f"later:{lesson_id}")],
                     [("➡️ Запросить следующую тему", f"next:{lesson_id}")]])


class Bot:
    def __init__(self, api, db_path, student=1374449699, teacher=1252270301,
                 timezone="Europe/Kyiv", start=17, end=22):
        self.api, self.student, self.teacher = api, student, teacher
        self.tz, self.start, self.end = ZoneInfo(timezone), start, end
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS lessons(
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
            rating INTEGER, requested INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS materials(
            id INTEGER PRIMARY KEY, lesson INTEGER, method TEXT, payload TEXT);
          CREATE TABLE IF NOT EXISTS reminders(
            slot TEXT PRIMARY KEY, lesson INTEGER);
          CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY, recipient INTEGER, method TEXT, payload TEXT,
            due REAL DEFAULT 0, expires REAL, tag TEXT, attempts INTEGER DEFAULT 0);
        """)

    def row(self, state):
        return self.db.execute("SELECT * FROM lessons WHERE state=? ORDER BY id DESC LIMIT 1",
                               (state,)).fetchone()

    def queue(self, recipient, method="sendMessage", expires=None, tag=None, **payload):
        self.db.execute("INSERT INTO outbox(recipient,method,payload,expires,tag) VALUES(?,?,?,?,?)",
                        (recipient, method, json.dumps(payload), expires, tag))

    def say(self, recipient, text, **kwargs):
        self.queue(recipient, text=text, **kwargs)

    def process(self, update):
        """Effects and update offset commit together; replayed updates do nothing."""
        update_id = update["update_id"]
        with self.db:
            saved = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            if saved and update_id < int(saved[0]):
                return
            if "callback_query" in update:
                self.callback(update["callback_query"])
            elif "message" in update:
                self.message(update["message"])
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(update_id + 1),))

    def message(self, message):
        uid = message.get("from", {}).get("id")
        if uid not in (self.student, self.teacher) or message.get("chat", {}).get("type") != "private":
            return
        text = message.get("text", "")
        command = text.split(maxsplit=1)[0].split("@")[0] if text else ""
        if command == "/start":
            self.db.execute("INSERT OR IGNORE INTO users VALUES(?)", (uid,))
            self.say(uid, "Бот подключён. Напоминания: пятница и суббота, "
                     f"{self.start}:00–{self.end}:00 ({self.tz.key}).\n" + self.help(uid))
            if uid == self.student:
                self.say(self.teacher, "Ученик подключился к боту.")
            return
        if command == "/help":
            self.say(uid, self.help(uid))
        elif command == "/status":
            active = self.row("active")
            draft = self.row("draft")
            latest = self.db.execute("SELECT * FROM lessons WHERE state='done' ORDER BY id DESC LIMIT 1").fetchone()
            info = f"Текущая тема: {active['title']}" if active else "Непройденных тем нет."
            if latest:
                info += f"\nПоследняя пройденная: {latest['title']}. Оценка: {latest['rating'] or 'ещё нет'}/5."
            if uid == self.teacher and draft:
                count = self.db.execute("SELECT COUNT(*) FROM materials WHERE lesson=?", (draft["id"],)).fetchone()[0]
                info += f"\nЧерновик: {draft['title']} ({count} материалов)."
            self.say(uid, info, **({"reply_markup": actions(active["id"])} if active and uid == self.student else {}))
        elif uid == self.teacher:
            self.teacher_message(message, command, text)
        elif command == "/next":
            latest = self.db.execute("SELECT * FROM lessons WHERE state IN ('active','done') ORDER BY id DESC LIMIT 1").fetchone()
            if latest:
                self.request_next(latest)
            else:
                # Persist a single initial request until a lesson is published.
                exists = self.db.execute("SELECT 1 FROM meta WHERE key='initial_request'").fetchone()
                if not exists:
                    self.db.execute("INSERT INTO meta VALUES('initial_request','1')")
                    self.say(self.teacher, "Ученик просит первую тему. Создай её через /new Название.")
                self.say(uid, "Запрос на первую тему отправлен.")
        else:
            self.say(uid, "Завершение темы подтверждается кнопкой «Точно прошёл тему» в /status.\n" + self.help(uid))

    def help(self, uid):
        if uid == self.teacher:
            return ("/new Название — создать тему\nОтправь PDF, сообщение со ссылкой и голосовое.\n"
                    "/publish — отправить тему ученику\n/cancel — удалить черновик\n/status — состояние\n/help — помощь")
        return "/status — текущая тема и кнопки\n/next — запросить следующую тему\n/help — помощь"

    def teacher_message(self, message, command, text):
        draft = self.row("draft")
        if command == "/new":
            title = text.partition(" ")[2].strip()
            if draft:
                self.say(self.teacher, "Черновик уже есть. Добавь материалы, выполни /publish или /cancel.")
            elif not title or len(title) > 200:
                self.say(self.teacher, "Напиши /new Название темы (до 200 символов).")
            else:
                self.db.execute("INSERT INTO lessons(title,state) VALUES(?,'draft')", (title,))
                self.say(self.teacher, "Тема создана. Пришли PDF, ссылку и голосовое; затем /publish.")
            return
        if command == "/cancel":
            if draft:
                self.db.execute("DELETE FROM materials WHERE lesson=?", (draft["id"],))
                self.db.execute("DELETE FROM lessons WHERE id=?", (draft["id"],))
            self.say(self.teacher, "Черновика больше нет.")
            return
        if command == "/publish":
            if not draft:
                self.say(self.teacher, "Сначала /new Название темы.")
                return
            materials = self.db.execute("SELECT * FROM materials WHERE lesson=? ORDER BY id", (draft["id"],)).fetchall()
            if self.row("active"):
                self.say(self.teacher, "Ученик ещё не подтвердил текущую тему. Черновик сохранён.")
            elif not materials:
                self.say(self.teacher, "Добавь хотя бы один материал.")
            else:
                self.db.execute("UPDATE lessons SET state='active' WHERE id=?", (draft["id"],))
                self.db.execute("DELETE FROM meta WHERE key='initial_request'")
                self.say(self.student, f"📚 Новая тема: {draft['title']}")
                for material in materials:
                    self.queue(self.student, material["method"], **json.loads(material["payload"]))
                self.say(self.student, "Изучи презентацию, выполни практику и послушай голосовое. Когда закончишь — подтверди:",
                         reply_markup=actions(draft["id"]))
                self.say(self.teacher, "Тема поставлена в очередь отправки. /status покажет прогресс ученика.")
            return
        if command.startswith("/"):
            self.say(self.teacher, self.help(self.teacher))
            return
        if not draft:
            self.say(self.teacher, "Для материалов сначала создай тему: /new Название.")
            return
        payload = {}
        if "document" in message:
            doc = message["document"]
            if not (doc.get("file_name", "").lower().endswith(".pdf") or doc.get("mime_type") == "application/pdf"):
                self.say(self.teacher, "Пришли презентацию как PDF-файл.")
                return
            method, payload = "sendDocument", {"document": doc["file_id"]}
        elif "voice" in message:
            method, payload = "sendVoice", {"voice": message["voice"]["file_id"]}
        elif text:
            method, payload = "sendMessage", {"text": text, "entities": message.get("entities", [])}
        else:
            self.say(self.teacher, "Поддерживаются PDF, текст со ссылкой и голосовые сообщения.")
            return
        if message.get("caption"):
            payload.update(caption=message["caption"], caption_entities=message.get("caption_entities", []))
        self.db.execute("INSERT INTO materials(lesson,method,payload) VALUES(?,?,?)",
                        (draft["id"], method, json.dumps(payload)))
        self.say(self.teacher, "Материал добавлен. Пришли следующий или /publish.")

    def request_next(self, lesson):
        if not lesson["requested"]:
            self.db.execute("UPDATE lessons SET requested=1 WHERE id=?", (lesson["id"],))
            self.say(self.teacher, f"➡️ Ученик просит следующую тему после «{lesson['title']}». Пришли её, когда сможешь.")
        self.say(self.student, "Запрос следующей темы отправлен. Материалы придут, когда будут готовы.")

    def callback(self, query):
        uid = query.get("from", {}).get("id")
        msg = query.get("message", {})
        if uid != self.student or msg.get("chat", {}).get("id") != self.student:
            return
        try:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
        except APIError:
            pass  # The business action remains valid even if the button spinner expired.
        parts = query.get("data", "").split(":")
        if len(parts) not in (2, 3) or not parts[1].isdigit():
            return
        action, lesson_id = parts[:2]
        lesson = self.db.execute("SELECT * FROM lessons WHERE id=? AND state IN ('active','done')", (int(lesson_id),)).fetchone()
        if not lesson:
            return
        if action == "done":
            if lesson["state"] == "active":
                self.db.execute("UPDATE lessons SET state='done' WHERE id=?", (lesson["id"],))
                self.db.execute("DELETE FROM outbox WHERE tag=?", (f"reminder:{lesson['id']}",))
                self.say(self.teacher, f"✅ Ученик подтвердил, что прошёл тему «{lesson['title']}».")
            self.say(uid, "Тема пройдена! Оцени занятие:", reply_markup=keyboard([
                [(f"{n} ⭐", f"rate:{lesson_id}:{n}") for n in range(1, 6)],
                [("➡️ Запросить следующую тему", f"next:{lesson_id}")]]))
        elif action == "later" and lesson["state"] == "active":
            self.say(uid, "Хорошо. Напомню в следующий час по расписанию.")
        elif action == "next":
            latest = self.db.execute("SELECT MAX(id) FROM lessons WHERE state IN ('active','done')").fetchone()[0]
            if lesson["id"] == latest:
                self.request_next(lesson)
            else:
                self.say(uid, "Новая тема уже есть. Открой /status.")
        elif action == "rate" and len(parts) == 3 and parts[2] in "12345" and len(parts[2]) == 1:
            if lesson["state"] != "done":
                return
            if lesson["rating"] is None:
                rating = int(parts[2])
                self.db.execute("UPDATE lessons SET rating=? WHERE id=?", (rating, lesson["id"]))
                self.say(self.teacher, f"Оценка темы «{lesson['title']}»: {'⭐' * rating} ({rating}/5).")
                self.say(uid, "Спасибо, оценка отправлена!", reply_markup=keyboard([
                    [("➡️ Запросить следующую тему", f"next:{lesson_id}")]]))
            else:
                self.say(uid, f"Оценка уже сохранена: {lesson['rating']}/5.")

    def tick(self, now=None):
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        # Only the exact scheduled minute; downtime does not generate old reminders.
        if now.weekday() not in (4, 5) or not self.start <= now.hour <= self.end or now.minute != 0:
            return
        lesson = self.row("active")
        if not lesson:
            return
        slot = now.strftime("%Y-%m-%dT%H")
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO reminders VALUES(?,?)", (slot, lesson["id"])).rowcount
            if inserted:
                self.say(self.student, f"📖 Ты уже прошёл тему «{lesson['title']}»?",
                         reply_markup=actions(lesson["id"]), expires=now.timestamp() + 300,
                         tag=f"reminder:{lesson['id']}")

    def flush(self):
        """One message per iteration, preserving order independently per recipient."""
        now = time.time()
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE expires IS NOT NULL AND expires<?", (now,))
            row = self.db.execute("""SELECT * FROM outbox o WHERE due<=? AND NOT EXISTS
                (SELECT 1 FROM outbox p WHERE p.recipient=o.recipient AND p.id<o.id)
                ORDER BY id LIMIT 1""", (now,)).fetchone()
            if not row:
                return
            try:
                self.api.call(row["method"], chat_id=row["recipient"], **json.loads(row["payload"]))
            except APIError as error:
                attempts = row["attempts"] + 1
                delay = max(error.retry_after, min(300, 2 ** min(attempts, 8)))
                LOG.warning("Delivery %s failed: code=%s; retry in %ss", row["id"], error.code, delay)
                self.db.execute("UPDATE outbox SET due=?,attempts=? WHERE id=?", (now + delay, attempts, row["id"]))
                if attempts == 1 and row["recipient"] == self.student:
                    self.say(self.teacher, "Не удалось доставить сообщение ученику. Проверь, что он нажал /start и не заблокировал бота. Повторю отправку автоматически.")
            else:
                self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))

    def run(self):
        self.api.call("getMe")
        # Do not silently discard pending updates or remove an existing deployment's webhook.
        info = self.api.call("getWebhookInfo")
        if info.get("url"):
            raise RuntimeError("У бота включён webhook. Отключи прежнее развёртывание перед запуском polling.")
        LOG.info("Bot running; timezone=%s", self.tz.key)
        while True:
            self.tick()
            self.flush()
            saved = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            pending = self.db.execute("SELECT 1 FROM outbox WHERE due<=? LIMIT 1", (time.time(),)).fetchone()
            try:
                updates = self.api.call("getUpdates", offset=int(saved[0]) if saved else 0,
                                        timeout=1 if pending else 10, allowed_updates=["message", "callback_query"])
                for update in updates:
                    self.process(update)
            except APIError as error:
                LOG.warning("Polling failed: code=%s", error.code)
                if error.code in (401, 409):
                    raise RuntimeError("Проверь токен и убедись, что запущен только один экземпляр бота.") from None
                time.sleep(max(3, min(error.retry_after, 60)))
            time.sleep(1)


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "")
    if not token or token == "PASTE_TOKEN_HERE":
        raise SystemExit("Вставь токен от @BotFather в BOT_TOKEN файла .env.")
    start, end = int(os.getenv("START_HOUR", "17")), int(os.getenv("END_HOUR", "22"))
    if not 0 <= start <= end <= 23:
        raise SystemExit("Часы должны удовлетворять 0 <= START_HOUR <= END_HOUR <= 23.")
    db_path = Path(os.getenv("DB_PATH", str(ROOT / "data" / "bot.sqlite3")))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    bot = Bot(Telegram(token), db_path, int(os.getenv("STUDENT_ID", "1374449699")),
              int(os.getenv("TEACHER_ID", "1252270301")), os.getenv("TIMEZONE", "Europe/Kyiv"), start, end)
    try:
        bot.run()
    except KeyboardInterrupt:
        LOG.info("Stopped")
    except (APIError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
    finally:
        bot.db.close()


if __name__ == "__main__":
    main()
