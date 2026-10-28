#!/usr/bin/env python3
"""
Telegram-бот для продажи одноразовых ссылок Vless-Links.

Запускает одновременно:
1. aiogram-бота (long polling) — общение с покупателями
2. aiohttp-сервер — принимает webhook-уведомления от ЮMoney об оплате
"""
import os
import json
import asyncio
import io
import logging
import random
import string
from datetime import datetime, timedelta
import aiohttp
from config import API_BASE_URL
import qrcode
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BufferedInputFile,
)

import config
import database as db
import api_client
import payments
from products import PRODUCTS, TRIAL, EXTEND_PRODUCTS, calculate_discounted_price, get_discount_info, DISCOUNT_INFO
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
support_users = {}


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()



# ===================== ПОДДЕРЖКА =====================
# Глобальный словарь для хранения соответствий сообщений
# Формат: {(admin_chat_id, admin_message_id): (user_id, user_message_id)}
support_messages = {}

# Файл для сохранения данных поддержки
SUPPORT_FILE = "support_data.json"

def load_support_data():
    """Загрузить данные поддержки при запуске"""
    global support_messages
    if os.path.exists(SUPPORT_FILE):
        try:
            with open(SUPPORT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Конвертируем ключи обратно в кортежи
                for key, value in data.items():
                    admin_chat_id, admin_msg_id = map(int, key.split(':'))
                    user_id, user_msg_id = value
                    support_messages[(admin_chat_id, admin_msg_id)] = (user_id, user_msg_id)
        except Exception as e:
            logger.error(f"Ошибка загрузки данных поддержки: {e}")

def save_support_data():
    """Сохранить данные поддержки"""
    try:
        data = {}
        for (admin_chat_id, admin_msg_id), (user_id, user_msg_id) in support_messages.items():
            key = f"{admin_chat_id}:{admin_msg_id}"
            data[key] = [user_id, user_msg_id]
        
        with open(SUPPORT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения данных поддержки: {e}")

# Загружаем при старте
load_support_data()

# ─────────────────────── РЕФЕРАЛЬНАЯ ПРОГРАММА ───────────────────────
REFERRAL_BONUS_DAYS = 7


def calculate_price(product_price: int, active_links_count: int) -> tuple:
    """
    Рассчитывает цену в зависимости от количества активных ссылок
    Возвращает (final_price, discount_message)
    """
    if active_links_count == 0:
        # Первая ссылка — полная цена
        return product_price, ""
    
    # Вторая и последующие — 100₽
    if product_price > 100:
        final_price = 100
        discount_message = f"💰 Скидка: {product_price - 100}₽ (вторая и последующие ссылки — 100₽)"
    else:
        final_price = product_price
        discount_message = ""
    
    return final_price, discount_message

# ─────────────────────── ИНСТРУКЦИЯ ПО УСТАНОВКЕ ───────────────────────
def get_happ_instructions(link: str = None) -> str:
    """Возвращает инструкцию по установке Happ с ссылкой"""
    instructions = (
        "\n\n📱 *Как подключиться:*\n\n"
        "1️⃣ *Скачайте приложение Happ:*\n"
        "   • [Android](https://play.google.com/store/apps/details?id=com.happproxy)\n"
        "   • [iOS](https://apps.apple.com/app/happ/id123456789)\n"
        "   • [Windows](https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe)\n\n"
        "2️⃣ *Запустите приложение*\n"
        "3️⃣ *Вставьте ссылку* в поле для подписки\n"
        "   (нажмите «+» или «Добавить подписку»)\n"
        "4️⃣ *Наслаждайтесь безопасным интернетом!* 🔒\n\n"
        "💡 *Совет:* Сохраните ссылку в заметках, чтобы не потерять!"
    )
    
    if link:
        return f"🔗 *Ссылка:*\n`{link}`\n\n" + instructions
    return instructions


class AdminStates(StatesGroup):
    waiting_reset_trial_id = State()
    waiting_client_info_id = State()
    waiting_disable_subscription_id = State()
    waiting_delete_link_id = State()
    waiting_change_file_client_id = State()
    waiting_change_file_link_id = State()
    waiting_claim_link = State()
    waiting_promocode = State()
    waiting_custom_promo = State()

    waiting_support_message = State()

class SupportStates(StatesGroup):
    waiting_message = State()

# ─────────────────────── УТИЛИТЫ ───────────────────────

async def ensure_user(telegram_id: int, username: str, referrer_id: int = None) -> dict:
    user = await db.get_user(telegram_id)
    if user:
        try:
            client = await api_client.get_client(user["client_id"])
            return dict(user)
        except api_client.ApiError as e:
            if "не найден" in str(e) or "404" in str(e):
                # Клиент удалён — создаём нового
                logger.info(f"Клиент {user['client_id']} удалён, создаю нового и сбрасываю trial")
                name = f"tg_{telegram_id}_{username or 'nouser'}"
                created = await api_client.create_client(name)
                await db.update_user_client(
                    telegram_id,
                    created["client_id"],
                    created["api_key"],
                    reset_trial=True
                )
                user = await db.get_user(telegram_id)
                
                # Уведомляем пользователя
                await bot.send_message(
                    telegram_id,
                    "🔄 Ваш аккаунт был восстановлен!\n"
                    "🎁 Пробный период снова доступен для активации."
                )
                
                return dict(user)
            else:
                raise
    
    name = f"tg_{telegram_id}_{username or 'nouser'}"
    created = await api_client.create_client(name)
    await db.create_user(telegram_id, username, created["client_id"], created["api_key"], referrer_id=referrer_id)
    user = await db.get_user(telegram_id)
    return dict(user)


async def reward_referrer(referrer_telegram_id: int, invited_telegram_id: int, invited_username: str) -> bool:
    referrer = await db.get_user(referrer_telegram_id)
    if not referrer:
        return False

    try:
        links = await api_client.get_client_links(referrer["client_id"])
    except Exception as e:
        logger.error(f"Не удалось получить ссылки реферера {referrer_telegram_id}: {e}")
        links = []

    try:
        if links:
            target = next((l for l in links if not l.get("is_used") and l.get("file_name") != "Disabled.txt"), None)
            if not target:
                target = next((l for l in links if l.get("file_name") != "Disabled.txt"), links[0])
            result = await api_client.extend_link_paid(target["id"], REFERRAL_BONUS_DAYS)
            if not result.get("success"):
                raise Exception(result.get("error", "extend_link_paid failed"))
        else:
            await generate_link_with_recovery(
                telegram_id=referrer_telegram_id,
                username=referrer["username"],
                file=TRIAL["file"],
                expires_in=REFERRAL_BONUS_DAYS * 86400,
                max_uses=1,
            )

        await db.mark_referral_rewarded(invited_telegram_id)
        await bot.send_message(
            referrer_telegram_id,
            f"🤝 Ваш друг @{invited_username or 'без username'} оплатил подписку!\n"
            f"🎁 Вам начислено +{REFERRAL_BONUS_DAYS} дней к подписке.",
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка начисления реферального бонуса для {referrer_telegram_id}: {e}")
        return False


def products_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for pid, p in PRODUCTS.items():
        rows.append([
            InlineKeyboardButton(
                text=f"{p['title']} — {p['price_rub']}₽",
                callback_data=f"buy:{pid}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_qr_bytes(url: str) -> bytes:
    """Генерирует QR-код для ссылки."""
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def format_remaining(expires_at_str: str) -> str:
    if not expires_at_str:
        return "—"
    try:
        from datetime import timezone
        if expires_at_str.endswith('Z'):
            expires_at_str = expires_at_str.replace('Z', '+00:00')
        expires_at = datetime.fromisoformat(expires_at_str)
        
        now = datetime.now(timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        
        remaining = expires_at - now
        
        if remaining.total_seconds() <= 0:
            return "⏳ Истекла"
        
        days = remaining.days
        hours = remaining.seconds // 3600
        minutes = (remaining.seconds % 3600) // 60
        seconds = remaining.seconds % 60
        
        if days > 0:
            return f"⏱ {days}д {hours}ч {minutes}м"
        elif hours > 0:
            return f"⏱ {hours}ч {minutes}м {seconds}с"
        elif minutes > 0:
            return f"⏱ {minutes}м {seconds}с"
        else:
            return f"⏱ {seconds}с"
    except Exception as e:
        logger.error(f"Error formatting remaining time: {e}")
        return "—"


async def generate_link_with_recovery(telegram_id: int, username: str, file: str, expires_in: int, max_uses: int) -> dict:
    user = await db.get_user(telegram_id)
    try:
        return await api_client.generate_link(
            api_key=user["api_key"], file=file, expires_in=expires_in, max_uses=max_uses
        )
    except api_client.ApiError as e:
        if "Invalid or inactive API key" not in str(e):
            raise
        logger.warning(f"Ключ для {telegram_id} невалиден, пересоздаю клиента")
        name = f"tg_{telegram_id}_{username or 'nouser'}"
        created = await api_client.create_client(name)
        await db.update_user_client(telegram_id, created["client_id"], created["api_key"])
        return await api_client.generate_link(
            api_key=created["api_key"], file=file, expires_in=expires_in, max_uses=max_uses
        )


# ─────────────────────── ХЕНДЛЕРЫ БОТА ───────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    telegram_id = message.from_user.id
    referrer_id = None

    await db.increment_start_counter()
    is_new_user = await db.increment_unique_user(telegram_id)
    if is_new_user:
        logger.info(f"Новый уникальный пользователь: {telegram_id} (@{message.from_user.username})")

    args = command.args
    if args and args.startswith("ref_"):
        try:
            candidate_id = int(args[4:])
            existing_user = await db.get_user(telegram_id)
            if candidate_id != telegram_id and existing_user is None:
                referrer = await db.get_user(candidate_id)
                if referrer:
                    referrer_id = candidate_id
        except ValueError:
            pass

    await ensure_user(telegram_id, message.from_user.username, referrer_id=referrer_id)
    is_admin = telegram_id in config.ADMIN_IDS

    greeting = (
        "👋 Привет! Ищешь надежный и быстрый VPN, тебе сюда, 13 стран + LTE обходы Белых списков. Здесь можно получить доступ по одноразовой ссылке.\n\n"
        "🎁 Есть бесплатный пробный период на 2 дня — жмите кнопку ниже.\n"
        "Установите приложение Happ (happ.info/#) и вставьте туда полученную ссылку.\n"
    )
    if referrer_id:
        greeting += f"\n\n🤝 Вы пришли по приглашению — после первой оплаты ваш друг получит бонусные дни!"

    # ✅ Кнопка для открытия меню (если меню не открылось)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть меню", callback_data="open_menu")]
    ])

    await message.answer(greeting, reply_markup=keyboard)

@dp.message(F.text == "🤝 Пригласить друга")
async def on_invite_friend(message: Message):
    telegram_id = message.from_user.id
    await ensure_user(telegram_id, message.from_user.username)

    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{telegram_id}"

    rewarded = await db.count_rewarded_referrals(telegram_id)
    total = await db.count_all_referrals(telegram_id)

    await message.answer(
        "🤝 *Пригласи друга — получи дни бесплатно!*\n\n"
        f"За каждого друга, который перейдёт по вашей ссылке и оплатит любой тариф, "
        f"вам начислится *+{REFERRAL_BONUS_DAYS} дней* подписки.\n\n"
        f"🔗 *Ваша реферальная ссылка:*\n"
        f"`{ref_link}`\n\n"
        f"📌 *Как использовать:*\n"
        f"1. Отправьте эту ссылку другу\n"
        f"2. Друг нажимает /start в боте\n"
        f"3. Покупает подписку\n"
        f"4. Вы получаете +{REFERRAL_BONUS_DAYS} дней!\n\n"
        f"👥 Перешло по ссылке: {total}\n"
        f"✅ Оплатили (принесли бонус): {rewarded}",
        parse_mode="Markdown",
    )


@dp.message(Command("stats_start"))
async def cmd_stats_start(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    
    stats = await db.get_start_stats()
    unique_users = await db.get_unique_users_count()
    
    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего нажатий /start: {stats['total_starts']}\n"
        f"👤 Уникальных пользователей: {unique_users}\n"
        f"🕐 Последнее обновление: {stats['last_updated'][:16].replace('T', ' ')}",
        parse_mode="Markdown"
    )


@dp.message(F.text == "🎁 Пробный период")
async def on_trial(message: Message):
    user = await ensure_user(message.from_user.id, message.from_user.username)
    if user["trial_used"]:
        await message.answer(
            "🙅 Пробный период уже был использован. Загляните в «💳 Купить» — там есть постоянные тарифы."
        )
        return
    await message.answer("⏳ Выдаю пробную ссылку...")
    try:
        result = await generate_link_with_recovery(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            file=TRIAL["file"],
            expires_in=TRIAL["expires_in"],
            max_uses=TRIAL["max_uses"],
        )
        await db.mark_trial_used(message.from_user.id)
        link = result["link"]
        qr_bytes = make_qr_bytes(link)
        
        caption = (
            f"🎁 *Пробная ссылка готова!*\n\n"
            f"🔗 `{link}`\n"
            f"⏱ Действует: 48 ч\n"
            f"🔢 Активаций: {TRIAL['max_uses']}"
        ) + get_happ_instructions()
        
        await message.answer_photo(
            photo=BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=caption,
            parse_mode="Markdown"
        )
    except api_client.ApiError as e:
        await message.answer(f"⚠️ Не удалось выдать ссылку: {e}")


@dp.message(F.text == "💳 Купить")
async def on_buy_button(message: Message):
    user = await ensure_user(message.from_user.id, message.from_user.username)
    
    # Получаем количество активных ссылок
    try:
        active_links_count = await api_client.get_active_links_count(user["client_id"])
    except Exception as e:
        logger.error(f"Ошибка получения активных ссылок: {e}")
        active_links_count = 0
    
    # Формируем информацию о скидке
    discount_info = get_discount_info(active_links_count)
    
    # Создаём красивое меню
    text = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "    💎 *ВЫБЕРИ ТАРИФ* 💎\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
    )
    
    if active_links_count == 0:
        text += "🆕 *Первая ссылка:* полная цена\n"
    else:
        text += f"🔥 *У вас уже есть {active_links_count} активных ссылок!*\n"
        text += f"💎 *Скидка {DISCOUNT_INFO['discount_percent']}% на этот тариф!*\n\n"
    
    text += "📋 *Доступные тарифы:*\n"
    text += "━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Добавляем тарифы с ценами и скидками
    for pid, p in PRODUCTS.items():
        original_price = p["price_rub"]
        if active_links_count > 0:
            final_price = int(original_price * (100 - DISCOUNT_INFO['discount_percent']) / 100)
            price_display = f"~~{original_price}₽~~ → *{final_price}₽* (🔥 -{DISCOUNT_INFO['discount_percent']}%)"
        else:
            price_display = f"*{original_price}₽*"
        
        text += f"{p['title']}\n"
        text += f"   📅 {p['description']}\n"
        text += f"   💰 {price_display}\n\n"
    
    text += "━━━━━━━━━━━━━━━━━━━\n"
    text += f"💡 {discount_info}"
    
    # Создаём клавиатуру с тарифами
    keyboard = []
    for pid, p in PRODUCTS.items():
        # Добавляем эмодзи для каждого тарифа
        emoji = p['title'].split()[0] if p['title'].split() else "📌"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{emoji} {p['title']}",
                callback_data=f"buy:{pid}"
            )
        ])
    
    await message.answer(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


@dp.message(F.text == "📋 Мои ссылки")
async def on_mylinks_button(message: Message):
    user = await db.get_user(message.from_user.id)
    
    if not user:
        await message.answer("⏳ Создаю пользователя...")
        await ensure_user(message.from_user.id, message.from_user.username)
        user = await db.get_user(message.from_user.id)
        if not user:
            await message.answer("❌ Не удалось создать пользователя. Попробуйте /start")
            return
    
    client_exists = True
    try:
        client = await api_client.get_client(user["client_id"])
    except api_client.ApiError as e:
        if "не найден" in str(e) or "404" in str(e):
            client_exists = False
            await message.answer("🔄 Ваш клиент был удалён на сервере. Создаю нового...")
        else:
            await message.answer(f"⚠️ Ошибка: {e}")
            return
    
    if not client_exists:
        try:
            name = f"tg_{message.from_user.id}_{message.from_user.username or 'nouser'}"
            created = await api_client.create_client(name)
            await db.update_user_client(message.from_user.id, created["client_id"], created["api_key"])
            user = await db.get_user(message.from_user.id)
            await message.answer(f"✅ Новый клиент создан (ID: {created['client_id']})")
        except Exception as e:
            await message.answer(f"❌ Не удалось создать клиента: {e}")
            return
    
    try:
        links = await api_client.get_client_links(user["client_id"])
    except api_client.ApiError as e:
        await message.answer(f"⚠️ Не удалось получить ссылки: {e}")
        return
    
    if not links:
        await message.answer(
            "📭 У вас пока нет ссылок.\n\n"
            "🎁 Нажмите «Пробный период» или 💳 «Купить» для получения.\n"
            "🔗 Если у вас есть ссылка из веб-интерфейса — нажмите «Привязать ссылку»."
        )
        return
    
    BASE_URL = "https://kongor.mooo.com"
    
    # Отправляем каждую ссылку отдельным сообщением с кнопкой копирования
    for link in links:
        status = "✅ Активна" if not link.get("is_used") else "🔴 Использована"
        expires = format_remaining(link.get("expires_at"))
        full_url = f"{BASE_URL}/{link['token']}"
        
        text = (
            f"🔗 *Токен:* `{link['token']}`\n"
            f"📄 *Файл:* {link['file_name']}\n"
            f"📊 *Использований:* {link['uses']}/{link['max_uses']}\n"
            f"⏱ *Осталось:* {expires}\n"
            f"📌 *Статус:* {status}\n\n"
            f"📋 *Ссылка для копирования:*\n"
            f"`{full_url}`"
        )
        
        # Кнопка для копирования
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Копировать ссылку", callback_data=f"copy_{link['token']}")],
            [InlineKeyboardButton(text="📱 QR-код", callback_data=f"qr_{link['token']}")],
        ])
        
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    
    # Добавляем информацию о пробном периоде
    if user["trial_used"]:
        await message.answer("🎁 *Пробный период:* использован ✅", parse_mode="Markdown")
    else:
        await message.answer("🎁 *Пробный период:* доступен! Нажмите «🎁 Пробный период».", parse_mode="Markdown")


