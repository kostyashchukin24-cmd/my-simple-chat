import asyncio
import os
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
    # Таблица сообщений (как в оригинале)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Таблица пользователей — с username, password и id
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def cleanup_old_messages():
    """Удаляет сообщения старше 24 часов."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages WHERE created_at < NOW() - INTERVAL '24 hours'")
    conn.commit()
    cur.close()
    conn.close()

def user_exists(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
    exists = cur.fetchone() is not None
    cur.close()
    conn.close()
    return exists

def register_user(username, password):
    if user_exists(username):
        return False, "Имя уже занято!"
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id", (username, password))
        user_id = cur.fetchone()[0]
        conn.commit()
        return True, user_id
    except Exception as e:
        conn.rollback()
        return False, f"Ошибка регистрации: {e}"
    finally:
        cur.close()
        conn.close()

def login_user(username, password):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s AND password = %s", (username, password))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return True, row[0]
    return False, None

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

def save_message(username, text):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO messages (username, text) VALUES (%s, %s)", (username, text))
    conn.commit()
    cur.close()
    conn.close()

# Инициализация
init_db()
cleanup_old_messages()

async def auth_flow():
    while True:
        action = await actions("💬 Чат — Вход или регистрация", buttons=["Войти", "Регистрация"])
        if action == "Регистрация":
            data = await input_group("Регистрация", [
                input("Имя", name="username", required=True),
                input("Пароль", name="password", type=PASSWORD, required=True)
            ])
            ok, result = register_user(data["username"], data["password"])
            if ok:
                put_success("✅ Регистрация успешна! Теперь войдите.")
                await asyncio.sleep(1.5)
                clear()
            else:
                put_error(f"❌ {result}")
                await asyncio.sleep(2)
                clear()
        else:  # Вход
            data = await input_group("Вход", [
                input("Имя", name="username", required=True),
                input("Пароль", name="password", type=PASSWORD, required=True)
            ])
            ok, user_id = login_user(data["username"], data["password"])
            if ok:
                clear()
                return user_id, data["username"]
            else:
                put_error("❌ Неверное имя или пароль!")
                await asyncio.sleep(2)
                clear()

async def main():
    global online_users

    # Аутентификация
    user_id, username = await auth_flow()

    # Отображаем ID и имя в верхнем левом углу
    put_text(f"[ID: {user_id}] {username}").style(
        "position: fixed; top: 10px; left: 10px; font-weight: bold; color: #2c3e50; z-index: 1000;"
    )

    put_markdown("## 💬 Чат (сообщения хранятся 24 часа)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загружаем историю
    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    # Проверка на дубликат имени (в рамках онлайн-пользователей)
    if username in online_users:
        put_warning("Вы уже в чате под этим именем!")
    online_users.add(username)

    # Системное сообщение
    save_message('📢', f'`{username}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{username}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(username, msg_box))

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=["Отправить", {"label": "Выйти", "type": "cancel"}])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)
        if data is None:
            break
        msg_box.append(put_markdown(f"`{username}`: {data['msg']}"))
        save_message(username, data['msg'])

    # Выход
    refresh_task.close()
    online_users.discard(username)
    save_message('📢', f'`{username}` покинул чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=True, cdn=False)
