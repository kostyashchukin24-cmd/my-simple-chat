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

# --- ДРУЗЬЯ ---
def send_friend_request(from_user, to_user):
    if from_user == to_user:
        return False
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO friend_requests (from_user, to_user)
            VALUES (%s, %s)
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

def get_pending_requests(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT from_user FROM friend_requests WHERE to_user = %s AND status = 'pending'", (username,))
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

    # ЗАГРУЖАЕМ ИСТОРИЮ ИЗ БАЗЫ
    for user, text in load_messages():
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    nickname = await input("Ваше имя", required=True, placeholder="Имя",
                           validate=lambda n: "Имя занято!" if n in online_users or n == '📢' else None)
    online_users.add(nickname)

    # СОХРАНЯЕМ В БАЗУ!
    save_message('📢', f'`{nickname}` присоединился к чату!')
    msg_box.append(put_markdown(f'📢 `{nickname}` присоединился к чату'))

    # ПОКАЗ ВХОДЯЩИХ ЗАПРОСОВ ПРИ ВХОДЕ
    pending = get_pending_requests(nickname)
    for user in pending:
        msg_box.append(put_markdown(f'📬 Запрос в друзья от `{user}`'))
        put_buttons([
            {'label': '✅ Принять', 'color': 'success'},
            {'label': '❌ Отклонить', 'color': 'danger'}
        ], onclick=[
            lambda u=user: accept_friend_request(u, nickname),
            lambda: toast("Запрос отклонён")
        ])

    refresh_task = run_async(refresh_msgs(nickname, msg_box))

    while True:
        data = await input_group("Сообщение", [
            input(name="msg", placeholder="Текст... (/add имя — добавить в друзья)"),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "👥 Друзья", "value": "friends", "type": "submit"},
                {"label": "Выйти", "type": "cancel"}
            ])
        ], validate=lambda d: ("msg", "Введите текст!") if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        if data["cmd"] == "friends":
            # МЕНЮ ДРУЗЕЙ
            pending_now = get_pending_requests(nickname)
            content = [put_markdown("## 👥 Друзья")]
            if pending_now:
                content.append(put_markdown("### 📬 Входящие запросы:"))
                for user in pending_now:
                    content.append(put_row([
                        put_text(user),
                        put_buttons([
                            {'label': '✅', 'color': 'success'},
                            {'label': '❌', 'color': 'danger'}
                        ], onclick=[
                            lambda u=user: accept_friend_request(u, nickname),
                            lambda: toast("Отклонено")
                        ])
                    ]))
            else:
                content.append(put_text("Нет входящих запросов."))

            content.append(put_text("Добавить в друзья:"))
            try:
                target = await input("Имя пользователя", required=False)
                if target and target != nickname and target != '📢':
                    if send_friend_request(nickname, target):
                        toast(f"✅ Запрос отправлен {target}")
                    else:
                        toast("❌ Не удалось отправить запрос")
            except Exception:
                pass  # пользователь закрыл окно
            continue

        msg_text = data['msg']
        if msg_text.startswith('/add '):
            target = msg_text[5:].strip()
            if target and target != nickname and target != '📢':
                if send_friend_request(nickname, target):
                    toast(f"✅ Запрос отправлен {target}")
                else:
                    toast("❌ Не удалось отправить запрос")
            continue

        msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
        save_message(nickname, data['msg'])  # ← ОБЯЗАТЕЛЬНО в БД

    refresh_task.close()
    online_users.discard(nickname)
    save_message('📢', f'`{nickname}` покинул чат!')
    toast("Вы вышли из чата!")
    put_buttons(['Вернуться'], onclick=lambda _: run_js('location.reload()'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
