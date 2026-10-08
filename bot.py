"""SILICON Telegram bot. Python standard library only."""
from __future__ import annotations


import ssl
ssl._create_default_https_context = ssl._create_unverified_context


import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE settings without overriding the process environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError:
        logging.getLogger("silicon_bot").exception("Konfiguratsiya faylini o‘qib bo‘lmadi: %s", path)
        return

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key.replace("_", "a").isalnum() or key[0].isdigit():
            logging.getLogger("silicon_bot").warning(".env faylining %s-qatori noto‘g‘ri; tashlab ketildi.", line_number)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


load_dotenv(ROOT / ".env")
DB_PATH = Path(os.getenv("SILICON_DB_PATH") or str(ROOT / "silicon.sqlite3"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("TELEGRAM_ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
LOG = logging.getLogger("silicon_bot")
HOME = "🏠 Bosh menyu"
BACK = "◀️ Orqaga"
ROLE_NAMES = {"admin": "Admin", "teacher": "O‘qituvchi", "student": "O‘quvchi"}
STATUS_NAMES = {
    "active": "🟢 Faol",
    "frozen": "🟡 Muzlatilgan",
    "exited": "🔴 Chiqib ketgan",
    "pending": "📝 Tasdiq kutilmoqda",
    "blocked": "🔴 Bloklangan",
}
ATTENDANCE_NAMES = {
    "present": "🟢 Keldi",
    "late": "🟡 Kechikdi",
    "excused": "🔵 Sababli",
    "absent": "🔴 Kelmagan",
}
WEEKDAYS = ("Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba")
WEEKDAY_KEYS = ("Dush", "Sesh", "Chor", "Pay", "Jum", "Shan", "Yak")


class BotError(Exception):
    """An error returned by Telegram's Bot API."""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                course TEXT NOT NULL DEFAULT '',
                group_id INTEGER,
                joined_at TEXT NOT NULL DEFAULT (datetime('now')),
                payment_due TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(group_id) REFERENCES groups(id)
            );
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                course TEXT NOT NULL DEFAULT '',
                teacher_id INTEGER,
                days TEXT NOT NULL DEFAULT '',
                start_time TEXT NOT NULL DEFAULT '',
                end_time TEXT NOT NULL DEFAULT '',
                room TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                FOREIGN KEY(teacher_id) REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                status TEXT NOT NULL,
                marked_by INTEGER NOT NULL,
                UNIQUE(user_id, group_id, day),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id),
                FOREIGN KEY(group_id) REFERENCES groups(id)
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                amount INTEGER NOT NULL,
                method TEXT NOT NULL,
                paid_at TEXT NOT NULL DEFAULT (datetime('now')),
                created_by INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                target TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_state (
                telegram_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL,
                data TEXT NOT NULL DEFAULT '{}'
            );
            INSERT OR IGNORE INTO settings(key, value) VALUES ('monthly_fee', '500000');
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(users)")}
        if "payment_due" not in columns:
            self.db.execute("ALTER TABLE users ADD COLUMN payment_due TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cursor = self.db.execute(sql, args)
        self.db.commit()
        return cursor

    def one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def state(self, user_id: int) -> tuple[str, dict[str, Any]] | None:
        row = self.one("SELECT state, data FROM bot_state WHERE telegram_id=?", (user_id,))
        if not row:
            return None
        try:
            return row["state"], json.loads(row["data"])
        except (ValueError, TypeError):
            self.clear_state(user_id)
            return None

    def set_state(self, user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        self.execute(
            "INSERT INTO bot_state(telegram_id,state,data) VALUES(?,?,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET state=excluded.state,data=excluded.data",
            (user_id, state, json.dumps(data or {}, ensure_ascii=False)),
        )

    def clear_state(self, user_id: int) -> None:
        self.execute("DELETE FROM bot_state WHERE telegram_id=?", (user_id,))


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ") + " so‘m"


def user_label(row: sqlite3.Row) -> str:
    return row["name"] or row["username"] or str(row["telegram_id"])


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def with_navigation(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    rows.append([("◀️ Orqaga", "back"), (HOME, "home")])
    return keyboard(rows)


class SiliconBot:
    def __init__(self) -> None:
        self.store = Store(DB_PATH)
        self.offset = 0
        self.token = TOKEN
        self.admin_ids = set(ADMIN_IDS)
        self.edit_message: tuple[int, int] | None = None

    def api(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = urllib.parse.urlencode(
            {key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
             for key, value in payload.items()}
        ).encode()
        request = urllib.request.Request(url, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise BotError(f"Telegram API {method} so‘rovi bajarilmadi: {error}") from error
        if not result.get("ok"):
            raise BotError(f"Telegram API {method}: {result.get('description', 'noma’lum xato')}")
        return result["result"]

    def show(
        self,
        chat_id: int,
        text: str,
        markup: dict[str, Any] | None = None,
        *,
        edit: tuple[int, int] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if markup is not None:
            payload["reply_markup"] = markup
        if edit:
            try:
                self.api("editMessageText", {**payload, "message_id": edit[1]})
                return
            except BotError as error:
                if "message is not modified" not in str(error):
                    LOG.warning("Message edit failed, sending a new panel: %s", error)
        self.api("sendMessage", payload)

    def record(self, actor: int, action: str) -> None:
        self.store.execute("INSERT INTO activity(actor_id,action) VALUES(?,?)", (actor, action))

    def person(self, user_id: int) -> sqlite3.Row | None:
        return self.store.one("SELECT * FROM users WHERE telegram_id=?", (user_id,))

    def role(self, user_id: int) -> str:
        if user_id in self.admin_ids:
            return "admin"
        row = self.person(user_id)
        return row["role"] if row and row["status"] == "active" else ""

    def start(self, chat_id: int, actor: dict[str, Any]) -> None:
        user_id = actor["id"]
        self.store.clear_state(user_id)
        if user_id in self.admin_ids:
            self.show(
                chat_id,
                "👋 SILICON botiga xush kelibsiz!\n\nIsh rejimini tanlang:",
                keyboard([[("👨‍💼 Admin", "role:admin"), ("👨‍🎓 O‘quvchi", "role:student")],
                          [("👨‍🏫 O‘qituvchi", "role:teacher")]]),
            )
            return
        role = self.role(user_id)
        if role:
            self.show_home(chat_id, user_id, role)
            return
        row = self.person(user_id)
        if row and row["status"] != "active":
            status_text = (
                "📝 Arizangiz admin tasdig‘ini kutmoqda.\nTasdiqlangach bot orqali xabar beramiz."
                if row["status"] == "pending"
                else f"⛔ Akkaunt holati: {STATUS_NAMES.get(row['status'], row['status'])}.\nAdministrator bilan bog‘laning."
            )
            self.show(chat_id, status_text)
            return
        self.show(
            chat_id,
            "👋 SILICON botiga xush kelibsiz!\n\nDavom etish uchun rolingizni tanlang:",
            keyboard([[("👨‍💼 Admin", "role:admin"), ("👨‍🎓 O‘quvchi", "role:student")],
                      [("👨‍🏫 O‘qituvchi", "role:teacher")]]),
        )

    def admin_home_text(self) -> str:
        return "👨‍💼 SILICON ADMIN PANEL\n\nKerakli bo‘limni tanlang. Ichki oynalarni tugma bosib yangilash mumkin."

    def admin_home_keyboard(self) -> dict[str, Any]:
        return keyboard([
            [("📊 Dashboard", "admin:dashboard")],
            [("👨‍🎓 O‘quvchilar", "admin:students"), ("👨‍🏫 Ustozlar", "admin:teachers")],
            [("👥 Guruhlar", "admin:groups"), ("💰 To‘lovlar", "admin:payments")],
            [("📝 Arizalar", "admin:applications"), ("📊 Hisobotlar", "admin:reports")],
            [("📢 Xabarlar", "admin:messages"), ("🏫 Xonalar", "admin:rooms")],
            [("📜 Faoliyat tarixi", "admin:audit"), ("⚙️ Sozlamalar", "admin:settings")],
        ])

    def teacher_home_keyboard(self) -> dict[str, Any]:
        return keyboard([
            [("📅 Bugungi darslar", "teacher:today")],
            [("👥 Mening guruhlarim", "teacher:groups"), ("👨‍🎓 O‘quvchilar", "teacher:students")],
            [("✅ Davomat", "teacher:attendance"), ("📅 Dars jadvali", "teacher:schedule")],
            [("💰 To‘lov holati", "teacher:payments"), ("📢 Admin bilan aloqa", "teacher:contact")],
            [("👤 Profil", "teacher:profile")],
        ])

    def student_home_keyboard(self) -> dict[str, Any]:
        return keyboard([
            [("👤 Profilim", "student:profile"), ("👥 Guruhim", "student:group")],
            [("✅ Davomatim", "student:attendance"), ("💰 To‘lov holati", "student:payments")],
            [("📢 Admin bilan aloqa", "student:contact")],
        ])

    def show_home(self, chat_id: int, user_id: int, role: str, edit: tuple[int, int] | None = None) -> None:
        if role == "admin":
            self.show(chat_id, self.admin_home_text(), self.admin_home_keyboard(), edit=edit)
        elif role == "teacher":
            self.show(chat_id, "👨‍🏫 SILICON — USTOZ PANELI\n\nBugungi dars va guruhlaringizni boshqaring.", self.teacher_home_keyboard(), edit=edit)
        elif role == "student":
            self.show(chat_id, "👨‍🎓 SILICON — O‘QUVCHI PANELI\n\nShaxsiy ma’lumotlaringiz va darslaringiz.", self.student_home_keyboard(), edit=edit)
        else:
            self.start(chat_id, {"id": user_id})

    def ask(self, chat_id: int, user_id: int, state: str, prompt: str, data: dict[str, Any] | None = None) -> None:
        self.store.set_state(user_id, state, data)
        self.show(chat_id, prompt, keyboard([[("❌ Bekor qilish", "cancel"), (HOME, "home")]]), edit=self.edit_message)

    def callback(self, query: dict[str, Any]) -> None:
        actor = query["from"]
        user_id = actor["id"]
        chat_id = query["message"]["chat"]["id"]
        message_id = query["message"]["message_id"]
        self.edit_message = (chat_id, message_id)
        self.api("answerCallbackQuery", {"callback_query_id": query["id"]})
        data = query.get("data", "")
        if data == "home":
            self.store.clear_state(user_id)
            self.show_home(chat_id, user_id, self.role(user_id), self.edit_message)
        elif data == "back":
            self.store.clear_state(user_id)
            self.show_home(chat_id, user_id, self.role(user_id), self.edit_message)
        elif data == "cancel":
            self.store.clear_state(user_id)
            self.show_home(chat_id, user_id, self.role(user_id), self.edit_message)
        elif data.startswith("role:"):
            self.choose_role(chat_id, user_id, actor, data.split(":", 1)[1])
        elif data.startswith("admin:") and self.role(user_id) == "admin":
            self.admin_action(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("teacher:") and self.role(user_id) == "teacher":
            self.teacher_action(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("student:") and self.role(user_id) == "student":
            self.student_action(chat_id, user_id, data.split(":", 1)[1])
        elif data.startswith("obj:") and self.role(user_id) == "admin":
            _, kind, value = data.split(":", 2)
            self.object_action(chat_id, user_id, kind, value)
        elif data.startswith("att:") and self.role(user_id) == "teacher":
            _, group, person, status = data.split(":", 3)
            self.mark_attendance(chat_id, user_id, int(group), int(person), status)
        elif data.startswith("app:") and self.role(user_id) == "admin":
            _, action, application_id = data.split(":", 2)
            self.application_action(chat_id, user_id, action, int(application_id))
        elif data.startswith("pay:") and self.role(user_id) == "admin":
            _, action, value = data.split(":", 2)
            self.payment_action(chat_id, user_id, action, value)
        elif data.startswith("flow:") and self.role(user_id):
            self.flow_callback(chat_id, user_id, data[5:])
        else:
            self.show(chat_id, "Bu amal uchun ruxsat yo‘q yoki hisobingiz tasdiqlanmagan.", edit=self.edit_message)

    def choose_role(self, chat_id: int, user_id: int, actor: dict[str, Any], role: str) -> None:
        if role == "admin":
            if user_id not in self.admin_ids:
                self.show(chat_id, "⛔ Admin rejimi faqat TELEGRAM_ADMIN_IDS ro‘yxatidagi akkauntlar uchun ochiq.", edit=self.edit_message)
                return
            self.show(chat_id, self.admin_home_text(), self.admin_home_keyboard(), edit=self.edit_message)
            return
        if role not in ("student", "teacher"):
            return
        if user_id in self.admin_ids:
            self.show(chat_id,"Bu Telegram akkaunti admin sifatida ro‘yxatdan o‘tgan. O‘quvchi yoki ustoz test profili uchun alohida Telegram akkauntidan foydalaning.",edit=self.edit_message)
            return
        self.store.set_state(user_id, f"register:{role}:name", {"role": role, "telegram": actor.get("username", "")})
        self.show(chat_id, "Ism-familiyangizni yuboring:", keyboard([[("❌ Bekor qilish", "cancel")]]), edit=self.edit_message)

    def handle_text(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        actor = message["from"]
        user_id = actor["id"]
        text = message.get("text", "").strip()
        if text.startswith("/start"):
            self.start(chat_id, actor)
            return
        if text.startswith("/cancel"):
            self.store.clear_state(user_id)
            self.show_home(chat_id, user_id, self.role(user_id))
            return
        state = self.store.state(user_id)
        if not state:
            if text == HOME:
                self.show_home(chat_id, user_id, self.role(user_id))
            elif not self.role(user_id):
                self.start(chat_id, actor)
            else:
                self.show(chat_id, "Menyudan bo‘lim tanlang yoki /start yuboring.")
            return
        name, data = state
        self.consume_text(chat_id, user_id, text, name, data)

    def consume_text(self, chat_id: int, user_id: int, text: str, state: str, data: dict[str, Any]) -> None:
        if state.startswith("register:") and state.endswith(":name"):
            if len(text) < 3 or len(text) > 80:
                self.show(chat_id, "Ism-familiya 3–80 belgidan iborat bo‘lsin. Qaytadan yuboring.")
                return
            data["name"] = text
            self.ask(chat_id, user_id, state.replace(":name", ":phone"), "Telefon raqamingizni yuboring.\nMasalan: +998901234567", data)
        elif state.startswith("register:") and state.endswith(":phone"):
            phone = "".join(c for c in text if c.isdigit() or c == "+")
            if len(phone) < 9:
                self.show(chat_id, "Telefon raqamini to‘liq yuboring. Masalan: +998901234567")
                return
            data["phone"] = phone
            role = data["role"]
            if role == "student":
                self.ask(chat_id, user_id, "register:course", "Qaysi kursda o‘qimoqchisiz? Kurs nomini yuboring.", data)
            else:
                self.ask(chat_id, user_id, "register:course", "Qaysi yo‘nalishda dars berasiz? Yo‘nalishni yuboring.", data)
        elif state == "register:course":
            if not 2 <= len(text) <= 80:
                self.show(chat_id, "Kurs yoki yo‘nalish nomini yozing.")
                return
            data["course"] = text
            role = data["role"]
            self.store.execute(
                "INSERT INTO users(telegram_id,username,name,phone,role,status,course,payment_due) VALUES(?,?,?,?,?,'pending',?,date('now','localtime','+30 days')) "
                "ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,name=excluded.name,"
                "phone=excluded.phone,role=excluded.role,status='pending',course=excluded.course",
                (user_id, data.get("telegram", ""), data["name"], data["phone"], role, text),
            )
            cursor = self.store.execute(
                "INSERT INTO applications(user_id,kind,details) VALUES(?,?,?)",
                (user_id, role, json.dumps(data, ensure_ascii=False)),
            )
            self.store.clear_state(user_id)
            self.show(chat_id, f"✅ {ROLE_NAMES[role]} sifatida arizangiz yuborildi.\nAdmin tasdig‘idan so‘ng bot sizga xabar beradi.")
            self.notify_admins(
                f"📝 YANGI {ROLE_NAMES[role].upper()} ARIZASI #{cursor.lastrowid}\n"
                f"👤 {data['name']}\n📱 {data['phone']}\n📚 {text}",
                keyboard([[("✅ Qabul qilish", f"app:accept:{cursor.lastrowid}"), ("❌ Rad etish", f"app:reject:{cursor.lastrowid}")]]),
            )
        elif state == "student_search":
            self.show_student_search(chat_id, user_id, text)
        elif state == "teacher_search":
            self.show_teacher_search(chat_id, user_id, text)
        elif state == "group_search":
            self.show_group_search(chat_id, user_id, text)
        elif state == "student_add":
            self.add_student_from_text(chat_id, user_id, text)
        elif state == "teacher_add":
            self.add_teacher_from_text(chat_id, user_id, text)
        elif state == "group_add":
            self.add_group_from_text(chat_id, user_id, text)
        elif state == "room_add":
            self.add_room_from_text(chat_id, user_id, text)
        elif state == "payment_student":
            self.payment_find_student(chat_id, user_id, text)
        elif state == "payment_amount":
            if not text.isdigit() or int(text) <= 0:
                self.show(chat_id, "To‘lov summasini musbat son bilan kiriting. Masalan: 500000")
                return
            data["amount"] = int(text)
            self.ask(chat_id, user_id, "payment_month", "To‘lov qaysi oy uchun? YYYY-MM shaklida yuboring. Masalan: 2026-10", data)
        elif state == "payment_month":
            try:
                datetime.strptime(text, "%Y-%m")
            except ValueError:
                self.show(chat_id, "Oy formatini YYYY-MM ko‘rinishida yuboring. Masalan: 2026-10")
                return
            data["month"] = text
            self.store.set_state(user_id, "payment_method", data)
            self.show(chat_id, f"👤 {data['name']}\n📅 Oy: {text}\n💰 Summa: {money(data['amount'])}\n\nTo‘lov turini tanlang:", keyboard([
                [("💵 Naqd", "pay:method:Naqd"), ("💳 Karta", "pay:method:Karta")],
                [("🏦 Click/Payme", "pay:method:Online")],
                [("❌ Bekor qilish", "cancel"), (HOME, "home")],
            ]))
        elif state == "message_compose":
            data["body"] = text[:3500]
            self.store.set_state(user_id, "message_confirm", data)
            self.show(
                chat_id,
                f"📢 XABAR KO‘RINISHI\n\nKimga: {data['target_label']}\nQabul qiluvchilar: {data['count']}\n\n{text[:3500]}",
                with_navigation([[("✅ Yuborish", "flow:message-send"), ("❌ Bekor qilish", "cancel")]]),
            )
        elif state == "teacher_group_message":
            data["body"]=text[:3000]
            self.store.set_state(user_id,"teacher_group_message_confirm",data)
            self.show(chat_id,f"📢 XABAR KO‘RINISHI\n\nGuruh: {data['group_name']}\nQabul qiluvchilar: {len(data['recipients'])}\n\n{data['body']}",
                with_navigation([[("✅ Yuborish","flow:teacher-message-send"),("❌ Bekor qilish","cancel")]]),edit=self.edit_message)
        elif state == "teacher_group_message_confirm":
            delivered=0
            for recipient in data["recipients"]:
                try:
                    self.api("sendMessage",{"chat_id":recipient,"text":f"📢 {data['group_name']}:\n\n{data['body']}"})
                    delivered+=1
                except BotError as error:
                    LOG.warning("Teacher group message to %s failed: %s",recipient,error)
            self.store.execute("INSERT INTO messages(sender_id,target,body) VALUES(?,?,?)",(user_id,f"group:{data['group_id']}",data["body"]))
            self.record(user_id,f"{data['group_name']} guruhiga xabar yubordi: {delivered} ta")
            self.store.clear_state(user_id)
            self.show(chat_id,f"✅ Xabar yuborildi: {delivered} ta o‘quvchiga.")
        elif state in ("teacher_add_request","teacher_group_request"):
            parts=[part.strip() for part in text.split(",")]
            if state=="teacher_add_request":
                if len(parts)<2 or not parts[1].lstrip("+").isdigit():
                    self.show(chat_id,"Ism va telefonni vergul bilan yuboring:\nAli Valiyev, +998901234567")
                    return
                details={"group_id":data["group_id"],"group_name":data["group_name"],"name":parts[0],"phone":parts[1]}
                kind="teacher_student_add"
                summary=f"👤 {parts[0]}\n📱 {parts[1]}\n👥 Guruh: {data['group_name']}"
            else:
                if len(parts)<6:
                    self.show(chat_id,"Format:\nGuruh nomi, Kurs, Kunlar, Boshlanish, Tugash, Xona")
                    return
                details={"name":parts[0],"course":parts[1],"days":parts[2],"start":parts[3],"end":parts[4],"room":parts[5]}
                kind="teacher_group"
                summary=f"👥 {parts[0]}\n📚 {parts[1]}\n📅 {parts[2]}\n🕐 {parts[3]}–{parts[4]}\n🏫 {parts[5]}"
            cursor=self.store.execute("INSERT INTO applications(user_id,kind,details) VALUES(?,?,?)",(user_id,kind,json.dumps(details,ensure_ascii=False)))
            self.store.clear_state(user_id)
            self.record(user_id,f"Admin tasdig‘iga so‘rov yubordi #{cursor.lastrowid}")
            self.show(chat_id,f"📨 So‘rovingiz admin tasdig‘iga yuborildi.\n\n{summary}")
            self.notify_admins(f"📨 USTOZ SO‘ROVI #{cursor.lastrowid}\n👨‍🏫 {self.name_of(user_id)}\n\n{summary}",
                keyboard([[("✅ Tasdiqlash",f"app:accept:{cursor.lastrowid}"),("❌ Rad etish",f"app:reject:{cursor.lastrowid}")]]))
        elif state == "teacher_remove_reason":
            details={"group_id":data["group_id"],"group_name":data["group_name"],"student_id":data["student_id"],"student_name":data["student_name"],"reason":text[:500]}
            cursor=self.store.execute("INSERT INTO applications(user_id,kind,details) VALUES(?,?,?)",(user_id,"teacher_student_remove",json.dumps(details,ensure_ascii=False)))
            self.store.clear_state(user_id)
            self.record(user_id,f"O‘quvchini guruhdan chiqarish so‘rovi #{cursor.lastrowid}")
            self.show(chat_id,"📨 Guruhdan chiqarish so‘rovi admin tasdig‘iga yuborildi.")
            self.notify_admins(f"📨 O‘QUVCHINI GURUHDAN CHIQARISH #{cursor.lastrowid}\n👨‍🏫 {self.name_of(user_id)}\n👥 {data['group_name']}\n👤 {data['student_name']}\nSabab: {text[:500]}",
                keyboard([[("✅ Tasdiqlash",f"app:accept:{cursor.lastrowid}"),("❌ Rad etish",f"app:reject:{cursor.lastrowid}")]]))
        elif state in ("teacher_contact","student_contact"):
            data["body"] = text[:2000]
            cursor = self.store.execute("INSERT INTO messages(sender_id,target,body) VALUES(?,?,?)", (user_id, "admin", text[:2000]))
            self.record(user_id, f"Adminlarga murojaat yuborildi #{cursor.lastrowid}")
            self.store.clear_state(user_id)
            self.show(chat_id, "✅ Murojaatingiz adminga yuborildi.")
            who="USTOZ" if state=="teacher_contact" else "O‘QUVCHI"
            self.notify_admins(f"📨 {who} MUROJAATI\n{self.name_of(user_id)}\n\n{text[:2000]}")
        elif state == "room_filter":
            self.show_room_schedule(chat_id, user_id, text)
        elif state == "settings_fee":
            if not text.isdigit() or int(text) < 0:
                self.show(chat_id, "Oylik to‘lovni musbat summa bilan yuboring.")
                return
            self.store.execute("UPDATE settings SET value=? WHERE key='monthly_fee'", (text,))
            self.record(user_id, f"Oylik to‘lov miqdorini {text} ga o‘zgartirdi")
            self.store.clear_state(user_id)
            self.show(chat_id, f"✅ Oylik to‘lov {money(int(text))} qilib saqlandi.", self.admin_home_keyboard())
        elif state == "edit-user":
            self.store.execute("UPDATE users SET name=? WHERE telegram_id=?", (text[:80],data["target"]))
            self.record(user_id, f"{data['role']} profilini tahrirladi")
            self.store.clear_state(user_id)
            self.object_action(chat_id,user_id,data["role"],str(data["target"]))
        elif state == "group_edit":
            parts=[p.strip() for p in text.split(",")]
            if len(parts)<4:
                self.show(chat_id,"4 ta qiymat kiriting: Kunlar, Boshlanish, Tugash, Xona")
                return
            self.store.execute("UPDATE groups SET days=?,start_time=?,end_time=?,room=? WHERE id=?",(parts[0],parts[1],parts[2],parts[3],data["group_id"]))
            self.record(user_id,f"Guruh {data['group_id']} jadvalini o‘zgartirdi")
            self.store.clear_state(user_id)
            self.show_group(chat_id,data["group_id"])
        elif state == "payment_search":
            rows=self.store.all("SELECT p.*,u.name FROM payments p JOIN users u ON u.telegram_id=p.user_id WHERE u.name LIKE ? OR u.phone LIKE ? ORDER BY p.id DESC LIMIT 20",(f"%{text}%",f"%{text}%"))
            self.store.clear_state(user_id)
            body="🔎 TO‘LOV QIDIRUVI\n\n"+("\n".join(f"{r['name']} · {r['month']} · {money(r['amount'])} · {r['method']}" for r in rows) if rows else "Mos to‘lov topilmadi.")
            self.show(chat_id,body,with_navigation([]),edit=self.edit_message)
        elif state == "teacher_attendance_note":
            data["day"] = date.today().isoformat()
            self.show_attendance(chat_id, user_id, int(data["group_id"]), data)
        else:
            self.store.clear_state(user_id)
            self.show(chat_id, "Kiritilgan amal topilmadi. Menyuga qaytdim.", self.admin_home_keyboard() if self.role(user_id) == "admin" else None)

    def notify_admins(self, text: str, markup: dict[str, Any] | None = None) -> None:
        for admin_id in self.admin_ids:
            try:
                payload: dict[str, Any] = {"chat_id": admin_id, "text": text}
                if markup:
                    payload["reply_markup"] = markup
                self.api("sendMessage", payload)
            except BotError as error:
                LOG.warning("Could not notify admin %s: %s", admin_id, error)

    def name_of(self, user_id: int) -> str:
        row = self.person(user_id)
        return user_label(row) if row else str(user_id)

    def admin_action(self, chat_id: int, user_id: int, action: str) -> None:
        if action == "dashboard":
            students = self.store.one("SELECT count(*) n FROM users WHERE role='student' AND status='active'")["n"]
            teachers = self.store.one("SELECT count(*) n FROM users WHERE role='teacher' AND status='active'")["n"]
            groups = self.store.one("SELECT count(*) n FROM groups WHERE status='active'")["n"]
            paid = self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE date(paid_at)=date('now','localtime')")["n"]
            fee = int(self.setting("monthly_fee"))
            debtors = self.debtors()
            new_apps = self.store.one("SELECT count(*) n FROM applications WHERE status='pending'")["n"]
            attendance = self.attendance_percent()
            body = (
                "📊 DASHBOARD\n\n"
                f"👨‍🎓 Faol o‘quvchilar: {students}\n👥 Faol guruhlar: {groups}\n"
                f"👨‍🏫 Ustozlar: {teachers}\n\n💰 Bugungi tushum: {money(paid)}\n"
                f"💳 Hisoblangan qarzdorlik: {money(sum(max(fee-r['paid'],0) for r in debtors))}\n"
                f"📝 Yangi arizalar: {new_apps}\n⚠️ Qarzdorlar: {len(debtors)}\n"
                f"✅ Bugungi davomat: {attendance}%\n\n"
                "Statistika bot bazasiga kiritilgan haqiqiy yozuvlardan hisoblanadi."
            )
            rows = [[("💰 Bugungi to‘lovlar", "admin:today_payments"), ("⚠️ Qarzdorlar", "admin:debtors")],
                    [("📝 Yangi arizalar", "admin:applications"), ("📅 Bugungi davomat", "admin:today_attendance")],
                    [("🔄 Yangilash", "admin:dashboard")], [(HOME, "home")]]
            self.show(chat_id, body, keyboard(rows), edit=self.edit_message)
        elif action == "students":
            self.show(chat_id, "👨‍🎓 O‘QUVCHILAR\n\nHolat bo‘yicha tanlang yoki ism/telefon orqali qidiring.",
                      with_navigation([[("🔎 O‘quvchi qidirish", "flow:student-search")],
                          [("🟢 Faol", "admin:students_active"), ("🟡 Muzlatilgan", "admin:students_frozen")],
                          [("🔴 Chiqib ketgan", "admin:students_exited"), ("🟠 Qarzdorlar", "admin:debtors")],
                          [("➕ O‘quvchi qo‘shish", "flow:student-add")]]), edit=self.edit_message)
        elif action.startswith("students_"):
            self.list_people(chat_id, "student", action.removeprefix("students_"))
        elif action == "teachers":
            self.show(chat_id, "👨‍🏫 USTOZLAR", with_navigation([
                [("🔎 Ustoz qidirish", "flow:teacher-search")],
                [("👨‍🏫 Barcha ustozlar", "admin:teachers_all")],
                [("🟢 Faol", "admin:teachers_active"), ("🔴 Faol emas", "admin:teachers_blocked")],
                [("➕ Ustoz qo‘shish", "flow:teacher-add")],
            ]), edit=self.edit_message)
        elif action.startswith("teachers_"):
            self.list_people(chat_id, "teacher", action.removeprefix("teachers_"))
        elif action == "groups":
            self.show(chat_id, "👥 GURUHLAR", with_navigation([
                [("🟢 Faol guruhlar", "admin:groups_active"), ("🔴 Yopilgan", "admin:groups_closed")],
                [("🔎 Guruh qidirish", "flow:group-search")],
                [("➕ Guruh ochish", "flow:group-add")],
                [("📅 Guruhlar jadvali", "admin:schedule")],
            ]), edit=self.edit_message)
        elif action in ("groups_active", "groups_closed"):
            status = "active" if action.endswith("active") else "closed"
            self.list_groups(chat_id, status)
        elif action == "schedule":
            self.list_groups(chat_id, "active", title="📅 GURUHLAR JADVALI")
        elif action == "payments":
            self.show(chat_id, "💰 TO‘LOVLAR\n\nTo‘lov yozuvlari va qarzdorlarni boshqaring.", with_navigation([
                [("➕ To‘lov kiritish", "flow:payment-add")],
                [("📅 Bugungi to‘lovlar", "admin:today_payments"), ("📆 Oylik to‘lovlar", "admin:monthly_payments")],
                [("⚠️ Qarzdorlar", "admin:debtors"), ("🟡 Qisman to‘laganlar", "admin:partial_payers")],
                [("📊 Moliyaviy hisobot", "admin:report_finance")],
                [("🔎 To‘lov qidirish", "flow:payment-search")],
            ]), edit=self.edit_message)
        elif action == "debtors":
            self.show_debtors(chat_id)
        elif action in ("today_payments", "monthly_payments", "partial_payers"):
            self.show_payments(chat_id, action)
        elif action == "applications":
            self.show_applications(chat_id, "pending")
        elif action.startswith("applications_"):
            self.show_applications(chat_id, action.removeprefix("applications_"))
        elif action == "reports":
            self.show(chat_id, "📊 HISOBOTLAR", with_navigation([
                [("💰 Moliyaviy", "admin:report_finance"), ("👨‍🎓 O‘quvchilar", "admin:report_students")],
                [("👥 Guruhlar", "admin:report_groups"), ("✅ Davomat", "admin:report_attendance")],
                [("👨‍🏫 Ustozlar", "admin:report_teachers")],
                [("📅 Kunlik", "admin:report_daily"), ("📆 Oylik", "admin:report_finance")],
                [("📥 CSV yuklab olish", "admin:export")],
            ]), edit=self.edit_message)
        elif action.startswith("report_"):
            self.show_report(chat_id, action.removeprefix("report_"))
        elif action == "export":
            self.send_csv(chat_id, user_id)
        elif action == "messages":
            self.message_menu(chat_id)
        elif action.startswith("message_"):
            self.start_broadcast(chat_id, user_id, action.removeprefix("message_"))
        elif action == "rooms":
            self.show_rooms(chat_id)
        elif action == "audit":
            self.show_audit(chat_id)
        elif action == "settings":
            fee = int(self.setting("monthly_fee"))
            self.show(chat_id, f"⚙️ SOZLAMALAR\n\nOylik to‘lov: {money(fee)}\n\nAdmin ID va bot tokeni server muhit o‘zgaruvchilarida boshqariladi.", with_navigation([
                [("💰 Oylik to‘lov miqdori", "flow:settings-fee")],
                [("🏫 Xonalar", "admin:rooms"), ("📚 Kurslar", "admin:report_groups")],
                [("👨‍💼 Admin ID ro‘yxati", "admin:admin_info")],
            ]), edit=self.edit_message)
        elif action == "admin_info":
            self.show(chat_id, "🔐 Admin ID lar TELEGRAM_ADMIN_IDS muhit o‘zgaruvchisidan olinadi:\n" + ", ".join(map(str, sorted(self.admin_ids))), with_navigation([]), edit=self.edit_message)
        elif action == "today_attendance":
            self.show_today_attendance(chat_id)
        else:
            self.show_home(chat_id, user_id, "admin", self.edit_message)

    def setting(self, name: str) -> str:
        row = self.store.one("SELECT value FROM settings WHERE key=?", (name,))
        return row["value"] if row else "500000"

    def debtors(self) -> list[sqlite3.Row]:
        return self.store.all(
            """SELECT u.*, coalesce(g.name,'Guruh biriktirilmagan') AS group_name,
               coalesce((SELECT sum(p.amount) FROM payments p WHERE p.user_id=u.telegram_id
                         AND p.month=strftime('%Y-%m','now','localtime')),0) AS paid
               FROM users u LEFT JOIN groups g ON g.id=u.group_id
               WHERE u.role='student' AND u.status='active'
               AND date(coalesce(nullif(u.payment_due,''),date(u.joined_at,'+30 days')),'+30 days') <= date('now','localtime')
               AND coalesce((SELECT sum(p.amount) FROM payments p WHERE p.user_id=u.telegram_id
                         AND p.month=strftime('%Y-%m','now','localtime')),0) < ?
               ORDER BY u.name""",
            (int(self.setting("monthly_fee")),),
        )

    def attendance_percent(self) -> int:
        row = self.store.one(
            "SELECT count(*) total, sum(status IN ('present','late')) good FROM attendance WHERE day=date('now','localtime')"
        )
        return round(100 * row["good"] / row["total"]) if row and row["total"] else 0

    def list_people(self, chat_id: int, role: str, status: str) -> None:
        sql_status = status if status not in ("all", "") else None
        query = "SELECT * FROM users WHERE role=?"
        args: tuple[Any, ...] = (role,)
        if sql_status == "blocked":
            query += " AND status IN ('blocked','exited')"
        elif sql_status:
            query += " AND status=?"
            args += (sql_status,)
        query += " ORDER BY name LIMIT 30"
        people = self.store.all(query, args)
        title = "O‘QUVCHILAR" if role == "student" else "USTOZLAR"
        if not people:
            self.show(chat_id, f"{title}\n\nHozircha bu ro‘yxat bo‘sh.", with_navigation([]), edit=self.edit_message)
            return
        rows = [[(f"{STATUS_NAMES.get(p['status'],'')} {user_label(p)}", f"obj:{role}:{p['telegram_id']}")] for p in people]
        self.show(chat_id, f"{title} · {len(people)} ta\n\nProfilni ochish uchun tanlang:", with_navigation(rows), edit=self.edit_message)

    def list_groups(self, chat_id: int, status: str, title: str = "👥 GURUHLAR") -> None:
        groups = self.store.all(
            """SELECT g.*, u.name teacher_name,
               (SELECT count(*) FROM users s WHERE s.group_id=g.id AND s.role='student' AND s.status='active') student_count
               FROM groups g LEFT JOIN users u ON u.telegram_id=g.teacher_id
               WHERE g.status=? ORDER BY g.name LIMIT 40""", (status,)
        )
        if not groups:
            self.show(chat_id, title + "\n\nHozircha guruh yo‘q. Guruh ochish tugmasi orqali yarating.", with_navigation([[("➕ Guruh ochish", "flow:group-add")]]), edit=self.edit_message)
            return
        rows = [[(f"👥 {g['name']} · {g['student_count']} o‘quvchi", f"obj:group:{g['id']}")] for g in groups]
        self.show(chat_id, f"{title}\n\nGuruhni tanlang:", with_navigation(rows), edit=self.edit_message)

    def show_student_search(self, chat_id: int, admin_id: int, query: str) -> None:
        self.store.clear_state(admin_id)
        rows = self.store.all(
            "SELECT * FROM users WHERE role='student' AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 20",
            (f"%{query}%", f"%{query}%"),
        )
        buttons = [[(f"👤 {user_label(row)}", f"obj:student:{row['telegram_id']}")] for row in rows]
        text = f"🔎 Qidiruv: {query}\n\n" + ("\n".join(f"• {user_label(r)} · {r['phone']}" for r in rows) if rows else "Mos o‘quvchi topilmadi.")
        self.show(chat_id, text, with_navigation(buttons), edit=self.edit_message)

    def show_teacher_search(self, chat_id: int, admin_id: int, query: str) -> None:
        self.store.clear_state(admin_id)
        rows = self.store.all("SELECT * FROM users WHERE role='teacher' AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 20", (f"%{query}%",f"%{query}%"))
        self.show(chat_id, f"🔎 Ustozlar: {query}", with_navigation([[(f"👨‍🏫 {user_label(r)}",f"obj:teacher:{r['telegram_id']}")] for r in rows]), edit=self.edit_message)

    def show_group_search(self, chat_id: int, admin_id: int, query: str) -> None:
        self.store.clear_state(admin_id)
        rows = self.store.all("SELECT id,name FROM groups WHERE name LIKE ? ORDER BY name LIMIT 20",(f"%{query}%",))
        self.show(chat_id, f"🔎 Guruhlar: {query}", with_navigation([[(f"👥 {r['name']}",f"obj:group:{r['id']}")] for r in rows]), edit=self.edit_message)

    def object_action(self, chat_id: int, admin_id: int, kind: str, value: str) -> None:
        if kind in ("student", "teacher"):
            row = self.person(int(value))
            if not row:
                self.show(chat_id, "Foydalanuvchi topilmadi.", with_navigation([]), edit=self.edit_message)
                return
            group = self.store.one("SELECT * FROM groups WHERE id=?", (row["group_id"],)) if row["group_id"] else None
            if kind == "student":
                group_label = group["name"] if group else "Biriktirilmagan"
                teacher_label = self.name_of(group["teacher_id"]) if group and group["teacher_id"] else "Biriktirilmagan"
                paid = self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE user_id=? AND month=strftime('%Y-%m','now','localtime')", (row["telegram_id"],))["n"]
                attendance_rows = self.store.one("SELECT count(*) total,sum(status IN ('present','late')) good FROM attendance WHERE user_id=?", (row["telegram_id"],))
                percent = round(100 * attendance_rows["good"] / attendance_rows["total"]) if attendance_rows["total"] else 0
                body = (f"👤 {user_label(row)}\n📱 {row['phone'] or '—'}\n📚 {row['course'] or '—'}\n"
                        f"👥 {group_label}\n👨‍🏫 {teacher_label}\n{STATUS_NAMES.get(row['status'],'')}\n"
                        f"✅ Davomat: {percent}%\n💰 Joriy oy to‘lovi: {money(paid)}\n📅 Qo‘shilgan: {row['joined_at'][:10]}")
                rows = [[("👤 Profil", f"obj:student:{value}")],
                        [("💰 To‘lovlar", f"obj:student-payments:{value}"), ("✅ Davomat", f"obj:student-attendance:{value}")],
                        [("👥 Guruh", f"obj:assign-group:{value}"), ("✏️ Tahrirlash", f"obj:edit-student:{value}")],
                        [("❄️ Muzlatish" if row["status"] != "frozen" else "🟢 Faollashtirish", f"obj:freeze:{value}"),
                         ("🔴 Chiqish", f"obj:exit:{value}")]]
            else:
                group_count = self.store.one("SELECT count(*) n FROM groups WHERE teacher_id=? AND status='active'", (row["telegram_id"],))["n"]
                student_count = self.store.one("SELECT count(*) n FROM users s JOIN groups g ON g.id=s.group_id WHERE g.teacher_id=? AND s.role='student' AND s.status='active'", (row["telegram_id"],))["n"]
                body = f"👨‍🏫 {user_label(row)}\n📱 {row['phone'] or '—'}\n📚 {row['course']}\n👥 Guruhlar: {group_count}\n👨‍🎓 O‘quvchilar: {student_count}\n{STATUS_NAMES.get(row['status'],'')}"
                rows = [[("👥 Guruhlar", f"obj:teacher-groups:{value}"), ("👨‍🎓 O‘quvchilar", f"obj:teacher-students:{value}")],
                        [("📊 Statistika", f"obj:teacher-stats:{value}"), ("✏️ Tahrirlash", f"obj:edit-teacher:{value}")],
                        [("🔴 Bloklash" if row["status"] == "active" else "🟢 Faollashtirish", f"obj:block:{value}")]]
            self.show(chat_id, body, with_navigation(rows), edit=self.edit_message)
        elif kind == "group":
            self.show_group(chat_id, int(value))
        elif kind == "room":
            room=self.store.one("SELECT * FROM rooms WHERE id=?",(int(value),))
            if not room:
                self.show(chat_id,"Xona topilmadi.",with_navigation([]),edit=self.edit_message)
                return
            groups=self.store.all("SELECT * FROM groups WHERE room=? AND status='active' ORDER BY days,start_time",(room["name"],))
            text=f"🏫 {room['name'].upper()}\n\n"
            if groups:
                text+="\n\n".join(
                    f"👥 {g['name']}\n📅 {g['days']}\n🕐 {g['start_time']}–{g['end_time']}\n👨‍🏫 {self.name_of(g['teacher_id']) if g['teacher_id'] else 'Ustoz biriktirilmagan'}"
                    for g in groups
                )
            else:
                text+="Hozircha faol guruh biriktirilmagan."
            self.show(chat_id,text,with_navigation([[(g["name"],f"obj:group:{g['id']}")] for g in groups]),edit=self.edit_message)
        elif kind == "group-toggle":
            group = self.store.one("SELECT * FROM groups WHERE id=?", (int(value),))
            if group:
                status = "closed" if group["status"] == "active" else "active"
                self.store.execute("UPDATE groups SET status=? WHERE id=?", (status, int(value)))
                self.record(admin_id, f"Guruh {group['name']} holatini {status} qildi")
                self.show_group(chat_id, int(value))
        elif kind == "group-students":
            students = self.store.all("SELECT * FROM users WHERE group_id=? AND role='student' ORDER BY name", (int(value),))
            self.show(chat_id, "👨‍🎓 GURUH O‘QUVCHILARI", with_navigation([[(user_label(s), f"obj:student:{s['telegram_id']}")] for s in students]), edit=self.edit_message)
        elif kind == "group-attendance":
            records = self.store.all("SELECT day,status,count(*) amount FROM attendance WHERE group_id=? GROUP BY day,status ORDER BY day DESC LIMIT 30", (int(value),))
            self.show(chat_id, "✅ GURUH DAVOMATI\n\n" + ("\n".join(f"{r['day']} · {ATTENDANCE_NAMES.get(r['status'],r['status'])}: {r['amount']}" for r in records) if records else "Davomat kiritilmagan."), with_navigation([]), edit=self.edit_message)
        elif kind == "group-payments":
            records = self.store.all("SELECT u.name,coalesce(sum(p.amount),0) paid FROM users u LEFT JOIN payments p ON p.user_id=u.telegram_id AND p.month=? WHERE u.group_id=? AND u.role='student' GROUP BY u.telegram_id ORDER BY u.name", (date.today().strftime("%Y-%m"),int(value)))
            fee = int(self.setting("monthly_fee"))
            self.show(chat_id, "💰 GURUH TO‘LOVLARI\n\n" + ("\n".join(f"{r['name']} · {money(r['paid'])} / {money(fee)}" for r in records) if records else "Guruhda o‘quvchi yo‘q."), with_navigation([]), edit=self.edit_message)
        elif kind == "group-teacher":
            group = self.store.one("SELECT teacher_id FROM groups WHERE id=?", (int(value),))
            if group and group["teacher_id"]:
                self.object_action(chat_id, admin_id, "teacher", str(group["teacher_id"]))
            else:
                teachers = self.store.all("SELECT telegram_id,name FROM users WHERE role='teacher' AND status='active' ORDER BY name")
                self.show(chat_id, "Ustoz tanlang:", with_navigation([[(r["name"],f"obj:assign-teacher:{value}:{r['telegram_id']}")] for r in teachers]), edit=self.edit_message)
        elif kind.startswith("assign-teacher"):
            parts = value.split(":")
            group_id, teacher_id = int(parts[0]), int(parts[1])
            self.store.execute("UPDATE groups SET teacher_id=? WHERE id=?", (teacher_id,group_id))
            self.record(admin_id, f"Guruh {group_id} ga ustoz {teacher_id} biriktirdi")
            self.show_group(chat_id, group_id)
        elif kind == "group-edit":
            group = self.store.one("SELECT name,days,start_time,end_time,room FROM groups WHERE id=?", (int(value),))
            if group:
                self.ask(chat_id, admin_id, "group_edit", "Yangi jadval va xonani vergul bilan yuboring:\nKunlar, Boshlanish, Tugash, Xona", {"group_id":int(value)})
        elif kind.startswith("student-"):
            self.person_subpage(chat_id, kind, int(value))
        elif kind.startswith("teacher-"):
            self.teacher_subpage(chat_id, kind, int(value))
        elif kind in ("freeze", "exit", "block"):
            user_id = int(value)
            row = self.person(user_id)
            if not row:
                self.show(chat_id, "Foydalanuvchi topilmadi.", with_navigation([]), edit=self.edit_message)
                return
            if kind == "freeze":
                new_status = "active" if row["status"] == "frozen" else "frozen"
            elif kind == "exit":
                new_status = "exited"
            else:
                new_status = "blocked" if row["status"] == "active" else "active"
            self.store.execute("UPDATE users SET status=? WHERE telegram_id=?", (new_status, user_id))
            self.record(admin_id, f"{row['role']} {user_label(row)} holatini {new_status} qildi")
            self.show(chat_id, f"✅ {user_label(row)}: {STATUS_NAMES[new_status]}", keyboard([[("👤 Profil", f"obj:{row['role']}:{user_id}")],[(HOME,"home")]]), edit=self.edit_message)
        elif kind == "student-payments":
            self.person_subpage(chat_id, kind, int(value))
        elif kind == "student-attendance":
            self.person_subpage(chat_id, kind, int(value))
        elif kind == "assign-group":
            groups = self.store.all("SELECT id,name FROM groups WHERE status='active' ORDER BY name")
            buttons = [[(g["name"], f"obj:group-assign:{value}:{g['id']}")] for g in groups]
            self.show(chat_id, "O‘quvchini qaysi guruhga biriktiramiz?", with_navigation(buttons), edit=self.edit_message)
        elif kind.startswith("group-assign"):
            _, student_id, group_id = kind.split("-")[-1], None, None
            parts = value.split(":")
            student_id, group_id = int(parts[0]), int(parts[1])
            self.store.execute("UPDATE users SET group_id=? WHERE telegram_id=?", (group_id, student_id))
            self.record(admin_id, f"O‘quvchi {student_id} guruh {group_id} ga biriktirildi")
            self.show_group(chat_id, group_id)
        elif kind.startswith("edit-"):
            user_id = int(value)
            row = self.person(user_id)
            if row:
                self.ask(chat_id, admin_id, "edit-user", "Yangi ism-familiyani yuboring:", {"target": user_id, "role": row["role"]})
        elif kind == "teacher-groups" or kind == "teacher-students":
            self.teacher_subpage(chat_id, kind, int(value))
        elif kind == "teacher-stats":
            self.teacher_subpage(chat_id, kind, int(value))

    def show_group(self, chat_id: int, group_id: int) -> None:
        group = self.store.one(
            """SELECT g.*,u.name teacher_name,(SELECT count(*) FROM users s WHERE s.group_id=g.id AND s.role='student' AND s.status='active') student_count
               FROM groups g LEFT JOIN users u ON u.telegram_id=g.teacher_id WHERE g.id=?""", (group_id,)
        )
        if not group:
            self.show(chat_id, "Guruh topilmadi.", with_navigation([]), edit=self.edit_message)
            return
        body = (f"👥 {group['name'].upper()}\n📚 Kurs: {group['course']}\n"
                f"👨‍🏫 Ustoz: {group['teacher_name'] or 'Biriktirilmagan'}\n👨‍🎓 O‘quvchilar: {group['student_count']}\n"
                f"📅 {group['days'] or 'Jadval kiritilmagan'}\n🕐 {group['start_time']}–{group['end_time']}\n"
                f"🏫 {group['room'] or 'Xona biriktirilmagan'}\n{STATUS_NAMES.get(group['status'],'')}")
        rows = [[("👨‍🎓 O‘quvchilar", f"obj:group-students:{group_id}"), ("👨‍🏫 Ustoz", f"obj:group-teacher:{group_id}")],
                [("📅 Jadval", f"obj:group:{group_id}"), ("🏫 Xona", f"obj:group:{group_id}")],
                [("✅ Davomat", f"obj:group-attendance:{group_id}"), ("💰 To‘lovlar", f"obj:group-payments:{group_id}")],
                [("✏️ Tahrirlash", f"obj:group-edit:{group_id}"),
                 ("🔴 Guruhni yopish" if group["status"] == "active" else "🟢 Guruhni ochish", f"obj:group-toggle:{group_id}")]]
        self.show(chat_id, body, with_navigation(rows), edit=self.edit_message)

    def person_subpage(self, chat_id: int, kind: str, user_id: int) -> None:
        row = self.person(user_id)
        if not row:
            self.show(chat_id, "O‘quvchi topilmadi.", with_navigation([]), edit=self.edit_message)
        elif kind == "student-payments":
            records = self.store.all("SELECT month,amount,method,paid_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 12", (user_id,))
            body = f"💰 TO‘LOVLAR · {user_label(row)}\n\n" + ("\n".join(f"• {r['month']} · {money(r['amount'])} · {r['method']}" for r in records) if records else "Hozircha to‘lov yozuvi yo‘q.")
            self.show(chat_id, body, with_navigation([[("➕ To‘lov kiritish", f"flow:payment-for-{user_id}")]]), edit=self.edit_message)
        elif kind == "student-attendance":
            records = self.store.all("SELECT day,status FROM attendance WHERE user_id=? ORDER BY day DESC LIMIT 15", (user_id,))
            body = f"✅ DAVOMAT · {user_label(row)}\n\n" + ("\n".join(f"• {r['day']} · {ATTENDANCE_NAMES[r['status']]}" for r in records) if records else "Davomat yozuvi yo‘q.")
            self.show(chat_id, body, with_navigation([]), edit=self.edit_message)

    def teacher_subpage(self, chat_id: int, kind: str, teacher_id: int) -> None:
        teacher = self.person(teacher_id)
        if kind == "teacher-groups":
            self.show_teacher_groups(chat_id, teacher_id)
        elif kind == "teacher-students":
            students = self.store.all("SELECT s.* FROM users s JOIN groups g ON g.id=s.group_id WHERE g.teacher_id=? AND s.role='student' ORDER BY s.name", (teacher_id,))
            self.show(chat_id, f"👨‍🎓 {teacher['name'] if teacher else 'Ustoz'} o‘quvchilari", with_navigation([[(f"👤 {user_label(s)}", f"obj:student:{s['telegram_id']}")] for s in students]), edit=self.edit_message)
        elif kind == "teacher-stats":
            groups = self.store.all("SELECT id,name FROM groups WHERE teacher_id=?", (teacher_id,))
            self.show(chat_id, f"📊 Ustoz statistikasi\n\nGuruhlar: {len(groups)}", with_navigation([[(g["name"],f"obj:group:{g['id']}")] for g in groups]), edit=self.edit_message)

    def show_teacher_groups(self, chat_id: int, teacher_id: int) -> None:
        groups = self.store.all("SELECT id,name FROM groups WHERE teacher_id=? ORDER BY name", (teacher_id,))
        self.show(chat_id, "👥 USTOZ GURUHLARI", with_navigation([[(g["name"],f"obj:group:{g['id']}")] for g in groups]), edit=self.edit_message)

    def show_debtors(self, chat_id: int) -> None:
        debtors = self.debtors()
        amount = int(self.setting("monthly_fee"))
        total_debt=sum(max(amount-r["paid"],0) for r in debtors)
        body = f"⚠️ QARZDORLAR\nJami: {len(debtors)} o‘quvchi\nHisoblangan qarz: {money(total_debt)}\n\n"
        body += "\n".join(f"{i}. {user_label(r)} · {money(max(amount-r['paid'],0))} · {r['group_name']}" for i,r in enumerate(debtors[:25],1))
        if not debtors:
            body += "Qarzdor o‘quvchilar yo‘q."
        rows = [[(f"👤 {user_label(r)} · {money(max(amount-r['paid'],0))}",f"obj:student:{r['telegram_id']}")] for r in debtors[:20]]
        rows += [[("📢 Barchasiga xabar", "admin:message_debtors")], [("📥 CSV hisobot", "admin:export")]]
        self.show(chat_id, body, with_navigation(rows), edit=self.edit_message)

    def show_payments(self, chat_id: int, mode: str) -> None:
        if mode == "today_payments":
            records = self.store.all("SELECT p.*,u.name FROM payments p JOIN users u ON u.telegram_id=p.user_id WHERE date(p.paid_at)=date('now','localtime') ORDER BY p.id DESC LIMIT 30")
            title = "📅 BUGUNGI TO‘LOVLAR"
        elif mode == "monthly_payments":
            records = self.store.all("SELECT p.*,u.name FROM payments p JOIN users u ON u.telegram_id=p.user_id WHERE strftime('%Y-%m',p.paid_at)=strftime('%Y-%m','now','localtime') ORDER BY p.id DESC LIMIT 50")
            title = "📆 OYLIK TO‘LOVLAR"
        else:
            records = self.store.all(
                """SELECT u.*,coalesce(g.name,'Guruh biriktirilmagan') group_name,
                   coalesce((SELECT sum(p.amount) FROM payments p WHERE p.user_id=u.telegram_id
                             AND p.month=strftime('%Y-%m','now','localtime')),0) paid
                   FROM users u LEFT JOIN groups g ON g.id=u.group_id
                   WHERE u.role='student' AND u.status='active'
                   AND coalesce((SELECT sum(p.amount) FROM payments p WHERE p.user_id=u.telegram_id
                             AND p.month=strftime('%Y-%m','now','localtime')),0)>0
                   AND coalesce((SELECT sum(p.amount) FROM payments p WHERE p.user_id=u.telegram_id
                             AND p.month=strftime('%Y-%m','now','localtime')),0) < ?
                   ORDER BY u.name""",(int(self.setting("monthly_fee")),)
            )
            title = "🟡 QISMAN TO‘LAGANLAR"
        if mode != "partial_payers":
            total = sum(r["amount"] for r in records)
            lines = [f"• {r['name']} · {money(r['amount'])} · {r['method']} · {r['month']}" for r in records]
            body = f"{title}\nJami: {money(total)}\nYozuvlar: {len(records)}\n\n" + ("\n".join(lines) if lines else "To‘lov yozuvi yo‘q.")
        else:
            lines = [f"• {user_label(r)} · {money(r['paid'])} / {money(int(self.setting('monthly_fee')))}" for r in records]
            body = f"{title}\n\n" + ("\n".join(lines) if lines else "Qisman to‘lovchilar yo‘q.")
        self.show(chat_id, body, with_navigation([[("➕ To‘lov kiritish", "flow:payment-add")]]), edit=self.edit_message)

    def show_applications(self, chat_id: int, status: str) -> None:
        rows = self.store.all(
            """SELECT a.*,u.name,u.phone,u.role,u.course FROM applications a JOIN users u ON u.telegram_id=a.user_id
               WHERE a.status=? ORDER BY a.id DESC LIMIT 30""", (status,)
        )
        title = {"pending":"🔴 YANGI ARIZALAR","accepted":"🟢 QABUL QILINGAN","rejected":"⚫ RAD ETILGAN"}.get(status, "📝 ARIZALAR")
        if not rows:
            self.show(chat_id, title + "\n\nAriza yo‘q.", with_navigation([
                [("🔴 Yangi", "admin:applications_pending"), ("🟢 Qabul qilingan", "admin:applications_accepted")],
                [("⚫ Rad etilgan", "admin:applications_rejected")],
            ]), edit=self.edit_message)
            return
        buttons = [[(f"#{r['id']} · {r['name']} · {ROLE_NAMES.get(r['role'],r['role'])}", f"app:view:{r['id']}")] for r in rows]
        buttons += [[("🔴 Yangi", "admin:applications_pending"), ("🟢 Qabul qilingan", "admin:applications_accepted")],
                    [("⚫ Rad etilgan", "admin:applications_rejected")]]
        self.show(chat_id, f"{title} · {len(rows)} ta\n\n" + "\n".join(f"#{r['id']} {r['name']} · {r['phone']} · {r['course']}" for r in rows), with_navigation(buttons), edit=self.edit_message)

    def application_action(self, chat_id: int, admin_id: int, action: str, app_id: int) -> None:
        row = self.store.one("SELECT a.*,u.name,u.phone,u.role,u.course,u.telegram_id FROM applications a JOIN users u ON u.telegram_id=a.user_id WHERE a.id=?", (app_id,))
        if not row:
            self.show(chat_id, "Ariza topilmadi.", with_navigation([]), edit=self.edit_message)
            return
        if action == "view":
            try:
                details=json.loads(row["details"] or "{}")
            except (ValueError,TypeError):
                details={}
            if row["kind"]=="teacher_student_add":
                summary=f"Ustoz: {row['name']}\nO‘quvchi: {details.get('name','—')}\nTelefon: {details.get('phone','—')}\nGuruh: {details.get('group_name','—')}"
            elif row["kind"]=="teacher_student_remove":
                summary=f"Ustoz: {row['name']}\nO‘quvchi: {details.get('student_name','—')}\nGuruh: {details.get('group_name','—')}\nSabab: {details.get('reason','—')}"
            elif row["kind"]=="teacher_group":
                summary=f"Ustoz: {row['name']}\nGuruh: {details.get('name','—')}\nKurs: {details.get('course','—')}\nKunlar: {details.get('days','—')}\nVaqt: {details.get('start','—')}–{details.get('end','—')}\nXona: {details.get('room','—')}"
            else:
                summary=f"👤 {row['name']}\n📱 {row['phone']}\n📚 {row['course']}\nRol: {ROLE_NAMES.get(row['role'],row['role'])}"
            body = f"📝 ARIZA #{app_id}\n\n{summary}\nHolat: {row['status']}\n📅 {row['created_at']}"
            rows = [[("✅ Qabul qilish", f"app:accept:{app_id}"), ("❌ Rad etish", f"app:reject:{app_id}")]] if row["status"] == "pending" else []
            self.show(chat_id, body, with_navigation(rows), edit=self.edit_message)
        elif action in ("accept", "reject"):
            if row["status"] != "pending":
                self.show(chat_id,f"Ariza #{app_id} allaqachon ko‘rib chiqilgan.",with_navigation([]),edit=self.edit_message)
                return
            status = "accepted" if action == "accept" else "rejected"
            if action == "accept" and row["kind"] not in ("student","teacher"):
                try:
                    details=json.loads(row["details"] or "{}")
                except (ValueError,TypeError) as error:
                    LOG.exception("Ariza #%s JSON ma’lumoti yaroqsiz",app_id)
                    self.show(chat_id,"Ariza ma’lumotlarini o‘qib bo‘lmadi. Tasdiqlash bekor qilindi.",with_navigation([]),edit=self.edit_message)
                    return
                try:
                    if row["kind"]=="teacher_student_add":
                        group=self.store.one("SELECT course FROM groups WHERE id=? AND teacher_id=?",(details["group_id"],row["telegram_id"]))
                        if not group:
                            self.show(chat_id,"So‘rovdagi guruh ustozga tegishli emas yoki yopilgan. So‘rovni qayta tekshiring.",with_navigation([]),edit=self.edit_message)
                            return
                        local_id=(self.store.one("SELECT min(telegram_id) value FROM users WHERE telegram_id<0")["value"] or 0)-1
                        self.store.execute("INSERT INTO users(telegram_id,name,phone,role,status,course,group_id,payment_due) VALUES(?,?,?,'student','active',?,?,date('now','localtime','+30 days'))",
                            (local_id,details["name"],details["phone"],group["course"],details["group_id"]))
                    elif row["kind"]=="teacher_student_remove":
                        result=self.store.execute("UPDATE users SET group_id=NULL WHERE telegram_id=? AND group_id=?",
                            (details["student_id"],details["group_id"]))
                        if not result.rowcount:
                            self.show(chat_id,"O‘quvchi bu guruhda topilmadi; so‘rov tasdiqlanmadi.",with_navigation([]),edit=self.edit_message)
                            return
                    elif row["kind"]=="teacher_group":
                        self.store.execute("INSERT INTO groups(name,course,teacher_id,days,start_time,end_time,room) VALUES(?,?,?,?,?,?,?)",
                            (details["name"],details["course"],row["telegram_id"],details["days"],details["start"],details["end"],details["room"]))
                except (sqlite3.IntegrityError,KeyError) as error:
                    LOG.exception("Ariza #%s ni bajarib bo‘lmadi",app_id)
                    self.show(chat_id,f"So‘rovni bajarib bo‘lmadi: {error}",with_navigation([]),edit=self.edit_message)
                    return
            if row["kind"] in ("student","teacher"):
                self.store.execute("UPDATE users SET status=? WHERE telegram_id=?", ("active" if action=="accept" else "exited",row["telegram_id"]))
            self.store.execute("UPDATE applications SET status=? WHERE id=?", (status,app_id))
            if row["kind"] in ("student","teacher") and action=="accept":
                self.notify_one(row["telegram_id"],"✅ Arizangiz qabul qilindi. SILICON botiga /start yuboring.")
            elif row["kind"] in ("student","teacher") and action=="reject":
                self.notify_one(row["telegram_id"],"Arizangiz hozircha rad etildi. Batafsil ma’lumot uchun administrator bilan bog‘laning.")
            else:
                self.notify_one(row["telegram_id"],f"{'✅' if action=='accept' else '❌'} Admin so‘rovingizni {'tasdiqladi' if action=='accept' else 'rad etdi'}: #{app_id}")
            self.record(admin_id, f"Ariza #{app_id} {status}")
            self.show(chat_id, f"✅ Ariza #{app_id} {status}.", with_navigation([]), edit=self.edit_message)

    def notify_one(self, user_id: int, text: str) -> None:
        try:
            self.api("sendMessage",{"chat_id":user_id,"text":text})
        except BotError as error:
            LOG.warning("User %s could not be notified: %s",user_id,error)

    def show_report(self, chat_id: int, report: str) -> None:
        if report in ("finance","daily"):
            total = self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE strftime('%Y-%m',paid_at)=strftime('%Y-%m','now','localtime')")["n"]
            method_rows = self.store.all("SELECT method,sum(amount) amount FROM payments WHERE strftime('%Y-%m',paid_at)=strftime('%Y-%m','now','localtime') GROUP BY method")
            methods = "\n".join(f"{r['method']}: {money(r['amount'])}" for r in method_rows) or "To‘lov yozuvi yo‘q"
            fee = int(self.setting("monthly_fee"))
            debtors = len(self.debtors())
            body = (f"📊 MOLIYAVIY HISOBOT · {date.today():%m/%Y}\n\n💰 Jami tushum: {money(total)}\n"
                    f"{methods}\n⚠️ Hisoblangan qarzdorlik: {money(debtors*fee)}")
        elif report == "students":
            rows = self.store.all("SELECT status,count(*) n FROM users WHERE role='student' GROUP BY status")
            body = "👨‍🎓 O‘QUVCHILAR HISOBOTI\n\n" + ("\n".join(f"{STATUS_NAMES.get(r['status'],r['status'])}: {r['n']}" for r in rows) or "O‘quvchi yo‘q.")
        elif report == "groups":
            rows = self.store.all("SELECT status,count(*) n FROM groups GROUP BY status")
            body = "👥 GURUHLAR HISOBOTI\n\n" + ("\n".join(f"{r['status']}: {r['n']}" for r in rows) or "Guruh yo‘q.")
        elif report == "attendance":
            marked = self.store.one("SELECT count(*) n FROM attendance WHERE day=date('now','localtime')")["n"]
            body = f"✅ BUGUNGI DAVOMAT\n\nQatnashuv: {self.attendance_percent()}%\nBelgilar: {marked}"
        elif report == "teachers":
            count = self.store.one("SELECT count(*) n FROM users WHERE role='teacher' AND status='active'")["n"]
            body = f"👨‍🏫 Faol ustozlar: {count}"
        else:
            body = "Hisobot uchun ma’lumot hali kiritilmagan."
        self.show(chat_id, body, with_navigation([]), edit=self.edit_message)

    def send_csv(self, chat_id: int, user_id: int) -> None:
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Ism", "Telefon", "Kurs", "Guruh", "Holat", "Joriy oy to‘lovi"])
        rows = self.store.all(
            """SELECT u.name,u.phone,u.course,u.status,g.name group_name,
               (SELECT coalesce(sum(amount),0) FROM payments p WHERE p.user_id=u.telegram_id AND p.month=strftime('%Y-%m','now','localtime')) paid
               FROM users u LEFT JOIN groups g ON g.id=u.group_id WHERE u.role='student' ORDER BY u.name"""
        )
        for row in rows:
            writer.writerow([row["name"],row["phone"],row["course"],row["group_name"] or "",row["status"],row["paid"]])
        boundary = "----siliconcsvboundary"
        data = output.getvalue().encode("utf-8-sig")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"silicon_students.csv\"\r\n"
                "Content-Type: text/csv\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/sendDocument", data=body,
                                         headers={"Content-Type":f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode())
            if not result.get("ok"):
                raise BotError(result.get("description","CSV yuborilmadi"))
            self.record(user_id, "O‘quvchilar CSV hisobotini yukladi")
        except (urllib.error.URLError, json.JSONDecodeError, BotError) as error:
            LOG.exception("CSV report delivery failed")
            self.show(chat_id, f"CSV yuborib bo‘lmadi: {error}", edit=self.edit_message)

    def message_menu(self, chat_id: int) -> None:
        self.show(chat_id, "📢 XABARLAR\n\nKimga xabar yuborilsin?", with_navigation([
            [("👨‍🎓 O‘quvchilarga", "admin:message_students"), ("👥 Guruhga", "admin:message_group")],
            [("⚠️ Qarzdorlarga", "admin:message_debtors"), ("👨‍🏫 Ustozlarga", "admin:message_teachers")],
            [("📢 Barchaga", "admin:message_all")],
            [("📜 Yuborilgan xabarlar", "admin:message_history")],
        ]), edit=self.edit_message)

    def start_broadcast(self, chat_id: int, admin_id: int, target: str) -> None:
        if target == "history":
            records = self.store.all("SELECT target,body,created_at FROM messages ORDER BY id DESC LIMIT 10")
            body = "\n\n".join(f"{r['created_at']} · {r['target']}\n{r['body'][:180]}" for r in records) or "Yuborilgan xabarlar yo‘q."
            self.show(chat_id, "📜 YUBORILGAN XABARLAR\n\n"+body, with_navigation([]), edit=self.edit_message)
            return
        if target == "group":
            groups = self.store.all("SELECT id,name FROM groups WHERE status='active' ORDER BY name")
            self.show(chat_id, "Qaysi guruhga xabar yuborilsin?", with_navigation([[(g["name"],f"flow:message-group-{g['id']}")] for g in groups]), edit=self.edit_message)
            return
        if target.startswith("group-"):
            group_id = int(target.split("-",1)[1])
            recipients = self.store.all("SELECT telegram_id FROM users WHERE group_id=? AND role='student' AND status='active'",(group_id,))
            group = self.store.one("SELECT name FROM groups WHERE id=?",(group_id,))
            target = "group:" + str(group_id)
            target_label = group["name"] if group else target
        elif target == "debtors":
            recipients = self.debtors()
            target_label = "Qarzdor o‘quvchilar"
        else:
            role = "teacher" if target == "teachers" else "student" if target == "students" else None
            recipients = self.store.all("SELECT telegram_id FROM users WHERE status='active'" + (" AND role=?" if role else ""), (role,) if role else ())
            target_label = {"teachers":"Ustozlar","students":"O‘quvchilar","all":"Barcha faol foydalanuvchilar"}[target]
        self.ask(chat_id, admin_id, "message_compose", f"✍️ {target_label} uchun xabar matnini yuboring:", {
            "target": target, "target_label": target_label,
            "recipients": [r["telegram_id"] for r in recipients], "count": len(recipients),
        })

    def show_rooms(self, chat_id: int) -> None:
        rooms = self.store.all("SELECT * FROM rooms ORDER BY name")
        today = WEEKDAYS[date.today().weekday()]
        groups = self.store.all("SELECT * FROM groups WHERE status='active' AND instr(days,?)>0 ORDER BY start_time", (today[:3],))
        rows = [[(f"🏫 {r['name']}",f"obj:room:{r['id']}")] for r in rooms]
        rows += [[("➕ Xona qo‘shish","flow:room-add"),("📅 Xona jadvali","flow:room-schedule")]]
        self.show(chat_id, "🏫 XONALAR\n\n"+"Ro‘yxat hozircha bo‘sh." if not rooms else "🏫 XONALAR\n\n"+ "\n".join(f"• {r['name']}" for r in rooms), with_navigation(rows), edit=self.edit_message)

    def show_room_schedule(self, chat_id: int, user_id: int, room: str) -> None:
        rows = self.store.all("SELECT name,days,start_time,end_time,teacher_id FROM groups WHERE room LIKE ? AND status='active' ORDER BY start_time",(f"%{room}%",))
        text = f"📅 XONA JADVALI · {room}\n\n" + ("\n".join(f"{r['days']} · {r['start_time']}–{r['end_time']} · {r['name']}" for r in rows) if rows else "Bu xonada dars jadvali topilmadi.")
        self.store.clear_state(user_id)
        self.show(chat_id,text,with_navigation([]),edit=self.edit_message)

    def show_audit(self, chat_id: int) -> None:
        rows = self.store.all("SELECT * FROM activity ORDER BY id DESC LIMIT 20")
        self.show(chat_id,"📜 FAOLIYAT TARIXI\n\n"+("\n".join(f"{r['created_at']} · {self.name_of(r['actor_id'])}\n{r['action']}" for r in rows) if rows else "Faoliyat tarixi hozircha bo‘sh."),with_navigation([]),edit=self.edit_message)

    def show_today_attendance(self, chat_id: int) -> None:
        rows = self.store.all("SELECT status,count(*) n FROM attendance WHERE day=date('now','localtime') GROUP BY status")
        body = "📅 BUGUNGI DAVOMAT\n\n" + ("\n".join(f"{ATTENDANCE_NAMES.get(r['status'],r['status'])}: {r['n']}" for r in rows) if rows else "Bugun davomat kiritilmagan.")
        self.show(chat_id,body,with_navigation([]),edit=self.edit_message)

    def add_student_from_text(self, chat_id: int, admin_id: int, text: str) -> None:
        parts = [p.strip() for p in text.split(",")]
        if len(parts) < 3 or not parts[1].lstrip("+").isdigit():
            self.show(chat_id,"Quyidagi formatda yuboring:\nIsm Familiya, +998901234567, Kurs")
            return
        data = {"name":parts[0],"phone":parts[1],"course":parts[2],"role":"student"}
        self.store.set_state(admin_id,"student_add_confirm",data)
        self.show(chat_id,f"Yangi o‘quvchi\n👤 {parts[0]}\n📱 {parts[1]}\n📚 {parts[2]}\n\nTasdiqlaysizmi?",with_navigation([[("✅ Yaratish","flow:student-save"),("❌ Bekor qilish","cancel")]]),edit=self.edit_message)

    def add_group_from_text(self, chat_id: int, admin_id: int, text: str) -> None:
        parts = [p.strip() for p in text.split(",")]
        if len(parts)<6:
            self.show(chat_id,"Guruhni vergul bilan ajratib yuboring:\nNomi, Kurs, Kunlar (Dush/Chor/Jum), Boshlanish, Tugash, Xona")
            return
        data={"name":parts[0],"course":parts[1],"days":parts[2],"start":parts[3],"end":parts[4],"room":parts[5]}
        self.store.set_state(admin_id,"group_add_confirm",data)
        self.show(chat_id,f"👥 {data['name']}\n📚 {data['course']}\n📅 {data['days']}\n🕐 {data['start']}–{data['end']}\n🏫 {data['room']}\n\nGuruh yarataymi?",with_navigation([[("✅ Yaratish","flow:group-save"),("❌ Bekor qilish","cancel")]]),edit=self.edit_message)

    def add_room_from_text(self, chat_id: int, admin_id: int, text: str) -> None:
        try:
            self.store.execute("INSERT INTO rooms(name) VALUES(?)",(text[:60],))
            self.record(admin_id,f"Xona yaratdi: {text[:60]}")
            self.show(chat_id,f"✅ {text[:60]} xonasi qo‘shildi.",with_navigation([]),edit=self.edit_message)
        except sqlite3.IntegrityError:
            self.show(chat_id,"Bu xona nomi allaqachon mavjud.",with_navigation([]),edit=self.edit_message)
        self.store.clear_state(admin_id)

    def payment_find_student(self, chat_id: int, admin_id: int, text: str) -> None:
        rows = self.store.all("SELECT * FROM users WHERE role='student' AND status='active' AND (name LIKE ? OR phone LIKE ?) ORDER BY name LIMIT 15",(f"%{text}%",f"%{text}%"))
        buttons=[[(f"👤 {user_label(r)} · {r['phone']}",f"flow:payment-user-{r['telegram_id']}")] for r in rows]
        if not rows:
            self.store.clear_state(admin_id)
            self.show(chat_id,"Faol o‘quvchi topilmadi.",with_navigation([]),edit=self.edit_message)
        else:
            self.show(chat_id,"To‘lov qilgan o‘quvchini tanlang:",with_navigation(buttons),edit=self.edit_message)

    def payment_action(self, chat_id: int, admin_id: int, action: str, value: str) -> None:
        if action == "method":
            state = self.store.state(admin_id)
            if not state or state[0] != "payment_method":
                self.show(chat_id,"To‘lov jarayoni tugagan.",with_navigation([]),edit=self.edit_message)
                return
            data=state[1]
            data["method"]=value
            self.store.set_state(admin_id,"payment_confirm",data)
            self.show(chat_id,f"💰 TO‘LOVNI TASDIQLASH\n\n👤 {data['name']}\n📅 {data['month']}\n💵 {money(data['amount'])}\n💳 {value}",with_navigation([[("✅ Tasdiqlash","flow:payment-save"),("❌ Bekor qilish","cancel")]]),edit=self.edit_message)

    def teacher_groups(self, teacher_id: int, only_today: bool = False) -> list[sqlite3.Row]:
        query="""SELECT g.*,(SELECT count(*) FROM users s WHERE s.group_id=g.id AND s.role='student' AND s.status='active') student_count
                 FROM groups g WHERE g.teacher_id=? AND g.status='active'"""
        args: tuple[Any,...]=(teacher_id,)
        if only_today:
            query+=" AND instr(days,?)>0"
            args+=(WEEKDAY_KEYS[date.today().weekday()],)
        return self.store.all(query+" ORDER BY start_time",args)

    def teacher_action(self, chat_id: int, teacher_id: int, action: str) -> None:
        if action.startswith("request-remove:"):
            _,group_id,student_id=action.split(":",2)
            group=self.store.one("SELECT name FROM groups WHERE id=? AND teacher_id=?",(int(group_id),teacher_id))
            student=self.store.one("SELECT name FROM users WHERE telegram_id=? AND group_id=? AND role='student'",(int(student_id),int(group_id)))
            if not group or not student:
                self.show(chat_id,"Bu o‘quvchi yoki guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            self.ask(chat_id,teacher_id,"teacher_remove_reason",f"⚠️ {student['name']} o‘quvchisini {group['name']} guruhidan chiqarish sababini yozing:",{
                "group_id":int(group_id),"group_name":group["name"],"student_id":int(student_id),"student_name":student["name"],
            })
            return
        if action.startswith("request-") and action[8:].isdigit():
            group_id=int(action.split("-",1)[1])
            group=self.store.one("SELECT id,name FROM groups WHERE id=? AND teacher_id=? AND status='active'",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            students=self.store.all("SELECT telegram_id,name FROM users WHERE group_id=? AND role='student' AND status='active' ORDER BY name",(group_id,))
            rows=[
                [("➕ O‘quvchi qo‘shish so‘rovi",f"flow:teacher-request-add-{group_id}")],
                [("❌ O‘quvchini chiqarish so‘rovi",f"teacher:request-remove-list-{group_id}")],
                [("👥 Yangi guruh ochish so‘rovi","flow:teacher-request-group")],
            ]
            self.show(chat_id,f"📨 ADMIN UCHUN SO‘ROV · {group['name']}\n\nO‘quvchini o‘chirish yoki guruh ochish admin tasdig‘i bilan amalga oshadi.",with_navigation(rows),edit=self.edit_message)
            return
        if action.startswith("request-remove-list-"):
            group_id=int(action.rsplit("-",1)[1])
            group=self.store.one("SELECT name FROM groups WHERE id=? AND teacher_id=?",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            people=self.store.all("SELECT telegram_id,name FROM users WHERE group_id=? AND role='student' AND status='active' ORDER BY name",(group_id,))
            self.show(chat_id,"Qaysi o‘quvchini guruhdan chiqarish bo‘yicha so‘rov yuborasiz?",with_navigation([[(p["name"],f"teacher:request-remove:{group_id}:{p['telegram_id']}")] for p in people]),edit=self.edit_message)
            return
        if action.startswith("group-"):
            group_id=int(action.split("-",1)[1])
            group=self.store.one(
                """SELECT g.*,(SELECT count(*) FROM users s WHERE s.group_id=g.id AND s.role='student' AND s.status='active') student_count
                   FROM groups g WHERE g.id=? AND g.teacher_id=? AND g.status='active'""",
                (group_id,teacher_id),
            )
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            body=(f"👥 {group['name'].upper()}\n📚 Kurs: {group['course']}\n🕐 {group['start_time']}–{group['end_time']}\n"
                  f"🏫 {group['room'] or 'Xona belgilanmagan'}\n👨‍🎓 O‘quvchilar: {group['student_count']}\n📅 {group['days']}")
            self.show(chat_id,body,with_navigation([
                [("✅ Davomat olish",f"flow:attendance-{group_id}")],
                [("👨‍🎓 O‘quvchilar",f"teacher:students-{group_id}"),("📊 Statistika",f"teacher:stats-{group_id}")],
                [("📢 Guruhga xabar",f"teacher:message-{group_id}")],
                [("📨 Admin uchun so‘rov",f"teacher:request-{group_id}")],
            ]),edit=self.edit_message)
            return
        if action.startswith("students-"):
            group_id=int(action.split("-",1)[1])
            group=self.store.one("SELECT id,name FROM groups WHERE id=? AND teacher_id=? AND status='active'",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            people=self.store.all("SELECT * FROM users WHERE group_id=? AND role='student' AND status='active' ORDER BY name",(group_id,))
            buttons=[[(f"👤 {user_label(p)}",f"teacher:student-{p['telegram_id']}")] for p in people]
            self.show(chat_id,f"👨‍🎓 {group['name']} · {len(people)} ta o‘quvchi",with_navigation(buttons),edit=self.edit_message)
            return
        if action.startswith("student-attendance-"):
            student_id=int(action.rsplit("-",1)[1])
            allowed=self.store.one("SELECT 1 FROM users WHERE telegram_id=? AND group_id IN (SELECT id FROM groups WHERE teacher_id=?)",(student_id,teacher_id))
            records=self.store.all("SELECT day,status FROM attendance WHERE user_id=? ORDER BY day DESC LIMIT 20",(student_id,)) if allowed else []
            self.show(chat_id,"✅ DAVOMAT TARIXI\n\n"+("\n".join(f"{r['day']} · {ATTENDANCE_NAMES.get(r['status'],r['status'])}" for r in records) if records else "Davomat yozuvi yo‘q."),with_navigation([]),edit=self.edit_message)
            return
        if action.startswith("student-"):
            student_id=int(action.split("-",1)[1])
            student=self.store.one("SELECT * FROM users WHERE telegram_id=? AND role='student' AND group_id IN (SELECT id FROM groups WHERE teacher_id=?)",(student_id,teacher_id))
            if not student:
                self.show(chat_id,"Bu o‘quvchi sizning guruhlaringizda topilmadi.",with_navigation([]),edit=self.edit_message)
                return
            attendance=self.store.one("SELECT count(*) total,sum(status IN ('present','late')) good FROM attendance WHERE user_id=?",(student_id,))
            percent=round(attendance["good"]*100/attendance["total"]) if attendance["total"] else 0
            paid=self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE user_id=? AND month=?",(student_id,date.today().strftime("%Y-%m")))["n"]
            fee=int(self.setting("monthly_fee"))
            self.show(chat_id,f"👤 {user_label(student)}\n📚 {student['course']}\n✅ Davomat: {percent}%\n{STATUS_NAMES.get(student['status'])}\n💰 To‘lov: {'🟢 To‘langan' if paid>=fee else '🔴 Qarzdor'}\n\nTo‘lovni o‘zgartirish vakolati faqat admin’da.",with_navigation([
                [("✅ Davomat tarixi",f"teacher:student-attendance-{student_id}")],
                [("📢 Adminga murojaat",f"teacher:contact-student-{student_id}")],
            ]),edit=self.edit_message)
            return
        if action.startswith("stats-"):
            group_id=int(action.split("-",1)[1])
            group=self.store.one("SELECT name FROM groups WHERE id=? AND teacher_id=?",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            stats=self.store.one("SELECT count(*) total,sum(status IN ('present','late')) good FROM attendance WHERE group_id=?",(group_id,))
            percent=round(stats["good"]*100/stats["total"]) if stats["total"] else 0
            self.show(chat_id,f"📊 {group['name']}\n\nO‘rtacha davomat: {percent}%\nDavomat belgilari: {stats['total']}",with_navigation([]),edit=self.edit_message)
            return
        if action.startswith("payments-"):
            group_id=int(action.split("-",1)[1])
            group=self.store.one("SELECT name FROM groups WHERE id=? AND teacher_id=?",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            rows=self.store.all("SELECT s.name,coalesce(sum(p.amount),0) paid FROM users s LEFT JOIN payments p ON p.user_id=s.telegram_id AND p.month=? WHERE s.group_id=? AND s.role='student' AND s.status='active' GROUP BY s.telegram_id ORDER BY s.name",(date.today().strftime("%Y-%m"),group_id))
            fee=int(self.setting("monthly_fee"))
            self.show(chat_id,f"💰 {group['name']} · To‘lov holati\n\n"+"\n".join(f"{r['name']} · {'🟢 To‘langan' if r['paid']>=fee else '🔴 Qarzdor'}" for r in rows),with_navigation([]),edit=self.edit_message)
            return
        if action.startswith("message-"):
            group_id=int(action.split("-",1)[1])
            group=self.store.one("SELECT id,name FROM groups WHERE id=? AND teacher_id=? AND status='active'",(group_id,teacher_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            recipients=self.store.all("SELECT telegram_id FROM users WHERE group_id=? AND role='student' AND status='active'",(group_id,))
            self.ask(chat_id,teacher_id,"teacher_group_message",f"✍️ {group['name']} guruhiga yuboriladigan xabarni yozing:",{"group_id":group_id,"group_name":group["name"],"recipients":[r["telegram_id"] for r in recipients]})
            return
        if action.startswith("contact-student-"):
            student_id=int(action.rsplit("-",1)[1])
            student=self.store.one("SELECT name FROM users WHERE telegram_id=? AND group_id IN (SELECT id FROM groups WHERE teacher_id=?)",(student_id,teacher_id))
            if not student:
                self.show(chat_id,"Bu o‘quvchi sizning guruhlaringizda topilmadi.",with_navigation([]),edit=self.edit_message)
                return
            self.ask(chat_id,teacher_id,"teacher_contact",f"📨 {student['name']} haqida admin uchun murojaat matnini yuboring:",{"student_id":student_id})
            return
        if action in ("today","groups","schedule"):
            today_only=action=="today"
            groups=self.teacher_groups(teacher_id,today_only)
            title={"today":"📅 BUGUNGI DARSLAR","groups":"👥 MENING GURUHLARIM","schedule":"📅 MENING JADVALIM"}[action]
            if action=="schedule":
                body=title+"\n\n"+("\n".join(f"{g['days']}\n{g['start_time']} — {g['name']} ({g['end_time']}) · {g['room']}" for g in groups) if groups else "Guruh biriktirilmagan.")
                self.show(chat_id,body,with_navigation([[(g["name"],f"teacher:group-{g['id']}")] for g in groups]),edit=self.edit_message)
            else:
                body=f"{title}\n\nSizda {len(groups)} ta dars:\n\n"+("\n\n".join(f"{i}. {g['name']}\n🕐 {g['start_time']}–{g['end_time']}\n👨‍🎓 {g['student_count']} o‘quvchi\n🏫 {g['room'] or 'Xona belgilanmagan'}" for i,g in enumerate(groups,1)) if groups else "Sizga guruh biriktirilmagan.")
                self.show(chat_id,body,with_navigation([[(f"{g['start_time']} 👥 {g['name']}",f"teacher:group-{g['id']}")] for g in groups]),edit=self.edit_message)
        elif action == "students":
            groups=self.teacher_groups(teacher_id)
            self.show(chat_id,"Qaysi guruh o‘quvchilarini ko‘ramiz?",with_navigation([[(g["name"],f"teacher:students-{g['id']}")] for g in groups]),edit=self.edit_message)
        elif action == "attendance":
            groups=self.teacher_groups(teacher_id)
            self.show(chat_id,"✅ Davomat uchun guruhni tanlang:",with_navigation([[(g["name"],f"flow:attendance-{g['id']}")] for g in groups]),edit=self.edit_message)
        elif action == "payments":
            groups=self.teacher_groups(teacher_id)
            buttons=[[(g["name"],f"teacher:payments-{g['id']}")] for g in groups]
            self.show(chat_id,"💰 TO‘LOV HOLATI\n\nFaqat ko‘rish mumkin; to‘lov kiritish huquqi admin’da.",with_navigation(buttons),edit=self.edit_message)
        elif action == "contact":
            self.ask(chat_id,teacher_id,"teacher_contact","📢 Admin uchun murojaat matnini yuboring:")
        elif action == "profile":
            teacher=self.person(teacher_id)
            groups=self.teacher_groups(teacher_id)
            students=sum(g["student_count"] for g in groups)
            self.show(chat_id,f"👨‍🏫 PROFILIM\n\nIsm: {user_label(teacher)}\nTelefon: {teacher['phone']}\nTelegram: @{teacher['username'] or '—'}\nGuruhlar: {len(groups)}\nO‘quvchilar: {students}\n\nShaxsiy ma’lumotlarni o‘zgartirish uchun admin bilan bog‘laning.",with_navigation([]),edit=self.edit_message)

    def student_action(self, chat_id: int, student_id: int, action: str) -> None:
        student=self.person(student_id)
        if action=="profile":
            group=self.store.one("SELECT * FROM groups WHERE id=?",(student["group_id"],)) if student and student["group_id"] else None
            self.show(chat_id,f"👤 PROFILIM\n\nIsm: {user_label(student)}\nTelefon: {student['phone']}\nKurs: {student['course']}\nGuruh: {group['name'] if group else 'Biriktirilmagan'}\nHolat: {STATUS_NAMES.get(student['status'])}",with_navigation([]),edit=self.edit_message)
        elif action=="group":
            if student and student["group_id"]:
                self.show_group_for_student(chat_id,student["group_id"])
            else:self.show(chat_id,"Sizga hali guruh biriktirilmagan. Admin bilan bog‘laning.",with_navigation([]),edit=self.edit_message)
        elif action=="attendance":
            rows=self.store.all("SELECT day,status FROM attendance WHERE user_id=? ORDER BY day DESC LIMIT 20",(student_id,))
            self.show(chat_id,"✅ DAVOMATIM\n\n"+("\n".join(f"{r['day']} · {ATTENDANCE_NAMES[r['status']]}" for r in rows) if rows else "Davomat yozuvi yo‘q."),with_navigation([]),edit=self.edit_message)
        elif action=="payments":
            fee=int(self.setting("monthly_fee"))
            paid=self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE user_id=? AND month=strftime('%Y-%m','now','localtime')",(student_id,))["n"]
            self.show(chat_id,f"💰 TO‘LOV HOLATI\n\nOy: {date.today():%B %Y}\nTo‘lanishi kerak: {money(fee)}\nTo‘langan: {money(paid)}\nHolat: {'🟢 To‘langan' if paid>=fee else '🔴 Qarzdor'}",with_navigation([]),edit=self.edit_message)
        elif action=="contact":
            self.ask(chat_id,student_id,"student_contact","📢 Administratorga murojaatingizni yozing:")

    def show_group_for_student(self, chat_id: int, group_id: int) -> None:
        group=self.store.one("SELECT * FROM groups WHERE id=?",(group_id,))
        if not group:
            self.show(chat_id,"Guruh topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        teacher=self.person(group["teacher_id"]) if group["teacher_id"] else None
        self.show(chat_id,f"👥 {group['name']}\n📚 Kurs: {group['course']}\n👨‍🏫 Ustoz: {user_label(teacher) if teacher else '—'}\n📅 {group['days']}\n🕐 {group['start_time']}–{group['end_time']}\n🏫 {group['room']}",with_navigation([]),edit=self.edit_message)

    def flow_callback(self, chat_id: int, user_id: int, action: str) -> None:
        role=self.role(user_id)
        admin_only=("student-search","teacher-search","group-search","student-add","teacher-add","group-add",
                    "room-add","room-schedule","payment-add","payment-search","payment-save","message-send",
                    "settings-fee","student-save","group-save")
        teacher_only=("attendance-","teacher-request-","teacher-message-")
        if (action in admin_only or action.startswith(("payment-for-","payment-user-","message-group-"))) and role!="admin":
            self.show(chat_id,"⛔ Bu amal faqat admin uchun.",with_navigation([]),edit=self.edit_message)
            return
        if action.startswith(teacher_only) and role!="teacher":
            self.show(chat_id,"⛔ Davomatni faqat tegishli ustoz belgilashi mumkin.",with_navigation([]),edit=self.edit_message)
            return
        if action in ("student-search","teacher-search","group-search"):
            self.ask(chat_id,user_id,action.replace("-","_"),"Ism, telefon yoki guruh nomini yuboring:")
        elif action=="student-add":
            self.ask(chat_id,user_id,"student_add","Ism Familiya, telefon, kurs formatida yuboring:")
        elif action=="teacher-add":
            self.ask(chat_id,user_id,"teacher_add","Ustoz ma’lumotlarini yuboring:\nIsm Familiya, Telegram ID, Telefon, Yo‘nalish")
        elif action=="group-add":
            self.ask(chat_id,user_id,"group_add","Guruh ma’lumotlarini vergul bilan yuboring:\nNomi, Kurs, Kunlar (Dush/Chor/Jum), Boshlanish, Tugash, Xona")
        elif action=="room-add":
            self.ask(chat_id,user_id,"room_add","Yangi xona nomini yuboring. Masalan: 101-xona")
        elif action=="room-schedule":
            self.ask(chat_id,user_id,"room_filter","Xona nomini yuboring:")
        elif action=="payment-add":
            self.ask(chat_id,user_id,"payment_student","To‘lov qilgan o‘quvchining ismi yoki telefonini kiriting:")
        elif action=="payment-search":
            self.ask(chat_id,user_id,"payment_search","O‘quvchi ismi yoki telefon raqamini yuboring:")
        elif action.startswith("payment-for-"):
            self.payment_start_for(chat_id,user_id,int(action.rsplit("-",1)[1]))
        elif action.startswith("payment-user-"):
            self.payment_start_for(chat_id,user_id,int(action.rsplit("-",1)[1]))
        elif action=="payment-save":
            self.save_payment(chat_id,user_id)
        elif action.startswith("message-group-"):
            self.start_broadcast(chat_id,user_id,action)
        elif action=="message-send":
            self.send_broadcast(chat_id,user_id)
        elif action=="teacher-message-send":
            state=self.store.state(user_id)
            if not state or state[0]!="teacher_group_message_confirm":
                self.show(chat_id,"Xabar yuborish jarayoni topilmadi.",with_navigation([]),edit=self.edit_message)
                return
            data=state[1]
            delivered=0
            for recipient in data["recipients"]:
                try:
                    self.api("sendMessage",{"chat_id":recipient,"text":f"📢 {data['group_name']}:\n\n{data['body']}"})
                    delivered+=1
                except BotError as error:
                    LOG.warning("Teacher broadcast to %s failed: %s",recipient,error)
            self.store.execute("INSERT INTO messages(sender_id,target,body) VALUES(?,?,?)",(user_id,f"group:{data['group_id']}",data["body"]))
            self.record(user_id,f"{data['group_name']} guruhiga xabar yubordi: {delivered} ta")
            self.store.clear_state(user_id)
            self.show(chat_id,f"✅ Xabar yuborildi: {delivered} ta o‘quvchiga.",with_navigation([]),edit=self.edit_message)
        elif action=="settings-fee":
            self.ask(chat_id,user_id,"settings_fee","Yangi oylik to‘lov summasini so‘mda yuboring:")
        elif action.startswith("teacher-request-add-"):
            group_id=int(action.rsplit("-",1)[1])
            group=self.store.one("SELECT name FROM groups WHERE id=? AND teacher_id=? AND status='active'",(group_id,user_id))
            if not group:
                self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
                return
            self.ask(chat_id,user_id,"teacher_add_request","O‘quvchi ism-familiyasi va telefonini vergul bilan yuboring:\nAli Valiyev, +998901234567",{"group_id":group_id,"group_name":group["name"]})
        elif action=="teacher-request-group":
            self.ask(chat_id,user_id,"teacher_group_request","Yangi guruh taklifini yuboring:\nNomi, Kurs, Kunlar, Boshlanish, Tugash, Xona")
        elif action.startswith("attendance-finish-"):
            self.confirm_attendance(chat_id,user_id,int(action.rsplit("-",1)[1]))
        elif action.startswith("attendance-group-"):
            self.show_attendance(chat_id,user_id,int(action.rsplit("-",1)[1]),{})
        elif action.startswith("attendance-all-"):
            self.set_all_present(chat_id,user_id,int(action.rsplit("-",1)[1]))
        elif action.startswith("attendance-"):
            self.start_attendance(chat_id,user_id,int(action.split("-",1)[1]))
        elif action=="attendance-save":
            self.save_attendance(chat_id,user_id)
        elif action.startswith("student-save"):
            self.save_new_student(chat_id,user_id)
        elif action=="group-save":
            self.save_new_group(chat_id,user_id)
        else:
            if action.startswith("attendance-finish-"):
                self.confirm_attendance(chat_id,user_id,int(action.rsplit("-",1)[1]))
            else:
                self.show(chat_id,"Bu jarayon mavjud emas.",with_navigation([]),edit=self.edit_message)

    def payment_start_for(self, chat_id: int, admin_id: int, student_id: int) -> None:
        student=self.person(student_id)
        if not student:
            self.show(chat_id,"O‘quvchi topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        fee=int(self.setting("monthly_fee"))
        self.ask(chat_id,admin_id,"payment_amount",f"👤 {user_label(student)}\nOylik to‘lov: {money(fee)}\n\nQabul qilingan summani kiriting:",{"user_id":student_id,"name":user_label(student)})

    def save_payment(self, chat_id: int, admin_id: int) -> None:
        state=self.store.state(admin_id)
        if not state or state[0]!="payment_confirm":
            self.show(chat_id,"To‘lov jarayoni topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        data=state[1]
        self.store.execute("INSERT INTO payments(user_id,month,amount,method,created_by) VALUES(?,?,?,?,?)",(data["user_id"],data["month"],data["amount"],data["method"],admin_id))
        paid=self.store.one("SELECT coalesce(sum(amount),0) n FROM payments WHERE user_id=? AND month=?",(data["user_id"],data["month"]))["n"]
        if paid>=int(self.setting("monthly_fee")):
            self.store.execute("UPDATE users SET payment_due=date('now','localtime','+1 month') WHERE telegram_id=?",(data["user_id"],))
        self.record(admin_id,f"{money(data['amount'])} to‘lov kiritdi · {data['name']}")
        self.store.clear_state(admin_id)
        self.show(chat_id,f"✅ To‘lov saqlandi.\n👤 {data['name']}\n📅 {data['month']}\n💰 {money(data['amount'])}\n💳 {data['method']}",with_navigation([]),edit=self.edit_message)

    def send_broadcast(self, chat_id: int, admin_id: int) -> None:
        state=self.store.state(admin_id)
        if not state or state[0]!="message_confirm":
            self.show(chat_id,"Xabar yuborish jarayoni topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        data=state[1]
        delivered=0
        failed=0
        for recipient in data["recipients"]:
            try:
                self.api("sendMessage",{"chat_id":recipient,"text":data["body"]})
                delivered+=1
            except BotError as error:
                LOG.warning("Broadcast to %s failed: %s",recipient,error)
                failed+=1
        self.store.execute("INSERT INTO messages(sender_id,target,body) VALUES(?,?,?)",(admin_id,data["target"],data["body"]))
        self.record(admin_id,f"Xabar yubordi: {data['target']} · {delivered} ta")
        self.store.clear_state(admin_id)
        self.show(chat_id,f"📢 Yuborish yakunlandi.\n✅ Yetkazildi: {delivered}\n⚠️ Yetkazilmadi: {failed}",with_navigation([]),edit=self.edit_message)

    def save_new_student(self, chat_id: int, admin_id: int) -> None:
        state=self.store.state(admin_id)
        if not state or state[0]!="student_add_confirm":
            self.show(chat_id,"O‘quvchi qo‘shish jarayoni topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        data=state[1]
        local_id=self.store.one("SELECT min(telegram_id) value FROM users WHERE telegram_id<0")["value"]
        local_id=(local_id or 0)-1
        self.store.execute("INSERT INTO users(telegram_id,name,phone,role,status,course,payment_due) VALUES(?,?,?,'student','active',?,date('now','localtime','+30 days'))",(local_id,data["name"],data["phone"],data["course"]))
        self.record(admin_id,f"Yangi o‘quvchi qo‘shdi: {data['name']}")
        self.store.clear_state(admin_id)
        self.show(chat_id,f"✅ {data['name']} o‘quvchilar ro‘yxatiga qo‘shildi.\nEslatma: Telegram hisobini keyinroq bog‘lash mumkin.",with_navigation([]),edit=self.edit_message)

    def add_teacher_from_text(self, chat_id: int, admin_id: int, text: str) -> None:
        parts=[p.strip() for p in text.split(",")]
        if len(parts)<4 or not parts[1].isdigit():
            self.show(chat_id,"To‘g‘ri format:\nIsm Familiya, Telegram ID, Telefon, Yo‘nalish")
            return
        try:
            self.store.execute("INSERT INTO users(telegram_id,name,phone,role,status,course) VALUES(?,?,?,'teacher','active',?)",(int(parts[1]),parts[0],parts[2],parts[3]))
            self.record(admin_id,f"Ustoz qo‘shdi: {parts[0]}")
            self.store.clear_state(admin_id)
            self.show(chat_id,f"✅ {parts[0]} ustoz sifatida qo‘shildi.",with_navigation([]),edit=self.edit_message)
        except sqlite3.IntegrityError:
            self.show(chat_id,"Bu Telegram ID ro‘yxatda bor yoki ma’lumotlar xato.")

    def save_new_group(self, chat_id: int, admin_id: int) -> None:
        state=self.store.state(admin_id)
        if not state or state[0]!="group_add_confirm":
            self.show(chat_id,"Guruh yaratish jarayoni topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        d=state[1]
        try:
            self.store.execute("INSERT INTO groups(name,course,days,start_time,end_time,room) VALUES(?,?,?,?,?,?)",(d["name"],d["course"],d["days"],d["start"],d["end"],d["room"]))
        except sqlite3.IntegrityError:
            self.show(chat_id,"Bu guruh nomi allaqachon mavjud.",with_navigation([]),edit=self.edit_message)
            self.store.clear_state(admin_id)
            return
        self.record(admin_id,f"Guruh yaratdi: {d['name']}")
        self.store.clear_state(admin_id)
        self.show(chat_id,f"✅ {d['name']} guruhi yaratildi.",with_navigation([]),edit=self.edit_message)

    def start_attendance(self, chat_id: int, teacher_id: int, group_id: int) -> None:
        group=self.store.one("SELECT * FROM groups WHERE id=? AND teacher_id=? AND status='active'",(group_id,teacher_id))
        if not group:
            self.show(chat_id,"Bu guruh sizga biriktirilmagan.",with_navigation([]),edit=self.edit_message)
            return
        day=date.today().isoformat()
        students=self.store.all("SELECT * FROM users WHERE group_id=? AND role='student' AND status='active' ORDER BY name",(group_id,))
        if not students:
            self.show(chat_id,"Guruhda faol o‘quvchi yo‘q.",with_navigation([]),edit=self.edit_message)
            return
        existing=self.store.all("SELECT user_id,status FROM attendance WHERE group_id=? AND day=?",(group_id,day))
        marks={str(row["user_id"]):row["status"] for row in existing}
        for student in students:
            marks.setdefault(str(student["telegram_id"]),"present")
        data={"group_id":group_id,"day":day,"marks":marks}
        self.store.set_state(teacher_id,"attendance_edit",data)
        self.show_attendance(chat_id,teacher_id,group_id,data)

    def show_attendance(self, chat_id: int, teacher_id: int, group_id: int, data: dict[str,Any]) -> None:
        group=self.store.one("SELECT * FROM groups WHERE id=?",(group_id,))
        students=self.store.all("SELECT * FROM users WHERE group_id=? AND role='student' AND status='active' ORDER BY name",(group_id,))
        state=self.store.state(teacher_id)
        marks=state[1].get("marks",{}) if state and state[0]=="attendance_edit" else data.get("marks",{})
        labels={"present":"🟢","late":"🟡","excused":"🔵","absent":"🔴"}
        rows=[]
        for student in students:
            status=marks.get(str(student["telegram_id"]),"present")
            rows.append([(f"{labels.get(status,'❓')} {user_label(student)} — {ATTENDANCE_NAMES.get(status)}",f"att:{group_id}:{student['telegram_id']}:{self.next_attendance(status)}")])
        rows.extend([[("🟢 Hammasi keldi",f"flow:attendance-all-{group_id}")],
                     [("💾 Davomatni saqlash",f"flow:attendance-finish-{group_id}")],
                     [("◀️ Orqaga","back"),(HOME,"home")]])
        self.show(chat_id,f"📋 DAVOMAT · {group['name']}\n📅 {date.today():%d.%m.%Y}\n\nHar bir o‘quvchini bosib holatini almashtiring. Dastlab hamma «Keldi» deb belgilanadi.",keyboard(rows),edit=self.edit_message)

    @staticmethod
    def next_attendance(status: str) -> str:
        order=("present","late","excused","absent")
        return order[(order.index(status)+1)%len(order)] if status in order else "present"

    def mark_attendance(self, chat_id: int, teacher_id: int, group_id: int, person_id: int, status: str) -> None:
        group=self.store.one("SELECT id FROM groups WHERE id=? AND teacher_id=?",(group_id,teacher_id))
        state=self.store.state(teacher_id)
        if not group or not state or state[0]!="attendance_edit":
            self.show(chat_id,"Davomat sessiyasi tugagan. Qaytadan boshlang.",with_navigation([]),edit=self.edit_message)
            return
        data=state[1]
        data.setdefault("marks",{})[str(person_id)]=status
        self.store.set_state(teacher_id,"attendance_edit",data)
        self.show_attendance(chat_id,teacher_id,group_id,data)

    def set_all_present(self, chat_id: int, teacher_id: int, group_id: int) -> None:
        state=self.store.state(teacher_id)
        if not state or state[0]!="attendance_edit":
            self.show(chat_id,"Davomat sessiyasi topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        students=self.store.all("SELECT telegram_id FROM users WHERE group_id=? AND role='student' AND status='active'",(group_id,))
        data=state[1]
        data["marks"]={str(s["telegram_id"]):"present" for s in students}
        self.store.set_state(teacher_id,"attendance_edit",data)
        self.show_attendance(chat_id,teacher_id,group_id,data)

    def confirm_attendance(self, chat_id: int, teacher_id: int, group_id: int) -> None:
        state=self.store.state(teacher_id)
        if not state or state[0]!="attendance_edit":
            self.show(chat_id,"Davomat sessiyasi topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        counts={status:sum(1 for v in state[1]["marks"].values() if v==status) for status in ATTENDANCE_NAMES}
        body="📊 DAVOMATNI TASDIQLASH\n\n"+"\n".join(f"{ATTENDANCE_NAMES[s]}: {counts[s]}" for s in ATTENDANCE_NAMES)
        self.show(chat_id,body,with_navigation([[("✅ Tasdiqlash",f"flow:attendance-save"),("✏️ O‘zgartirish",f"flow:attendance-group-{group_id}")]]),edit=self.edit_message)

    def save_attendance(self, chat_id: int, teacher_id: int) -> None:
        state=self.store.state(teacher_id)
        if not state or state[0]!="attendance_edit":
            self.show(chat_id,"Davomat sessiyasi topilmadi.",with_navigation([]),edit=self.edit_message)
            return
        data=state[1]
        group=self.store.one("SELECT name FROM groups WHERE id=?",(data["group_id"],))
        for person_id,status in data["marks"].items():
            self.store.execute("INSERT INTO attendance(user_id,group_id,day,status,marked_by) VALUES(?,?,?,?,?) "
                               "ON CONFLICT(user_id,group_id,day) DO UPDATE SET status=excluded.status,marked_by=excluded.marked_by",
                               (int(person_id),data["group_id"],data["day"],status,teacher_id))
        counts={s:sum(1 for v in data["marks"].values() if v==s) for s in ATTENDANCE_NAMES}
        self.record(teacher_id,f"{group['name'] if group else 'Guruh'} davomatini saqladi")
        self.store.clear_state(teacher_id)
        self.show(chat_id,"✅ Davomat saqlandi.\n\n"+"\n".join(f"{ATTENDANCE_NAMES[s]}: {counts[s]}" for s in ATTENDANCE_NAMES),with_navigation([]),edit=self.edit_message)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.callback(update["callback_query"])
        elif "message" in update and update["message"].get("text"):
            self.handle_text(update["message"])
        elif "message" in update and update["message"].get("contact"):
            message=update["message"]
            state=self.store.state(message["from"]["id"])
            if state and state[0].endswith(":phone"):
                contact=message["contact"]
                if contact.get("user_id")==message["from"]["id"]:
                    self.consume_text(message["chat"]["id"],message["from"]["id"],contact["phone_number"],state[0],state[1])
                else:
                    self.show(message["chat"]["id"],"Iltimos, o‘zingizning telefon raqamingizni yuboring.")

    def run(self) -> None:
        if not self.token:
            raise SystemExit("TELEGRAM_BOT_TOKEN sozlanmagan. README.md dagi ishga tushirish bo‘limiga qarang.")
        if not self.admin_ids:
            raise SystemExit("TELEGRAM_ADMIN_IDS sozlanmagan. Kamida bitta Telegram ID ni vergul bilan ajratib kiriting.")
        me=self.api("getMe",{})
        LOG.info("SILICON bot @%s ishga tushdi. Adminlar: %s",me.get("username"),len(self.admin_ids))
        while True:
            try:
                updates=self.api("getUpdates",{"offset":self.offset,"timeout":30,"allowed_updates":["message","callback_query"]})
                for update in updates:
                    self.offset=update["update_id"]+1
                    try:
                        self.handle_update(update)
                    except Exception:
                        LOG.exception("Yangilanishni qayta ishlashda xato")
            except KeyboardInterrupt:
                LOG.info("Bot to‘xtatildi.")
                return
            except BotError:
                LOG.exception("Telegram bilan aloqa xatosi")
                time.sleep(3)


def main() -> None:
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    SiliconBot().run()


if __name__ == "__main__":
    main()