@dp.message(F.text == "📎 Привязать ссылку")
async def on_claim_link(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer(
            "❌ Сначала нажмите /start, чтобы создать профиль.\n\n"
            "После этого вы сможете привязать ссылку."
        )
        return
    
    await message.answer(
        "🔗 *Привязка ссылки к аккаунту*\n\n"
        "Отправьте мне **полную ссылку** или **токен** (набор из 16 символов), "
        "который вы получили в веб-интерфейсе.\n\n"
        "📌 *Примеры:*\n"
        "• `https://kongor.mooo.com/a22dd605ad5bd430`\n"
        "• `a22dd605ad5bd430`\n\n"
        "После привязки ссылка появится в «📋 Мои ссылки» и вы сможете её продлить.",
        parse_mode="Markdown"
    )
    
    # Переключаем состояние — ждём ссылку от пользователя
    await state.set_state(AdminStates.waiting_claim_link)


@dp.callback_query(F.data.startswith("copy_"))
async def copy_link(callback: CallbackQuery):
    token = callback.data.split("_", 1)[1]
    full_url = f"https://kongor.mooo.com/{token}"
    
    # Отвечаем на callback, чтобы убрать уведомление
    await callback.answer()
    
    # Отправляем ссылку в виде текста для копирования
    await callback.message.answer(
        f"📋 *Скопируйте ссылку!*\n\n"
        f"`{full_url}`\n\n"
        f"Нажмите на ссылку и удерживайте, чтобы скопировать.",
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "cancel_claim")
async def on_cancel_claim(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("❌ Привязка отменена.")


@dp.callback_query(F.data == "cancel_extend")
async def on_cancel_extend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Продление отменено.")


@dp.callback_query(F.data.startswith("claim:"))
async def on_claim_selected(callback: CallbackQuery):
    await callback.answer()
    link_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    user = await db.get_user(user_id)
    if not user:
        await callback.message.answer("❌ Сначала нажмите /start")
        return
    
    try:
        link_info = await api_client.get_link(link_id)
        
        if link_info.get("is_used"):
            await callback.message.answer("❌ Эта ссылка уже использована.")
            return
        
        new_link = await api_client.generate_link(
            api_key=user["api_key"],
            file=link_info["file_name"],
            expires_in=link_info.get("expires_in", 86400),
            max_uses=link_info.get("max_uses", 1)
        )
        
        await callback.message.edit_text(
            f"✅ Ссылка успешно привязана!\n\n"
            f"🔗 {new_link['link']}\n"
            f"📌 Токен: `{new_link['token']}`\n"
            f"📄 Файл: {new_link['file']}\n"
            f"🔢 Использований: {new_link['max_uses']}\n\n"
            f"Теперь ссылка доступна в «📋 Мои ссылки».",
            parse_mode="Markdown"
        )
        
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")


@dp.message(F.text == "🎫 Промокод")
async def on_promocode_button(message: Message, state: FSMContext):
    await message.answer(
        "🎫 *Введите промокод*\n\n"
        "Отправьте код, который вы получили.\n"
        "Пример: `SUMMER2026`",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_promocode)


@dp.message(AdminStates.waiting_promocode)
async def process_promocode(message: Message, state: FSMContext):
    await state.clear()
    
    code = message.text.strip().upper()
    telegram_id = message.from_user.id
    username = message.from_user.username
    
    # Проверяем промокод
    result = await db.use_promocode(code, telegram_id)
    
    if not result["success"]:
        await message.answer(f"❌ {result['error']}")
        return
    
    discount_percent = result.get("discount_percent", 0)
    discount_days = result.get("discount_days", 0)
    
    # ─── ЕСЛИ 100% СКИДКА ───
    if discount_percent >= 100:
        await message.answer(
            f"🎉 *ПОЗДРАВЛЯЮ!*\n\n"
            f"Вы активировали промокод на 100% скидку!\n"
            f"📅 Бонусных дней: +{discount_days}\n\n"
            f"⏳ Выдаю бесплатную ссылку..."
        )
        
        try:
            # Получаем пользователя
            user = await ensure_user(telegram_id, username)
            
            # Берём первый тариф из PRODUCTS для бесплатной ссылки
            product_id = list(PRODUCTS.keys())[0]
            product = PRODUCTS[product_id]
            
            # Создаём ссылку с бонусными днями
            expires_in = product["expires_in"] + (discount_days * 86400)
            result_link = await generate_link_with_recovery(
                telegram_id=telegram_id,
                username=username,
                file=product["file"],
                expires_in=expires_in,
                max_uses=product["max_uses"],
            )
            link = result_link["link"]
            
            # Сохраняем заказ как оплаченный
            import uuid
            unique_payment_id = f"free_{uuid.uuid4().hex[:8]}"
            await db.create_order(
                telegram_id,
                product_id,
                unique_payment_id,
                status="succeeded",
                link=link
            )
            
            # Очищаем промокод
            await db.clear_user_promocode(telegram_id)
            
            # Отправляем QR и ссылку
            qr_bytes = make_qr_bytes(link)
            caption = (
                f"✅ *Бесплатная ссылка готова!*\n\n"
                f"🔗 `{link}`\n"
                f"⏱ Действует: {expires_in // 3600} ч\n"
                f"🎁 Бонусных дней: +{discount_days}\n"
                f"🔢 Использований: {product['max_uses']}"
            ) + get_happ_instructions()
            
            await message.answer_photo(
                photo=BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=caption,
                parse_mode="Markdown"
            )
            
            return
            
        except Exception as e:
            logger.error(f"Ошибка при выдаче бесплатной ссылки: {e}")
            await message.answer(f"⚠️ Ошибка: {e}")
            return
    
    # ─── ЕСЛИ СКИДКА МЕНЬШЕ 100% ───
    await db.save_user_promocode(
        telegram_id,
        code,
        discount_percent,
        discount_days
    )
    
    text = f"✅ *Промокод применён!*\n\n"
    
    if discount_percent > 0:
        text += f"🎁 Скидка: {discount_percent}%\n"
    if discount_days > 0:
        text += f"📅 Бонусных дней: +{discount_days}\n"
    
    text += f"\n💳 Теперь вы можете купить подписку со скидкой!"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(AdminStates.waiting_claim_link)
async def process_claim_link(message: Message, state: FSMContext):
    """Обработчик ввода ссылки для привязки (привязывает ЛЮБУЮ ссылку)"""
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Сначала нажмите /start")
        await state.clear()
        return
    
    text = message.text.strip()
    
    # Извлекаем токен
    if text.startswith("https://") or text.startswith("http://"):
        parts = text.split("/")
        token = parts[-1]
        if not token or len(token) < 10:
            await message.answer(
                "❌ Неверный формат ссылки.\n\n"
                "Отправьте ссылку вида:\n"
                "https://kongor.mooo.com/a22dd605ad5bd430\n\n"
                "Или просто токен: a22dd605ad5bd430"
            )
            return
    else:
        token = text
    
    if len(token) < 10:
        await message.answer(
            "❌ Токен слишком короткий.\n\n"
            "Отправьте полную ссылку или токен (минимум 10 символов)."
        )
        return
    
    try:
        # Ищем ссылку по токену через API
        link_info = await api_client.get_link_by_token(token)
        
        if not link_info:
            await message.answer(
                f"❌ Ссылка с токеном `{token}` не найдена в базе данных."
            )
            await state.clear()
            return
        
        # ═══════════════════════════════════════════════════
        # ✅ ПРИВЯЗЫВАЕМ ССЫЛКУ В ЛЮБОМ СОСТОЯНИИ
        # ═══════════════════════════════════════════════════
        
        # Обновляем client_id у ссылки
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{API_BASE_URL}/links/{link_info['id']}/bind",
                json={"client_id": user["client_id"]},
                timeout=15
            ) as resp:
                data = await resp.json()
        
        if data.get("success"):
            # Получаем обновлённую информацию
            updated_link = await api_client.get_link(link_info['id'])
            
            status_text = "✅ Ссылка успешно привязана!"
            if updated_link.get('is_used'):
                status_text += "\n\n⚠️ Ссылка была отключена. Для активации нажмите «Сбросить» в веб-панели."
            if updated_link.get('file_name') == 'Disabled.txt':
                status_text += "\n\n⚠️ Файл заменён на Disabled.txt. Для восстановления нажмите «Сбросить»."
            
            await message.answer(
                f"{status_text}\n\n"
                f"🔗 {API_BASE_URL}/{updated_link['token']}\n"
                f"📌 Токен: {updated_link['token']}\n"
                f"📄 Файл: {updated_link.get('file_name', '—')}\n"
                f"🔢 Использований: {updated_link.get('uses', 0)}/{updated_link.get('max_uses', 1)}\n"
                f"⏱ Действует до: {updated_link.get('expires_at', '—')[:16].replace('T', ' ')}\n\n"
                f"📋 Нажмите «Мои ссылки», чтобы увидеть обновлённый список.",
                reply_markup=main_menu_keyboard(message.from_user.id in config.ADMIN_IDS)
            )
        else:
            await message.answer(
                f"❌ Ошибка привязки: {data.get('error', 'Неизвестная ошибка')}"
            )
        
    except api_client.ApiError as e:
        error_msg = str(e)
        if "not found" in error_msg.lower() or "404" in error_msg:
            await message.answer(
                f"❌ Ссылка с токеном `{token}` не найдена.\n\n"
                "Проверьте правильность токена."
            )
        else:
            await message.answer(
                f"⚠️ Ошибка: {e}\n\n"
                "Попробуйте ещё раз."
            )
    except Exception as e:
        logger.error(f"Ошибка при привязке ссылки: {e}")
        await message.answer(
            f"⚠️ Произошла ошибка: {str(e)}\n\n"
            "Попробуйте ещё раз."
        )
    
    await state.clear()


@dp.message(Command("unbind_link"))
async def cmd_unbind_link(message: Message):
    """Отвязать ссылку от аккаунта"""
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ Использование: `/unbind_link TOKEN`\n"
            "Пример: `/unbind_link 1591ff4b97e7be28`",
            parse_mode="Markdown"
        )
        return
    
    token = args[1].strip()
    
    try:
        link_info = await api_client.get_link_by_token(token)
        if not link_info:
            await message.answer(f"❌ Ссылка с токеном `{token}` не найдена.", parse_mode="Markdown")
            return
        
        # Отвязываем
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{API_BASE_URL}/links/{link_info['id']}/unbind",
                json={"unbind": True},
                timeout=15
            ) as resp:
                data = await resp.json()
        
        if data.get("success"):
            await message.answer(
                f"✅ *Ссылка `{token}` отвязана!*\n\n"
                f"Теперь её можно привязать к другому аккаунту.",
                parse_mode="Markdown"
            )
        else:
            await message.answer(f"❌ Ошибка: {data.get('error', 'Неизвестная ошибка')}")
            
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(Command("create_promo"))
async def cmd_create_promo(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ *Использование:*\n"
            "`/create_promo КОД СКИДКА% ДНИ МАКС_ИСПОЛЬЗОВАНИЙ`\n\n"
            "Примеры:\n"
            "• `/create_promo SUMMER2026 10 0 50` — скидка 10%, без бонусных дней, 50 использований\n"
            "• `/create_promo BONUS 0 3 10` — +3 дня, без скидки, 10 использований\n"
            "• `/create_promo VIP 20 7 100` — скидка 20% + 7 дней, 100 использований",
            parse_mode="Markdown"
        )
        return
    
    code = args[1].upper()
    discount_percent = int(args[2]) if len(args) > 2 else 0
    discount_days = int(args[3]) if len(args) > 3 else 0
    max_uses = int(args[4]) if len(args) > 4 else 1
    
    if len(code) < 3:
        await message.answer("❌ Код должен содержать минимум 3 символа.")
        return
    
    success = await db.create_promocode(code, discount_percent, discount_days, max_uses, created_by=message.from_user.id)
    
    if success:
        await message.answer(
            f"✅ *Промокод создан!*\n\n"
            f"📌 Код: `{code}`\n"
            f"🎁 Скидка: {discount_percent}%\n"
            f"📅 Бонусных дней: {discount_days}\n"
            f"🔢 Использований: {max_uses}\n\n"
            f"📋 Скопируйте код и отправьте его клиентам!",
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Такой промокод уже существует")


@dp.message(Command("list_promo"))
async def cmd_list_promo(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    promos = await db.list_promocodes()
    
    if not promos:
        await message.answer("📭 Нет созданных промокодов.")
        return
    
    text = "📋 *Список промокодов:*\n\n"
    for p in promos:
        text += (
            f"🔹 `{p['code']}`\n"
            f"   Скидка: {p['discount_percent']}% | Дней: {p['discount_days']}\n"
            f"   Использовано: {p['used_count']}/{p['max_uses']}\n"
            f"   Статус: {'✅ Активен' if p['is_active'] else '❌ Неактивен'}\n\n"
        )
    
    await message.answer(text, parse_mode="Markdown")



@dp.message(Command("delete_all_promo"))
async def cmd_delete_all_promo(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="admin:delete_all_promo_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:delete_all_promo_cancel")],
    ])
    
    await message.answer(
        "⚠️ *Вы уверены, что хотите удалить ВСЕ промокоды?*\n\n"
        "Это действие НЕЛЬЗЯ отменить.\n"
        "Будут удалены все промокоды и история их использований.",
        parse_mode="Markdown",
        reply_markup=kb
    )


