import asyncio
import os
import bcrypt

import psycopg2
from psycopg2.extras import RealDictCursor

from pywebio import start_server
from pywebio.input import input, input_group, actions, PASSWORD, select
from pywebio.output import put_markdown, put_scrollable, put_error, put_buttons, toast, output, clear, put_text
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

def get_private_partners(my_name):
    """Возвращает список пользователей, с которыми есть ЛС (за 24ч)"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT username FROM messages
        WHERE recipient = %s AND created_at >= NOW() - INTERVAL '24 hours'
        UNION
        SELECT DISTINCT recipient FROM messages
        WHERE username = %s AND recipient IS NOT NULL AND created_at >= NOW() - INTERVAL '24 hours'
    """, (my_name, my_name))
    partners = [row[0] for row in cur.fetchall() if row[0] != my_name]
    cur.close()
    conn.close()
    return sorted(partners)

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

def load_public_messages():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT username, text FROM messages
        WHERE recipient IS NULL AND created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY created_at ASC
        LIMIT 100
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r["username"], r["text"]) for r in rows]

def load_private_messages(my_name, partner):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT username, text, created_at FROM messages
        WHERE created_at >= NOW() - INTERVAL '24 hours'
          AND (
            (username = %s AND recipient = %s)
            OR (username = %s AND recipient = %s)
          )
        ORDER BY created_at ASC
    """, (my_name, partner, partner, my_name))
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
    cur.execute("DELETE FROM messages WHERE recipient IS NULL")
    conn.commit()
    cur.close()
    conn.close()

init_db()

# --- Общий чат ---
async def show_public_chat(display_name, msg_box):
    # Загружаем историю
    for user, text in load_public_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    save_message('📢', f'`{display_name}` присоединился к общему чату!')
    msg_box.append(put_markdown(f'📢 `{display_name}` присоединился к общему чату!'))

    # Обновление
    async def refresh():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT MAX(created_at) FROM messages WHERE recipient IS NULL")
        last_time = cur.fetchone()[0] or '2020-01-01'
        cur.close()
        conn.close()

        while True:
            await asyncio.sleep(1)
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT username, text FROM messages
                WHERE recipient IS NULL AND created_at > %s
                ORDER BY created_at ASC
            """, (last_time,))
            new = cur.fetchall()
            cur.close()
            conn.close()

            for msg in new:
                if msg["username"] != display_name:
                    txt = f'📢 {msg["text"]}' if msg["username"] == '📢' else f"`{msg['username']}`: {msg['text']}"
                    msg_box.append(put_markdown(txt))
                    last_time = msg["created_at"]

    return run_async(refresh())

# --- Личные чаты ---
async def show_private_chats(display_name):
    while True:
        clear()  # Очищаем весь экран
        put_markdown("## 💬 Личные сообщения")
        
        partners = get_private_partners(display_name)
        all_users = [u for u in get_all_users() if u != display_name]
        
        options = []
        if partners:
            put_text("Ваши диалоги:")
            for p in partners:
                options.append({"label": f"💬 {p}", "value": p})
            put_buttons(options, onclick=lambda p: asyncio.create_task(open_private_chat(display_name, p)))
            put_text("")
        
        if all_users:
            put_buttons([{"label": "➕ Новый чат", "value": "new", "color": "primary"}],
                        onclick=lambda _: asyncio.create_task(start_new_private_chat(display_name, all_users)))
        
        put_buttons([{"label": "⬅️ Назад к общему чату", "value": "back"}],
                    onclick=lambda _: asyncio.create_task(main_chat_interface(display_name)))
        
        # Ждём, пока пользователь что-то нажмёт (на самом деле уходим в open_private_chat)
        await asyncio.sleep(3600)  # просто удерживаем экран

async def start_new_private_chat(display_name, all_users):
    target = await select("Выберите получателя", options=all_users)
    if target:
        await open_private_chat(display_name, target)

async def open_private_chat(display_name, partner):
    clear()
    put_markdown(f"## 💬 Личный чат с `{partner}`")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загружаем историю
    for msg in load_private_messages(display_name, partner):
        if msg["username"] == display_name:
            msg_box.append(put_markdown(f"**Вы**: {msg['text']}"))
        else:
            msg_box.append(put_markdown(f"`{msg['username']}`: {msg['text']}"))

    # Обновление (упрощённое — без фоновой задачи, чтобы не усложнять)
    while True:
        data = await input_group(f"Сообщение для {partner}", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "⬅️ Назад к списку", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            return await show_private_chats(display_name)

        save_message(display_name, data['msg'], recipient=partner)
        msg_box.append(put_markdown(f"**Вы**: {data['msg']}"))

# --- Главный интерфейс ---
async def main_chat_interface(display_name):
    clear()
    put_markdown("## 💬 Общий чат")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    refresh_task = await show_public_chat(display_name, msg_box)

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "Очистить чат", "value": "clear", "color": "danger"},
                {"label": "Личные чаты", "value": "private"},
                {"label": "Выйти", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        if data["cmd"] == "clear":
            clear_chat()
            msg_box.clear()
            toast("✅ Общий чат очищен!")
            save_message('📢', 'Общий чат был очищен.')
            msg_box.append(put_markdown('📢 Общий чат был очищен.'))
            continue

        if data["cmd"] == "private":
            return await show_private_chats(display_name)

        msg_box.append(put_markdown(f"`{display_name}`: {data['msg']}"))
        save_message(display_name, data['msg'])

    refresh_task.close()
    save_message('📢', f'`{display_name}` покинул общий чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

# --- Основная функция входа ---
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
                    input("Ваше имя в чате", name="display_name", required=True)
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
                    online_users.add(user['display_name'])
                    return await main_chat_interface(user['display_name'])
                else:
                    toast("❌ Неверный email или пароль!", color='error')
            except Exception as e:
                put_error(f"Ошибка входа: {str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
