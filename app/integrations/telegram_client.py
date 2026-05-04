"""Telegram Bot API client — multi-user, with inline keyboard support."""

import os
import re
import requests

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


def _get_url(method: str) -> str:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


def _strip_markdown(text: str) -> str:
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group().replace('`', ''), text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)
    return text


def _split_message(text: str) -> list:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    return chunks


def send_typing(chat_id: str) -> None:
    try:
        requests.post(_get_url("sendChatAction"), json={
            "chat_id": chat_id, "action": "typing",
        }, timeout=5)
    except Exception:
        pass


def send_message(chat_id: str, text: str) -> dict:
    text = _strip_markdown(text)
    chunks = _split_message(text)
    resp = None
    for chunk in chunks:
        resp = requests.post(_get_url("sendMessage"), json={
            "chat_id": chat_id, "text": chunk,
        }, timeout=15)
        resp.raise_for_status()
    return resp.json() if resp else {}


def send_message_with_keyboard(chat_id: str, text: str, keyboard: list) -> dict:
    text = _strip_markdown(text)
    resp = requests.post(_get_url("sendMessage"), json={
        "chat_id": chat_id,
        "text": text[:MAX_MESSAGE_LENGTH],
        "reply_markup": {"inline_keyboard": keyboard},
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def send_session_reminder(chat_id: str, text: str, session_day: str, week_number: int) -> dict:
    prefix = f"session_action:{session_day}:{week_number}"
    keyboard = [[
        {"text": "Done ✓", "callback_data": f"{prefix}:done"},
        {"text": "Skip", "callback_data": f"{prefix}:skip"},
    ], [
        {"text": "Easier", "callback_data": f"{prefix}:easier"},
        {"text": "Harder", "callback_data": f"{prefix}:harder"},
        {"text": "Swap day", "callback_data": f"{prefix}:swap"},
    ]]
    return send_message_with_keyboard(chat_id, text, keyboard)


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(_get_url("answerCallbackQuery"), json={
            "callback_query_id": callback_query_id, "text": text,
        }, timeout=5)
    except Exception:
        pass


def send_photo(chat_id: str, photo_bytes: bytes, caption: str = None) -> dict:
    files = {"photo": ("chart.png", photo_bytes, "image/png")}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = _strip_markdown(caption)[:1024]
    resp = requests.post(_get_url("sendPhoto"), data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def set_webhook(url: str) -> dict:
    resp = requests.post(_get_url("setWebhook"), json={"url": url}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def delete_webhook() -> dict:
    resp = requests.post(_get_url("deleteWebhook"), timeout=10)
    resp.raise_for_status()
    return resp.json()
