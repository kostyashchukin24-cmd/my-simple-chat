import asyncio
import json
import os
from datetime import datetime, timedelta

from pywebio import start_server
from pywebio.input import *
from pywebio.output import *
from pywebio.session import run_async, run_js

# Настройки
CHAT_FILE = "chat_history.json"
MAX_MESSAGES_COUNT = 100
MAX_AGE_HOURS = 24

# Глобальные данные
chat_msgs = []
online_users = set()


def load_messages():
    """Загружает сообщения из файла, фильтруя по возрасту"""
    if not os.path.exists(CHAT_FILE):
        return []
    try:
        with open(CHAT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        now = datetime.now()
        filtered = []
        for msg in data:  # ← ИСПРАВЛЕНО: было "for msg in" без "data"
            try:
                ts = datetime.fromisoformat(msg["timestamp"])
                if (now - ts).total_seconds() < MAX_AGE_HOURS * 3600:
                    filtered.append((msg["user"], msg["text"]))
            except:
                continue
        return filtered[-MAX_MESSAGES_COUNT:]
    except Exception as e:
        print(f"Ошибка загрузки: {e}")
        return []


def save_messages(messages):
    """Сохраняет сообщения в файл с временной меткой"""
    data = []
    for user, text in messages:
        data.append({
            "user": user,
            "text": text,
            "timestamp": datetime.now().isoformat()
        })
    try:
        with open(CHAT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data[-MAX_MESSAGES_COUNT:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения: {e}")


# Загружаем историю один раз при запуске
chat_msgs = load_messages()


async def main():
    global chat_msgs

    put_markdown("## 🧊 Добро пожаловать в онлайн чат!\nСообщения сохраняются на 24 часа!")

    msg_box = output()
    put_scrollable(msg_box, height=300, keep_bottom=True)

    # Показываем всю историю новому пользователю
    for user, text in chat_msgs:
        if user == '📢':
            msg_box.append(put_markdown(f'📢 {text}'))
        else:
            msg_box.append(put_markdown(f"`{user}`: {text}"))

    nickname = await input("Войти в чат", required=True, placeholder="Ваше имя",
                           validate=lambda n: "Такой ник уже используется!" if n in online_users or n == '📢' else None)
    online_users.add(nickname)

    # Системное сообщение: вход
    chat_msgs.append(('📢', f'`{nickname}` присоединился к чату!'))
    save_messages(chat_msgs)
    msg_box.append(put_markdown(f'📢 `{nickname}` присоединился к чату'))

    refresh_task = run_async(refresh_msg(nickname, msg_box))

    while True:
        data = await input_group("💭 Новое сообщение", [
            input(placeholder="Текст сообщения ...", name="msg"),
            actions(name="cmd", buttons=["Отправить", {'label': "Выйти из чата", 'type': 'cancel'}])
        ], validate=lambda m: ('msg', "Введите текст сообщения!") if m["cmd"] == "Отправить" and not m['msg'] else None)

        if data is None:
            break

        msg_box.append(put_markdown(f"`{nickname}`: {data['msg']}"))
        chat_msgs.append((nickname, data['msg']))
        save_messages(chat_msgs)

    refresh_task.close()

    online_users.remove(nickname)
    toast("Вы вышли из чата!")
    chat_msgs.append(('📢', f'Пользователь `{nickname}` покинул чат!'))
    save_messages(chat_msgs)
    msg_box.append(put_markdown(f'📢 Пользователь `{nickname}` покинул чат!'))

    put_buttons(['Перезайти'], onclick=lambda btn: run_js('window.location.reload()'))


async def refresh_msg(nickname, msg_box):
    global chat_msgs
    last_idx = len(chat_msgs)

    while True:
        await asyncio.sleep(0.5)
        current_len = len(chat_msgs)
        if current_len > last_idx:
            for i in range(last_idx, current_len):
                user, text = chat_msgs[i]
                if user != nickname:
                    if user == '📢':
                        msg_box.append(put_markdown(f'📢 {text}'))
                    else:
                        msg_box.append(put_markdown(f"`{user}`: {text}"))
            last_idx = current_len


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, host='0.0.0.0', port=port, debug=False, cdn=False)
