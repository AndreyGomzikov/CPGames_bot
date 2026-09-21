"""Для ответа в Telegram боте на команды /new и /process"""
import datetime
import os

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .database import SessionLocal
# Импорты из вашего проекта
from .models import Comment


load_dotenv()


TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
if not TG_BOT_TOKEN:
    raise ValueError("Не указан TG_BOT_TOKEN в .env файле")

# Словарь для хранения действий пользователя
user_actions = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    await update.message.reply_text(
        "👋 Привет! Я бот для управления комментариями VK.\n\n"
        "Доступные команды:\n"
        "/new - Показать 5 последних необработанных комментариев\n"
        "/process [ID] - Отметить комментарий как обработанный (например: /process 123)"
    )


async def show_new_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /new - показывает свежие необработанные комментарии"""
    db = SessionLocal()
    try:
        # Берем последние 5 необработанных комментариев
        comments = db.query(Comment).filter(
            Comment.is_processed == False
        ).order_by(Comment.date.desc()).limit(5).all()

        if not comments:
            await update.message.reply_text("✅ Сейчас нет новых необработанных комментариев!")
            return

        text = "📝 *Последние необработанные комментарии:*\n\n"
        for c in comments:
            # Ограничиваем текст, чтобы не было слишком длинных сообщений
            short_text = c.text[:50] + "..." if len(c.text) > 50 else c.text
            text += f"*ID {c.id}* | {c.author_name}:\n{short_text}\n[Ссылка]({c.url})\n\n"

        await update.message.reply_text(text, parse_mode="Markdown")
    finally:
        db.close()


async def process_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /process 123 - отмечает комментарий как обработанный"""
    db = SessionLocal()
    try:
        if not context.args:
            await update.message.reply_text("❌ Укажите ID комментария. Пример: `/process 1`", parse_mode="Markdown")
            return

        comment_id = int(context.args[0])
        comment = db.query(Comment).filter(Comment.id == comment_id).first()

        if not comment:
            await update.message.reply_text(f"❌ Комментарий с ID {comment_id} не найден в базе.")
            return

        # Меняем флаг
        comment.is_processed = True
        comment.updated_at = datetime.datetime.now()
        db.commit()

        await update.message.reply_text(f"✅ Комментарий ID {comment_id} отмечен как *обработанный (отвеченный)*!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Некорректный ID. Введите цифру.")
    finally:
        db.close()


def main():
    """Запуск бота"""
    application = Application.builder().token(TG_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("new", show_new_comments))
    application.add_handler(CommandHandler("process", process_comment))

    print("🤖 Telegram бот запущен и слушает команды...")
    application.run_polling()


if __name__ == "__main__":
    main()
