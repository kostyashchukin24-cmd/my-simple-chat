import asyncio
import os

import bcrypt

import psycopg2
from psycopg2.extras import RealDictCursor

from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js

online_users = set()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_user(email: str, password: str, display_name: str):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s)",
            (email, hash_password(password), display_name)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:  # email уже существует
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
    
def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"], sslmode="require")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Таблица сообщений (уже есть)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    
    # 🔥 НОВАЯ ТАБЛИЦА: пользователи
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
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

# Инициализация БД
init_db()

def hash_password(password: str) -> str:
    """Превращает пароль в зашифрованную строку (хэш)"""
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    """Проверяет, совпадает ли пароль с хэшем"""
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def register_user(email: str, password: str, display_name: str) -> bool:
    """Регистрирует нового пользователя. Возвращает True, если успешно."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (email, password_hash, display_name) VALUES (%s, %s, %s)",
            (email, hash_password(password), display_name)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        # Email уже существует (из-за UNIQUE)
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def authenticate_user(email: str, password: str):
    """Проверяет email и пароль. Возвращает данные пользователя или None."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, email, password_hash, display_name FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    
    if user and verify_password(password, user['password_hash']):
        return dict(user)  # Возвращаем словарь с данными
    return None
    
async def main():
    global online_users

    put_markdown("## 💬 Чат (сообщения хранятся 24 часа)")

    # --- Экран входа или регистрации ---
    current_user = None
    while current_user is None:
        action = await actions("Добро пожаловать!", buttons=["Войти", "Зарегистрироваться"])

               if action == "Зарегистрироваться":
            try:
                reg_data = await input_group("Регистрация", [
                    input("Email", name="email", type=INPUT_TYPE.EMAIL, required=True),
                    input("Пароль", name="password", type=INPUT_TYPE.PASSWORD, required=True),
                    input("Ваше имя в чате", name="display_name", required=True, placeholder="Например, Анна")
                ])
                
                success = register_user(reg_data['email'], reg_data['password'], reg_data['display_name'])
                if success:
                    toast("✅ Регистрация успешна! Теперь войдите.")
                else:
                    toast("❌ Email уже используется!", color='error')
            except Exception as e:
                # ВАЖНО: покажи ошибку!
                put_error(f"Ошибка регистрации: {str(e)}")
                print("Ошибка:", e)  # Это будет в логах Render

        elif action == "Войти":
            try:
                login_data = await input_group("Вход", [
                    input("Email", name="email", type=INPUT_TYPE.EMAIL, required=True),
                    input("Пароль", name="password", type=INPUT_TYPE.PASSWORD, required=True)
                ])
                
                user = authenticate_user(login_data['email'], login_data['password'])
                if user:
                    current_user = user
                    toast(f"Привет, {user['display_name']}!")
                else:
                    toast("❌ Неверный email или пароль!", color='error')
             except Exception as e:
                put_error(f"Ошибка входа: {str(e)}")
                print("Ошибка входа:", e)

    # --- Пользователь вошёл! Теперь чат ---
    display_name = current_user['display_name']
    if display_name in online_users:
        # Можно добавить суффикс, но пока просто пропустим
        pass
    online_users.add(display_name)

    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загружаем историю сообщений
    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    # Приветствие в чате
    save_message('📢', f'`{display_name}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{display_name}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(display_name, msg_box))

    # Основной цикл чата
    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=["Отправить", {"label": "Выйти", "type": "cancel"}])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        msg_box.append(put_markdown(f"`{display_name}`: {data['msg']}"))
        save_message(display_name, data['msg'])

    # Выход из чата
    refresh_task.close()
    online_users.discard(display_name)
    save_message('📢', f'`{display_name}` покинул чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться в чат'], onclick=lambda _: run_js('location.reload()'))
    

async def refresh_msgs(my_name, msg_box):
    # Получаем время последнего сообщения при старте
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




