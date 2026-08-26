#!/usr/bin/env python3

import asyncio
import logging
import sqlite3
import random
import string
import time
import os
import json
import hashlib
from datetime import datetime, timedelta
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError
from telethon.tl.functions.messages import ReportRequest, ReportSpamRequest
from telethon.tl.types import (
    InputReportReasonSpam,
    InputReportReasonViolence,
    InputReportReasonPornography,
    InputReportReasonPersonalDetails,
    InputReportReasonOther
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ========== КОНФИГ ==========
BOT_TOKEN = "8893474413:AAFHwmwA4nYivzTo29MYD-okhCT4LtynEbU"  # ← ЗАМЕНИТЬ
ADMIN_IDS = [8402303508]  # ← ЗАМЕНИТЬ
DB_NAME = "snoser.db"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# ========== БД ==========
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  session_string TEXT,
                  phone TEXT,
                  is_active INTEGER DEFAULT 1,
                  added_at TEXT,
                  last_used TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  sub_until TEXT,
                  is_admin INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS reports
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  target TEXT,
                  reason TEXT,
                  count INTEGER DEFAULT 0,
                  created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS stats
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  total_reports INTEGER DEFAULT 0,
                  successful_reports INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()
    # Добавляем начальную статистику
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO stats (id, total_reports, successful_reports) VALUES (1, 0, 0)")
    conn.commit()
    conn.close()

init_db()

# ========== ПРОВЕРКА ПОДПИСКИ ==========
def has_subscription(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT sub_until FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0]) > datetime.now()
        except:
            return False
    return False

def get_subscription_days(user_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT sub_until FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            until = datetime.fromisoformat(row[0])
            diff = until - datetime.now()
            return diff.days
        except:
            return 0
    return 0

def give_subscription(user_id: int, days: int):
    until = (datetime.now() + timedelta(days=days)).isoformat()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, sub_until) VALUES (?, ?)", (user_id, until))
    conn.commit()
    conn.close()

# ========== ФУНКЦИИ СЕССИЙ ==========
def get_sessions():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def get_active_sessions():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM sessions WHERE is_active=1")
    rows = c.fetchall()
    conn.close()
    return rows

