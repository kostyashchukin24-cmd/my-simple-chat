import asyncio
import os
import random
import smtplib
import psycopg2
from psycopg2.extras import RealDictCursor
from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js
from email.mime.text import MIMEText
from datetime import datetime, timedelta

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
            email TEXT PRIMARY KEY,
            verified BOOLEAN DEFAULT FALSE,
            verify_code TEXT,
            code_expires TIMESTAMPTZ
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

def generate_and_save_code(email):
    code = str(random.randint(100000, 999999))
    expires = datetime.utcnow() + timedelta(minutes=10)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (email, verify_code, code_expires)
        VALUES (%s, %s, %s)
        ON CONFLICT (email) DO UPDATE
        SET verify_code = %s, code_expires = %s, verified = FALSE
    """, (email, code, expires, code, expires))
    conn.commit()
    cur.close()
    conn.close()
    return code

def verify_code(email, code):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT verify_code, code_expires FROM users
        WHERE email = %s AND verified = FALSE
    """, (email,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False
    stored_code, expires = row
    # Убираем timezone для сравнения
    now = datetime.utcnow()
    expires_naive = expires.replace(tzinfo=None) if expires.tzinfo else expires
    valid = stored_code == code and now < expires_naive
    if valid:
        cur.execute("UPDATE users SET verified = TRUE WHERE email = %s", (email,))
        conn.commit()
    cur.close()
    conn.close()
    return valid

def send_verification_code(email, code):
    msg = MIMEText(f"Ваш код подтверждения для чата: {code}\nДействителен 10 минут.")
    msg["Subject"] = "Код подтверждения чата"
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        server.send_message(msg)

# Инициализация БД
init_db()

async def main():
    global online_users

    put_markdown("## 💬 Чат (сообщения хранятся 24 часа)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загружаем историю
    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    # Шаг 1: ввод email
    while True:
        email = await input("Ваш email (только Gmail)", required=True, placeholder="user@gmail.com")
        if not email.endswith("@gmail.com"):
            toast("Разрешены только адреса Gmail (@gmail.com)!", color="error")
            continue
        break

    # Шаг 2: генерация и отправка кода
    try:
        code = generate_and_save_code(email)
        send_verification_code(email, code)
        toast(f"Код отправлен на {email}", color="success")
    except Exception as e:
        toast(f"Ошибка отправки письма: {str(e)}", color="error")
        return

    # Шаг 3: ввод кода (максимум 3 попытки)
    verified = False
    for attempt in range(3):
        user_code = await input("Введите 6-значный код из письма", required=True, placeholder="123456")
        if verify_code(email, user_code):
            verified = True
            break
        else:
            toast("Неверный или просроченный код!", color="error")

    if not verified:
        toast("Превышено количество попыток. Попробуйте позже.", color="warn")
        return

    # Используем часть до @ как имя (или можно дать выбрать)
    nickname = email.split("@")[0]
    if nickname in online_users or nickname == '📢':
        nickname = email  # fallback на полный email

    online_users.add(nickname)
    save_message('📢', f'`{nickname}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{nickname}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(nickname, msg_box))

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=["Отправить", {"label": "Выйти", "type": "cancel"}])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)
        if data is None:
            break
        msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
        save_message(nickname, data['msg'])

    refresh_task.close()
    online_users.discard(nickname)
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
