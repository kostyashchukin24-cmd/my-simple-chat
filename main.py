import asyncio
import os
import bcrypt

import psycopg2
from psycopg2.extras import RealDictCursor

from pywebio import start_server
from pywebio.input import input, input_group, actions, PASSWORD, select
from pywebio.output import put_markdown, put_scrollable, put_error, put_buttons, toast, output
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
            recipient TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_all_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT display_name FROM users ORDER BY display_name")
    names = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return names

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_user(email: str, password: str, display_name: str) -> bool:
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s)",
            (email, hash_password(password), display_name)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def authenticate_user(email: str, password: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user and verify_password(password, user['password_hash']):
        return dict(user)
    return None

def load_messages_for_user(my_name):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT username, text, recipient FROM messages
        WHERE created_at >= NOW() - INTERVAL '24 hours'
          AND (
            recipient IS NULL
            OR username = %s
            OR recipient = %s
          )
        ORDER BY created_at ASC
        LIMIT 200
    """, (my_name, my_name))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def save_message(sender, text, recipient=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (username, text, recipient) VALUES (%s, %s, %s)",
        (sender, text, recipient)
    )
    conn.commit()
    cur.close()
    conn.close()

def clear_chat():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    cur.close()
    conn.close()

init_db()

async def refresh_msgs(my_name, msg_box, last_time):
    while True:
        await asyncio.sleep(1)
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT username, text, recipient, created_at FROM messages
            WHERE created_at > %s
              AND (
                recipient IS NULL
                OR username = %s
                OR recipient = %s
              )
            ORDER BY created_at ASC
        """, (last_time, my_name, my_name))
        new = cur.fetchall()
        cur.close()
        conn.close()

        for msg in new:
            if msg["recipient"] is None:
                txt = f'📢 {msg["text"]}' if msg["username"] == '📢' else f"`{msg['username']}`: {msg['text']}"
            elif msg["recipient"] == my_name:
                txt = f"📩 **ЛС от `{msg['username']}`**: {msg['text']}"
            else:
                txt = f"📤 **ЛС → `{msg['recipient']}`**: {msg['text']}"
            msg_box.append(put_markdown(txt))
            last_time = msg["created_at"]

async def confirm_and_clear(msg_box):
    confirmed = await actions("⚠️ Очистка чата", [
        "Да, очистить всё",
        "Отмена"
    ], help_text="Это удалит ВСЕ сообщения из чата для всех пользователей!")
    
    if confirmed == "Да, очистить всё":
        clear_chat()
        msg_box.clear()
        toast("✅ Чат очищен!", color='success')
        save_message('📢', 'Чат был полностью очищен.')
        msg_box.append(put_markdown('📢 Чат был полностью очищен.'))

async def main():
    global online_users

    put_markdown("## 💬 Чат с личными сообщениями")

    current_user = None
    while current_user is None:
        action = await actions("Добро пожаловать!", buttons=["Войти", "Зарегистрироваться"])

        if action == "Зарегистрироваться":
            try:
                reg_data = await input_group("Регистрация", [
                    input("Email", name="email", required=True,
                          validate=lambda x: "Email должен содержать @" if "@" not in x else None),
                    input("Пароль", name="password", type=PASSWORD, required=True),
                    input("Ваше имя в чате", name="display_name", required=True, placeholder="Например, Анна")
                ])
                if register_user(reg_data['email'], reg_data['password'], reg_data['display_name']):
                    toast("✅ Регистрация успешна! Теперь войдите.")
                else:
                    toast("❌ Email уже используется!", color='error')
            except Exception as e:
                put_error(f"Ошибка регистрации: {str(e)}")

        elif action == "Войти":
            try:
                login_data = await input_group("Вход", [
                    input("Email", name="email", required=True,
                          validate=lambda x: "Email должен содержать @" if "@" not in x else None),
                    input("Пароль", name="password", type=PASSWORD, required=True)
                ])
                user = authenticate_user(login_data['email'], login_data['password'])
                if user:
                    current_user = user
                    toast(f"Привет, {user['display_name']}!")
                else:
                    toast("❌ Неверный email или пароль!", color='error')
            except Exception as e:
                put_error(f"Ошибка входа: {str(e)}")

    display_name = current_user['display_name']
    online_users.add(display_name)

    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загружаем историю (публичные + личные)
    for msg in load_messages_for_user(display_name):
        if msg["recipient"] is None:
            txt = f'📢 {msg["text"]}' if msg["username"] == '📢' else f"`{msg['username']}`: {msg['text']}"
        elif msg["recipient"] == display_name:
            txt = f"📩 **ЛС от `{msg['username']}`**: {msg['text']}"
        else:
            txt = f"📤 **ЛС → `{msg['recipient']}`**: {msg['text']}"
        msg_box.append(put_markdown(txt))

    save_message('📢', f'`{display_name}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{display_name}` присоединился к чату'))

    # Получаем last_time для обновления
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT MAX(created_at) FROM messages")
    last_time = cur.fetchone()[0] or '2020-01-01'
    cur.close()
    conn.close()

    refresh_task = run_async(refresh_msgs(display_name, msg_box, last_time))

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "Личное сообщение", "value": "private"},
                {"label": "Очистить чат", "value": "clear", "color": "danger"},
                {"label": "Выйти", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        if data["cmd"] == "clear":
            await confirm_and_clear(msg_box)
            continue

        if data["cmd"] == "private":
            all_users = get_all_users()
            others = [u for u in all_users if u != display_name]
            if not others:
                toast("Нет других пользователей для отправки ЛС.", color='warn')
                continue
            target = await select("Кому отправить ЛС?", options=others)
            if target:
                save_message(display_name, data['msg'], recipient=target)
                msg_box.append(put_markdown(f"📤 **ЛС → `{target}`**: {data['msg']}"))
            continue

        # Публичное сообщение
        msg_box.append(put_markdown(f"`{display_name}`: {data['msg']}"))
        save_message(display_name, data['msg'])

    refresh_task.close()
    online_users.discard(display_name)
    save_message('📢', f'`{display_name}` покинул чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться в чат'], onclick=lambda _: run_js('location.reload()'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