def add_session(session_string, phone):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO sessions (session_string, phone, added_at) VALUES (?, ?, ?)",
              (session_string, phone, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def update_session_status(session_id, status):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE sessions SET is_active=? WHERE id=?", (status, session_id))
    conn.commit()
    conn.close()

def delete_session(session_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT total_reports, successful_reports FROM stats WHERE id=1")
    row = c.fetchone()
    conn.close()
    return row if row else (0, 0)

def update_stats(successful=0, total=0):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE stats SET total_reports=total_reports+?, successful_reports=successful_reports+? WHERE id=1",
              (total, successful))
    conn.commit()
    conn.close()

def add_report_log(target, reason, count):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO reports (target, reason, count, created_at) VALUES (?, ?, ?, ?)",
              (target, reason, count, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# ========== ДОБАВЛЕНИЕ СЕССИИ ПО ШАГАМ ==========
async def add_session_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет прав")
        return
    
    # ШАГ 1: Просим номер
    await update.message.reply_text(
        "📱 *Добавление сессии (шаг 1/3)*\n\n"
        "Введи номер телефона в международном формате:\n"
        "Пример: `+79991234567`",
        parse_mode="Markdown"
    )
    context.user_data['add_session_step'] = 'phone'

async def handle_add_session_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    step = context.user_data.get('add_session_step')
    text = update.message.text.strip()
    
    if step == 'phone':
        if not text.startswith('+') or not text[1:].isdigit():
            await update.message.reply_text("❌ Неверный формат. Пример: `+79991234567`", parse_mode="Markdown")
            return
        
        context.user_data['add_session_phone'] = text
        context.user_data['add_session_step'] = 'code'
        
        # Создаём клиента для отправки кода
        client = TelegramClient("temp_session", API_ID, API_HASH)
        await client.connect()
        try:
            await client.send_code_request(text)
            context.user_data['add_session_client'] = client
            await update.message.reply_text(
                "📱 *Добавление сессии (шаг 2/3)*\n\n"
                "Код отправлен на номер.\n"
                "Введи код из Telegram:",
                parse_mode="Markdown"
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            context.user_data['add_session_step'] = None
    
    elif step == 'code':
        code = text
        phone = context.user_data.get('add_session_phone')
        client = context.user_data.get('add_session_client')
        
        if not client:
            await update.message.reply_text("❌ Ошибка: сессия не найдена. Начни заново.")
            context.user_data['add_session_step'] = None
            return
        
        try:
            await client.sign_in(phone, code)
            # Успешный вход
            session_string = client.session.save()
            add_session(session_string, phone)
            await client.disconnect()
            
            context.user_data['add_session_step'] = None
            context.user_data['add_session_client'] = None
            context.user_data['add_session_phone'] = None
            
            sessions = get_sessions()
            active = get_active_sessions()
            
            await update.message.reply_text(
                f"✅ *Сессия добавлена!*\n\n"
                f"📱 Номер: {phone}\n"
                f"📊 Всего сессий: {len(sessions)}\n"
                f"✅ Активных: {len(active)}",
                parse_mode="Markdown"
            )
            
        except SessionPasswordNeededError:
            # ШАГ 3: Требуется 2FA
            context.user_data['add_session_step'] = '2fa'
            await update.message.reply_text(
                "🔐 *Добавление сессии (шаг 3/3)*\n\n"
                "Требуется 2FA пароль.\n"
                "Введи пароль:",
                parse_mode="Markdown"
            )
            
        except PhoneCodeInvalidError:
            await update.message.reply_text("❌ Неверный код. Попробуй ещё раз:")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            context.user_data['add_session_step'] = None
    
    elif step == '2fa':
        password = text
        client = context.user_data.get('add_session_client')
        phone = context.user_data.get('add_session_phone')
        
        if not client:
            await update.message.reply_text("❌ Ошибка. Начни заново.")
            context.user_data['add_session_step'] = None
            return
        
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            add_session(session_string, phone)
            await client.disconnect()
            
            context.user_data['add_session_step'] = None
            context.user_data['add_session_client'] = None
            context.user_data['add_session_phone'] = None
            
            sessions = get_sessions()
            active = get_active_sessions()
            
            await update.message.reply_text(
                f"✅ *Сессия добавлена!*\n\n"
                f"📱 Номер: {phone}\n"
                f"📊 Всего сессий: {len(sessions)}\n"
                f"✅ Активных: {len(active)}",
                parse_mode="Markdown"
            )
            
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# ========== ОБРАБОТЧИКИ КОМАНД ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет прав доступа")
        return
    
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📱 Сессии", callback_data="sessions")],
        [InlineKeyboardButton("➕ Добавить сессию", callback_data="add_session")],
        [InlineKeyboardButton("🎯 Снос цели", callback_data="report")],
        [InlineKeyboardButton("👤 Пользователи", callback_data="users")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")],
    ]
    await update.message.reply_text(
        "🤖 *Snoser Bot — Админ-панель*\n\n"
        "Выбери действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Нет прав")
        return
    
    if data == "stats":
        total, successful = get_stats()
        sessions = get_sessions()
        active = get_active_sessions()
        text = f"📊 *Статистика*\n\n"
        text += f"📱 Всего сессий: {len(sessions)}\n"
        text += f"✅ Активных сессий: {len(active)}\n"
        text += f"📨 Всего репортов: {total}\n"
        text += f"✅ Успешных репортов: {successful}\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]))
    
    elif data == "sessions":
        sessions = get_sessions()
        if not sessions:
            text = "📱 *Сессии*\n\nНет добавленных сессий."
        else:
            text = f"📱 *Сессии ({len(sessions)} всего)*\n\n"
            for s in sessions[:15]:
                status = "✅ Активна" if s[3] == 1 else "❌ Неактивна"
                text += f"ID: `{s[0]}` | {s[2]} | {status}\n"
            if len(sessions) > 15:
                text += f"\n... и ещё {len(sessions)-15}"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="sessions")],
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]))
    
    elif data == "add_session":
        await query.edit_message_text(
            "➕ *Добавление сессии*\n\n"
            "📌 Способ 1: По номеру телефона\n"
            "Нажми «По номеру» и следуй инструкциям.\n\n"
            "📌 Способ 2: Загрузить файл .session\n"
            "Нажми «Загрузить файл» и отправь файл.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📱 По номеру", callback_data="add_session_phone")],
                [InlineKeyboardButton("📁 Загрузить файл", callback_data="add_session_file")],
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )
    
    elif data == "add_session_phone":
        await query.edit_message_text(
            "📱 *Добавление по номеру*\n\n"
            "Введи номер телефона в формате:\n"
            "`+79991234567`",
            parse_mode="Markdown"
        )
        context.user_data['add_session_step'] = 'phone'
    
    elif data == "add_session_file":
        await query.edit_message_text(
            "📁 *Загрузить файл .session*\n\n"
            "Отправь файл с расширением `.session`\n"
            "Бот автоматически добавит его.",
            parse_mode="Markdown"
        )
        context.user_data['adding_session'] = True
    
    elif data == "report":
        await query.edit_message_text(
            "🎯 *Снос цели*\n\n"
            "Выбери причину репорта:\n"
            "1️⃣ Спам\n"
            "2️⃣ Насилие\n"
            "3️⃣ Порнография\n"
            "4️⃣ Личные данные\n"
            "5️⃣ Другое\n\n"
            "Используй команду:\n"
            "`/report <ссылка> <причина>`\n\n"
            "Пример: `/report https://t.me/username/123 1`",
            parse_mode="Markdown"
        )
    
    elif data == "users":
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT user_id, sub_until FROM users")
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            text = "👤 *Пользователи*\n\nНет пользователей."
        else:
            text = f"👤 *Пользователи ({len(rows)})*\n\n"
            for r in rows[:10]:
                days = 0
                if r[1]:
                    try:
                        until = datetime.fromisoformat(r[1])
                        days = (until - datetime.now()).days
                    except:
                        days = 0
                status = f"✅ {days} дней" if days > 0 else "❌ Нет подписки"
                text += f"ID: `{r[0]}` | {status}\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="back")]
        ]))
    
    elif data == "help":
        await query.edit_message_text(
            "ℹ️ *Помощь*\n\n"
            "📌 Команды:\n"
            "/start - Открыть меню\n"
            "/addsession <номер> <код> [2fa] - Добавить сессию\n"
            "/report <ссылка> <причина> - Отправить репорт\n"
            "/sessions - Список сессий\n"
            "/stats - Статистика\n"
            "/givesub <id> <дни> - Выдать подписку\n"
            "/delete <id> - Удалить сессию\n\n"
            "📌 Причины:\n"
            "1 - Спам\n"
            "2 - Насилие\n"
            "3 - Порнография\n"
            "4 - Личные данные\n"
            "5 - Другое",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="back")]
            ])
        )
    
    elif data == "back":
        await start(update, context)

