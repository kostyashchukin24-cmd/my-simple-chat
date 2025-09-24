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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS friend_requests (
            id SERIAL PRIMARY KEY,
            from_user TEXT NOT NULL,
            to_user TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (from_user, to_user)
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

# --- ДРУЗЬЯ ---
def add_friend(user, friend):
    if user == friend or friend == '📢':
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO friend_requests (from_user, to_user)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        """, (user, friend))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        cur.close()
        conn.close()

def get_friends(user):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT to_user FROM friend_requests WHERE from_user = %s
        UNION
        SELECT from_user FROM friend_requests WHERE to_user = %s
    """, (user, user))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return sorted({r["to_user"] for r in rows})

async def show_friends_popup(nickname):
    friends = get_friends(nickname)
    content = [put_markdown("## 👥 Мои друзья")]
    if friends:
        content.append(put_table([[f] for f in friends], header=["Имя"]))
    else:
        content.append(put_text("Список пуст."))

    content.append(put_text(""))
    content.append(put_text("Добавить в друзья:"))
    try:
        target = await input("Имя пользователя", placeholder="Введите имя", required=False)
        if target and target != nickname and target != '📢':
            if add_friend(nickname, target):
                toast(f"✅ {target} добавлен в друзья!")
            else:
                toast("❌ Не удалось добавить.")
    except Exception:
        pass  # пользователь закрыл окно

# Инициализация БД
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

async def main():
    global online_users
    put_markdown("## 💬 Чат (сообщения хранятся 24 часа)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    nickname = await input("Ваше имя", required=True, placeholder="Имя",
                           validate=lambda n: "Имя занято!" if n in online_users or n == '📢' else None)
    online_users.add(nickname)

    save_message('📢', f'`{nickname}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{nickname}` присоединился к чату'))

    refresh_task = run_async(refresh_msgs(nickname, msg_box))

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

        if data["cmd"] == "friends":
            await show_friends_popup(nickname)
            continue

        msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
        save_message(nickname, data['msg'])

    refresh_task.close()
    online_users.discard(nickname)
    save_message('📢', f'`{nickname}` покинул чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
