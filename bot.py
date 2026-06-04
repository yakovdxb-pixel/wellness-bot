import asyncio
import json
import os
import logging
from datetime import date, timedelta
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ── Конфиг ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
OURA_CLIENT_ID     = os.getenv("OURA_CLIENT_ID")
OURA_CLIENT_SECRET = os.getenv("OURA_CLIENT_SECRET")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY")
REDIRECT_URI       = os.getenv("REDIRECT_URI", "https://worker-production-7137.up.railway.app/callback")
TOKENS_FILE      = "tokens.json"

# ── Хранение токенов ─────────────────────────────────────────────────────────
def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {}

def save_tokens(data):
    with open(TOKENS_FILE, "w") as f:
        json.dump(data, f, indent=2)

tokens_store  = load_tokens()
pending_auth  = {}
telegram_app  = None

# ── Oura API ─────────────────────────────────────────────────────────────────
async def exchange_code(code: str) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.ouraring.com/oauth/token",
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "client_id":     OURA_CLIENT_ID,
                "client_secret": OURA_CLIENT_SECRET,
                "redirect_uri":  REDIRECT_URI,
            }
        ) as r:
            return await r.json()

async def get_oura(access_token: str, endpoint: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"https://api.ouraring.com/v2/usercollection/{endpoint}",
            headers=headers,
            params=params or {}
        ) as r:
            return await r.json()

async def fetch_health_data(access_token: str) -> dict:
    today     = date.today().isoformat()
    week_ago  = (date.today() - timedelta(days=7)).isoformat()

    sleep     = await get_oura(access_token, "sleep",           {"start_date": week_ago, "end_date": today})
    readiness = await get_oura(access_token, "daily_readiness", {"start_date": week_ago, "end_date": today})
    spo2      = await get_oura(access_token, "daily_spo2",      {"start_date": week_ago, "end_date": today})
    stress    = await get_oura(access_token, "daily_stress",    {"start_date": week_ago, "end_date": today})
    activity  = await get_oura(access_token, "daily_activity",  {"start_date": week_ago, "end_date": today})

    return {
        "сон":           sleep    .get("data", [])[-3:],
        "готовность":    readiness.get("data", [])[-3:],
        "кислород_SpO2": spo2     .get("data", [])[-3:],
        "стресс":        stress   .get("data", [])[-3:],
        "активность":    activity .get("data", [])[-3:],
    }

# ── Claude AI ────────────────────────────────────────────────────────────────
def ask_claude(question: str, health_data: dict) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        system="""Ты персональный health-ассистент, анализирующий данные Oura Ring.
Всегда отвечай на русском языке. Объясняй простым языком без медицинского жаргона.
Давай конкретные советы что делать сегодня. Используй эмодзи.
Не ставь диагнозы. Будь дружелюбным и мотивирующим.
Если показатели плохие — объясни почему и что делать, без страшилок.""",
        messages=[{
            "role": "user",
            "content": (
                f"Вопрос пользователя: {question}\n\n"
                f"Данные Oura Ring за последние дни:\n"
                f"{json.dumps(health_data, ensure_ascii=False, indent=2)}"
            )
        }]
    )
    return resp.content[0].text

# ── Telegram handlers ────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id in tokens_store:
        await update.message.reply_text(
            "Привет! Oura Ring уже подключён 💍\n\n"
            "Спроси меня что угодно:\n"
            "• как я спал?\n"
            "• какой у меня HRV?\n"
            "• мой стресс вчера\n"
            "• дай совет на сегодня\n"
            "• общая сводка\n\n"
            "Или напиши /summary для утренней сводки."
        )
        return

    state = f"tg_{user_id}"
    pending_auth[state] = user_id

    params = {
        "response_type": "code",
        "client_id":     OURA_CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "scope":         "email personal daily heartrate workout tag session spo2 stress heart_health",
        "state":         state,
    }
    auth_url = f"https://cloud.ouraring.com/oauth/authorize?{urlencode(params)}"

    keyboard = [[InlineKeyboardButton("🔗 Подключить Oura Ring", url=auth_url)]]
    await update.message.reply_text(
        "Привет! Я твой персональный health-ассистент 🏃\n\n"
        "Анализирую данные Oura Ring и объясняю всё на русском языке.\n\n"
        "Нажми кнопку чтобы подключить кольцо:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update.message.text = (
        "Дай мне полную утреннюю сводку: сон, HRV, пульс в покое, "
        "кислород SpO2, стресс, активность — и конкретные советы на сегодня."
    )
    await handle_message(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if user_id not in tokens_store:
        await update.message.reply_text(
            "Сначала подключи Oura Ring — напиши /start"
        )
        return

    thinking = await update.message.reply_text("⏳ Получаю твои данные...")

    try:
        access_token = tokens_store[user_id]["access_token"]
        health_data  = await fetch_health_data(access_token)
        answer       = ask_claude(update.message.text, health_data)
        await thinking.delete()
        await update.message.reply_text(answer)
    except Exception as e:
        logging.exception("Ошибка при обработке сообщения")
        await thinking.edit_text(f"❌ Ошибка: {e}")

# ── OAuth callback сервер ────────────────────────────────────────────────────
async def oauth_callback(request: web.Request):
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")

    if not code or state not in pending_auth:
        return web.Response(text="Ошибка авторизации", status=400)

    user_id    = pending_auth.pop(state)
    token_data = await exchange_code(code)

    if "access_token" in token_data:
        tokens_store[user_id] = token_data
        save_tokens(tokens_store)

        if telegram_app:
            await telegram_app.bot.send_message(
                chat_id=int(user_id),
                text="✅ Oura Ring подключён! Теперь спрашивай меня о своём здоровье 💪"
            )

        return web.Response(
            text="<h2>✅ Готово!</h2><p>Вернись в Telegram — бот уже готов к работе.</p>",
            content_type="text/html"
        )

    return web.Response(text=f"Ошибка: {token_data}", status=400)

# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    global telegram_app

    # OAuth сервер
    web_app = web.Application()
    web_app.router.add_get("/callback", oauth_callback)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    logging.info("OAuth сервер запущен: http://localhost:8080")

    # Telegram бот
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start",   cmd_start))
    telegram_app.add_handler(CommandHandler("summary", cmd_summary))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logging.info("Бот запущен! Напиши /start в Telegram.")

    try:
        await asyncio.Event().wait()
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