@dp.callback_query(F.data == "admin:delete_all_promo_confirm")
async def admin_delete_all_promo_confirm(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    count = await db.delete_all_promocodes()
    
    await callback.message.edit_text(
        f"✅ *Удалено {count} промокодов!*\n\n"
        f"Все промокоды и история их использований удалены.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:delete_all_promo_cancel")
async def admin_delete_all_promo_cancel(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "❌ Удаление отменено.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(F.text == "❓ Помощь")
async def support_start(message: Message, state: FSMContext):
    await state.set_state(SupportStates.waiting_message)
    
    await message.answer(
        "👋 <b>Добро пожаловать в VpnGreatBot!</b>\n\n"
        "ℹ️ <b>Как это работает:</b>\n\n"
        "🎁 <b>Пробный период</b> — бесплатная VPN-ссылка на 2 дня (доступна один раз).\n"
        "💳 <b>Купить</b> — выберите тариф.\n"
        "🤝 <b>Пригласи друга</b> — +5 дней после первой оплаты друга.\n"
        "📋 <b>Мои ссылки</b> — просмотр ваших VPN-ссылок.\n"
        "🔗 <b>Привязать ссылку</b> — добавьте уже имеющуюся ссылку.\n\n"
        "━━━━━━━━━━━━━━\n"
        "💬 <b>Напишите сообщение администратору.</b>\n"
        "Следующее сообщение будет отправлено в поддержку.\n\n"
        "Для отмены используйте /cancel",
        parse_mode="HTML"
    )


@dp.message(SupportStates.waiting_message)
async def support_message(message: Message, state: FSMContext):
    global support_messages
    
    # Отправляем всем админам
    for admin_id in config.ADMIN_IDS:
        # Создаём клавиатуру для ответа
        reply_keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✏️ Ответить",
                        callback_data=f"reply_to_{message.from_user.id}"
                    )
                ]
            ]
        )
        
        sent = await bot.send_message(
            admin_id,
            f"📩 <b>Новое обращение</b>\n\n"
            f"👤 {message.from_user.full_name}\n"
            f"🆔 <code>{message.from_user.id}</code>\n"
            f"📛 @{message.from_user.username or 'нет'}\n\n"
            f"💬 {message.text}",
            parse_mode="HTML",
            reply_markup=reply_keyboard
        )
        
        # Запоминаем: сообщение админу -> id клиента
        support_messages[(admin_id, sent.message_id)] = (message.from_user.id, message.message_id)
        
        # Сохраняем в файл
        save_support_data()
    
    await state.clear()
    await message.answer(
        "✅ Сообщение отправлено администратору.\n"
        "Ожидайте ответа."
    )

