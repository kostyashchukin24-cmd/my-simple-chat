import asyncio
import os
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor
from pywebio import start_server
from pywebio.input import input, input_group, actions, PASSWORD
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            friend_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (user_id, friend_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS private_messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            CHECK (sender_id != receiver_id)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

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

def clear_chat():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    cur.close()
    conn.close()

# === FRIENDS ===
def add_friend(user_id: int, friend_id: int) -> bool:
    if user_id == friend_id:
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO friends (user_id, friend_id) VALUES (%s, %s)",
            (user_id, friend_id)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()

def get_friends(user_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT u.display_name, u.id
        FROM friends f
        JOIN users u ON f.friend_id = u.id
        WHERE f.user_id = %s
    """, (user_id,))
    friends = cur.fetchall()
    cur.close()
    conn.close()
    return friends

def search_users(query: str, exclude_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT id, display_name FROM users
        WHERE display_name ILIKE %s AND id != %s
        LIMIT 10
    """, (f"%{query}%", exclude_id))
    users = cur.fetchall()
    cur.close()
    conn.close()
    return users

# === PRIVATE MESSAGES ===
def save_private_message(sender_id: int, receiver_id: int, text: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO private_messages (sender_id, receiver_id, text) VALUES (%s, %s, %s)",
        (sender_id, receiver_id, text)
    )
    conn.commit()
    cur.close()
    conn.close()

def load_private_messages(user_id: int, friend_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT 
            pm.text,
            u.display_name AS sender_name,
            (pm.sender_id = %s) AS is_own
        FROM private_messages pm
        JOIN users u ON u.id = pm.sender_id
        WHERE 
            (pm.sender_id = %s AND pm.receiver_id = %s)
            OR (pm.sender_id = %s AND pm.receiver_id = %s)
        AND pm.created_at >= NOW() - INTERVAL '24 hours'
        ORDER BY pm.created_at ASC
    """, (user_id, user_id, friend_id, friend_id, user_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def clear_private_chat(user_id: int, friend_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM private_messages
        WHERE (sender_id = %s AND receiver_id = %s)
           OR (sender_id = %s AND receiver_id = %s)
    """, (user_id, friend_id, friend_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

# === UI: PRIVATE CHAT ===
async def open_private_chat(current_user, friend):
    user_id = current_user['id']
    friend_id = friend['id']
    friend_name = friend['display_name']

    put_markdown(f"## 💬 Личный чат с `{friend_name}` (сообщения хранятся 24 часа)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    messages = load_private_messages(user_id, friend_id)
    for msg in messages:
        prefix = "**Вы**:" if msg['is_own'] else f"`{msg['sender_name']}`:"
        msg_box.append(put_markdown(f"{prefix} {msg['text']}"))

    while True:
        data = await input_group(f"Сообщение → {friend_name}", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "Очистить чат", "value": "clear", "color": "danger"},
                {"label": "Назад", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        if data["cmd"] == "clear":
            confirmed = await actions("⚠️ Очистка личного чата", ["Да, очистить", "Отмена"])
            if confirmed == "Да, очистить":
                clear_private_chat(user_id, friend_id)
                msg_box.clear()
                toast("✅ Личный чат очищен!", color='success')
            continue

        save_private_message(user_id, friend_id, data['msg'])
        msg_box.append(put_markdown(f"**Вы**: {data['msg']}"))

# === UI: FRIENDS MENU ===
async def manage_friends(current_user):
    while True:
        action = await actions("Друзья", [
            "Поиск и добавить",
            "Мои друзья",
            "Назад"
        ])
        if action == "Назад":
            break
        elif action == "Поиск и добавить":
            query = await input("Введите имя для поиска", required=True)
            candidates = search_users(query, current_user['id'])
            if not candidates:
                toast("Никого не найдено", color='warning')
                continue
            choices = [{"label": u['display_name'], "value": u['id']} for u in candidates]
            choices.append({"label": "Отмена", "value": None})
            friend_id = await actions("Выберите пользователя", choices)
            if friend_id:
                if add_friend(current_user['id'], friend_id):
                    toast("✅ Друг добавлен!", color='success')
                else:
                    toast("❌ Уже в друзьях", color='error')
        elif action == "Мои друзья":
            friends = get_friends(current_user['id'])
            if not friends:
                put_markdown("📭 У вас пока нет друзей.")
                continue
            friend_choices = [
                {"label": f"💬 {f['display_name']}", "value": dict(f)}
                for f in friends
            ]
            friend_choices.append({"label": "Назад", "value": None})
            selected = await actions("Выберите друга", friend_choices)
            if selected:
                await open_private_chat(current_user, selected)

# === MAIN CHAT ===
init_db()

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
    put_markdown("## 💬 Общий чат (сообщения хранятся 24 часа)")
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

    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    save_message('📢', f'`{display_name}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{display_name}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(display_name, msg_box))

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст..."),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "Очистить чат", "value": "clear", "color": "danger"},
                {"label": "Друзья", "value": "friends", "color": "secondary"},
                {"label": "Выйти", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        if data["cmd"] == "clear":
            await confirm_and_clear(msg_box)
            continue
        elif data["cmd"] == "friends":
            await manage_friends(current_user)
            continue

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