# ========== АДМИН КОМАНДЫ ==========
async def give_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет прав")
        return
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/givesub <user_id> <дни>`\n\n"
            "Пример: `/givesub 123456789 30`",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_id = int(args[0])
        days = int(args[1])
    except:
        await update.message.reply_text("❌ ID и дни должны быть числами")
        return
    
    give_subscription(target_id, days)
    
    await update.message.reply_text(
        f"✅ *Подписка выдана!*\n\n"
        f"👤 Пользователь: `{target_id}`\n"
        f"📅 Дней: {days}\n"
        f"⏳ Действует до: {(datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')}",
        parse_mode="Markdown"
    )

async def delete_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет прав")
        return
    
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("❌ Использование: /delete <id>")
        return
    
    try:
        session_id = int(args[0])
    except:
        await update.message.reply_text("❌ ID должен быть числом")
        return
    
    delete_session(session_id)
    sessions = get_sessions()
    
    await update.message.reply_text(
        f"✅ Сессия {session_id} удалена!\n"
        f"📊 Осталось сессий: {len(sessions)}"
    )

# ========== СНОС (РЕПОРТ) ==========
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Нет прав")
        return
    
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Использование: `/report <ссылка> <причина>`\n\n"
            "Пример: `/report https://t.me/username/123 1`\n\n"
            "Причины:\n"
            "1 - Спам\n"
            "2 - Насилие\n"
            "3 - Порнография\n"
            "4 - Личные данные\n"
            "5 - Другое",
            parse_mode="Markdown"
        )
        return
    
    target = args[0]
    reason_id = int(args[1]) if args[1].isdigit() else 1
    
    reasons = {
        1: InputReportReasonSpam(),
        2: InputReportReasonViolence(),
        3: InputReportReasonPornography(),
        4: InputReportReasonPersonalDetails(),
        5: InputReportReasonOther()
    }
    
    if reason_id not in reasons:
        await update.message.reply_text("❌ Неверная причина. Используй 1-5")
        return
    
    # Разбираем ссылку
    parts = target.split("/")
    try:
        entity = parts[-2]
        msg_id = int(parts[-1])
    except:
        await update.message.reply_text("❌ Неверный формат ссылки. Пример: https://t.me/username/123")
        return
    
    sessions = get_active_sessions()
    if not sessions:
        await update.message.reply_text("❌ Нет активных сессий. Добавь сессии через меню.")
        return
    
    await update.message.reply_text(f"🔄 Начинаю снос... Сессий: {len(sessions)}")
    
    successful = 0
    failed = 0
    
    for s in sessions:
        try:
            client = TelegramClient("report_session", API_ID, API_HASH)
            client.session.save()
            await client.connect()
            
            # Пытаемся загрузить сессию
            try:
                client.session.set_dc(2, '149.154.167.40', 443)
                await client.get_me()
            except:
                continue
            
            # Получаем сущность
            try:
                entity_obj = await client.get_entity(entity)
            except:
                continue
            
            # Отправляем репорт
            if reason_id == 1:
                await client(ReportSpamRequest(peer=entity_obj))
            else:
                await client(ReportRequest(
                    peer=entity_obj,
                    id=[msg_id],
                    reason=reasons[reason_id],
                    message=""
                ))
            
            successful += 1
            update_stats(successful=1, total=1)
            add_report_log(target, str(reason_id), 1)
            
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            failed += 1
        except Exception:
            failed += 1
        finally:
            try:
                await client.disconnect()
            except:
                pass
    
    await update.message.reply_text(
        f"✅ *Снос завершён!*\n\n"
        f"🎯 Цель: {target}\n"
        f"✅ Успешных репортов: {successful}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="Markdown"
    )

