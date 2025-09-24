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
    # Таблица сообщений
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # Таблица заявок в друзья
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
    cur.execute("""
        SELECT from_user FROM friend_requests
        WHERE to_user = %s AND status = 'pending'
    """, (to_user,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["from_user"] for r in rows]

def accept_friend_request(from_user, to_user):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE friend_requests
        SET status = 'accepted'
        WHERE from_user = %s AND to_user = %s AND status = 'pending'
    """, (from_user, to_user))
    conn.commit()
    cur.close()
    conn.close()

def reject_friend_request(from_user, to_user):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE friend_requests
        SET status = 'rejected'
        WHERE from_user = %s AND to_user = %s AND status = 'pending'
    """, (from_user, to_user))
    conn.commit()
    cur.close()
    conn.close()

def get_friends(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT from_user FROM friend_requests
        WHERE to_user = %s AND status = 'accepted'
        UNION
        SELECT to_user FROM friend_requests
        WHERE from_user = %s AND status = 'accepted'
    """, (username, username))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r["from_user"] for r in rows]

# --- Обновление сообщений (без изменений) ---
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

# --- Основная функция ---
async def main():
    global online_users
    init_db()
    put_markdown("## 💬 Чат с друзьями (сообщения — 24 ч)")
    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Загрузка истории
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

    # Проверка входящих запросов в друзья
    pending = get_pending_requests(nickname)
    for req in pending:
        msg_box.append(put_markdown(f'📬 Запрос в друзья от `{req}`'))
        put_buttons([
            {'label': f'✅ Принять {req}', 'value': f'accept_{req}', 'color': 'success'},
            {'label': f'❌ Отклонить {req}', 'value': f'reject_{req}', 'color': 'danger'}
        ], onclick=[
            lambda u=req: accept_friend_request(u, nickname),
            lambda u=req: reject_friend_request(u, nickname)
        ])

    refresh_task = run_async(refresh_msgs(nickname, msg_box))

    try:
        while True:
            data = await input_group("Сообщение", [
                input(name="msg", placeholder="Текст... (/add имя — добавить в друзья)"),
                actions(name="cmd", buttons=["Отправить", {"label": "Выйти", "type": "cancel"}])
            ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

            if data is None:
                break

            msg_text = data['msg']

            # Обработка команды /add
            if msg_text.startswith('/add '):
                target = msg_text[5:].strip()
                if not target:
                    toast("Укажите имя после /add")
                elif target == nickname:
                    toast("Нельзя добавить себя!")
                elif target == '📢':
                    toast("Нельзя добавить систему!")
                else:
                    if send_friend_request(nickname, target):
                        toast(f"Запрос отправлен пользователю `{target}`")
                    else:
                        toast("Не удалось отправить запрос (возможно, уже отправлен или ошибка)")
                continue  # не отправлять как сообщение

            # Обычное сообщение
            msg_box.append(put_markdown(f"`{nickname}`: {msg_text}"))
            save_message(nickname, msg_text)

    finally:
        refresh_task.close()
        online_users.discard(nickname)
        save_message('📢', f'`{nickname}` покинул чат!')
        toast("Вы вышли из чата!")
        put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
