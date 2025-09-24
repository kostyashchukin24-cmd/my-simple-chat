import asyncio
import os
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js

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
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

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

def register_user(username, password):
    conn = get_db()
    cur = conn.cursor()
    try:
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        cur.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (username, hashed))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def authenticate_user(username, password):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        stored_hash = row["password_hash"]
        return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
    return False

# Инициализация БД
init_db()

async def main():
    put_markdown("## 💬 Чат (сообщения хранятся 24 часа)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загрузка истории
    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    # Выбор: регистрация или вход
    auth_choice = await radio("Выберите действие", options=['Войти', 'Зарегистрироваться'], required=True)

    nickname = None
    if auth_choice == 'Зарегистрироваться':
        while True:
            reg_data = await input_group("Регистрация", [
                input(name='username', placeholder="Имя пользователя", required=True),
                input(name='password', type=PASSWORD, placeholder="Пароль", required=True)
            ])
            if register_user(reg_data['username'], reg_data['password']):
                nickname = reg_data['username']
                break
            else:
                toast("Имя уже занято! Попробуйте другое.", color='error')
    else:  # Войти
        while True:
            login_data = await input_group("Вход", [
                input(name='username', placeholder="Имя пользователя", required=True),
                input(name='password', type=PASSWORD, placeholder="Пароль", required=True)
            ])
            if authenticate_user(login_data['username'], login_data['password']):
                nickname = login_data['username']
                break
            else:
                toast("Неверное имя или пароль!", color='error')

    # Приветствие в чате
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
        save_message('📢', f'`{nickname}` покинул чат!')
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
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