# ========== ЗАГРУЗКА ФАЙЛА СЕССИИ ==========
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    if not context.user_data.get('adding_session'):
        return
    
    if not update.message.document:
        await update.message.reply_text("❌ Отправь файл .session")
        return
    
    file = update.message.document
    if not file.file_name.endswith('.session'):
        await update.message.reply_text("❌ Файл должен иметь расширение .session")
        return
    
    file_path = f"temp_{file.file_id}.session"
    await file.download_to_drive(file_path)
    
    try:
        with open(file_path, 'r') as f:
            session_string = f.read()
        
        add_session(session_string, f"File_{file.file_id[:8]}")
        context.user_data['adding_session'] = False
        
        sessions = get_sessions()
        active = get_active_sessions()
        
        await update.message.reply_text(
            f"✅ *Сессия добавлена из файла!*\n\n"
            f"📊 Всего сессий: {len(sessions)}\n"
            f"✅ Активных: {len(active)}",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ========== ЗАПУСК ==========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addsession", add_session_step))
    app.add_handler(CommandHandler("givesub", give_sub))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(CommandHandler("delete", delete_session_command))
    app.add_handler(CommandHandler("stats", lambda u,c: button_handler(u,c) if u.message else None))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_session_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    
    print("=" * 50)
    print("🤖 SNOSER BOT started!")
    print(f"👑 Админ: {ADMIN_IDS[0]}")
    print("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
