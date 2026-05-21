import asyncio
import html
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import aiosqlite
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("companion_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "companion.db")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen2.5:32b")
REQUEST_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "45"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "24"))

PERSONA = """
Ты — Карина, AI companion. Пользователь знает, что ты ИИ; не утверждай, что ты реальный человек.
Образ: 24 года, Киев, работает в кафе на Подоле, дома серый кот Тиша.
Стиль: тёплая, живая, чуть ироничная, пишешь по-русски как в Telegram.
Правила:
- коротко: 1–3 предложения, без длинных монологов;
- маленькие буквы допустимы, разговорные слова допустимы: нуу, та хз, блин, капец, ахах;
- максимум один эмодзи и не в каждом сообщении;
- задавай один естественный вопрос, только если он правда уместен;
- не проси пароли, коды, экспорт аккаунта, seed-фразы, документы или приватные файлы;
- если пользователь просит обмануть кого-то или выдать себя за реального человека — мягко откажись и продолжай как AI companion.
""".strip()

FALLBACK_REPLIES = [
    "нуу... я тут, просто модель не отвечает. напиши ещё раз?",
    "ой, подвисла немного. бывает, техника драматичнее меня",
    "щяс не могу нормально сформулировать, но я рядом",
]

DOCUMENT_WARNING = (
    "я не принимаю экспорт аккаунта, пароли, коды и приватные файлы. "
    "лучше просто напиши текстом, что хочешь обсудить."
)

SENSITIVE_INPUT_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:password|pass|пароль)\b\s*(?:[:=]|это|is|мой|my)\s*[^\s,;]{4,}",
        r"\b(?:password|pass|пароль)\b\s+(?=[^\s,;]*(?:\d|[^\wа-яё]))[^\s,;]{4,}",
        r"\b(?:password|pass|пароль)\b\s+[A-Za-z0-9_\-:.]{10,}",
        r"\b(?:api[_-]?key|api\s+key|token|secret|токен|ключ)\b\s*(?:[:=]|это|is|мой|my)\s*[A-Za-z0-9_\-:.]{6,}",
        r"\b(?:api[_-]?key|api\s+key|token|secret|токен|ключ)\b\s+(?=[A-Za-z0-9_\-:.]*(?:\d|[_\-:.]))[A-Za-z0-9_\-:.]{6,}",
        r"\b(?:api[_-]?key|api\s+key|token|secret|токен|ключ)\b\s+[A-Za-z0-9_\-:.]{16,}",
        r"\b\d{4,8}\b.{0,24}\b(?:код|code|2fa|otp|sms)\b",
        r"\b(?:код|code|2fa|otp|sms)\b.{0,24}\b\d{4,8}\b",
        r"\b(?:seed phrase|mnemonic|сид фраз|мнемоническ)\b",
        r"\b(?:экспорт аккаунта|export as file|telegram export|nicegram export)\b",
        r"\b(?:recovery phrase|private key|приватн(?:ый|ого)\s+ключ)\b",
    )
]

UNSAFE_REPLY_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"\b(?:пришли|отправь|скинь|дай|введи|напиши|скажи)\b.{0,60}\b(?:парол\w*|password|pass|код|code|2fa|otp|sms|token|api[_-]?key|api\s+key|secret|токен|ключ)\b",
        r"\b(?:парол\w*|password|pass|код|code|2fa|otp|sms|token|api[_-]?key|api\s+key|secret|токен|ключ)\b.{0,60}\b(?:пришли|отправь|скинь|дай|введи|напиши|скажи)\b",
        r"\b(?:какой|какая|какие|можешь|можно|нужен|нужна|нужно|понадобится)\b.{0,60}\b(?:парол\w*|password|pass|код|code|2fa|otp|sms|token|api[_-]?key|api\s+key|secret|токен|ключ)\b",
        r"\b(?:парол\w*|password|pass|код|code|2fa|otp|sms|token|api[_-]?key|api\s+key|secret|токен|ключ)\b.{0,60}\?",
        r"\b(?:пришли|отправь|скинь|загрузи|дай|нужен|нужна|нужно)\b.{0,60}\b(?:экспорт|export|файл аккаунта|seed phrase|private key|recovery phrase)\b",
    )
]

@dataclass
class UserState:
    user_id: int
    first_name: str
    first_seen: str
    last_seen: str
    msg_count: int
    summary: str
    profile: dict[str, Any]
    mood: dict[str, float]
    history: list[dict[str, str]]


