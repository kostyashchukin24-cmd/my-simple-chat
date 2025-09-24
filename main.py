import asyncio
import os
import hashlib
import psycopg2
from psycopg2.extras import RealDictCursor
from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js

online_users = set()

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friend_requests (
            id SERIAL PRIMARY KEY,
            from_user TEXT NOT NULL,
            to_user TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (from_user, to_user)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def register_user(username, password):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, hash_password(password)))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def authenticate(username, password):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row and row["password_hash"] == hash_password(password)

# --- Друзья ---
def send_friend_request(from_user, to_user):
    if from_user == to_user:
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO friend_requests (from_user, to_user, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT (from_user, to_user) DO NOTHING
        """, (from_user, to_user))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def get_pending_requests(to_user):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT from_user FROM friend_requests WHERE to_user = %s AND status = 'pending'", (to_user,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["from_user"] for r in rows]

def accept_friend_request(from_user, to_user):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE friend_requests SET status = 'accepted' WHERE from_user = %s AND to_user = %s", (from_user, to_user))
    conn.commit()
    cur.close()
    conn.close()

def reject_friend_request(from_user, to_user):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE friend_requests SET status = 'rejected' WHERE from_user = %s AND to_user = %s", (from_user, to_user))
    conn.commit()
    cur.close()
    conn.close()

def get_friends(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT from_user FROM friend_requests WHERE to_user = %s AND status = 'accepted'
        UNION
        SELECT to_user FROM friend_requests WHERE from_user = %s AND status = 'accepted'
    """, (username, username))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["from_user"] for r in rows]

# --- Чат ---
def load_messages():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT username, text FROM messages
        WHERE created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY created_at ASC
        LIMIT 100
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r["username"], r["text"]) for r in rows]

def save_message(user, text):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (username, text) VALUES (%s, %s)", (user, text))
    conn.commit()
    cur.close()
    conn.close()

async def refresh_msgs(my_name, msg_box):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT MAX(created_at) FROM messages")
    last_time = cur.fetchone()[0] or '2020-01-01'
    cur.close()
    conn.close()

    while True:
        await asyncio.sleep(1)
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT username, text, created_at FROM messages
            WHERE created_at > %s
            ORDER BY created_at ASC
        """, (last_time,))
        new = cur.fetchall()
        cur.close()
        conn.close()

        for msg in new:
            if msg["username"] != my_name:
                txt = f'📢 {msg["text"]}' if msg["username"] == '📢' else f"`{msg['username']}`: {msg['text']}"
                msg_box.append(put_markdown(txt))
                last_time = msg["created_at"]

# --- Меню друзей ---
async def show_friends_menu(nickname):
    while True:
        clear()
        put_markdown("## 👥 Друзья")
        
        friends = get_friends(nickname)
        pending = get_pending_requests(nickname)

        if friends:
            put_markdown("### ✅ Ваши друзья:")
            put_table([[f] for f in friends], header=["Имя"])
        else:
            put_text("Список друзей пуст.")

        if pending:
            put_markdown("### 📬 Входящие запросы:")
            for user in pending:
                put_row([
                    put_text(user),
                    put_buttons([
                        {'label': '✅ Принять', 'value': 'accept', 'color': 'success'},
                        {'label': '❌ Отклонить', 'value': 'reject', 'color': 'danger'}
                    ], onclick=[
                        lambda u=user: accept_friend_request(u, nickname),
                        lambda u=user: reject_friend_request(u, nickname)
                    ])
                ])
        else:
            put_text("Нет новых запросов.")

        put_text("")
        target = await input("Добавить в друзья (имя)", placeholder="Имя", required=False)
        if target:
            if target == nickname:
                toast("Нельзя добавить себя!")
            elif send_friend_request(nickname, target):
                toast(f"Запрос отправлен {target}!")
            else:
                toast("Не удалось отправить запрос.")

        # Кнопка "Назад"
        await actions("", buttons=['⬅️ Назад в чат'])
        break

# --- Аутентификация ---
async def auth():
    while True:
        act = await actions("Добро пожаловать!", buttons=["Войти", "Зарегистрироваться"])
        if act == "Зарегистрироваться":
            data = await input_group("Регистрация", [
                input("Имя", name="user", required=True),
                input("Пароль", name="pwd", type=PASSWORD, required=True),
                input("Повторите пароль", name="pwd2", type=PASSWORD, required=True)
            ], validate=lambda d: ("pwd2", "Пароли не совпадают!") if d["pwd"] != d["pwd2"] else None)

            if register_user(data["user"], data["pwd"]):
                toast("✅ Регистрация успешна!")
            else:
                toast("❌ Имя занято!")

        elif act == "Войти":
            data = await input_group("Вход", [
                input("Имя", name="user", required=True),
                input("Пароль", name="pwd", type=PASSWORD, required=True)
            ])
            if authenticate(data["user"], data["pwd"]):
                return data["user"]
            else:
                toast("❌ Неверное имя или пароль!")

# --- Основной чат ---
async def chat_main(nickname):
    global online_users
    if nickname in online_users:
        put_error("Вы уже в чате!")
        await asyncio.sleep(2)
        return

    online_users.add(nickname)

    put_markdown("## 💬 Чат (сообщения — 24 ч)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    save_message('📢', f'`{nickname}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{nickname}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(nickname, msg_box))

    try:
        while True:
            data = await input_group("Сообщение", [
                input(name="msg", placeholder="Текст..."),
                actions(name="cmd", buttons=[
                    "Отправить",
                    {"label": "👥 Друзья", "value": "friends", "type": "submit"},
                    {"label": "Выйти", "type": "cancel"}
                ])
            ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

            if data is None:
                break
            elif data["cmd"] == "friends":
                await show_friends_menu(nickname)
                # После возврата — перерисуем чат
                clear()
                await chat_main(nickname)  # рекурсивный возврат (простой способ)
                return
            else:
                msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
                save_message(nickname, data['msg'])

    finally:
        refresh_task.close()
        online_users.discard(nickname)
        save_message('📢', f'`{nickname}` покинул чат!')
        toast("Вы вышли из чата!")
        put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

# --- Запуск ---
async def main():
    init_db()
    nickname = await auth()
    clear()
    await chat_main(nickname)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