@dp.callback_query(F.data.startswith("reply_to_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    """Начало ответа клиенту через кнопку"""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("⛔ У вас нет доступа.", show_alert=True)
        return
    
    user_id = int(callback.data.split("_")[2])
    
    # Сохраняем ID клиента в состояние
    await state.update_data(reply_user_id=user_id)
    await state.set_state(AdminStates.waiting_support_message)
    
    await callback.message.answer(
        f"✏️ <b>Ответ клиенту</b>\n\n"
        f"Отправьте сообщение, которое будет доставлено пользователю.\n"
        f"Для отмены используйте /cancel",
        parse_mode="HTML"
    )
    
    await callback.answer()

@dp.message(AdminStates.waiting_support_message)
async def admin_reply_send(message: Message, state: FSMContext):
    """Отправка ответа клиенту"""
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        await state.clear()
        return
    
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    
    if not user_id:
        await message.answer("❌ Ошибка: клиент не найден.")
        await state.clear()
        return
    
    try:
        # Отправляем ответ клиенту
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ администратора:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        
        await message.answer(
            f"✅ Ответ отправлен клиенту (ID: {user_id})"
        )
        
        # Логируем
        logger.info(f"Админ {message.from_user.id} ответил клиенту {user_id}")
        
    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке: {e}")
    
    await state.clear()


@dp.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    
    if current_state == SupportStates.waiting_message:
        await message.answer(
            "❌ Отправка сообщения отменена.\n"
            "Вы можете вернуться позже через кнопку «❓ Помощь»."
        )
    elif current_state == AdminStates.waiting_support_message:
        await message.answer(
            "❌ Отправка ответа отменена."
        )
    else:
        await message.answer(
            "❌ Нет активных действий для отмены."
        )
    
    await state.clear()


@dp.message(Command("reset_user"))
async def cmd_reset_user(message: Message):
    """Сбросить пользователя (пересоздать клиента)"""
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "❌ Использование: `/reset_user TELEGRAM_ID`\n"
            "Пример: `/reset_user 123456789`",
            parse_mode="Markdown"
        )
        return
    
    try:
        telegram_id = int(args[1])
    except ValueError:
        await message.answer("❌ Неверный ID. Используйте число.")
        return
    
    user = await db.get_user(telegram_id)
    if not user:
        await message.answer(f"❌ Пользователь {telegram_id} не найден в БД бота.")
        return
    
    try:
        # Пробуем получить клиента
        client = await api_client.get_client(user["client_id"])
        if client:
            await message.answer(f"✅ Клиент существует (ID: {user['client_id']}).\n"
                               f"Если проблемы — удалите запись вручную или пересоздайте.")
            return
    except api_client.ApiError as e:
        if "не найден" in str(e) or "404" in str(e):
            # Клиент удалён — пересоздаём
            name = f"tg_{telegram_id}_{user['username'] or 'nouser'}"
            created = await api_client.create_client(name)
            await db.update_user_client(telegram_id, created["client_id"], created["api_key"], reset_trial=True)
            await message.answer(
                f"✅ Пользователь {telegram_id} восстановлен!\n"
                f"🆕 Новый client_id: {created['client_id']}\n"
                f"🔑 API ключ: {created['api_key'][:16]}...\n"
                f"🎁 Пробный период сброшен (можно заново)."
            )
        else:
            await message.answer(f"⚠️ Ошибка: {e}")

@dp.message(F.reply_to_message)
async def admin_reply_by_reply(message: Message):
    """Ответ администратора через reply (альтернативный способ)"""
    global support_messages
    
    # Проверяем, что админ
    if message.from_user.id not in config.ADMIN_IDS:
        return
    
    # Проверяем, есть ли в кэше
    key = (message.chat.id, message.reply_to_message.message_id)
    
    if key not in support_messages:
        await message.reply(
            "⚠️ Это сообщение не из системы поддержки.\n"
            "Используйте кнопку «✏️ Ответить» под сообщением клиента."
        )
        return
    
    user_id, user_msg_id = support_messages[key]
    
    try:
        await bot.send_message(
            user_id,
            f"💬 <b>Ответ администратора:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        
        await message.reply(
            f"✅ Ответ отправлен клиенту (ID: {user_id})"
        )
        
        logger.info(f"Админ {message.from_user.id} ответил клиенту {user_id} через reply")
        
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

@dp.callback_query(F.data.startswith("buy:"))
async def on_buy(callback: CallbackQuery):
    product_id = callback.data.split(":", 1)[1]
    product = PRODUCTS.get(product_id)
    if not product:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    
    user = await ensure_user(callback.from_user.id, callback.from_user.username)
    
    original_price = product["price_rub"]
    
    # Получаем количество активных ссылок
    try:
        active_links_count = await api_client.get_active_links_count(user["client_id"])
    except Exception as e:
        logger.error(f"Ошибка получения активных ссылок: {e}")
        active_links_count = 0
    
    # Рассчитываем цену со скидкой
    if active_links_count > 0:
        discount_percent = DISCOUNT_INFO['discount_percent']
        final_price = int(original_price * (100 - discount_percent) / 100)
        discount_text = f"🔥 Скидка {discount_percent}% — экономия {original_price - final_price}₽!"
    else:
        final_price = original_price
        discount_text = "🆕 Первая ссылка — полная цена"
    
    promo_text = ""
    user_promo = await db.get_user_promocode(callback.from_user.id)
    
    if user_promo:
        promo_discount = user_promo["discount_percent"]
        discount_days = user_promo["discount_days"]
        
        if promo_discount >= 100:
            # ... бесплатная подписка ...
            return
        elif promo_discount > 0:
            final_price = int(final_price * (100 - promo_discount) / 100)
            promo_text = f"🎫 + промокод: скидка {promo_discount}%"
    
    # Создаём платёж
    payment = payments.create_payment(
        amount_rub=final_price,
        description=f"{product['title']} - {discount_text}",
        metadata={
            "telegram_id": callback.from_user.id,
            "product_id": product_id,
            "discount_percent": discount_percent if active_links_count > 0 else 0,
            "discount_days": discount_days if user_promo else 0,
            "original_price": original_price,
            "final_price": final_price,
            "active_links_count": active_links_count
        },
    )
    
    await db.create_order(callback.from_user.id, product_id, payment["payment_id"])
    
    # Формируем красивое сообщение о заказе
    text = (
        "━━━━━━━━━━━━━━━━━━━\n"
        "    🛒 *ВАШ ЗАКАЗ* 🛒\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 *Тариф:* {product['title']}\n"
        f"📅 {product['description']}\n\n"
        f"💰 *Цена:* {final_price}₽\n"
    )
    
    if active_links_count > 0:
        text += f"🔥 *Скидка:* {DISCOUNT_INFO['discount_percent']}%\n"
        text += f"💎 *Экономия:* {original_price - final_price}₽\n"
    
    if promo_text:
        text += f"{promo_text}\n"
    
    text += f"\n📊 *Активных ссылок:* {active_links_count}\n"
    text += "━━━━━━━━━━━━━━━━━━━\n"
    text += "👇 Нажмите кнопку для оплаты"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Оплатить", url=payment["confirmation_url"])
    ]])
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    
    if user_promo:
        await db.clear_user_promocode(callback.from_user.id)
    
    await callback.answer()


# ─────────────────────── АДМИН-ПАНЕЛЬ ───────────────────────

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список клиентов", callback_data="admin:list_clients")],
        [InlineKeyboardButton(text="♻️ Восстановить клиента", callback_data="admin:restore_client_menu")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🔄 Сбросить статистику", callback_data="admin:reset_stats")],
        [InlineKeyboardButton(text="🎫 Управление промокодами", callback_data="admin:promocodes")],
    ])


def client_actions_keyboard(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Информация и ссылки", callback_data=f"admin:client_info:{client_id}")],
        [InlineKeyboardButton(text="📁 Сменить файл", callback_data=f"admin:change_file:{client_id}")],
        [InlineKeyboardButton(text="🔄 Сбросить триал", callback_data=f"admin:reset_trial:{client_id}")],
        [InlineKeyboardButton(text="⛔ Отключить подписку", callback_data=f"admin:disable_sub:{client_id}")],
        [InlineKeyboardButton(text="🗑 Удалить все ссылки", callback_data=f"admin:delete_all_links:{client_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back_to_clients")],
    ])


@dp.message(F.text == "⚙️ Админка")
async def on_admin_button(message: Message):
    if message.from_user.id not in config.ADMIN_IDS:
        return
    await message.answer("⚙️ Админ-панель:", reply_markup=admin_panel_keyboard())


@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    s = await db.stats()
    start_stats = await db.get_start_stats()
    unique_users = await db.get_unique_users_count()
    
    text = (
        f"📊 *Статистика бота*\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"💰 Оплаченных заказов: {s['paid_orders']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Активность бота*\n"
        f"🚀 Нажатий /start: {start_stats.get('total_starts', 0) if start_stats else 0}\n"
        f"👤 Уникальных посетителей: {unique_users}\n"
        f"🕐 Обновлено: {start_stats.get('last_updated', '—')[:16].replace('T', ' ') if start_stats and start_stats.get('last_updated') else '—'}"
    )
    
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin:restore_client_menu")
async def admin_restore_client_menu(callback: CallbackQuery):
    """Меню восстановления клиента"""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    try:
        # Получаем удалённых клиентов
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_BASE_URL}/clients/deleted", timeout=15) as resp:
                if resp.status == 200:
                    deleted_clients = await resp.json()
                else:
                    deleted_clients = []
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка получения удалённых клиентов: {e}")
        return
    
    if not deleted_clients:
        await callback.message.edit_text(
            "📭 *Нет удалённых клиентов*\n\n"
            "Все клиенты активны.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back_to_menu")]
            ])
        )
        await callback.answer()
        return
    
    keyboard = []
    for client in deleted_clients[:20]:
        name = client.get('name', 'Без имени')
        if name.startswith("tg_"):
            parts = name.split("_")
            if len(parts) >= 3:
                name = f"{parts[2]}_{parts[1]}"
            else:
                name = name.replace("tg_", "")
        if len(name) > 25:
            name = name[:22] + "..."
        
        deleted_at = client.get('deleted_at', '')
        if deleted_at:
            try:
                deleted_date = datetime.fromisoformat(deleted_at.replace('Z', '+00:00'))
                deleted_str = deleted_date.strftime('%d.%m %H:%M')
            except:
                deleted_str = 'недавно'
        else:
            deleted_str = 'недавно'
        
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 {client['id']} · {name} (удалён {deleted_str})",
                callback_data=f"admin:restore_client:{client['id']}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back_to_menu")
    ])
    
    await callback.message.edit_text(
        "♻️ *Восстановление удалённых клиентов*\n\n"
        "Выберите клиента для восстановления.\n"
        "Все его ссылки также будут восстановлены.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@dp.callback_query(F.data == "open_menu")
async def open_menu(callback: CallbackQuery):
    await callback.answer()
    is_admin = callback.from_user.id in config.ADMIN_IDS
    await callback.message.answer(
        "📋 Главное меню:",
        reply_markup=main_menu_keyboard(is_admin)
    )

@dp.callback_query(F.data.startswith("admin:restore_client:"))
async def admin_restore_client(callback: CallbackQuery):
    """Восстановление конкретного клиента"""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    client_id = int(callback.data.split(":")[2])
    
    await callback.answer("⏳ Восстанавливаю...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_BASE_URL}/clients/restore/{client_id}",
                timeout=15
            ) as resp:
                data = await resp.json()
        
        if data.get("success"):
            await callback.message.edit_text(
                f"✅ *Клиент ID {client_id} восстановлен!*\n\n"
                f"{data.get('message', '')}\n\n"
                f"🔄 Все ссылки клиента снова активны.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="♻️ Восстановить ещё", callback_data="admin:restore_client_menu")],
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back_to_menu")]
                ])
            )
        else:
            await callback.message.edit_text(
                f"❌ *Ошибка восстановления*\n\n{data.get('error', 'Неизвестная ошибка')}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:restore_client_menu")]
                ])
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Ошибка:* {e}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:restore_client_menu")]
            ])
        )


@dp.callback_query(F.data == "admin:promocodes")
async def admin_promocodes(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод (с выбором)", callback_data="admin:promo_create")],
        [InlineKeyboardButton(text="✏️ Создать свой промокод", callback_data="admin:promo_custom")],
        [InlineKeyboardButton(text="📋 Список промокодов", callback_data="admin:promo_list")],
        [InlineKeyboardButton(text="🗑️ Удалить все промокоды", callback_data="admin:delete_all_promo_confirm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back_to_menu")],
    ])
    
    await callback.message.edit_text(
        "🎫 *Управление промокодами*\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔹 Без скидки (только дни)", callback_data="admin:promo_set:0")],
        [InlineKeyboardButton(text="🔸 5% скидка", callback_data="admin:promo_set:5")],
        [InlineKeyboardButton(text="🔶 10% скидка", callback_data="admin:promo_set:10")],
        [InlineKeyboardButton(text="🔷 15% скидка", callback_data="admin:promo_set:15")],
        [InlineKeyboardButton(text="💎 20% скидка", callback_data="admin:promo_set:20")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:promocodes")],
    ])
    
    await callback.message.edit_text(
        "🎫 *Создание промокода*\n\n"
        "Шаг 1: Выберите размер скидки:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:promo_set:"))
async def admin_promo_set_discount(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    discount = int(callback.data.split(":")[2])
    await state.update_data(promo_discount=discount)
    await state.update_data(promo_days=0)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 0 дней (только скидка)", callback_data="admin:promo_days:0")],
        [InlineKeyboardButton(text="📅 +3 дня", callback_data="admin:promo_days:3")],
        [InlineKeyboardButton(text="📅 +5 дней", callback_data="admin:promo_days:5")],
        [InlineKeyboardButton(text="📅 +7 дней", callback_data="admin:promo_days:7")],
        [InlineKeyboardButton(text="📅 +10 дней", callback_data="admin:promo_days:10")],
        [InlineKeyboardButton(text="📅 +14 дней", callback_data="admin:promo_days:14")],
        [InlineKeyboardButton(text="📅 +30 дней", callback_data="admin:promo_days:30")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:promo_create")],
    ])
    
    await callback.message.edit_text(
        f"🎫 *Создание промокода*\n\n"
        f"✅ Скидка: {discount}%\n\n"
        f"Шаг 2: Выберите количество бонусных дней:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:promo_days:"))
async def admin_promo_set_days(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    days = int(callback.data.split(":")[2])
    await state.update_data(promo_days=days)
    
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 1 использование", callback_data="admin:promo_uses:1")],
        [InlineKeyboardButton(text="🔢 5 использований", callback_data="admin:promo_uses:5")],
        [InlineKeyboardButton(text="🔢 10 использований", callback_data="admin:promo_uses:10")],
        [InlineKeyboardButton(text="🔢 50 использований", callback_data="admin:promo_uses:50")],
        [InlineKeyboardButton(text="🔢 100 использований", callback_data="admin:promo_uses:100")],
        [InlineKeyboardButton(text="🔢 Безлимит (999)", callback_data="admin:promo_uses:999")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:promo_set:" + str(discount))],
    ])
    
    await callback.message.edit_text(
        f"🎫 *Создание промокода*\n\n"
        f"✅ Скидка: {discount}%\n"
        f"✅ Бонусных дней: {days}\n\n"
        f"Шаг 3: Выберите максимальное количество использований:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:promo_uses:"))
async def admin_promo_create_final(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    max_uses = int(callback.data.split(":")[2])
    
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    days = data.get("promo_days", 0)
    
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    
    success = await db.create_promocode(
        code=code,
        discount_percent=discount,
        discount_days=days,
        max_uses=max_uses,
        created_by=callback.from_user.id
    )
    
    if success:
        await callback.message.edit_text(
            f"✅ *Промокод создан!*\n\n"
            f"📌 Код: `{code}`\n"
            f"🎁 Скидка: {discount}%\n"
            f"📅 Бонусных дней: {days}\n"
            f"🔢 Использований: {max_uses}\n\n"
            f"📋 Скопируйте код и отправьте его клиентам!",
            parse_mode="Markdown"
        )
    else:
        await callback.message.edit_text(
            "❌ *Ошибка создания промокода*\n\n"
            "Попробуйте ещё раз.",
            parse_mode="Markdown"
        )
    
    await state.clear()
    await callback.answer()


@dp.callback_query(F.data == "admin:promo_custom")
async def admin_promo_custom(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "✏️ *Создание своего промокода*\n\n"
        "Введите данные в формате:\n"
        "`КОД СКИДКА% ДНИ МАКС_ИСПОЛЬЗОВАНИЙ`\n\n"
        "Примеры:\n"
        "• `SUMMER2026 10 0 50` — скидка 10%, без бонусных дней, 50 использований\n"
        "• `BONUS 0 3 10` — +3 дня, без скидки, 10 использований\n"
        "• `VIP 20 7 100` — скидка 20% + 7 дней, 100 использований\n\n"
        "Отправьте команду с параметрами:",
        parse_mode="Markdown"
    )
    await state.set_state(AdminStates.waiting_custom_promo)


@dp.message(AdminStates.waiting_custom_promo)
async def process_custom_promo(message: Message, state: FSMContext):
    await state.clear()
    
    if message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ У вас нет доступа.")
        return
    
    args = message.text.strip().split()
    
    if len(args) < 4:
        await message.answer(
            "❌ *Неверный формат*\n\n"
            "Нужно указать 4 параметра:\n"
            "`КОД СКИДКА% ДНИ МАКС_ИСПОЛЬЗОВАНИЙ`\n\n"
            "Пример: `SUMMER2026 10 7 50`",
            parse_mode="Markdown"
        )
        return
    
    code = args[0].upper()
    
    try:
        discount_percent = int(args[1])
        discount_days = int(args[2])
        max_uses = int(args[3])
    except ValueError:
        await message.answer("❌ Скидка, дни и количество использований должны быть числами.")
        return
    
    if len(code) < 3:
        await message.answer("❌ Код должен содержать минимум 3 символа.")
        return
    
    if discount_percent < 0 or discount_percent > 100:
        await message.answer("❌ Скидка должна быть от 0 до 100.")
        return
    
    if discount_days < 0:
        await message.answer("❌ Количество дней не может быть отрицательным.")
        return
    
    if max_uses < 1:
        await message.answer("❌ Количество использований должно быть не менее 1.")
        return
    
    success = await db.create_promocode(
        code=code,
        discount_percent=discount_percent,
        discount_days=discount_days,
        max_uses=max_uses,
        created_by=message.from_user.id
    )
    
    if success:
        await message.answer(
            f"✅ *Промокод создан!*\n\n"
            f"📌 Код: `{code}`\n"
            f"🎁 Скидка: {discount_percent}%\n"
            f"📅 Бонусных дней: {discount_days}\n"
            f"🔢 Использований: {max_uses}\n\n"
            f"📋 Скопируйте код и отправьте его клиентам!",
            parse_mode="Markdown"
        )
    else:
        await message.answer("❌ Такой промокод уже существует")


@dp.callback_query(F.data.startswith("qr_"))
async def show_qr(callback: CallbackQuery):
    token = callback.data.split("_", 1)[1]
    full_url = f"https://kongor.mooo.com/{token}"
    
    await callback.answer()
    
    try:
        qr_bytes = make_qr_bytes(full_url)
        await callback.message.answer_photo(
            photo=BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=f"📱 *QR-код для ссылки:*\n`{full_url}`\n\nОтсканируйте камерой телефона.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка генерации QR-кода: {e}")
        await callback.message.answer("⚠️ Не удалось сгенерировать QR-код.")


# ═══════════════════════════════════════════════════
# УПРАВЛЕНИЕ ПРОМОКОДАМИ (ПОЛНОСТЬЮ ИСПРАВЛЕННОЕ)
# ═══════════════════════════════════════════════════

@dp.callback_query(F.data == "admin:promo_list")
async def admin_promo_list(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    promos = await db.list_promocodes()
    
    if not promos:
        await callback.message.edit_text(
            "📭 *Нет созданных промокодов*\n\n"
            "Нажмите «Создать промокод», чтобы добавить новый.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo_create")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:promocodes")],
            ])
        )
        await callback.answer()
        return
    
    text = "📋 *Список промокодов:*\n\n"
    keyboard = []
    
    for p in promos:
        status = "✅ Активен" if p['is_active'] else "❌ Неактивен"
        text += (
            f"🔹 `{p['code']}`\n"
            f"   💰 Скидка: {p['discount_percent']}%\n"
            f"   📅 Дней: {p['discount_days']}\n"
            f"   🔢 {p['used_count']}/{p['max_uses']}\n"
            f"   📊 {status}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(
                text=f"🗑 Удалить {p['code']}",
                callback_data=f"admin:delete_promo:{p['id']}:{p['code']}"
            )
        ])
    
    keyboard.append([
        InlineKeyboardButton(text="➕ Создать промокод", callback_data="admin:promo_create"),
        InlineKeyboardButton(text="🗑️ Удалить все", callback_data="admin:delete_all_promo_confirm")
    ])
    keyboard.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="admin:promocodes")
    ])
    
    await callback.message.edit_text(
        text, 
        parse_mode="Markdown", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:delete_promo:"))
async def admin_delete_promo(callback: CallbackQuery):
    """Запрос подтверждения удаления"""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    parts = callback.data.split(":")
    promo_id = int(parts[2])
    promo_code = parts[3] if len(parts) > 3 else "неизвестный"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:delete_promo_confirm:{promo_id}:{promo_code}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:delete_promo_cancel")]
    ])
    
    await callback.message.edit_text(
        f"🗑 *Удалить промокод* `{promo_code}`?\n\n"
        f"Это действие НЕЛЬЗЯ отменить!\n"
        f"Будут удалены все записи об использовании этого промокода.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:delete_promo_confirm:"))
async def admin_delete_promo_confirm(callback: CallbackQuery):
    """Подтверждение удаления промокода"""
    logger.info(f"=== УДАЛЕНИЕ ПРОМОКОДА ===")
    logger.info(f"Callback data: {callback.data}")
    logger.info(f"User ID: {callback.from_user.id}")
    
    if callback.from_user.id not in config.ADMIN_IDS:
        logger.warning(f"Доступ запрещён для {callback.from_user.id}")
        await callback.answer()
        return
    
    parts = callback.data.split(":")
    logger.info(f"Parts: {parts}")
    
    promo_id = int(parts[2])
    promo_code = parts[3] if len(parts) > 3 else "неизвестный"
    
    logger.info(f"Удаляем промокод: ID={promo_id}, CODE={promo_code}")
    
    await callback.answer("⏳ Удаляю...")
    
    try:
        import aiosqlite
        from config import DB_PATH
        
        async with aiosqlite.connect(DB_PATH) as db:
            # Удаляем записи об использовании
            await db.execute("DELETE FROM promocode_uses WHERE promocode_id = ?", (promo_id,))
            # Удаляем сам промокод
            cursor = await db.execute("DELETE FROM promocodes WHERE id = ?", (promo_id,))
            await db.commit()
            deleted_count = cursor.rowcount
        
        logger.info(f"Удалено записей: {deleted_count}")
        
        if deleted_count > 0:
            await callback.message.edit_text(
                f"✅ *Промокод `{promo_code}` удалён!*",
                parse_mode="Markdown"
            )
            await asyncio.sleep(1)
            await admin_promo_list(callback)
        else:
            await callback.message.edit_text(
                f"❌ *Промокод `{promo_code}` не найден*",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"Ошибка удаления промокода {promo_id}: {e}")
        await callback.message.edit_text(
            f"❌ *Ошибка:* {str(e)}",
            parse_mode="Markdown"
        )


@dp.callback_query(F.data == "admin:delete_promo_cancel")
async def admin_delete_promo_cancel(callback: CallbackQuery):
    """Отмена удаления промокода"""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    await callback.answer("❌ Удаление отменено")
    await admin_promo_list(callback)

@dp.callback_query(F.data == "admin:back_to_menu")
async def admin_back_to_menu(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "⚙️ Админ-панель:",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:reset_stats")
async def admin_reset_stats(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сбросить", callback_data="admin:reset_stats_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:reset_stats_cancel")],
    ])
    
    await callback.message.edit_text(
        "⚠️ *Вы уверены, что хотите сбросить статистику?*\n\n"
        "Это действие НЕЛЬЗЯ отменить.\n"
        "Будут сброшены:\n"
        "• Количество нажатий /start\n"
        "• Количество уникальных пользователей",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:reset_stats_confirm")
async def admin_reset_stats_confirm(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    try:
        await db.reset_start_stats()
        
        await callback.message.edit_text(
            "✅ *Статистика успешно сброшена!*\n\n"
            "Все счётчики обнулены.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ *Ошибка при сбросе статистики:*\n`{e}`",
            parse_mode="Markdown"
        )
    await callback.answer()


@dp.callback_query(F.data == "admin:reset_stats_cancel")
async def admin_reset_stats_cancel(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    await callback.message.edit_text(
        "⚙️ Админ-панель:",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()


@dp.callback_query(F.data == "admin:list_clients")
async def admin_list_clients(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    await callback.answer()
    
    try:
        clients = await api_client.list_clients()
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка получения списка: {e}")
        return

    if not clients:
        await callback.message.answer("📭 Клиентов пока нет.")
        return

    lines = ["👥 *Список клиентов:*\n"]
    for c in clients:
        name = c['name']
        if name.startswith("tg_"):
            parts = name.split("_")
            if len(parts) >= 3:
                name = f"{parts[2]}_{parts[1]}"
            else:
                name = name.replace("tg_", "")
        if len(name) > 30:
            name = name[:27] + "..."
        status_icon = "🟢" if c['links_count'] > 0 else "🔵"
        lines.append(f"{status_icon} `{c['id']}` · {name}")
    lines.append(f"\n📌 *Всего клиентов:* {len(clients)}")
    
    keyboard = []
    for c in clients:
        name = c['name']
        if name.startswith("tg_"):
            parts = name.split("_")
            if len(parts) >= 3:
                name = f"{parts[2]}_{parts[1]}"
            else:
                name = name.replace("tg_", "")
        if len(name) > 25:
            name = name[:22] + "..."
        status_icon = "🟢" if c['links_count'] > 0 else "🔵"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{status_icon} {c['id']} · {name}",
                callback_data=f"admin:client_actions:{c['id']}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(text="🔄 Обновить список", callback_data="admin:list_clients")
    ])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    new_text = "\n".join(lines)
    
    try:
        await callback.message.edit_text(new_text, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" in str(e):
            await callback.message.answer("👥 Список клиентов актуален.", reply_markup=reply_markup)
        else:
            raise

# В админ-панели добавь кнопку "Восстановить клиента"
@dp.callback_query(F.data.startswith("admin:restore_client:"))
async def admin_restore_client(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    
    telegram_id = int(callback.data.split(":")[2])
    user = await db.get_user(telegram_id)
    
    if not user:
        await callback.message.answer("❌ Пользователь не найден")
        return
    
    try:
        # Пробуем восстановить через API
        response = await api_client.restore_client(user["client_id"])
        if response.get("success"):
            await callback.message.answer(
                f"✅ Клиент {user['client_id']} восстановлен!\n"
                f"👤 Пользователь: @{user['username'] or 'нет'}"
            )
        else:
            # Если не получилось — пересоздаём
            name = f"tg_{telegram_id}_{user['username'] or 'nouser'}"
            created = await api_client.create_client(name)
            await db.update_user_client(telegram_id, created["client_id"], created["api_key"], reset_trial=True)
            await callback.message.answer(
                f"✅ Клиент пересоздан!\n"
                f"🆕 Новый client_id: {created['client_id']}"
            )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")


@dp.callback_query(F.data.startswith("admin:client_actions:"))
async def admin_client_actions(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await callback.message.answer(
        f"👤 *Действия с клиентом* `{client_id}`",
        reply_markup=client_actions_keyboard(client_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:client_info:"))
async def admin_client_info(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await callback.answer()

    try:
        client = await api_client.get_client(client_id)
        links = await api_client.get_client_links(client_id)
    except api_client.ApiError as e:
        await callback.message.answer(f"⚠️ {e}")
        return

    lines = [
        f"👤 *Клиент:* {client['name']} (ID: {client['id']})",
        f"📅 *Создан:* {client['created_at'][:16].replace('T', ' ')}",
        f"🔗 *Всего ссылок:* {len(links)}\n",
    ]
    
    if links:
        lines.append("📋 *Список ссылок:*\n")
        for i, link in enumerate(links[:20], 1):
            status_icon = "🟢" if not link.get("is_used") else "🔴"
            status_text = "Активна" if not link.get("is_used") else "Использована"
            expires = format_remaining(link.get("expires_at"))
            current_file = link.get("file_name", "—")
            file_icon = "📄" if current_file != "Disabled.txt" else "🚫"
            lines.append(
                f"{i}. `{link['token']}`\n"
                f"   {file_icon} *Файл:* `{current_file}`\n"
                f"   📊 {link['uses']}/{link['max_uses']} | ⏱ {expires}\n"
                f"   {status_icon} {status_text}\n"
            )
        if len(links) > 20:
            lines.append(f"\n...и ещё {len(links) - 20} ссылок.")
    else:
        lines.append("📭 У клиента нет ссылок.")

    keyboard = [
        [InlineKeyboardButton(text="📁 Сменить файл для всех ссылок", callback_data=f"admin:change_file:{client_id}")]
    ]
    if links:
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить все ссылки", callback_data=f"admin:delete_all_links:{client_id}")])
        keyboard.append([InlineKeyboardButton(text="⛔ Отключить все ссылки", callback_data=f"admin:disable_sub:{client_id}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к клиенту", callback_data=f"admin:client_actions:{client_id}")])
    
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await callback.message.answer("\n".join(lines), reply_markup=reply_markup)


@dp.callback_query(F.data.startswith("admin:change_file:"))
async def admin_change_file_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await state.update_data(client_id=client_id)
    await state.set_state(AdminStates.waiting_change_file_client_id)
    try:
        links = await api_client.get_client_links(client_id)
        current_file = "—"
        if links:
            for link in links:
                if not link.get("is_used"):
                    current_file = link.get("file_name", "—")
                    break
            if current_file == "—" and links:
                current_file = links[0].get("file_name", "—")
        files = await api_client.get_files()
        if not files:
            await callback.message.answer("❌ Нет доступных файлов в папке.")
            await state.clear()
            return
        keyboard = []
        for f in files[:20]:
            is_current = f['name'] == current_file
            button_text = f"✅ {f['name']}" if is_current else f"📄 {f['name']}"
            keyboard.append([
                InlineKeyboardButton(text=button_text, callback_data=f"admin:select_file:{f['name']}")
            ])
        keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin:cancel_change_file")])
        await callback.message.answer(
            f"📁 *Выберите файл для клиента ID {client_id}*\n\n"
            f"📌 *Текущий файл:* `{current_file}`\n"
            f"Файлы из папки `/var/www/Vless-Links/files/`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка получения списка файлов: {e}")
        await state.clear()
    await callback.answer()


@dp.callback_query(F.data == "admin:cancel_change_file")
async def admin_cancel_change_file(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    await state.clear()
    await callback.message.answer("❌ Отменено.")
    await callback.answer()


@dp.callback_query(F.data.startswith("admin:select_file:"))
async def admin_select_file(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    file_name = callback.data.split(":", 2)[2]
    data = await state.get_data()
    client_id = data.get("client_id")
    if not client_id:
        await callback.message.answer("❌ Ошибка: клиент не найден.")
        await state.clear()
        await callback.answer()
        return
    await callback.answer(f"⏳ Меняю файл на {file_name}...")
    try:
        links = await api_client.get_client_links(client_id)
        if not links:
            await callback.message.answer(f"📭 У клиента {client_id} нет ссылок для изменения.")
            await state.clear()
            return
        updated = 0
        for link in links:
            if not link.get("is_used"):
                await api_client.update_link(link["id"], file=file_name)
                updated += 1
        await callback.message.answer(
            f"✅ *Файл изменён!*\n\n"
            f"👤 Клиент ID: {client_id}\n"
            f"📄 Новый файл: `{file_name}`\n"
            f"🔗 Обновлено ссылок: {updated}\n\n"
            f"Теперь все активные ссылки клиента ведут на `{file_name}`."
        )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")
    await state.clear()


@dp.callback_query(F.data.startswith("admin:delete_all_links:"))
async def admin_delete_all_links(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await callback.answer("⏳ Удаляю все ссылки...")
    try:
        links = await api_client.get_client_links(client_id)
        deleted = 0
        for link in links:
            await api_client.delete_link(link["id"])
            deleted += 1
        await callback.message.answer(f"✅ Удалено {deleted} ссылок.")
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")


@dp.callback_query(F.data.startswith("admin:disable_sub:"))
async def admin_disable_subscription(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await callback.answer()
    try:
        links = await api_client.get_client_links(client_id)
        if not links:
            await callback.message.answer(f"📭 У клиента {client_id} нет активных ссылок.")
            return
        disabled_count = 0
        for link in links:
            if not link.get("is_used"):
                await api_client.update_link(link["id"], file="Disabled.txt")
                disabled_count += 1
        await callback.message.answer(
            f"✅ Отключено {disabled_count} ссылок для клиента {client_id}.\n"
            f"Файлы заменены на Disabled.txt."
        )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")


@dp.callback_query(F.data.startswith("admin:reset_trial:"))
async def admin_reset_trial(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    client_id = int(callback.data.split(":")[2])
    await callback.answer()
    user = await db.get_user_by_client_id(client_id)
    if not user:
        await callback.message.answer(f"❌ Клиент {client_id} не привязан к пользователю бота.")
        return
    await db.reset_trial(user["telegram_id"])
    await callback.message.answer(f"✅ Пробный период для клиента {client_id} сброшен.")


@dp.callback_query(F.data == "admin:back_to_clients")
async def admin_back_to_clients(callback: CallbackQuery):
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer()
        return
    await callback.answer()
    try:
        clients = await api_client.list_clients()
        if not clients:
            await callback.message.answer("📭 Клиентов пока нет.")
            return
        for c in clients:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🟢 Действия", callback_data=f"admin:client_actions:{c['id']}")]
            ])
            await callback.message.answer(
                f"🆔 {c['id']} · {c['name']} · ссылок: {c['links_count']}",
                reply_markup=kb
            )
    except Exception as e:
        await callback.message.answer(f"⚠️ Ошибка: {e}")


@dp.message(F.text == "🔄 Продлить ссылку")
async def on_extend_link(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Сначала нажмите /start")
        return
    
    try:
        links = await api_client.get_client_links(user["client_id"])
    except Exception as e:
        await message.answer(f"⚠️ Ошибка получения ссылок: {e}")
        return
    
    if not links:
        await message.answer(
            "📭 У вас нет ссылок.\n\n"
            "🎁 Нажмите «Пробный период» или 💳 «Купить» для получения."
        )
        return
    
    keyboard = []
    for link in links:
        status = "🔴 Использована" if link.get("is_used") else "🟢 Активна"
        expires = format_remaining(link.get("expires_at"))
        keyboard.append([
            InlineKeyboardButton(
                text=f"🔗 {link['token'][:8]}... — {status} ({expires})",
                callback_data=f"extend_select:{link['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_extend")])
    
    await message.answer(
        "🔄 *Выберите ссылку для продления*\n\n"
        "Нажмите на ссылку, чтобы выбрать срок продления.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@dp.callback_query(F.data.startswith("extend_select:"))
async def on_extend_select(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    link_id = int(callback.data.split(":")[1])
    await state.update_data(extend_link_id=link_id)
    
    keyboard = []

    for product_id, product in EXTEND_PRODUCTS.items():
        keyboard.append([
            InlineKeyboardButton(
                text=f"{product['title']} — {product['price_rub']}₽",
                callback_data=f"extend_pay:{product_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="cancel_extend"
        )
    ])    
    await callback.message.edit_text(
        "💳 *Выберите срок продления*\n\n"
        "Выберите период, на который хотите продлить подписку.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )


@dp.callback_query(F.data.startswith("extend_pay:"))
async def on_extend_pay(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    product_id = callback.data.split(":", 1)[1]
    product = EXTEND_PRODUCTS.get(product_id)
    if not product:
        await callback.message.edit_text("❌ Тариф продления не найден.")
        return
    
    days = product["days"]
    original_price = product["price_rub"]
    
    # ✅ Получаем пользователя
    user = await db.get_user(callback.from_user.id)
    if not user:
        await callback.message.edit_text("❌ Сначала нажмите /start")
        return
    
    # ✅ Получаем количество активных ссылок у клиента
    try:
        active_links_count = await api_client.get_active_links_count(user["client_id"])
    except Exception as e:
        logger.error(f"Ошибка получения активных ссылок: {e}")
        active_links_count = 0
    
    # ✅ Рассчитываем цену со скидкой (из products.py)
    final_price, discount_reason = calculate_discounted_price(original_price, active_links_count)
    
    # Промокоды на продление (опционально)
    user_promo = await db.get_user_promocode(callback.from_user.id)
    if user_promo and user_promo["discount_percent"] > 0:
        discount_percent = user_promo["discount_percent"]
        final_price = int(final_price * (100 - discount_percent) / 100)
    else:
        discount_percent = 0
    
    data_state = await state.get_data()
    link_id = data_state.get("extend_link_id")
    
    if not link_id:
        await callback.message.edit_text("❌ Ошибка: ссылка не выбрана.")
        return
    
    payment = payments.create_payment(
        amount_rub=final_price,
        description=f"Продление на {days} дней - {discount_reason}",
        metadata={
            "telegram_id": callback.from_user.id,
            "link_id": link_id,
            "days": days,
            "type": "extend",
            "active_links_count": active_links_count,
            "original_price": original_price,
            "final_price": final_price,
            "discount_percent": discount_percent
        },
    )
    
    await db.create_order(
        callback.from_user.id,
        product_id,
        payment["payment_id"],
        link_id=link_id,
        days=days
    )
    
    price_text = f"{final_price}₽"
    if active_links_count == 0:
        price_text += " (первая ссылка)"
    else:
        price_text += f" ({discount_reason})"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Оплатить", url=payment["confirmation_url"])
    ]])
    
    await callback.message.edit_text(
        f"💳 *Заказ создан*\n\n"
        f"📌 Продление на {days} дней\n"
        f"💰 Сумма: {price_text}\n"
        f"📊 Активных ссылок: {active_links_count}\n\n"
        f"Нажмите «Оплатить» для перехода к оплате.",
        parse_mode="Markdown",
        reply_markup=kb
    )
    
    if user_promo:
        await db.clear_user_promocode(callback.from_user.id)


# ─────────────────────── WEBHOOK ЮMONEY ───────────────────────

async def yoomoney_webhook(request: web.Request):
    try:
        data = dict(await request.post())
    except Exception as e:
        logger.exception(e)
        return web.Response(status=400)

    # ─── ДОБАВЬ ЭТО ───
    logger.info(f"=== ВЕБХУК ПОЛУЧЕН ===")
    logger.info(f"DATA: {data}")
    # ─── КОНЕЦ ───

    logger.warning(data)

    logger.warning(data)

    if data.get("test_notification") == "true":
        logger.info("Получено тестовое уведомление ЮMoney")
        return web.Response(status=200)

    payment_id = data.get("label")
    
    if not payment_id:
        return web.Response(status=200)

    order = await db.get_order_by_payment(payment_id)
    if not order:
        logger.warning(f"Webhook: заказ для label={payment_id} не найден")
        return web.Response(status=200)

    order = dict(order)
    
    if order["status"] == "succeeded":
        return web.Response(status=200)

    telegram_id = order["telegram_id"]
    user = await db.get_user(telegram_id)

    if not user:
        logger.error(f"Webhook: пользователь {telegram_id} не найден")
        return web.Response(status=200)

    product_id = order.get("product_id", "")
    link_id = order.get("link_id")
    days = order.get("days", 0)

    if product_id.startswith("extend_") and link_id:
        try:
            result = await api_client.extend_link_paid(link_id, days)
            
            if result.get("success"):
                await db.mark_order_succeeded(payment_id, f"extended_link_{link_id}")
                
                await bot.send_message(
                    telegram_id,
                    f"✅ Подписка продлена на {days} дней!\n\n"
                    f"📌 Теперь ваша ссылка активна до нового срока.\n"
                    f"🗓 Новый срок: {result.get('new_expires_at', '—')[:16].replace('T', ' ')}"
                )
            else:
                raise Exception(result.get("error", "Неизвестная ошибка"))
                
        except Exception as e:
            logger.error(f"Ошибка при продлении ссылки {link_id}: {e}")
            await db.mark_order_status(payment_id, "error")
            await bot.send_message(
                telegram_id,
                "⚠️ Оплата прошла, но при продлении подписки произошла ошибка. "
                "Мы уже разбираемся, напишите в поддержку.",
            )
        return web.Response(status=200)

    product = PRODUCTS.get(product_id)
    if not product:
        logger.error(f"Webhook: продукт {product_id} не найден")
        return web.Response(status=200)

    try:
        result = await generate_link_with_recovery(
            telegram_id=telegram_id,
            username=user["username"],
            file=product["file"],
            expires_in=product["expires_in"],
            max_uses=product["max_uses"],
        )
        link = result["link"]
        await db.mark_order_succeeded(payment_id, link)

        qr_bytes = make_qr_bytes(link)
        caption = (
            f"✅ Оплата получена!\n\n"
            f"🔗 `{link}`\n"
            f"⏱ Действует: {product['expires_in'] // 3600} ч\n"
            f"🔢 Использований: {product['max_uses']}"
        ) + get_happ_instructions()
        
        await bot.send_photo(
            chat_id=telegram_id,
            photo=BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=caption,
            parse_mode="Markdown"
        )

        if user["referrer_id"] and not user["referral_rewarded"]:
            await reward_referrer(user["referrer_id"], telegram_id, user["username"])

    except Exception as e:
        logger.error(f"Ошибка при выдаче ссылки после оплаты: {e}")
        await db.mark_order_status(payment_id, "error")
        await bot.send_message(
            telegram_id,
            "⚠️ Оплата прошла, но при выдаче ссылки произошла ошибка. "
            "Мы уже разбираемся, напишите в поддержку.",
        )

    return web.Response(status=200)


# ─────────────────────── WEBHOOK PAYRAM ───────────────────────

async def payram_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"PayRam webhook: ошибка парсинга JSON: {e}")
        return web.Response(status=400)

    logger.info(f"PayRam webhook received: {data}")

    event = data.get("event")
    if event != "payment.succeeded":
        logger.info(f"PayRam webhook: игнорируем событие {event}")
        return web.Response(status=200)

    payment_data = data.get("data", {})
    metadata = payment_data.get("metadata", {})
    
    telegram_id = metadata.get("telegram_id")
    if not telegram_id:
        logger.error("PayRam webhook: telegram_id не найден в metadata")
        return web.Response(status=200)

    product_id = metadata.get("product_id")
    link_id = metadata.get("link_id")
    days = metadata.get("days", 0)

    user = await db.get_user(int(telegram_id))
    if not user:
        logger.error(f"PayRam webhook: пользователь {telegram_id} не найден")
        return web.Response(status=200)

    if link_id and days > 0:
        try:
            result = await api_client.extend_link_paid(int(link_id), int(days))
            if result.get("success"):
                await bot.send_message(
                    telegram_id,
                    f"✅ Подписка продлена на {days} дней!\n\n"
                    f"📌 Теперь ваша ссылка активна до нового срока.\n"
                    f"🗓 Новый срок: {result.get('new_expires_at', '—')[:16].replace('T', ' ')}"
                )
            else:
                raise Exception(result.get("error", "Неизвестная ошибка"))
        except Exception as e:
            logger.error(f"Ошибка при продлении ссылки {link_id}: {e}")
            await bot.send_message(
                telegram_id,
                "⚠️ Оплата прошла, но при продлении подписки произошла ошибка. "
                "Мы уже разбираемся, напишите в поддержку.",
            )
        return web.Response(status=200)

    if not product_id:
        logger.error("PayRam webhook: product_id не найден")
        return web.Response(status=200)

    product = PRODUCTS.get(product_id)
    if not product:
        logger.error(f"PayRam webhook: продукт {product_id} не найден")
        return web.Response(status=200)

    try:
        result = await generate_link_with_recovery(
            telegram_id=int(telegram_id),
            username=user["username"],
            file=product["file"],
            expires_in=product["expires_in"],
            max_uses=product["max_uses"],
        )
        link = result["link"]

        qr_bytes = make_qr_bytes(link)
        caption = (
            f"✅ Оплата получена!\n\n"
            f"🔗 `{link}`\n"
            f"⏱ Действует: {product['expires_in'] // 3600} ч\n"
            f"🔢 Использований: {product['max_uses']}"
        ) + get_happ_instructions()
        
        await bot.send_photo(
            chat_id=int(telegram_id),
            photo=BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=caption,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Ошибка при выдаче ссылки после оплаты: {e}")
        await bot.send_message(
            telegram_id,
            "⚠️ Оплата прошла, но при выдаче ссылки произошла ошибка. "
            "Мы уже разбираемся, напишите в поддержку.",
        )

    return web.Response(status=200)


# ─────────────────────── КЛАВИАТУРЫ ───────────────────────

def main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🎁 Пробный период"), KeyboardButton(text="💳 Купить")],
        [KeyboardButton(text="📋 Мои ссылки"), KeyboardButton(text="📎 Привязать ссылку")],
        [KeyboardButton(text="🔄 Продлить ссылку"), KeyboardButton(text="🤝 Пригласить друга")],
        [KeyboardButton(text="🎫 Промокод"), KeyboardButton(text="❓ Помощь")],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="⚙️ Админка")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def build_webhook_app() -> web.Application:
    app = web.Application()
    app.router.add_post(config.YOOMONEY_WEBHOOK_PATH, yoomoney_webhook)
    app.router.add_post("/payram/webhook", payram_webhook)
    return app


# ─────────────────────── ЗАПУСК ───────────────────────

async def main():
    await db.init_db()

    webhook_app = build_webhook_app()
    runner = web.AppRunner(webhook_app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    logger.info(f"Webhook-сервер ЮMoney запущен на {config.WEBHOOK_HOST}:{config.WEBHOOK_PORT}")

    logger.info("Бот запускается (long polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