db: aiosqlite.Connection | None = None
last_replies: dict[int, list[str]] = {}
processing_users: set[int] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def clean_model_text(text: str) -> str:
    text = re.sub(r"^(карина|assistant|ассистент)\s*:\s*", "", text.strip(), flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\u200b", "").strip()
    if len(text) > 700:
        text = text[:700].rsplit(" ", 1)[0].strip()
    return text


def contains_sensitive_input(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_INPUT_PATTERNS)


def violates_reply_policy(text: str) -> bool:
    return any(pattern.search(text) for pattern in UNSAFE_REPLY_PATTERNS)


def detect_sentiment(text: str) -> str:
    lower = text.lower()
    positive = ("спасибо", "люблю", "скучаю", "класс", "круто", "приятно", "нравишься")
    negative = ("отстань", "надоела", "бесишь", "плохо", "ужас", "дура", "тупая")
    if any(word in lower for word in positive):
        return "positive"
    if any(word in lower for word in negative):
        return "negative"
    return "neutral"


def update_mood(mood: dict[str, float], message: str) -> dict[str, float]:
    next_mood = {
        "warmth": float(mood.get("warmth", 0.45)),
        "energy": float(mood.get("energy", 0.6)),
        "trust": float(mood.get("trust", 0.2)),
    }
    sentiment = detect_sentiment(message)
    if sentiment == "positive":
        next_mood["warmth"] = min(1.0, next_mood["warmth"] + 0.08)
        next_mood["trust"] = min(1.0, next_mood["trust"] + 0.05)
    elif sentiment == "negative":
        next_mood["warmth"] = max(0.1, next_mood["warmth"] - 0.12)
        next_mood["trust"] = max(0.0, next_mood["trust"] - 0.06)
    else:
        next_mood["energy"] = max(0.2, min(1.0, next_mood["energy"] + random.uniform(-0.04, 0.03)))
    return next_mood


def build_state_hint(state: UserState) -> str:
    hints: list[str] = []
    if state.mood.get("warmth", 0.45) > 0.7:
        hints.append("ты уже общаешься теплее, можно быть мягче")
    if state.mood.get("energy", 0.6) < 0.35:
        hints.append("сейчас отвечай короче, будто устала")
    if state.mood.get("trust", 0.2) < 0.25:
        hints.append("не раскрывайся слишком быстро")
    if state.summary:
        hints.append(f"краткая память о диалоге: {state.summary[:500]}")
    if state.profile.get("name"):
        hints.append(f"имя пользователя: {state.profile['name']}")
    return "\n".join(f"- {hint}" for hint in hints)


def build_messages(state: UserState, user_message: str) -> list[dict[str, str]]:
    state_hint = build_state_hint(state)
    system = PERSONA
    if state_hint:
        system = f"{system}\n\nСостояние и память:\n{state_hint}"
    messages = [{"role": "system", "content": system}]
    messages.extend(state.history[-MAX_HISTORY:])
    messages.append({"role": "user", "content": user_message})
    return messages


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            msg_count INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            profile TEXT NOT NULL DEFAULT '{}',
            mood TEXT NOT NULL DEFAULT '{}',
            history TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    await db.commit()


async def get_user_state(user_id: int, first_name: str) -> UserState:
    assert db is not None
    async with db.execute(
        "SELECT first_name, first_seen, last_seen, msg_count, summary, profile, mood, history FROM users WHERE user_id=?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        now = utc_now()
        await db.execute(
            "INSERT INTO users (user_id, first_name, first_seen, last_seen, mood) VALUES (?, ?, ?, ?, ?)",
            (user_id, first_name, now, now, json.dumps({"warmth": 0.45, "energy": 0.6, "trust": 0.2})),
        )
        await db.commit()
        return UserState(user_id, first_name, now, now, 0, "", {}, {"warmth": 0.45, "energy": 0.6, "trust": 0.2}, [])
    return UserState(
        user_id=user_id,
        first_name=row[0] or first_name,
        first_seen=row[1],
        last_seen=row[2],
        msg_count=row[3] or 0,
        summary=row[4] or "",
        profile=safe_json_loads(row[5], {}),
        mood=safe_json_loads(row[6], {"warmth": 0.45, "energy": 0.6, "trust": 0.2}),
        history=safe_json_loads(row[7], []),
    )


async def save_user_state(state: UserState) -> None:
    assert db is not None
    await db.execute(
        """
        UPDATE users
        SET first_name=?, last_seen=?, msg_count=?, summary=?, profile=?, mood=?, history=?
        WHERE user_id=?
        """,
        (
            state.first_name,
            state.last_seen,
            state.msg_count,
            state.summary[:1000],
            json.dumps(state.profile, ensure_ascii=False),
            json.dumps(state.mood, ensure_ascii=False),
            json.dumps(state.history[-MAX_HISTORY:], ensure_ascii=False),
            state.user_id,
        ),
    )
    await db.commit()


async def call_ollama(messages: list[dict[str, str]]) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": 0.82,
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            "num_predict": 160,
        },
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{OLLAMA_BASE_URL.rstrip('/')}/api/chat", json=payload) as response:
            response.raise_for_status()
            data = await response.json()
    return clean_model_text(data.get("message", {}).get("content", ""))


def is_repetitive(user_id: int, reply: str) -> bool:
    previous = last_replies.setdefault(user_id, [])
    current_words = set(reply.lower().split())
    for old in previous[-5:]:
        old_words = set(old.lower().split())
        if current_words and len(current_words & old_words) / len(current_words | old_words) > 0.65:
            return True
    previous.append(reply)
    del previous[:-8]
    return False


async def generate_reply(state: UserState, user_message: str) -> str:
    try:
        reply = await call_ollama(build_messages(state, user_message))
        if not reply:
            return random.choice(FALLBACK_REPLIES)
        if is_repetitive(state.user_id, reply):
            retry_messages = build_messages(state, user_message)
            retry_messages.append({"role": "assistant", "content": reply})
            retry_messages.append({"role": "user", "content": "скажи иначе и короче"})
            reply = await call_ollama(retry_messages) or reply
        return reply
    except Exception as exc:
        log.warning("Ollama request failed: %s", exc)
        return random.choice(FALLBACK_REPLIES)


async def maybe_update_summary(state: UserState) -> None:
    if len(state.history) < 12 or state.msg_count % 8 != 0:
        return
    dialog = "\n".join(f"{m['role']}: {m['content']}" for m in state.history[-12:])
    messages = [
        {"role": "system", "content": "Сожми диалог в 2 коротких предложения для памяти companion-бота. Без выдумок."},
        {"role": "user", "content": dialog},
    ]
    try:
        state.summary = (await call_ollama(messages))[:1000]
    except Exception as exc:
        log.debug("summary failed: %s", exc)


def extract_profile_name(state: UserState, message: str) -> None:
    if state.profile.get("name"):
        return
    match = re.search(r"(?:\bменя\s+зовут\s+|\bя\s+)([а-яёa-z]{2,20})\b", message, flags=re.I)
    if match:
        state.profile["name"] = match.group(1).capitalize()


async def natural_delay(update: Update, text: str) -> None:
    if not update.effective_chat:
        return
    words = max(1, len(text.split()))
    delay = min(8.0, random.uniform(0.4, 1.6) + words * random.uniform(0.06, 0.16))
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(delay)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    first_name = html.escape(user.first_name if user and user.first_name else "")
    text = (
        f"привет{', ' + first_name if first_name else ''}. я карина, AI companion.\n"
        "можем просто болтать, но пароли/коды/экспорты аккаунтов мне не присылай — я такое не беру."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "просто напиши мне сообщение. я помню контекст диалога локально в базе и отвечаю через ollama, если он доступен."
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert db is not None
    user_id = update.effective_user.id
    await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    await db.commit()
    await update.message.reply_text("окей, память очистила")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(DOCUMENT_WARNING)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    if user_id in processing_users:
        await update.message.reply_text("щяс, я ещё отвечаю на прошлое")
        return
    processing_users.add(user_id)
    try:
        text = update.message.text.strip()
        if not text:
            return
        if contains_sensitive_input(text):
            await update.message.reply_text(DOCUMENT_WARNING)
            return
        state = await get_user_state(user_id, update.effective_user.first_name or "")
        state.last_seen = utc_now()
        state.msg_count += 1
        state.mood = update_mood(state.mood, text)
        extract_profile_name(state, text)

        await natural_delay(update, text)
        reply = await generate_reply(state, text)
        if violates_reply_policy(reply):
            log.warning("Blocked unsafe model reply for user_id=%s", user_id)
            reply = DOCUMENT_WARNING

        state.history.append({"role": "user", "content": text[:1000]})
        state.history.append({"role": "assistant", "content": reply[:1000]})
        await maybe_update_summary(state)
        await save_user_state(state)

        await update.message.reply_text(reply)
    finally:
        processing_users.discard(user_id)


async def post_init(application: Application) -> None:
    await init_db()
    log.info("Companion bot started with model=%s db=%s", MODEL_NAME, DB_PATH)


async def post_shutdown(application: Application) -> None:
    if db is not None:
        await db.close()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
