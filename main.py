import asyncio
import os
import hashlib
import psycopg2
from psycopg2.extras import RealDictCursor
from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js, info

# Глобальное хранилище онлайн-пользователей (по имени из БД)
online_users = set()

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # Таблица сообщений
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                    (username, hash_password(password)))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False  # имя занято
    finally:
        cur.close()
        conn.close()

def authenticate_user(username, password):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT username, password_hash FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user and user["password_hash"] == hash_password(password):
        return True
    return False

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

async def auth_flow():
    while True:
        action = await actions("Добро пожаловать!", buttons=['Войти', 'Зарегистрироваться'])
        if action == 'Зарегистрироваться':
            data = await input_group("Регистрация", [
                input('Имя пользователя', name='username', required=True),
                input('Пароль', name='password', type=PASSWORD, required=True),
                input('Повторите пароль', name='password2', type=PASSWORD, required=True)
            ], validate=lambda d: ('password2', 'Пароли не совпадают!') if d['password'] != d['password2'] else None)

            if register_user(data['username'], data['password']):
                toast("Регистрация успешна! Теперь войдите.")
            else:
                toast("Имя занято! Выберите другое.")

        elif action == 'Войти':
            data = await input_group("Вход", [
                input('Имя пользователя', name='username', required=True),
                input('Пароль', name='password', type=PASSWORD, required=True)
            ])
            if authenticate_user(data['username'], data['password']):
                return data['username']
            else:
                toast("Неверное имя или пароль!")

async def main():
    global online_users

    # Инициализация БД (выполняется один раз)
    init_db()

    put_markdown("## 💬 Защищённый чат (сообщения хранятся 24 часа)")

    # Аутентификация
    nickname = await auth_flow()

    if nickname in online_users:
        toast("Вы уже в чате в другой вкладке!")
        await sleep(3)
        run_js('location.reload()')
        return

    online_users.add(nickname)

    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загрузка истории
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
                actions(name="cmd", buttons=["Отправить", {"label": "Выйти", "type": "cancel"}])
            ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

            if data is None:
                break

            msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
            save_message(nickname, data['msg'])

    finally:
        refresh_task.close()
        online_users.discard(nickname)
        save_message('📢', f'`{nickname}` покинул чат!')
        toast("Вы вышли из чата!")
        put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
