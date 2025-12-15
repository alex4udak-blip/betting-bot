import os
import logging
import json
import sqlite3
import asyncio
import re
import hmac
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus
from typing import Optional, Any

import aiohttp
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, JobQueue
import anthropic

# ML imports (for prediction learning)
try:
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score
    import joblib
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    np = None

# ===== CONFIGURATION (from config.py) =====
from config import (
    TELEGRAM_TOKEN, FOOTBALL_API_KEY, ODDS_API_KEY, CLAUDE_API_KEY,
    FOOTBALL_API_URL, ODDS_API_URL, AFFILIATE_LINK, CRYPTO_WALLETS,
    CRYPTOBOT_TOKEN, CRYPTO_PRICES, FREE_DAILY_LIMIT, ADMIN_IDS,
    SUPPORT_USERNAME, WEBHOOK_SECRET_1WIN, WEBHOOK_SECRET_CRYPTO,
    HTTP_TIMEOUT, WEB_SERVER_PORT, DB_PATH, ML_MODELS_DIR, ML_MIN_SAMPLES,
    LOG_LEVEL, LOG_FORMAT, is_admin, validate_config, MONETIZATION_ENABLED
)

logging.basicConfig(format=LOG_FORMAT, level=getattr(logging, LOG_LEVEL))
logger = logging.getLogger(__name__)

claude_client = None
if CLAUDE_API_KEY:
    claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# Global aiohttp session (initialized on first use)
_http_session: Optional[aiohttp.ClientSession] = None

async def get_http_session() -> aiohttp.ClientSession:
    """Get or create global aiohttp session"""
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        _http_session = aiohttp.ClientSession(timeout=timeout)
    return _http_session

async def close_http_session() -> None:
    """Close global aiohttp session"""
    global _http_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
        _http_session = None

# Live mode subscribers
live_subscribers = set()
inplay_subscribers = set()

# Track already sent alerts to prevent duplicates (match_id -> timestamp)
sent_alerts = {}  # {match_id: datetime} - cleared after match starts

# Matches cache to reduce API calls
matches_cache = {
    "data": [],
    "updated_at": None,
    "ttl_seconds": 120  # Cache for 2 minutes
}

# Extended competitions for Standard plan (25 leagues)
COMPETITIONS = {
    # Tier 1 - Top leagues
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "EL": "Europa League",
    "ELC": "Championship",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "BSA": "Brasileirão",
    
    # Tier 2 - Secondary leagues (NEW!)
    "BL2": "Bundesliga 2",
    "SB": "Serie B",
    "FL2": "Ligue 2",
    "SD": "Segunda División",
    "SPL": "Scottish Premier",
    "BJL": "Jupiler Pro League",
    "ASL": "Liga Argentina",
    "EL1": "League One",
    "FAC": "FA Cup",
    "DFB": "DFB-Pokal",
    "MLS": "MLS",
}

# Top clubs that should never be underestimated
TOP_CLUBS = [
    "Real Madrid", "Barcelona", "Bayern Munich", "Bayern München", "Manchester City", 
    "Liverpool", "Arsenal", "Chelsea", "Manchester United",
    "Paris Saint-Germain", "PSG", "Juventus", "Inter Milan", "AC Milan",
    "Borussia Dortmund", "Atlético Madrid", "Napoli"
]

# Cup competitions (higher upset risk)
CUP_KEYWORDS = ["Cup", "Copa", "Coupe", "Pokal", "Coppa", "EFL", "FA Cup"]

def is_cup_match(match: dict) -> bool:
    """Check if match is a cup competition"""
    competition = match.get("competition", {}).get("name") or ""
    return any(kw in competition for kw in CUP_KEYWORDS)

def filter_cup_matches(matches: list, exclude: bool = False) -> list:
    """Filter matches - if exclude=True, remove cup matches"""
    if not exclude:
        return matches
    return [m for m in matches if not is_cup_match(m)]

# ===== TRANSLATIONS =====
TRANSLATIONS = {
    "ru": {
        "welcome": "👋 Привет! Я AI-бот для ставок на футбол.\n\nИспользуй меню ниже или напиши название команды.",
        "top_bets": "🔥 Топ ставки",
        "matches": "⚽ Матчи",
        "stats": "📊 Статистика",
        "favorites": "⭐ Избранное",
        "settings": "⚙️ Настройки",
        "help_btn": "❓ Помощь",
        "daily_limit": "⚠️ Достигнут лимит ({limit} прогнозов/день).\n\n💎 **Получи безлимитный доступ!**\nСделай депозит в 1win — получи премиум автоматически.\n\n👇 Нажми на кнопку ниже:",
        "daily_limit_simple": "⚠️ Достигнут лимит ({limit} прогнозов/день).\n\n💎 **Премиум скоро будет доступен!**\nМы работаем над системой оплаты.",
        "place_bet": "🎰 Поставить",
        "no_matches": "Матчей не найдено",
        "analyzing": "🔍 Анализирую...",
        "cup_warning": "⚠️ Кубковый матч — выше риск сенсации!",
        "rotation_warning": "⚠️ Возможна ротация состава",
        "top_club_warning": "⚠️ Топ-клуб — не ставь против",
        "unlimited": "🎰 Безлимитный доступ",
        # New translations
        "choose_action": "Выбери действие:",
        "recommendations": "📊 Рекомендации",
        "today": "📅 Сегодня",
        "tomorrow": "📆 Завтра",
        "leagues": "🏆 Лиги",
        "live_alerts": "🔔 Live-алерты",
        "help": "❓ Помощь",
        "matches_today": "📅 **МАТЧИ СЕГОДНЯ**",
        "matches_tomorrow": "📆 **МАТЧИ ЗАВТРА**",
        "recs_today": "📊 Рекомендации на сегодня",
        "recs_tomorrow": "📊 Рекомендации на завтра",
        "top_leagues": "🏆 **Топ лиги:**",
        "other_leagues": "🏆 **Другие лиги:**",
        "more_leagues": "➕ Ещё лиги",
        "back": "🔙 Назад",
        "back_to_leagues": "🔙 К лигам",
        "loading": "🔍 Загружаю {name}...",
        "no_matches_league": "❌ Нет матчей {name}",
        "free_predictions": "💎 Бесплатно: {limit} прогноза/день",
        "unlimited_deposit": "🔓 Безлимит: сделай депозит по ссылке",
        "live_alerts_on": "🔔 **Live-алерты включены!**\n\nКаждые 10 минут проверяю матчи.\nЕсли найду ставку 70%+ за 1-3 часа — пришлю алерт!\n\nНапиши /live чтобы выключить.",
        "live_alerts_off": "🔕 **Live-алерты выключены**\n\nНапиши /live чтобы включить снова.",
        "live_alert_title": "🚨 LIVE АЛЕРТ!",
        "in_hours": "Через {hours} часа",
        "bet": "⚡ СТАВКА:",
        "confidence": "📊 Уверенность:",
        "odds": "💰 Коэфф:",
        "reason": "📝 Почему:",
        "first_start_title": "🎉 **Добро пожаловать в AI Betting Bot!**",
        "first_start_text": """🤖 **Что умеет бот:**
• AI анализирует форму, H2H, составы, погоду
• Учитывает класс команд, мотивацию, усталость
• Прозрачная статистика — сам видишь точность!

🆓 **Бесплатно:**
• 3 прогноза в день
• Полная статистика и аналитика
• Алерты на топовые матчи

⚡ **Как начать:**
Просто напиши название команды (например: *Барселона*) или нажми кнопку ниже!""",
        "detected_settings": "🌍 Определил твои настройки:",
        "language_label": "Язык",
        "timezone_label": "Часовой пояс",
        "change_in_settings": "Можешь изменить в настройках",
        # Settings UI
        "admin_only": "⛔ Только для админов",
        "limit_reset": "✅ Лимит сброшен!\n\nUser ID: {user_id}\nDaily requests: 0/{limit}\n\nТеперь можешь делать {limit} новых прогнозов.",
        "premium_removed": "✅ Premium статус убран!\n\nUser ID: {user_id}\nPremium: {premium}\nDaily requests: {requests}/{limit}\n\nТеперь лимит будет работать.",
        "select_min_odds": "📉 Выбери минимальный коэффициент:",
        "min_odds_set": "✅ Минимальный коэфф: {value}",
        "select_max_odds": "📈 Выбери максимальный коэффициент:",
        "max_odds_set": "✅ Максимальный коэфф: {value}",
        "select_risk": "⚠️ Выбери уровень риска:",
        "risk_set": "✅ Риск: {value}",
        "select_language": "🌍 Выбери язык:",
        "select_timezone": "🕐 Выбери часовой пояс:",
        "select_league": "➕ Выбери лигу:",
        "league_added": "✅ {name} добавлена!",
        "team_added": "✅ {name} добавлена в избранное!",
        "greeting_response": "👋 Привет! Выбери действие или напиши название команды:",
        "upcoming_matches": "⚽ **Ближайшие матчи:**",
        "analyzing_bets": "🔍 Анализирую лучшие ставки...",
        "analysis_error": "❌ Ошибка анализа.",
        "sure_searching": "🎯 Ищу уверенные ставки (75%+)...",
        "searching_match": "🔍 Ищу матч...",
        "match_not_found": "😕 Не нашёл матч: {query}",
        "available_matches": "📋 **Доступные матчи:**",
        "match_found": "✅ Нашёл: {home} vs {away}\n🏆 {comp}\n\n⏳ Собираю статистику...",
        "premium_btn": "💎 Премиум",
        "no_sure_bets": "❌ Нет уверенных ставок 75%+ на ближайшие дни.",
        # Referral system
        "referral_btn": "👥 Друзья",
        "referral_title": "👥 **Реферальная программа**",
        "referral_desc": "Приглашай друзей и получай бонусные дни премиума!",
        "referral_link": "🔗 **Твоя ссылка:**",
        "referral_stats": "📊 **Твоя статистика:**",
        "referral_invited": "Приглашено",
        "referral_premium": "Купили премиум",
        "referral_earned": "Заработано дней",
        "referral_bonus": "**+{days} дней** премиума за приглашённого друга!",
        "referral_copy": "👆 Нажми на ссылку, чтобы скопировать",
        "referral_rules": "📋 **Правила:**\n• За каждого друга, который купит премиум — **+3 дня** тебе\n• Бонус начисляется автоматически",
        "referral_welcome": "🎁 Тебя пригласил друг! Получи бонус при покупке премиума.",
        "referral_reminder": "👥 **Приглашай друзей!**\n\nЗа каждого друга с премиумом получишь **+3 дня** бесплатно!\n\n🔗 Твоя ссылка: `{link}`",
        # Streak system
        "streak_title": "🔥 **Твоя серия: {days} дней!**",
        "streak_bonus": "🎁 Бонус за серию: **+{bonus}** к точности прогнозов!",
        "streak_lost": "😢 Серия потеряна! Начинай заново.",
        "streak_record": "🏆 Твой рекорд: {record} дней",
        "streak_milestone": "🎉 **{days} дней подряд!** Ты в огне! 🔥",
        # Social proof
        "social_wins_today": "🏆 **Сегодня выиграли {count} юзеров!**",
        "social_total_wins": "📊 Всего выигрышей за неделю: **{count}**",
        "social_top_win": "💰 Лучший выигрыш дня: **{odds}x** на {match}!",
        "social_accuracy": "🎯 Точность прогнозов за неделю: **{accuracy}%**",
        "social_friend_won": "🎉 Твой друг **{name}** выиграл ставку!\n\n{match}\n⚡ {bet} @ {odds}\n\n👥 Приглашай ещё друзей: /ref",
        # Notifications
        "notif_welcome_back": "👋 С возвращением! Вот топ ставки на сегодня:",
        "notif_hot_match": "🔥 **Горячий матч через {hours}ч!**\n\n{match}\n📊 Уверенность: {confidence}%",
        "notif_daily_digest": "📊 **Твоя статистика за день:**\n• Прогнозов: {predictions}\n• Выигрышей: {wins}\n• Серия: {streak} дней 🔥",
        # Premium page
        "premium_title": "💎 **ПРЕМИУМ ДОСТУП**",
        "premium_unlimited": "🎯 Безлимитные прогнозы с точностью 70%+",
        "premium_option1_title": "**Вариант 1: Депозит в 1win** 🎰",
        "premium_option1_desc": "Сделай депозит — получи премиум автоматически!",
        "premium_option2_title": "**Вариант 2: Крипта (USDT/TON)** 💰",
        "premium_option2_crypto": "Выбери тариф ниже — оплата через @CryptoBot",
        "premium_option2_manual": "Напиши @{support} для оплаты",
        "premium_free_title": "👥 **Бесплатный способ!**",
        "premium_free_desc": "Приглашай друзей — получай **+3 дня** за каждого!",
        "premium_earned": "Уже заработано: **{days} дней**",
        "premium_click_below": "Нажми кнопку ниже 👇",
        "premium_after_payment": "После оплаты — скинь скрин @{support}",
        "premium_deposit_btn": "🎰 Депозит в 1win",
        "premium_contact_btn": "💬 Написать @{support}",
        "premium_friends_btn": "👥 Бесплатно (друзья)",
        "premium_status": "✅ У тебя премиум до: {date}",
        "friend_fallback": "Друг",
        # Prediction results
        "pred_result_title": "📊 **Результат прогноза**",
        "pred_correct": "Прогноз верный!",
        "pred_incorrect": "Прогноз не сработал",
        "pred_push": "Возврат (push)",
        "bet_main": "⚡ ОСНОВНАЯ",
        "bet_alt": "📌 АЛЬТЕРНАТИВНАЯ",
        # Daily digest
        "daily_digest_title": "☀️ **ДАЙДЖЕСТ НА СЕГОДНЯ**",
        "place_bet_btn": "🎰 Ставить",
        "all_matches_btn": "📅 Все матчи",
        # ===== NOTIFICATION SYSTEM =====
        # Evening digest (22:00 MSK)
        "evening_digest_title": "📊 **РЕЗУЛЬТАТЫ ДНЯ**",
        "evening_best_bet": "🔥 ЛУЧШИЙ тип ставки сегодня:",
        "evening_overall": "📈 Общий результат:",
        "evening_top_match": "🏆 Топ-матч:",
        "evening_tomorrow_count": "Завтра {count} матчей с прогнозами!",
        "evening_cta": "Жми /predict чтобы получить",
        # Morning alert (10:00)
        "morning_alert_title": "⚽ **Сегодня {count} топ-матчей!**",
        "morning_main_match": "🔝 Главный матч дня:",
        "morning_cta": "Прогноз готов → /predict",
        # Inactive user alert (3 days)
        "inactive_title": "👋 **Давно не виделись!**",
        "inactive_stats": "За эти дни наши прогнозы:",
        "inactive_wins": "✅ {wins} побед из {total} ({percent}%)",
        "inactive_streak": "Лучшая серия: {streak} подряд 🔥",
        "inactive_cta": "Жми /predict — там свежие матчи",
        # Weekly report (Sunday 20:00)
        "weekly_title": "📊 **ИТОГИ НЕДЕЛИ**",
        "weekly_accuracy": "✅ Точность: {wins}/{total} ({percent}%)",
        "weekly_best_day": "🔥 Лучший день: {day} ({wins}/{total})",
        "weekly_best_bet_type": "🏆 Лучший тип ставки:",
        "weekly_next_week": "Следующая неделя — {count} матчей!",
        # Referral bonus
        "referral_bonus_title": "🎁 **БОНУС ЗА ДРУЗЕЙ!**",
        "referral_bonus_desc": "Пригласи 2 друзей — получи **3 бесплатных прогноза**!",
        "referral_bonus_progress": "📊 Прогресс: {current}/2 друзей",
        "referral_bonus_claimed": "🎉 Бонус получен! +3 прогноза на сегодня",
        "referral_bonus_friend_gets": "Твой друг тоже получит 3 бесплатных прогноза!",
        "referral_invite_btn": "👥 Пригласить друзей",
        # New user onboarding
        "onboard_welcome": "🎉 **Добро пожаловать!**\n\nЯ AI-бот для ставок на футбол с точностью 70%+",
        "onboard_step1": "1️⃣ Напиши /predict — получи прогноз",
        "onboard_step2": "2️⃣ Включи /live — получай алерты",
        "onboard_step3": "3️⃣ Пригласи друзей — получи бонус",
        "onboard_free_today": "🎁 Сегодня {count} бесплатных прогнозов!",
        "onboard_try_now": "Попробуй прямо сейчас 👇",
        "try_prediction_btn": "🎯 Попробовать прогноз",
        "where_to_bet": "🎰 **Где делать ставки:**",
        "bet_partner_text": "Делай ставки у нашего партнёра 1win — бонус +500% на первый депозит!",
        "open_1win_btn": "🎰 Открыть 1win",
        # Hot match alert
        "hot_match_title": "🔥 **ГОРЯЧИЙ МАТЧ!**",
        "hot_match_starts": "⏰ Начало через {hours}ч",
        "hot_match_confidence": "📊 Уверенность: {percent}%",
        "hot_match_cta": "Успей поставить!",
        "analyze_match_btn": "🔍 Анализ матча",
        # Day names
        "day_monday": "Понедельник",
        "day_tuesday": "Вторник",
        "day_wednesday": "Среда",
        "day_thursday": "Четверг",
        "day_friday": "Пятница",
        "day_saturday": "Суббота",
        "day_sunday": "Воскресенье",
        # Monetization placeholders (when disabled)
        "premium_coming_soon": "🚀 Премиум скоро будет доступен!\n\nМы работаем над системой оплаты. Следите за обновлениями!",
        "betting_coming_soon": "🎰 Партнёрская программа скоро!",
        "place_bet_disabled": "📊 Сделать ставку",
    },
    "en": {
        "welcome": "👋 Hello! I'm an AI betting bot for football.\n\nUse the menu below or type a team name.",
        "top_bets": "🔥 Top Bets",
        "matches": "⚽ Matches",
        "stats": "📊 Stats",
        "favorites": "⭐ Favorites",
        "settings": "⚙️ Settings",
        "help_btn": "❓ Help",
        "daily_limit": "⚠️ Daily limit reached ({limit} predictions).\n\n💎 **Get unlimited access!**\nMake a deposit on 1win — get premium automatically.\n\n👇 Tap the button below:",
        "daily_limit_simple": "⚠️ Daily limit reached ({limit} predictions).\n\n💎 **Premium coming soon!**\nWe're working on the payment system.",
        "place_bet": "🎰 Place bet",
        "no_matches": "No matches found",
        "analyzing": "🔍 Analyzing...",
        "cup_warning": "⚠️ Cup match — higher upset risk!",
        "rotation_warning": "⚠️ Possible squad rotation",
        "top_club_warning": "⚠️ Top club — don't bet against",
        "unlimited": "🎰 Get unlimited access",
        # New translations
        "choose_action": "Choose an action:",
        "recommendations": "📊 Recommendations",
        "today": "📅 Today",
        "tomorrow": "📆 Tomorrow",
        "leagues": "🏆 Leagues",
        "live_alerts": "🔔 Live alerts",
        "help": "❓ Help",
        "matches_today": "📅 **TODAY'S MATCHES**",
        "matches_tomorrow": "📆 **TOMORROW'S MATCHES**",
        "recs_today": "📊 Today's recommendations",
        "recs_tomorrow": "📊 Tomorrow's recommendations",
        "top_leagues": "🏆 **Top Leagues:**",
        "other_leagues": "🏆 **Other Leagues:**",
        "more_leagues": "➕ More leagues",
        "back": "🔙 Back",
        "back_to_leagues": "🔙 To leagues",
        "loading": "🔍 Loading {name}...",
        "no_matches_league": "❌ No matches for {name}",
        "free_predictions": "💎 Free: {limit} predictions/day",
        "unlimited_deposit": "🔓 Unlimited: make a deposit via link",
        "live_alerts_on": "🔔 **Live alerts enabled!**\n\nChecking matches every 10 minutes.\nIf I find a 70%+ bet 1-3 hours before — I'll send an alert!\n\nType /live to disable.",
        "live_alerts_off": "🔕 **Live alerts disabled**\n\nType /live to enable again.",
        "live_alert_title": "🚨 LIVE ALERT!",
        "in_hours": "In {hours} hours",
        "bet": "⚡ BET:",
        "confidence": "📊 Confidence:",
        "odds": "💰 Odds:",
        "reason": "📝 Why:",
        "first_start_title": "🎉 **Welcome to AI Betting Bot!**",
        "first_start_text": """🤖 **What the bot does:**
• AI analyzes form, H2H, lineups, weather
• Considers team class, motivation, fatigue
• Transparent stats — see accuracy yourself!

🆓 **Free:**
• 3 predictions per day
• Full statistics and analytics
• Alerts for top matches

⚡ **How to start:**
Just type a team name (e.g. *Barcelona*) or tap a button below!""",
        "detected_settings": "🌍 Detected your settings:",
        "language_label": "Language",
        "timezone_label": "Timezone",
        "change_in_settings": "You can change this in settings",
        # Settings UI
        "admin_only": "⛔ Admin only",
        "limit_reset": "✅ Limit reset!\n\nUser ID: {user_id}\nDaily requests: 0/{limit}\n\nYou can make {limit} new predictions.",
        "premium_removed": "✅ Premium status removed!\n\nUser ID: {user_id}\nPremium: {premium}\nDaily requests: {requests}/{limit}\n\nLimit is now active.",
        "select_min_odds": "📉 Select minimum odds:",
        "min_odds_set": "✅ Min odds: {value}",
        "select_max_odds": "📈 Select maximum odds:",
        "max_odds_set": "✅ Max odds: {value}",
        "select_risk": "⚠️ Select risk level:",
        "risk_set": "✅ Risk: {value}",
        "select_language": "🌍 Select language:",
        "select_timezone": "🕐 Select timezone:",
        "select_league": "➕ Select league:",
        "league_added": "✅ {name} added!",
        "team_added": "✅ {name} added to favorites!",
        "greeting_response": "👋 Hello! Choose an action or type a team name:",
        "upcoming_matches": "⚽ **Upcoming matches:**",
        "analyzing_bets": "🔍 Analyzing best bets...",
        "analysis_error": "❌ Analysis error.",
        "sure_searching": "🎯 Searching high confidence bets (75%+)...",
        "searching_match": "🔍 Searching match...",
        "match_not_found": "😕 Match not found: {query}",
        "available_matches": "📋 **Available matches:**",
        "match_found": "✅ Found: {home} vs {away}\n🏆 {comp}\n\n⏳ Gathering stats...",
        "premium_btn": "💎 Premium",
        "no_sure_bets": "❌ No confident bets 75%+ found for upcoming days.",
        # Referral system
        "referral_btn": "👥 Friends",
        "referral_title": "👥 **Referral Program**",
        "referral_desc": "Invite friends and earn bonus premium days!",
        "referral_link": "🔗 **Your link:**",
        "referral_stats": "📊 **Your stats:**",
        "referral_invited": "Invited",
        "referral_premium": "Bought premium",
        "referral_earned": "Days earned",
        "referral_bonus": "**+{days} days** premium for referred friend!",
        "referral_copy": "👆 Tap the link to copy",
        "referral_rules": "📋 **Rules:**\n• For each friend who buys premium — **+3 days** for you\n• Bonus is granted automatically",
        "referral_welcome": "🎁 You were invited by a friend! Get a bonus when buying premium.",
        "referral_reminder": "👥 **Invite friends!**\n\nGet **+3 days** free for each friend with premium!\n\n🔗 Your link: `{link}`",
        # Streak system
        "streak_title": "🔥 **Your streak: {days} days!**",
        "streak_bonus": "🎁 Streak bonus: **+{bonus}** prediction accuracy!",
        "streak_lost": "😢 Streak lost! Start again.",
        "streak_record": "🏆 Your record: {record} days",
        "streak_milestone": "🎉 **{days} days in a row!** You're on fire! 🔥",
        # Social proof
        "social_wins_today": "🏆 **{count} users won today!**",
        "social_total_wins": "📊 Total wins this week: **{count}**",
        "social_top_win": "💰 Best win today: **{odds}x** on {match}!",
        "social_accuracy": "🎯 Weekly prediction accuracy: **{accuracy}%**",
        "social_friend_won": "🎉 Your friend **{name}** won a bet!\n\n{match}\n⚡ {bet} @ {odds}\n\n👥 Invite more friends: /ref",
        # Notifications
        "notif_welcome_back": "👋 Welcome back! Here are today's top bets:",
        "notif_hot_match": "🔥 **Hot match in {hours}h!**\n\n{match}\n📊 Confidence: {confidence}%",
        "notif_daily_digest": "📊 **Your daily stats:**\n• Predictions: {predictions}\n• Wins: {wins}\n• Streak: {streak} days 🔥",
        # Premium page
        "premium_title": "💎 **PREMIUM ACCESS**",
        "premium_unlimited": "🎯 Unlimited predictions with 70%+ accuracy",
        "premium_option1_title": "**Option 1: Deposit on 1win** 🎰",
        "premium_option1_desc": "Make a deposit — get premium automatically!",
        "premium_option2_title": "**Option 2: Crypto (USDT/TON)** 💰",
        "premium_option2_crypto": "Choose plan below — pay via @CryptoBot",
        "premium_option2_manual": "Contact @{support} to pay",
        "premium_free_title": "👥 **Free method!**",
        "premium_free_desc": "Invite friends — get **+3 days** per friend!",
        "premium_earned": "Already earned: **{days} days**",
        "premium_click_below": "Click button below 👇",
        "premium_after_payment": "After payment — send screenshot to @{support}",
        "premium_deposit_btn": "🎰 Deposit on 1win",
        "premium_contact_btn": "💬 Contact @{support}",
        "premium_friends_btn": "👥 Free (invite friends)",
        "premium_status": "✅ You have premium until: {date}",
        "friend_fallback": "Friend",
        # Prediction results
        "pred_result_title": "📊 **Prediction Result**",
        "pred_correct": "Prediction correct!",
        "pred_incorrect": "Prediction failed",
        "pred_push": "Push (void)",
        "bet_main": "⚡ MAIN",
        "bet_alt": "📌 ALTERNATIVE",
        # Daily digest
        "daily_digest_title": "☀️ **TODAY'S DIGEST**",
        "place_bet_btn": "🎰 Place bet",
        "all_matches_btn": "📅 All matches",
        # ===== NOTIFICATION SYSTEM =====
        "evening_digest_title": "📊 **DAY RESULTS**",
        "evening_best_bet": "🔥 BEST bet type today:",
        "evening_overall": "📈 Overall result:",
        "evening_top_match": "🏆 Top match:",
        "evening_tomorrow_count": "Tomorrow {count} matches with predictions!",
        "evening_cta": "Tap /predict to get it",
        "morning_alert_title": "⚽ **Today {count} top matches!**",
        "morning_main_match": "🔝 Main match of the day:",
        "morning_cta": "Prediction ready → /predict",
        "inactive_title": "👋 **Long time no see!**",
        "inactive_stats": "Our predictions these days:",
        "inactive_wins": "✅ {wins} wins out of {total} ({percent}%)",
        "inactive_streak": "Best streak: {streak} in a row 🔥",
        "inactive_cta": "Tap /predict — fresh matches there",
        "weekly_title": "📊 **WEEK RESULTS**",
        "weekly_accuracy": "✅ Accuracy: {wins}/{total} ({percent}%)",
        "weekly_best_day": "🔥 Best day: {day} ({wins}/{total})",
        "weekly_best_bet_type": "🏆 Best bet type:",
        "weekly_next_week": "Next week — {count} matches!",
        "referral_bonus_title": "🎁 **FRIEND BONUS!**",
        "referral_bonus_desc": "Invite 2 friends — get **3 free predictions**!",
        "referral_bonus_progress": "📊 Progress: {current}/2 friends",
        "referral_bonus_claimed": "🎉 Bonus claimed! +3 predictions today",
        "referral_bonus_friend_gets": "Your friend also gets 3 free predictions!",
        "referral_invite_btn": "👥 Invite friends",
        "onboard_welcome": "🎉 **Welcome!**\n\nI'm an AI football betting bot with 70%+ accuracy",
        "onboard_step1": "1️⃣ Type /predict — get a prediction",
        "onboard_step2": "2️⃣ Enable /live — get alerts",
        "onboard_step3": "3️⃣ Invite friends — get bonus",
        "onboard_free_today": "🎁 Today {count} free predictions!",
        "onboard_try_now": "Try it now 👇",
        "try_prediction_btn": "🎯 Try a prediction",
        "where_to_bet": "🎰 **Where to bet:**",
        "bet_partner_text": "Bet with our partner 1win — +500% bonus on first deposit!",
        "open_1win_btn": "🎰 Open 1win",
        "hot_match_title": "🔥 **HOT MATCH!**",
        "hot_match_starts": "⏰ Starts in {hours}h",
        "hot_match_confidence": "📊 Confidence: {percent}%",
        "hot_match_cta": "Bet now!",
        "analyze_match_btn": "🔍 Match Analysis",
        "day_monday": "Monday",
        "day_tuesday": "Tuesday",
        "day_wednesday": "Wednesday",
        "day_thursday": "Thursday",
        "day_friday": "Friday",
        "day_saturday": "Saturday",
        "day_sunday": "Sunday",
        # Monetization placeholders (when disabled)
        "premium_coming_soon": "🚀 Premium coming soon!\n\nWe're working on the payment system. Stay tuned!",
        "betting_coming_soon": "🎰 Betting partner coming soon!",
        "place_bet_disabled": "📊 Place bet",
    },
    "pt": {
        "welcome": "👋 Olá! Sou um bot de apostas com IA para futebol.\n\nUse o menu ou digite o nome de um time.",
        "top_bets": "🔥 Top Apostas",
        "matches": "⚽ Jogos",
        "stats": "📊 Estatísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Config",
        "help_btn": "❓ Ajuda",
        "daily_limit": "⚠️ Limite diário atingido ({limit} previsões).\n\n💎 **Acesso ilimitado!**\nFaça um depósito no 1win — receba premium automaticamente.\n\n👇 Toque no botão abaixo:",
        "daily_limit_simple": "⚠️ Limite diário atingido ({limit} previsões).\n\n💎 **Premium em breve!**\nEstamos trabalhando no sistema de pagamento.",
        "place_bet": "🎰 Apostar",
        "no_matches": "Nenhum jogo encontrado",
        "analyzing": "🔍 Analisando...",
        "cup_warning": "⚠️ Jogo de copa — maior risco!",
        "rotation_warning": "⚠️ Possível rotação",
        "top_club_warning": "⚠️ Clube top — não aposte contra",
        "unlimited": "🎰 Acesso ilimitado",
        # New translations
        "choose_action": "Escolha uma ação:",
        "recommendations": "📊 Recomendações",
        "today": "📅 Hoje",
        "tomorrow": "📆 Amanhã",
        "leagues": "🏆 Ligas",
        "live_alerts": "🔔 Alertas ao vivo",
        "help": "❓ Ajuda",
        "matches_today": "📅 **JOGOS DE HOJE**",
        "matches_tomorrow": "📆 **JOGOS DE AMANHÃ**",
        "recs_today": "📊 Recomendações de hoje",
        "recs_tomorrow": "📊 Recomendações de amanhã",
        "top_leagues": "🏆 **Top Ligas:**",
        "other_leagues": "🏆 **Outras Ligas:**",
        "more_leagues": "➕ Mais ligas",
        "back": "🔙 Voltar",
        "back_to_leagues": "🔙 Para ligas",
        "loading": "🔍 Carregando {name}...",
        "no_matches_league": "❌ Sem jogos para {name}",
        "free_predictions": "💎 Grátis: {limit} previsões/dia",
        "unlimited_deposit": "🔓 Ilimitado: faça um depósito",
        "live_alerts_on": "🔔 **Alertas ao vivo ativados!**\n\nVerificando jogos a cada 10 minutos.\nSe encontrar aposta 70%+ em 1-3h — envio alerta!\n\nDigite /live para desativar.",
        "live_alerts_off": "🔕 **Alertas ao vivo desativados**\n\nDigite /live para ativar.",
        "live_alert_title": "🚨 ALERTA AO VIVO!",
        "in_hours": "Em {hours} horas",
        "bet": "⚡ APOSTA:",
        "confidence": "📊 Confiança:",
        "odds": "💰 Odds:",
        "reason": "📝 Por quê:",
        "first_start_title": "🎉 **Bem-vindo ao AI Betting Bot!**",
        "first_start_text": """🤖 **O que o bot faz:**
• IA analisa forma, H2H, escalações, clima
• Considera classe do time, motivação, fadiga
• Estatísticas transparentes — veja a precisão!

🆓 **Grátis:**
• 3 previsões por dia
• Estatísticas completas
• Alertas para jogos top

⚡ **Como começar:**
Digite o nome de um time (ex: *Barcelona*) ou toque um botão abaixo!""",
        "detected_settings": "🌍 Detectei suas configurações:",
        "language_label": "Idioma",
        "timezone_label": "Fuso horário",
        "change_in_settings": "Você pode mudar nas configurações",
        # Settings UI
        "admin_only": "⛔ Somente admin",
        "limit_reset": "✅ Limite zerado!\n\nUser ID: {user_id}\nDaily requests: 0/{limit}\n\nVocê pode fazer {limit} novas previsões.",
        "premium_removed": "✅ Premium removido!\n\nUser ID: {user_id}\nPremium: {premium}\nDaily requests: {requests}/{limit}\n\nLimite agora ativo.",
        "select_min_odds": "📉 Selecione odds mínimas:",
        "min_odds_set": "✅ Odds mín: {value}",
        "select_max_odds": "📈 Selecione odds máximas:",
        "max_odds_set": "✅ Odds máx: {value}",
        "select_risk": "⚠️ Selecione nível de risco:",
        "risk_set": "✅ Risco: {value}",
        "select_language": "🌍 Selecione idioma:",
        "select_timezone": "🕐 Selecione fuso horário:",
        "select_league": "➕ Selecione liga:",
        "league_added": "✅ {name} adicionada!",
        "team_added": "✅ {name} adicionado aos favoritos!",
        "greeting_response": "👋 Olá! Escolha uma ação ou digite o nome do time:",
        "upcoming_matches": "⚽ **Próximos jogos:**",
        "analyzing_bets": "🔍 Analisando melhores apostas...",
        "analysis_error": "❌ Erro na análise.",
        "sure_searching": "🎯 Buscando apostas confiáveis (75%+)...",
        "searching_match": "🔍 Procurando jogo...",
        "match_not_found": "😕 Jogo não encontrado: {query}",
        "available_matches": "📋 **Jogos disponíveis:**",
        "match_found": "✅ Encontrado: {home} vs {away}\n🏆 {comp}\n\n⏳ Coletando estatísticas...",
        "premium_btn": "💎 Premium",
        "no_sure_bets": "❌ Nenhuma aposta confiável 75%+ encontrada para os próximos dias.",
        # Referral system
        "referral_btn": "👥 Amigos",
        "referral_title": "👥 **Programa de Indicação**",
        "referral_desc": "Convide amigos e ganhe dias de premium!",
        "referral_link": "🔗 **Seu link:**",
        "referral_stats": "📊 **Suas estatísticas:**",
        "referral_invited": "Convidados",
        "referral_premium": "Compraram premium",
        "referral_earned": "Dias ganhos",
        "referral_bonus": "**+{days} dias** de premium pelo amigo indicado!",
        "referral_copy": "👆 Toque no link para copiar",
        "referral_rules": "📋 **Regras:**\n• Para cada amigo que comprar premium — **+3 dias** para você\n• Bônus é concedido automaticamente",
        "referral_welcome": "🎁 Você foi convidado por um amigo! Ganhe bônus ao comprar premium.",
        "referral_reminder": "👥 **Convide amigos!**\n\nGanhe **+3 dias** grátis para cada amigo com premium!\n\n🔗 Seu link: `{link}`",
        # Streak system
        "streak_title": "🔥 **Sua sequência: {days} dias!**",
        "streak_bonus": "🎁 Bônus de sequência: **+{bonus}** precisão!",
        "streak_lost": "😢 Sequência perdida! Comece de novo.",
        "streak_record": "🏆 Seu recorde: {record} dias",
        "streak_milestone": "🎉 **{days} dias seguidos!** Você está on fire! 🔥",
        # Social proof
        "social_wins_today": "🏆 **{count} usuários ganharam hoje!**",
        "social_total_wins": "📊 Total de vitórias esta semana: **{count}**",
        "social_top_win": "💰 Melhor vitória de hoje: **{odds}x** em {match}!",
        "social_accuracy": "🎯 Precisão semanal: **{accuracy}%**",
        "social_friend_won": "🎉 Seu amigo **{name}** ganhou uma aposta!\n\n{match}\n⚡ {bet} @ {odds}\n\n👥 Convide mais amigos: /ref",
        # Notifications
        "notif_welcome_back": "👋 Bem-vindo de volta! Aqui estão as melhores apostas de hoje:",
        "notif_hot_match": "🔥 **Jogo quente em {hours}h!**\n\n{match}\n📊 Confiança: {confidence}%",
        "notif_daily_digest": "📊 **Suas estatísticas do dia:**\n• Previsões: {predictions}\n• Vitórias: {wins}\n• Sequência: {streak} dias 🔥",
        # Premium page
        "premium_title": "💎 **ACESSO PREMIUM**",
        "premium_unlimited": "🎯 Previsões ilimitadas com 70%+ de precisão",
        "premium_option1_title": "**Opção 1: Depósito no 1win** 🎰",
        "premium_option1_desc": "Faça um depósito — ganhe premium automaticamente!",
        "premium_option2_title": "**Opção 2: Cripto (USDT/TON)** 💰",
        "premium_option2_crypto": "Escolha o plano abaixo — pague via @CryptoBot",
        "premium_option2_manual": "Contate @{support} para pagar",
        "premium_free_title": "👥 **Método gratuito!**",
        "premium_free_desc": "Convide amigos — ganhe **+3 dias** por amigo!",
        "premium_earned": "Já ganhou: **{days} dias**",
        "premium_click_below": "Clique no botão abaixo 👇",
        "premium_after_payment": "Após o pagamento — envie print para @{support}",
        "premium_deposit_btn": "🎰 Depósito no 1win",
        "premium_contact_btn": "💬 Contatar @{support}",
        "premium_friends_btn": "👥 Grátis (convide amigos)",
        "premium_status": "✅ Você tem premium até: {date}",
        "friend_fallback": "Amigo",
        # Prediction results
        "pred_result_title": "📊 **Resultado da Previsão**",
        "pred_correct": "Previsão correta!",
        "pred_incorrect": "Previsão falhou",
        "pred_push": "Push (void)",
        "bet_main": "⚡ PRINCIPAL",
        "bet_alt": "📌 ALTERNATIVA",
        # Daily digest
        "daily_digest_title": "☀️ **RESUMO DO DIA**",
        "place_bet_btn": "🎰 Apostar",
        "all_matches_btn": "📅 Todos os jogos",
        # ===== NOTIFICATION SYSTEM =====
        "evening_digest_title": "📊 **RESULTADOS DO DIA**",
        "evening_best_bet": "🔥 MELHOR tipo de aposta hoje:",
        "evening_overall": "📈 Resultado geral:",
        "evening_top_match": "🏆 Melhor jogo:",
        "evening_tomorrow_count": "Amanhã {count} jogos com previsões!",
        "evening_cta": "Toque /predict para obter",
        "morning_alert_title": "⚽ **Hoje {count} jogos top!**",
        "morning_main_match": "🔝 Jogo principal do dia:",
        "morning_cta": "Previsão pronta → /predict",
        "inactive_title": "👋 **Faz tempo!**",
        "inactive_stats": "Nossas previsões esses dias:",
        "inactive_wins": "✅ {wins} vitórias de {total} ({percent}%)",
        "inactive_streak": "Melhor sequência: {streak} seguidas 🔥",
        "inactive_cta": "Toque /predict — jogos frescos lá",
        "weekly_title": "📊 **RESULTADOS DA SEMANA**",
        "weekly_accuracy": "✅ Precisão: {wins}/{total} ({percent}%)",
        "weekly_best_day": "🔥 Melhor dia: {day} ({wins}/{total})",
        "weekly_best_bet_type": "🏆 Melhor tipo de aposta:",
        "weekly_next_week": "Próxima semana — {count} jogos!",
        "referral_bonus_title": "🎁 **BÔNUS DE AMIGOS!**",
        "referral_bonus_desc": "Convide 2 amigos — ganhe **3 previsões grátis**!",
        "referral_bonus_progress": "📊 Progresso: {current}/2 amigos",
        "referral_bonus_claimed": "🎉 Bônus resgatado! +3 previsões hoje",
        "referral_bonus_friend_gets": "Seu amigo também ganha 3 previsões grátis!",
        "referral_invite_btn": "👥 Convidar amigos",
        "onboard_welcome": "🎉 **Bem-vindo!**\n\nSou um bot de apostas de futebol com IA com 70%+ de precisão",
        "onboard_step1": "1️⃣ Digite /predict — obtenha uma previsão",
        "onboard_step2": "2️⃣ Ative /live — receba alertas",
        "onboard_step3": "3️⃣ Convide amigos — ganhe bônus",
        "onboard_free_today": "🎁 Hoje {count} previsões grátis!",
        "onboard_try_now": "Tente agora 👇",
        "try_prediction_btn": "🎯 Testar previsão",
        "where_to_bet": "🎰 **Onde apostar:**",
        "bet_partner_text": "Aposte com nosso parceiro 1win — bônus +500% no primeiro depósito!",
        "open_1win_btn": "🎰 Abrir 1win",
        "hot_match_title": "🔥 **JOGO QUENTE!**",
        "hot_match_starts": "⏰ Começa em {hours}h",
        "hot_match_confidence": "📊 Confiança: {percent}%",
        "hot_match_cta": "Aposte agora!",
        "analyze_match_btn": "🔍 Análise do Jogo",
        "day_monday": "Segunda",
        "day_tuesday": "Terça",
        "day_wednesday": "Quarta",
        "day_thursday": "Quinta",
        "day_friday": "Sexta",
        "day_saturday": "Sábado",
        "day_sunday": "Domingo",
        # Monetization placeholders (when disabled)
        "premium_coming_soon": "🚀 Premium em breve!\n\nEstamos trabalhando no sistema de pagamento. Fique ligado!",
        "betting_coming_soon": "🎰 Parceiro de apostas em breve!",
        "place_bet_disabled": "📊 Fazer aposta",
    },
    "es": {
        "welcome": "👋 ¡Hola! Soy un bot de apuestas con IA para fútbol.\n\nUsa el menú o escribe el nombre de un equipo.",
        "top_bets": "🔥 Top Apuestas",
        "matches": "⚽ Partidos",
        "stats": "📊 Estadísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Ajustes",
        "help_btn": "❓ Ayuda",
        "daily_limit": "⚠️ Límite diario alcanzado ({limit} pronósticos).\n\n💎 **¡Acceso ilimitado!**\nHaz un depósito en 1win — obtén premium automáticamente.\n\n👇 Toca el botón abajo:",
        "daily_limit_simple": "⚠️ Límite diario alcanzado ({limit} pronósticos).\n\n💎 **¡Premium próximamente!**\nEstamos trabajando en el sistema de pago.",
        "place_bet": "🎰 Apostar",
        "no_matches": "No se encontraron partidos",
        "analyzing": "🔍 Analizando...",
        "cup_warning": "⚠️ Partido de copa — ¡mayor riesgo!",
        "rotation_warning": "⚠️ Posible rotación",
        "top_club_warning": "⚠️ Club top — no apuestes en contra",
        "unlimited": "🎰 Acceso ilimitado",
        # New translations
        "choose_action": "Elige una acción:",
        "recommendations": "📊 Recomendaciones",
        "today": "📅 Hoy",
        "tomorrow": "📆 Mañana",
        "leagues": "🏆 Ligas",
        "live_alerts": "🔔 Alertas en vivo",
        "help": "❓ Ayuda",
        "matches_today": "📅 **PARTIDOS DE HOY**",
        "matches_tomorrow": "📆 **PARTIDOS DE MAÑANA**",
        "recs_today": "📊 Recomendaciones de hoy",
        "recs_tomorrow": "📊 Recomendaciones de mañana",
        "top_leagues": "🏆 **Top Ligas:**",
        "other_leagues": "🏆 **Otras Ligas:**",
        "more_leagues": "➕ Más ligas",
        "back": "🔙 Atrás",
        "back_to_leagues": "🔙 A ligas",
        "loading": "🔍 Cargando {name}...",
        "no_matches_league": "❌ Sin partidos para {name}",
        "free_predictions": "💎 Gratis: {limit} pronósticos/día",
        "unlimited_deposit": "🔓 Ilimitado: haz un depósito",
        "live_alerts_on": "🔔 **¡Alertas en vivo activadas!**\n\nRevisando partidos cada 10 minutos.\nSi encuentro apuesta 70%+ en 1-3h — ¡te aviso!\n\nEscribe /live para desactivar.",
        "live_alerts_off": "🔕 **Alertas en vivo desactivadas**\n\nEscribe /live para activar.",
        "live_alert_title": "🚨 ¡ALERTA EN VIVO!",
        "in_hours": "En {hours} horas",
        "bet": "⚡ APUESTA:",
        "confidence": "📊 Confianza:",
        "odds": "💰 Cuota:",
        "reason": "📝 Por qué:",
        "first_start_title": "🎉 **¡Bienvenido a AI Betting Bot!**",
        "first_start_text": """🤖 **Qué hace el bot:**
• IA analiza forma, H2H, alineaciones, clima
• Considera clase del equipo, motivación, fatiga
• Estadísticas transparentes — ¡ve la precisión!

🆓 **Gratis:**
• 3 pronósticos por día
• Estadísticas completas
• Alertas para partidos top

⚡ **Cómo empezar:**
Escribe un equipo (ej: *Barcelona*) o toca un botón abajo!""",
        "detected_settings": "🌍 Detecté tus ajustes:",
        "language_label": "Idioma",
        "timezone_label": "Zona horaria",
        "change_in_settings": "Puedes cambiarlo en ajustes",
        # Settings UI
        "admin_only": "⛔ Solo admin",
        "limit_reset": "✅ ¡Límite reiniciado!\n\nUser ID: {user_id}\nDaily requests: 0/{limit}\n\nPuedes hacer {limit} pronósticos nuevos.",
        "premium_removed": "✅ ¡Premium eliminado!\n\nUser ID: {user_id}\nPremium: {premium}\nDaily requests: {requests}/{limit}\n\nEl límite está activo.",
        "select_min_odds": "📉 Selecciona cuota mínima:",
        "min_odds_set": "✅ Cuota mín: {value}",
        "select_max_odds": "📈 Selecciona cuota máxima:",
        "max_odds_set": "✅ Cuota máx: {value}",
        "select_risk": "⚠️ Selecciona nivel de riesgo:",
        "risk_set": "✅ Riesgo: {value}",
        "select_language": "🌍 Selecciona idioma:",
        "select_timezone": "🕐 Selecciona zona horaria:",
        "select_league": "➕ Selecciona liga:",
        "league_added": "✅ ¡{name} añadida!",
        "team_added": "✅ ¡{name} añadido a favoritos!",
        "greeting_response": "👋 ¡Hola! Elige una acción o escribe el nombre del equipo:",
        "upcoming_matches": "⚽ **Próximos partidos:**",
        "analyzing_bets": "🔍 Analizando mejores apuestas...",
        "analysis_error": "❌ Error de análisis.",
        "sure_searching": "🎯 Buscando apuestas seguras (75%+)...",
        "searching_match": "🔍 Buscando partido...",
        "match_not_found": "😕 Partido no encontrado: {query}",
        "available_matches": "📋 **Partidos disponibles:**",
        "match_found": "✅ Encontrado: {home} vs {away}\n🏆 {comp}\n\n⏳ Recopilando estadísticas...",
        "premium_btn": "💎 Premium",
        "no_sure_bets": "❌ No se encontraron apuestas seguras 75%+ para los próximos días.",
        # Referral system
        "referral_btn": "👥 Amigos",
        "referral_title": "👥 **Programa de Referidos**",
        "referral_desc": "¡Invita amigos y gana días de premium!",
        "referral_link": "🔗 **Tu enlace:**",
        "referral_stats": "📊 **Tus estadísticas:**",
        "referral_invited": "Invitados",
        "referral_premium": "Compraron premium",
        "referral_earned": "Días ganados",
        "referral_bonus": "**+{days} días** de premium por amigo referido!",
        "referral_copy": "👆 Toca el enlace para copiar",
        "referral_rules": "📋 **Reglas:**\n• Por cada amigo que compre premium — **+3 días** para ti\n• El bono se otorga automáticamente",
        "referral_welcome": "🎁 ¡Fuiste invitado por un amigo! Obtén un bono al comprar premium.",
        "referral_reminder": "👥 **¡Invita amigos!**\n\n¡Obtén **+3 días** gratis por cada amigo con premium!\n\n🔗 Tu enlace: `{link}`",
        # Streak system
        "streak_title": "🔥 **Tu racha: {days} días!**",
        "streak_bonus": "🎁 Bono de racha: **+{bonus}** precisión!",
        "streak_lost": "😢 ¡Racha perdida! Empieza de nuevo.",
        "streak_record": "🏆 Tu récord: {record} días",
        "streak_milestone": "🎉 **¡{days} días seguidos!** ¡Estás en fuego! 🔥",
        # Social proof
        "social_wins_today": "🏆 **¡{count} usuarios ganaron hoy!**",
        "social_total_wins": "📊 Total de victorias esta semana: **{count}**",
        "social_top_win": "💰 Mejor victoria de hoy: **{odds}x** en {match}!",
        "social_accuracy": "🎯 Precisión semanal: **{accuracy}%**",
        "social_friend_won": "🎉 ¡Tu amigo **{name}** ganó una apuesta!\n\n{match}\n⚡ {bet} @ {odds}\n\n👥 Invita más amigos: /ref",
        # Notifications
        "notif_welcome_back": "👋 ¡Bienvenido de vuelta! Aquí están las mejores apuestas de hoy:",
        "notif_hot_match": "🔥 **¡Partido caliente en {hours}h!**\n\n{match}\n📊 Confianza: {confidence}%",
        "notif_daily_digest": "📊 **Tus estadísticas del día:**\n• Pronósticos: {predictions}\n• Victorias: {wins}\n• Racha: {streak} días 🔥",
        # Premium page
        "premium_title": "💎 **ACCESO PREMIUM**",
        "premium_unlimited": "🎯 Pronósticos ilimitados con 70%+ de precisión",
        "premium_option1_title": "**Opción 1: Depósito en 1win** 🎰",
        "premium_option1_desc": "¡Haz un depósito — obtén premium automáticamente!",
        "premium_option2_title": "**Opción 2: Cripto (USDT/TON)** 💰",
        "premium_option2_crypto": "Elige el plan abajo — paga vía @CryptoBot",
        "premium_option2_manual": "Contacta @{support} para pagar",
        "premium_free_title": "👥 **¡Método gratuito!**",
        "premium_free_desc": "¡Invita amigos — gana **+3 días** por amigo!",
        "premium_earned": "Ya ganaste: **{days} días**",
        "premium_click_below": "Haz clic en el botón abajo 👇",
        "premium_after_payment": "Después del pago — envía captura a @{support}",
        "premium_deposit_btn": "🎰 Depósito en 1win",
        "premium_contact_btn": "💬 Contactar @{support}",
        "premium_friends_btn": "👥 Gratis (invita amigos)",
        "premium_status": "✅ Tienes premium hasta: {date}",
        "friend_fallback": "Amigo",
        # Prediction results
        "pred_result_title": "📊 **Resultado del Pronóstico**",
        "pred_correct": "¡Pronóstico correcto!",
        "pred_incorrect": "Pronóstico fallido",
        "pred_push": "Push (void)",
        "bet_main": "⚡ PRINCIPAL",
        "bet_alt": "📌 ALTERNATIVA",
        # Daily digest
        "daily_digest_title": "☀️ **RESUMEN DEL DÍA**",
        "place_bet_btn": "🎰 Apostar",
        "all_matches_btn": "📅 Todos los partidos",
        # ===== NOTIFICATION SYSTEM =====
        "evening_digest_title": "📊 **RESULTADOS DEL DÍA**",
        "evening_best_bet": "🔥 MEJOR tipo de apuesta hoy:",
        "evening_overall": "📈 Resultado general:",
        "evening_top_match": "🏆 Mejor partido:",
        "evening_tomorrow_count": "Mañana {count} partidos con pronósticos!",
        "evening_cta": "Toca /predict para obtener",
        "morning_alert_title": "⚽ **Hoy {count} partidos top!**",
        "morning_main_match": "🔝 Partido principal del día:",
        "morning_cta": "Pronóstico listo → /predict",
        "inactive_title": "👋 **¡Cuánto tiempo!**",
        "inactive_stats": "Nuestros pronósticos estos días:",
        "inactive_wins": "✅ {wins} victorias de {total} ({percent}%)",
        "inactive_streak": "Mejor racha: {streak} seguidas 🔥",
        "inactive_cta": "Toca /predict — partidos frescos ahí",
        "weekly_title": "📊 **RESULTADOS DE LA SEMANA**",
        "weekly_accuracy": "✅ Precisión: {wins}/{total} ({percent}%)",
        "weekly_best_day": "🔥 Mejor día: {day} ({wins}/{total})",
        "weekly_best_bet_type": "🏆 Mejor tipo de apuesta:",
        "weekly_next_week": "Próxima semana — {count} partidos!",
        "referral_bonus_title": "🎁 **¡BONO DE AMIGOS!**",
        "referral_bonus_desc": "Invita 2 amigos — obtén **3 pronósticos gratis**!",
        "referral_bonus_progress": "📊 Progreso: {current}/2 amigos",
        "referral_bonus_claimed": "🎉 ¡Bono reclamado! +3 pronósticos hoy",
        "referral_bonus_friend_gets": "Tu amigo también recibe 3 pronósticos gratis!",
        "referral_invite_btn": "👥 Invitar amigos",
        "onboard_welcome": "🎉 **¡Bienvenido!**\n\nSoy un bot de apuestas de fútbol con IA con 70%+ de precisión",
        "onboard_step1": "1️⃣ Escribe /predict — obtén un pronóstico",
        "onboard_step2": "2️⃣ Activa /live — recibe alertas",
        "onboard_step3": "3️⃣ Invita amigos — obtén bono",
        "onboard_free_today": "🎁 Hoy {count} pronósticos gratis!",
        "onboard_try_now": "Pruébalo ahora 👇",
        "try_prediction_btn": "🎯 Probar pronóstico",
        "where_to_bet": "🎰 **Dónde apostar:**",
        "bet_partner_text": "Apuesta con nuestro socio 1win — ¡bono +500% en primer depósito!",
        "open_1win_btn": "🎰 Abrir 1win",
        "hot_match_title": "🔥 **¡PARTIDO CALIENTE!**",
        "hot_match_starts": "⏰ Empieza en {hours}h",
        "hot_match_confidence": "📊 Confianza: {percent}%",
        "hot_match_cta": "¡Apuesta ahora!",
        "analyze_match_btn": "🔍 Análisis del Partido",
        "day_monday": "Lunes",
        "day_tuesday": "Martes",
        "day_wednesday": "Miércoles",
        "day_thursday": "Jueves",
        "day_friday": "Viernes",
        "day_saturday": "Sábado",
        "day_sunday": "Domingo",
        # Monetization placeholders (when disabled)
        "premium_coming_soon": "🚀 ¡Premium próximamente!\n\nEstamos trabajando en el sistema de pago. ¡Mantente atento!",
        "betting_coming_soon": "🎰 ¡Socio de apuestas próximamente!",
        "place_bet_disabled": "📊 Hacer apuesta",
    },
    "id": {
        "welcome": "👋 Halo! Saya bot taruhan AI untuk sepak bola.\n\nGunakan menu di bawah atau ketik nama tim.",
        "top_bets": "🔥 Taruhan Top",
        "matches": "⚽ Pertandingan",
        "stats": "📊 Statistik",
        "favorites": "⭐ Favorit",
        "settings": "⚙️ Pengaturan",
        "help_btn": "❓ Bantuan",
        "daily_limit": "⚠️ Batas harian tercapai ({limit} prediksi).\n\n💎 **Akses tak terbatas!**\nLakukan deposit di 1win — dapatkan premium otomatis.\n\n👇 Ketuk tombol di bawah:",
        "daily_limit_simple": "⚠️ Batas harian tercapai ({limit} prediksi).\n\n💎 **Premium segera hadir!**\nKami sedang mengerjakan sistem pembayaran.",
        "place_bet": "🎰 Pasang taruhan",
        "no_matches": "Tidak ada pertandingan",
        "analyzing": "🔍 Menganalisis...",
        "cup_warning": "⚠️ Pertandingan piala — risiko lebih tinggi!",
        "rotation_warning": "⚠️ Kemungkinan rotasi pemain",
        "top_club_warning": "⚠️ Klub top — jangan taruhan melawan",
        "unlimited": "🎰 Akses tak terbatas",
        # New translations
        "choose_action": "Pilih aksi:",
        "recommendations": "📊 Rekomendasi",
        "today": "📅 Hari ini",
        "tomorrow": "📆 Besok",
        "leagues": "🏆 Liga",
        "live_alerts": "🔔 Notifikasi live",
        "help": "❓ Bantuan",
        "matches_today": "📅 **PERTANDINGAN HARI INI**",
        "matches_tomorrow": "📆 **PERTANDINGAN BESOK**",
        "recs_today": "📊 Rekomendasi hari ini",
        "recs_tomorrow": "📊 Rekomendasi besok",
        "top_leagues": "🏆 **Liga Top:**",
        "other_leagues": "🏆 **Liga Lainnya:**",
        "more_leagues": "➕ Liga lainnya",
        "back": "🔙 Kembali",
        "back_to_leagues": "🔙 Ke liga",
        "loading": "🔍 Memuat {name}...",
        "no_matches_league": "❌ Tidak ada pertandingan untuk {name}",
        "free_predictions": "💎 Gratis: {limit} prediksi/hari",
        "unlimited_deposit": "🔓 Tak terbatas: lakukan deposit melalui link",
        "live_alerts_on": "🔔 **Notifikasi live aktif!**\n\nMemeriksa pertandingan setiap 10 menit.\nJika menemukan taruhan 70%+ dalam 1-3 jam — akan dikirim notifikasi!\n\nKetik /live untuk menonaktifkan.",
        "live_alerts_off": "🔕 **Notifikasi live dinonaktifkan**\n\nKetik /live untuk mengaktifkan lagi.",
        "live_alert_title": "🚨 NOTIFIKASI LIVE!",
        "in_hours": "Dalam {hours} jam",
        "bet": "⚡ TARUHAN:",
        "confidence": "📊 Keyakinan:",
        "odds": "💰 Odds:",
        "reason": "📝 Alasan:",
        "first_start_title": "🎉 **Selamat datang di AI Betting Bot!**",
        "first_start_text": """🤖 **Yang dilakukan bot:**
• AI menganalisis form, H2H, lineup, cuaca
• Pertimbangkan kelas tim, motivasi, kelelahan
• Statistik transparan — lihat akurasinya!

🆓 **Gratis:**
• 3 prediksi per hari
• Statistik lengkap
• Alert untuk pertandingan top

⚡ **Cara mulai:**
Ketik nama tim (misal: *Barcelona*) atau tap tombol di bawah!""",
        "detected_settings": "🌍 Pengaturan terdeteksi:",
        "language_label": "Bahasa",
        "timezone_label": "Zona waktu",
        "change_in_settings": "Anda dapat mengubahnya di pengaturan",
        # Settings UI
        "admin_only": "⛔ Khusus admin",
        "limit_reset": "✅ Batas direset!\n\nUser ID: {user_id}\nPermintaan harian: 0/{limit}\n\nAnda dapat membuat {limit} prediksi baru.",
        "premium_removed": "✅ Status premium dihapus!\n\nUser ID: {user_id}\nPremium: {premium}\nPermintaan harian: {requests}/{limit}\n\nBatas sekarang aktif.",
        "select_min_odds": "📉 Pilih odds minimum:",
        "min_odds_set": "✅ Odds min: {value}",
        "select_max_odds": "📈 Pilih odds maksimum:",
        "max_odds_set": "✅ Odds maks: {value}",
        "select_risk": "⚠️ Pilih tingkat risiko:",
        "risk_set": "✅ Risiko: {value}",
        "select_language": "🌍 Pilih bahasa:",
        "select_timezone": "🕐 Pilih zona waktu:",
        "select_league": "➕ Pilih liga:",
        "league_added": "✅ {name} ditambahkan!",
        "team_added": "✅ {name} ditambahkan ke favorit!",
        "greeting_response": "👋 Halo! Pilih aksi atau ketik nama tim:",
        "upcoming_matches": "⚽ **Pertandingan mendatang:**",
        "analyzing_bets": "🔍 Menganalisis taruhan terbaik...",
        "analysis_error": "❌ Error analisis.",
        "sure_searching": "🎯 Mencari taruhan pasti (75%+)...",
        "searching_match": "🔍 Mencari pertandingan...",
        "match_not_found": "😕 Pertandingan tidak ditemukan: {query}",
        "available_matches": "📋 **Pertandingan tersedia:**",
        "match_found": "✅ Ditemukan: {home} vs {away}\n🏆 {comp}\n\n⏳ Mengumpulkan statistik...",
        "premium_btn": "💎 Premium",
        "no_sure_bets": "❌ Tidak ada taruhan pasti 75%+ untuk hari-hari mendatang.",
        # Referral system
        "referral_btn": "👥 Teman",
        "referral_title": "👥 **Program Referral**",
        "referral_desc": "Undang teman dan dapatkan hari premium bonus!",
        "referral_link": "🔗 **Link Anda:**",
        "referral_stats": "📊 **Statistik Anda:**",
        "referral_invited": "Diundang",
        "referral_premium": "Beli premium",
        "referral_earned": "Hari diperoleh",
        "referral_bonus": "**+{days} hari** premium untuk teman yang direferensikan!",
        "referral_copy": "👆 Ketuk link untuk menyalin",
        "referral_rules": "📋 **Aturan:**\n• Untuk setiap teman yang membeli premium — **+3 hari** untuk Anda\n• Bonus diberikan otomatis",
        "referral_welcome": "🎁 Anda diundang oleh teman! Dapatkan bonus saat membeli premium.",
        "referral_reminder": "👥 **Undang teman!**\n\nDapatkan **+3 hari** gratis untuk setiap teman dengan premium!\n\n🔗 Link Anda: `{link}`",
        # Streak system
        "streak_title": "🔥 **Streak Anda: {days} hari!**",
        "streak_bonus": "🎁 Bonus streak: **+{bonus}** akurasi prediksi!",
        "streak_lost": "😢 Streak hilang! Mulai lagi.",
        "streak_record": "🏆 Rekor Anda: {record} hari",
        "streak_milestone": "🎉 **{days} hari berturut-turut!** Anda luar biasa! 🔥",
        # Social proof
        "social_wins_today": "🏆 **{count} pengguna menang hari ini!**",
        "social_total_wins": "📊 Total kemenangan minggu ini: **{count}**",
        "social_top_win": "💰 Kemenangan terbaik hari ini: **{odds}x** di {match}!",
        "social_accuracy": "🎯 Akurasi prediksi mingguan: **{accuracy}%**",
        "social_friend_won": "🎉 Teman Anda **{name}** menang taruhan!\n\n{match}\n⚡ {bet} @ {odds}\n\n👥 Undang lebih banyak teman: /ref",
        # Notifications
        "notif_welcome_back": "👋 Selamat datang kembali! Ini taruhan top hari ini:",
        "notif_hot_match": "🔥 **Pertandingan panas dalam {hours} jam!**\n\n{match}\n📊 Keyakinan: {confidence}%",
        "notif_daily_digest": "📊 **Statistik harian Anda:**\n• Prediksi: {predictions}\n• Kemenangan: {wins}\n• Streak: {streak} hari 🔥",
        # Premium page
        "premium_title": "💎 **AKSES PREMIUM**",
        "premium_unlimited": "🎯 Prediksi tak terbatas dengan akurasi 70%+",
        "premium_option1_title": "**Opsi 1: Deposit di 1win** 🎰",
        "premium_option1_desc": "Lakukan deposit — dapatkan premium otomatis!",
        "premium_option2_title": "**Opsi 2: Kripto (USDT/TON)** 💰",
        "premium_option2_crypto": "Pilih paket di bawah — bayar via @CryptoBot",
        "premium_option2_manual": "Hubungi @{support} untuk membayar",
        "premium_free_title": "👥 **Cara gratis!**",
        "premium_free_desc": "Undang teman — dapatkan **+3 hari** per teman!",
        "premium_earned": "Sudah diperoleh: **{days} hari**",
        "premium_click_below": "Klik tombol di bawah 👇",
        "premium_after_payment": "Setelah pembayaran — kirim screenshot ke @{support}",
        "premium_deposit_btn": "🎰 Deposit di 1win",
        "premium_contact_btn": "💬 Hubungi @{support}",
        "premium_friends_btn": "👥 Gratis (undang teman)",
        "premium_status": "✅ Anda memiliki premium hingga: {date}",
        "friend_fallback": "Teman",
        # Prediction results
        "pred_result_title": "📊 **Hasil Prediksi**",
        "pred_correct": "Prediksi benar!",
        "pred_incorrect": "Prediksi gagal",
        "pred_push": "Push (void)",
        "bet_main": "⚡ UTAMA",
        "bet_alt": "📌 ALTERNATIF",
        # Daily digest
        "daily_digest_title": "☀️ **RINGKASAN HARI INI**",
        "place_bet_btn": "🎰 Pasang taruhan",
        "all_matches_btn": "📅 Semua pertandingan",
        # ===== NOTIFICATION SYSTEM =====
        "evening_digest_title": "📊 **HASIL HARI INI**",
        "evening_best_bet": "🔥 TIPE taruhan TERBAIK hari ini:",
        "evening_overall": "📈 Hasil keseluruhan:",
        "evening_top_match": "🏆 Pertandingan top:",
        "evening_tomorrow_count": "Besok {count} pertandingan dengan prediksi!",
        "evening_cta": "Ketuk /predict untuk mendapatkan",
        "morning_alert_title": "⚽ **Hari ini {count} pertandingan top!**",
        "morning_main_match": "🔝 Pertandingan utama hari ini:",
        "morning_cta": "Prediksi siap → /predict",
        "inactive_title": "👋 **Lama tidak berjumpa!**",
        "inactive_stats": "Prediksi kami beberapa hari ini:",
        "inactive_wins": "✅ {wins} kemenangan dari {total} ({percent}%)",
        "inactive_streak": "Streak terbaik: {streak} berturut-turut 🔥",
        "inactive_cta": "Ketuk /predict — pertandingan segar di sana",
        "weekly_title": "📊 **HASIL MINGGU INI**",
        "weekly_accuracy": "✅ Akurasi: {wins}/{total} ({percent}%)",
        "weekly_best_day": "🔥 Hari terbaik: {day} ({wins}/{total})",
        "weekly_best_bet_type": "🏆 Tipe taruhan terbaik:",
        "weekly_next_week": "Minggu depan — {count} pertandingan!",
        "referral_bonus_title": "🎁 **BONUS TEMAN!**",
        "referral_bonus_desc": "Undang 2 teman — dapatkan **3 prediksi gratis**!",
        "referral_bonus_progress": "📊 Progress: {current}/2 teman",
        "referral_bonus_claimed": "🎉 Bonus diklaim! +3 prediksi hari ini",
        "referral_bonus_friend_gets": "Temanmu juga dapat 3 prediksi gratis!",
        "referral_invite_btn": "👥 Undang teman",
        "onboard_welcome": "🎉 **Selamat datang!**\n\nSaya bot taruhan sepak bola AI dengan akurasi 70%+",
        "onboard_step1": "1️⃣ Ketik /predict — dapatkan prediksi",
        "onboard_step2": "2️⃣ Aktifkan /live — terima notifikasi",
        "onboard_step3": "3️⃣ Undang teman — dapatkan bonus",
        "onboard_free_today": "🎁 Hari ini {count} prediksi gratis!",
        "onboard_try_now": "Coba sekarang 👇",
        "try_prediction_btn": "🎯 Coba prediksi",
        "where_to_bet": "🎰 **Di mana bertaruh:**",
        "bet_partner_text": "Taruhan dengan mitra kami 1win — bonus +500% pada deposit pertama!",
        "open_1win_btn": "🎰 Buka 1win",
        "hot_match_title": "🔥 **PERTANDINGAN PANAS!**",
        "hot_match_starts": "⏰ Mulai dalam {hours}j",
        "hot_match_confidence": "📊 Kepercayaan: {percent}%",
        "hot_match_cta": "Taruhan sekarang!",
        "analyze_match_btn": "🔍 Analisis Pertandingan",
        "day_monday": "Senin",
        "day_tuesday": "Selasa",
        "day_wednesday": "Rabu",
        "day_thursday": "Kamis",
        "day_friday": "Jumat",
        "day_saturday": "Sabtu",
        "day_sunday": "Minggu",
        # Monetization placeholders (when disabled)
        "premium_coming_soon": "🚀 Premium segera hadir!\n\nKami sedang mengerjakan sistem pembayaran. Nantikan!",
        "betting_coming_soon": "🎰 Mitra taruhan segera hadir!",
        "place_bet_disabled": "📊 Pasang taruhan",
    }
}

def get_text(key, lang="ru"):
    """Get translated text"""
    if lang in TRANSLATIONS and key in TRANSLATIONS[lang]:
        return TRANSLATIONS[lang][key]
    return TRANSLATIONS["ru"].get(key, key)

def get_main_keyboard(lang="ru"):
    """Get main reply keyboard - always visible at bottom"""
    keyboard = [
        [KeyboardButton(get_text("top_bets", lang)), KeyboardButton(get_text("matches", lang))],
        [KeyboardButton(get_text("stats", lang)), KeyboardButton(get_text("favorites", lang))],
        [KeyboardButton(get_text("premium_btn", lang)), KeyboardButton(get_text("settings", lang))],
        [KeyboardButton(get_text("help_btn", lang))]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_limit_text(lang: str = "ru") -> str:
    """Get daily limit text - shows simple version without 1win when monetization disabled."""
    if MONETIZATION_ENABLED:
        return get_limit_text(lang)
    else:
        return get_text("daily_limit_simple", lang).format(limit=FREE_DAILY_LIMIT)


def get_bet_button(user_id: int, lang: str = "ru", text_key: str = "place_bet") -> list:
    """Get betting button row based on MONETIZATION_ENABLED.
    Returns empty list if monetization is disabled.
    text_key: translation key for button text (default: "place_bet", alt: "place_bet_btn")
    """
    if not MONETIZATION_ENABLED:
        return []  # No button when disabled

    affiliate_url = get_affiliate_link(user_id)
    if affiliate_url:
        return [InlineKeyboardButton(get_text(text_key, lang), url=affiliate_url)]
    return []


def get_premium_buttons(user_id: int, lang: str = "ru") -> list:
    """Get premium/1win buttons based on MONETIZATION_ENABLED.
    Returns empty list if monetization is disabled.
    """
    if not MONETIZATION_ENABLED:
        return []  # No buttons when disabled

    affiliate_url = get_affiliate_link(user_id)
    if affiliate_url:
        return [
            InlineKeyboardButton(get_text("open_1win_btn", lang), url=affiliate_url),
            InlineKeyboardButton("💳 Crypto", callback_data="cmd_premium")
        ]
    return []


# Timezone mapping by language/country code
LANGUAGE_TIMEZONE_MAP = {
    "ru": "Europe/Moscow",
    "uk": "Europe/Kiev",
    "en": "Europe/London",
    "en-US": "America/New_York",
    "en-GB": "Europe/London",
    "pt": "America/Sao_Paulo",
    "pt-BR": "America/Sao_Paulo",
    "pt-PT": "Europe/Lisbon",
    "es": "Europe/Madrid",
    "es-MX": "America/Mexico_City",
    "es-AR": "America/Argentina/Buenos_Aires",
    "de": "Europe/Berlin",
    "fr": "Europe/Paris",
    "it": "Europe/Rome",
    "tr": "Europe/Istanbul",
    "ar": "Asia/Dubai",
    "hi": "Asia/Kolkata",
    "id": "Asia/Jakarta",
    "zh": "Asia/Shanghai",
    "ja": "Asia/Tokyo",
    "ko": "Asia/Seoul",
}

# Language names for display
LANGUAGE_NAMES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "pt": "🇧🇷 Português",
    "es": "🇪🇸 Español",
    "id": "🇮🇩 Indonesia",
}


def detect_timezone(user) -> str:
    """Detect timezone from Telegram language_code"""
    lang_code = user.language_code or "ru"

    # Try exact match first (e.g., en-US, pt-BR)
    if lang_code in LANGUAGE_TIMEZONE_MAP:
        return LANGUAGE_TIMEZONE_MAP[lang_code]

    # Try base language (e.g., en, pt)
    base_lang = lang_code.split("-")[0] if "-" in lang_code else lang_code
    return LANGUAGE_TIMEZONE_MAP.get(base_lang, "Europe/Moscow")


def detect_language(user) -> str:
    """Detect user language from Telegram settings"""
    lang_code = user.language_code or "ru"
    if lang_code.startswith("pt"):
        return "pt"
    elif lang_code.startswith("es"):
        return "es"
    elif lang_code.startswith("en"):
        return "en"
    return "ru"


# ===== TIMEZONES =====

TIMEZONES = {
    "msk": ("Europe/Moscow", "🇷🇺 Москва (MSK)"),
    "kiev": ("Europe/Kiev", "🇺🇦 Киев (EET)"),
    "london": ("Europe/London", "🇬🇧 Лондон (GMT)"),
    "paris": ("Europe/Paris", "🇫🇷 Париж (CET)"),
    "istanbul": ("Europe/Istanbul", "🇹🇷 Стамбул (TRT)"),
    "dubai": ("Asia/Dubai", "🇦🇪 Дубай (GST)"),
    "mumbai": ("Asia/Kolkata", "🇮🇳 Мумбаи (IST)"),
    "jakarta": ("Asia/Jakarta", "🇮🇩 Джакарта (WIB)"),
    "manila": ("Asia/Manila", "🇵🇭 Манила (PHT)"),
    "sao_paulo": ("America/Sao_Paulo", "🇧🇷 Сан-Паулу (BRT)"),
    "lagos": ("Africa/Lagos", "🇳🇬 Лагос (WAT)"),
    "new_york": ("America/New_York", "🇺🇸 Нью-Йорк (EST)"),
}

def convert_utc_to_user_tz(utc_time_str, user_tz="Europe/Moscow"):
    """Convert UTC time string to user's timezone"""
    try:
        # Parse UTC time
        if utc_time_str.endswith("Z"):
            utc_time_str = utc_time_str[:-1] + "+00:00"
        
        utc_dt = datetime.fromisoformat(utc_time_str)
        
        # If naive datetime, assume UTC
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        
        # Convert to user timezone
        user_zone = ZoneInfo(user_tz)
        local_dt = utc_dt.astimezone(user_zone)
        
        return local_dt.strftime("%H:%M")
    except Exception as e:
        logger.error(f"Timezone conversion error: {e}")
        # Fallback to UTC
        try:
            dt = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
            return dt.strftime("%H:%M") + " UTC"
        except:
            return "?"

def get_tz_offset_str(user_tz="Europe/Moscow"):
    """Get timezone offset string like +3, -5, etc."""
    try:
        now = datetime.now(ZoneInfo(user_tz))
        offset = now.utcoffset()
        hours = int(offset.total_seconds() // 3600)
        return f"UTC{'+' if hours >= 0 else ''}{hours}"
    except:
        return "UTC"


def format_match_datetime(utc_date_str: str, user_tz: str = "Europe/Moscow", lang: str = "ru") -> str:
    """Format match datetime for user's timezone.

    Args:
        utc_date_str: ISO format UTC datetime string (from API)
        user_tz: User's timezone string
        lang: Language for day/month names

    Returns:
        Formatted string like "📅 Сегодня 19:30" or "📅 15 дек, 21:00"
    """
    try:
        # Parse UTC time
        utc_time = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))

        # Convert to user timezone
        user_zone = ZoneInfo(user_tz)
        local_time = utc_time.astimezone(user_zone)
        now_local = datetime.now(user_zone)

        # Format time
        time_str = local_time.strftime("%H:%M")

        # Check if today/tomorrow
        if local_time.date() == now_local.date():
            day_label = {"ru": "Сегодня", "en": "Today", "pt": "Hoje", "es": "Hoy", "id": "Hari ini"}.get(lang, "Today")
            return f"📅 {day_label} {time_str}"
        elif local_time.date() == (now_local + timedelta(days=1)).date():
            day_label = {"ru": "Завтра", "en": "Tomorrow", "pt": "Amanhã", "es": "Mañana", "id": "Besok"}.get(lang, "Tomorrow")
            return f"📅 {day_label} {time_str}"
        else:
            # Format with date
            month_names = {
                "ru": ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
                "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                "pt": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
                "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
                "id": ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
            }
            months = month_names.get(lang, month_names["en"])
            month = months[local_time.month - 1]
            return f"📅 {local_time.day} {month}, {time_str}"
    except Exception as e:
        logger.warning(f"Error formatting match time: {e}")
        return ""


# ===== DATABASE =====

def get_db_connection(timeout: int = 30):
    """Get database connection with proper settings to avoid locking."""
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
    conn.execute("PRAGMA busy_timeout=30000")  # 30 second timeout
    return conn


def init_db():
    """Initialize SQLite database"""
    conn = get_db_connection()
    c = conn.cursor()
    
    # Users table with daily usage tracking
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        min_odds REAL DEFAULT 1.3,
        max_odds REAL DEFAULT 3.0,
        risk_level TEXT DEFAULT 'medium',
        language TEXT DEFAULT 'ru',
        is_premium INTEGER DEFAULT 0,
        daily_requests INTEGER DEFAULT 0,
        last_request_date TEXT,
        timezone TEXT DEFAULT 'Europe/Moscow'
    )''')
    
    # Favorite teams
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        team_name TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
    # Favorite leagues
    c.execute('''CREATE TABLE IF NOT EXISTS favorite_leagues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        league_code TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
    # Predictions tracking with bet categories
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        match_id INTEGER,
        home_team TEXT,
        away_team TEXT,
        bet_type TEXT,
        bet_category TEXT,
        confidence INTEGER,
        odds REAL,
        predicted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        result TEXT,
        is_correct INTEGER,
        checked_at TIMESTAMP,
        ml_features_json TEXT,
        expected_value REAL,
        stake_percent REAL,
        profit REAL,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')

    # Live alert subscribers (persistent storage)
    c.execute('''CREATE TABLE IF NOT EXISTS live_subscribers (
        user_id INTEGER PRIMARY KEY,
        subscribed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ML training data table
    c.execute('''CREATE TABLE IF NOT EXISTS ml_training_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        bet_category TEXT,
        features_json TEXT,
        target INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (prediction_id) REFERENCES predictions(id)
    )''')

    # ML model metadata
    c.execute('''CREATE TABLE IF NOT EXISTS ml_models (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_type TEXT,
        accuracy REAL,
        samples_count INTEGER,
        trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        model_path TEXT
    )''')

    # Confidence calibration - tracks predicted vs actual accuracy
    c.execute('''CREATE TABLE IF NOT EXISTS confidence_calibration (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bet_category TEXT,
        confidence_band TEXT,
        predicted_count INTEGER DEFAULT 0,
        actual_wins INTEGER DEFAULT 0,
        calibration_factor REAL DEFAULT 1.0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Learning patterns - what works and what doesn't
    c.execute('''CREATE TABLE IF NOT EXISTS learning_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pattern_type TEXT,
        pattern_key TEXT UNIQUE,
        pattern_data TEXT,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Learning log - track what the system learned
    c.execute('''CREATE TABLE IF NOT EXISTS learning_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT,
        description TEXT,
        data_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Odds history - for line movement tracking
    c.execute('''CREATE TABLE IF NOT EXISTS odds_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_key TEXT,
        bookmaker TEXT,
        market TEXT,
        outcome TEXT,
        odds REAL,
        recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # User personalization stats
    c.execute('''CREATE TABLE IF NOT EXISTS user_bet_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        bet_category TEXT,
        total_bets INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        avg_odds REAL DEFAULT 1.5,
        roi REAL DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, bet_category)
    )''')

    # Add new columns if they don't exist (for migration)
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN bet_category TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN daily_requests INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_request_date TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN timezone TEXT DEFAULT 'Europe/Moscow'")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN live_alerts INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN exclude_cups INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN bet_rank INTEGER DEFAULT 1")
    except:
        pass
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN league_code TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN ml_features_json TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN premium_expires TEXT")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN source TEXT DEFAULT 'organic'")
    except:
        pass
    try:
        c.execute("ALTER TABLE ml_training_data ADD COLUMN bet_rank INTEGER DEFAULT 1")
    except:
        pass

    # ROI tracking columns
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN expected_value REAL")
    except:
        pass
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN stake_percent REAL")
    except:
        pass
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN profit REAL")
    except:
        pass

    # ROI analytics table - tracks profitability by category and conditions
    c.execute('''CREATE TABLE IF NOT EXISTS roi_analytics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bet_category TEXT,
        condition_key TEXT,
        total_bets INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        total_staked REAL DEFAULT 0,
        total_returned REAL DEFAULT 0,
        roi_percent REAL DEFAULT 0,
        avg_odds REAL DEFAULT 0,
        avg_ev REAL DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(bet_category, condition_key)
    )''')

    # Coach history - for automatic new coach detection
    c.execute('''CREATE TABLE IF NOT EXISTS coach_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id INTEGER,
        team_name TEXT,
        coach_id INTEGER,
        coach_name TEXT,
        contract_start TEXT,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        matches_tracked INTEGER DEFAULT 0,
        UNIQUE(team_id, coach_id)
    )''')

    # 1win deposits tracking
    c.execute('''CREATE TABLE IF NOT EXISTS deposits_1win (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        onewin_user_id TEXT,
        amount REAL,
        currency TEXT DEFAULT 'BRL',
        event TEXT,
        transaction_id TEXT UNIQUE,
        country TEXT,
        premium_days INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')

    # CryptoBot payments tracking
    c.execute('''CREATE TABLE IF NOT EXISTS crypto_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        invoice_id TEXT UNIQUE,
        amount REAL,
        currency TEXT,
        days INTEGER,
        status TEXT DEFAULT 'pending',
        paid_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')

    # Referrals tracking
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER UNIQUE,
        bonus_granted INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (referrer_id) REFERENCES users(user_id),
        FOREIGN KEY (referred_id) REFERENCES users(user_id)
    )''')

    # Add referred_by column to users table
    try:
        c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    except:
        pass

    # Add streak columns
    try:
        c.execute("ALTER TABLE users ADD COLUMN streak_days INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN streak_record INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_streak_date TEXT")
    except:
        pass

    # Referral bonus system - 2 friends = 3 free predictions
    try:
        c.execute("ALTER TABLE users ADD COLUMN referral_bonus_claimed INTEGER DEFAULT 0")
    except:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN bonus_predictions INTEGER DEFAULT 0")
    except:
        pass

    # Pending UTM sources - stores UTM before user is created
    c.execute('''CREATE TABLE IF NOT EXISTS pending_utm (
        user_id INTEGER PRIMARY KEY,
        utm_source TEXT,
        referrer_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Prediction errors analysis - stores WHY predictions failed for learning
    c.execute('''CREATE TABLE IF NOT EXISTS prediction_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        league_code TEXT,
        bet_category TEXT,
        error_type TEXT,
        expected_value REAL,
        actual_value REAL,
        error_description TEXT,
        features_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (prediction_id) REFERENCES predictions(id)
    )''')

    # League learning stats - tracks accuracy and lessons per league
    c.execute('''CREATE TABLE IF NOT EXISTS league_learning (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        league_code TEXT,
        bet_category TEXT,
        total_predictions INTEGER DEFAULT 0,
        correct_predictions INTEGER DEFAULT 0,
        common_error_type TEXT,
        adjustment_factor REAL DEFAULT 1.0,
        lessons_json TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(league_code, bet_category)
    )''')

    # Feature-based error patterns - learns WHEN specific bets fail
    # Key insight: "П1 fails when injuries > 8 AND away_position < home_position"
    c.execute('''CREATE TABLE IF NOT EXISTS feature_error_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bet_category TEXT,
        feature_condition TEXT,
        condition_key TEXT UNIQUE,
        total_predictions INTEGER DEFAULT 0,
        wins INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        avg_confidence_when_failed REAL,
        suggested_adjustment INTEGER DEFAULT 0,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Smart adjustments - learned rules for confidence correction
    c.execute('''CREATE TABLE IF NOT EXISTS smart_adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bet_category TEXT,
        condition_type TEXT,
        condition_value TEXT,
        adjustment_percent INTEGER,
        sample_size INTEGER DEFAULT 0,
        win_rate REAL,
        is_active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(bet_category, condition_type, condition_value)
    )''')

    # Key players - impact scores for important players (auto-updated from API)
    c.execute('''CREATE TABLE IF NOT EXISTS key_players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_id INTEGER,
        team_name TEXT,
        player_id INTEGER,
        player_name TEXT,
        position TEXT,
        impact_attack INTEGER DEFAULT 0,
        impact_defense INTEGER DEFAULT 0,
        impact_creativity INTEGER DEFAULT 0,
        impact_goals INTEGER DEFAULT 0,
        goals_season INTEGER DEFAULT 0,
        assists_season INTEGER DEFAULT 0,
        minutes_played INTEGER DEFAULT 0,
        market_value TEXT,
        is_key_player BOOLEAN DEFAULT 1,
        league_code TEXT,
        -- Flat track bully tracking (goals vs strong/weak opponents)
        goals_vs_top6 INTEGER DEFAULT 0,
        goals_vs_mid INTEGER DEFAULT 0,
        goals_vs_bottom6 INTEGER DEFAULT 0,
        games_vs_top6 INTEGER DEFAULT 0,
        games_vs_mid INTEGER DEFAULT 0,
        games_vs_bottom6 INTEGER DEFAULT 0,
        is_big_game_player BOOLEAN DEFAULT 0,
        is_flat_track_bully BOOLEAN DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(team_id, player_id)
    )''')

    # Player performance history - tracks individual match performances
    c.execute('''CREATE TABLE IF NOT EXISTS player_match_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        player_name TEXT,
        team_id INTEGER,
        match_id INTEGER,
        opponent_id INTEGER,
        opponent_name TEXT,
        opponent_position INTEGER,
        opponent_class TEXT,
        goals INTEGER DEFAULT 0,
        assists INTEGER DEFAULT 0,
        minutes INTEGER DEFAULT 0,
        match_date DATE,
        league_code TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(player_id, match_id)
    )''')

    # Ensemble ML models - stores multiple model types for voting
    c.execute('''CREATE TABLE IF NOT EXISTS ensemble_models (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        model_name TEXT,
        model_type TEXT,
        bet_category TEXT,
        accuracy REAL,
        precision_score REAL,
        recall_score REAL,
        f1_score REAL,
        samples_count INTEGER,
        feature_importance TEXT,
        trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        model_path TEXT,
        is_active BOOLEAN DEFAULT 1,
        UNIQUE(model_name, bet_category)
    )''')

    conn.commit()
    conn.close()

    # Run migrations for existing databases
    migrate_database()

    logger.info("Database initialized")


def migrate_database():
    """Add missing columns to existing tables (for database upgrades)"""
    conn = get_db_connection()
    c = conn.cursor()

    # Check and add missing columns to key_players table (for flat track bully tracking)
    key_players_columns = [
        ("goals_vs_top6", "INTEGER DEFAULT 0"),
        ("goals_vs_mid", "INTEGER DEFAULT 0"),
        ("goals_vs_bottom6", "INTEGER DEFAULT 0"),
        ("games_vs_top6", "INTEGER DEFAULT 0"),
        ("games_vs_mid", "INTEGER DEFAULT 0"),
        ("games_vs_bottom6", "INTEGER DEFAULT 0"),
        ("is_big_game_player", "BOOLEAN DEFAULT 0"),
        ("is_flat_track_bully", "BOOLEAN DEFAULT 0"),
    ]

    for col_name, col_type in key_players_columns:
        try:
            c.execute(f"ALTER TABLE key_players ADD COLUMN {col_name} {col_type}")
            logger.info(f"Added column {col_name} to key_players")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Remove duplicate favorite teams (keep only first entry)
    try:
        c.execute("""
            DELETE FROM favorite_teams
            WHERE id NOT IN (
                SELECT MIN(id) FROM favorite_teams GROUP BY user_id, team_name
            )
        """)
        deleted_teams = c.rowcount
        if deleted_teams > 0:
            logger.info(f"Removed {deleted_teams} duplicate favorite teams")
    except Exception as e:
        logger.warning(f"Could not clean favorite_teams duplicates: {e}")

    # Remove duplicate favorite leagues (keep only first entry)
    try:
        c.execute("""
            DELETE FROM favorite_leagues
            WHERE id NOT IN (
                SELECT MIN(id) FROM favorite_leagues GROUP BY user_id, league_code
            )
        """)
        deleted_leagues = c.rowcount
        if deleted_leagues > 0:
            logger.info(f"Removed {deleted_leagues} duplicate favorite leagues")
    except Exception as e:
        logger.warning(f"Could not clean favorite_leagues duplicates: {e}")

    # Add match_time column to predictions for smart result checking
    try:
        c.execute("ALTER TABLE predictions ADD COLUMN match_time TEXT")
        logger.info("Added match_time column to predictions")
    except sqlite3.OperationalError:
        pass  # Column already exists

    conn.commit()
    conn.close()


def get_user(user_id):
    """Get user settings"""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row  # Read by column names
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        # Convert to dict for safe access
        data = dict(row)
        return {
            "user_id": data.get("user_id"),
            "username": data.get("username"),
            "min_odds": data.get("min_odds", 1.3),
            "max_odds": data.get("max_odds", 3.0),
            "risk_level": data.get("risk_level", "medium"),
            "language": data.get("language", "ru"),
            "is_premium": data.get("is_premium", 0),
            "daily_requests": data.get("daily_requests", 0),
            "last_request_date": data.get("last_request_date"),
            "timezone": data.get("timezone", "Europe/Moscow"),
            "exclude_cups": data.get("exclude_cups", 0)
        }
    return None

def save_pending_utm(user_id: int, utm_source: str, referrer_id: int = None):
    """Save UTM source for user before they complete registration.
    This persists UTM even if bot restarts between /start and language selection."""
    if utm_source == "organic" and referrer_id is None:
        return  # Don't save default organic

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO pending_utm (user_id, utm_source, referrer_id, created_at)
                 VALUES (?, ?, ?, datetime('now'))""",
              (user_id, utm_source, referrer_id))
    conn.commit()
    conn.close()
    logger.info(f"Saved pending UTM for {user_id}: {utm_source}, ref={referrer_id}")


def get_pending_utm(user_id: int) -> dict:
    """Get pending UTM data for user."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT utm_source, referrer_id FROM pending_utm WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()

    if row:
        return {"utm_source": row[0] or "organic", "referrer_id": row[1]}
    return {"utm_source": "organic", "referrer_id": None}


def delete_pending_utm(user_id: int):
    """Delete pending UTM after user is created."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM pending_utm WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def create_user(user_id, username=None, language="ru", source=None):
    """Create new user. Returns True if new user created, False if already exists.
    If source is None, checks pending_utm table for stored UTM source."""
    conn = get_db_connection()
    c = conn.cursor()

    # Check if user already exists
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = c.fetchone() is not None

    if not exists:
        # If source not explicitly provided, check pending_utm
        if source is None:
            pending = get_pending_utm(user_id)
            source = pending["utm_source"]

        c.execute("INSERT INTO users (user_id, username, language, source) VALUES (?, ?, ?, ?)",
                  (user_id, username, language, source))
        conn.commit()

        # Clean up pending UTM
        delete_pending_utm(user_id)

    conn.close()

    return not exists  # True if new user was created


async def notify_admins_new_user(bot, user_id: int, username: str = None, language: str = "ru", source: str = "organic"):
    """Send notification to all admins about new user registration."""
    if not ADMIN_IDS:
        return

    # Get total user count
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        conn.close()
    except:
        total_users = "?"

    # Build notification message
    username_display = f"@{username}" if username else "—"
    source_emoji = {
        "organic": "🌱",
        "referral": "👥",
        "ads": "📢",
        "1win": "🎰"
    }.get(source, "📥")

    lang_flag = {
        "ru": "🇷🇺",
        "en": "🇬🇧",
        "pt": "🇧🇷",
        "es": "🇪🇸",
        "id": "🇮🇩"
    }.get(language, "🌍")

    message = f"""🆕 **Новый пользователь!**

👤 ID: `{user_id}`
📛 Username: {username_display}
{lang_flag} Язык: {language}
{source_emoji} Источник: {source}

📊 Всего пользователей: **{total_users}**"""

    # Send to all admins
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} about new user: {e}")

# Whitelist of allowed settings fields (prevents SQL injection)
ALLOWED_USER_SETTINGS = frozenset({
    'min_odds', 'max_odds', 'risk_level', 'language',
    'is_premium', 'daily_requests', 'last_request_date', 'timezone',
    'exclude_cups'
})

def update_user_settings(user_id: int, **kwargs) -> None:
    """Update user settings (SQL injection safe)"""
    conn = get_db_connection()
    c = conn.cursor()

    for key, value in kwargs.items():
        # Only allow whitelisted fields
        if key in ALLOWED_USER_SETTINGS:
            # Use parameterized query with validated column name
            query = f"UPDATE users SET {key} = ? WHERE user_id = ?"
            c.execute(query, (value, user_id))

    conn.commit()
    conn.close()

def check_daily_limit(user_id):
    """Check if user has reached daily limit. Returns (can_use, remaining, use_bonus)
    use_bonus is True if we should consume a bonus prediction instead of daily limit"""
    logger.info(f"check_daily_limit called for user {user_id}")

    user = get_user(user_id)
    if not user:
        logger.info(f"User {user_id} not found in DB, allowing request")
        return True, FREE_DAILY_LIMIT, False

    # Check premium status (including expiry)
    if user.get("is_premium", 0):
        # Verify premium hasn't expired
        expired = check_premium_expired(user_id)
        if not expired:
            logger.info(f"User {user_id} is PREMIUM (valid), no limit")
            return True, 999, False
        else:
            logger.info(f"User {user_id} premium EXPIRED, applying limit")

    today = datetime.now().strftime("%Y-%m-%d")
    last_date = user.get("last_request_date") or ""  # Handle None
    daily_requests = user.get("daily_requests") or 0  # Handle None
    bonus_predictions = user.get("bonus_predictions") or 0  # Referral bonus

    logger.info(f"User {user_id}: requests={daily_requests}, last_date='{last_date}', today={today}, limit={FREE_DAILY_LIMIT}, bonus={bonus_predictions}")

    # Reset counter if new day or empty date
    if last_date != today:
        update_user_settings(user_id, daily_requests=0, last_request_date=today)
        logger.info(f"User {user_id}: New day, reset to 0")
        return True, FREE_DAILY_LIMIT, False

    if daily_requests >= FREE_DAILY_LIMIT:
        # Check if user has bonus predictions
        if bonus_predictions > 0:
            logger.info(f"User {user_id}: Daily limit reached but has {bonus_predictions} bonus predictions")
            return True, bonus_predictions, True  # Will use bonus prediction
        logger.info(f"User {user_id}: ⛔ LIMIT REACHED ({daily_requests} >= {FREE_DAILY_LIMIT})")
        return False, 0, False

    remaining = FREE_DAILY_LIMIT - daily_requests
    logger.info(f"User {user_id}: ✅ OK, remaining={remaining}")
    return True, remaining, False

def increment_daily_usage(user_id, use_bonus: bool = False):
    """Increment daily usage counter or use bonus prediction if over limit"""
    logger.info(f"increment_daily_usage called for user {user_id}, use_bonus={use_bonus}")

    user = get_user(user_id)
    if not user:
        logger.warning(f"User {user_id} not found, cannot increment")
        return

    # Don't increment for premium users
    if user.get("is_premium", 0):
        logger.info(f"User {user_id} is premium, not incrementing")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    last_date = user.get("last_request_date") or ""  # Handle None
    current = user.get("daily_requests") or 0  # Handle None
    bonus_predictions = user.get("bonus_predictions") or 0

    # Check if should use bonus prediction
    if use_bonus or (current >= FREE_DAILY_LIMIT and bonus_predictions > 0):
        # Use bonus prediction instead of incrementing daily usage
        use_bonus_prediction(user_id)
        logger.info(f"User {user_id}: Used bonus prediction (remaining: {bonus_predictions - 1})")
        return

    if last_date != today:
        update_user_settings(user_id, daily_requests=1, last_request_date=today)
        logger.info(f"User {user_id}: First request today → 1")
    else:
        new_count = current + 1
        update_user_settings(user_id, daily_requests=new_count)
        logger.info(f"User {user_id}: {current} → {new_count}")

def add_favorite_team(user_id, team_name):
    """Add favorite team (ignores if already exists)"""
    conn = get_db_connection()
    c = conn.cursor()
    # Check if already exists
    c.execute("SELECT 1 FROM favorite_teams WHERE user_id = ? AND team_name = ?", (user_id, team_name))
    if not c.fetchone():
        c.execute("INSERT INTO favorite_teams (user_id, team_name) VALUES (?, ?)", (user_id, team_name))
        conn.commit()
    conn.close()

def remove_favorite_team(user_id, team_name):
    """Remove favorite team"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM favorite_teams WHERE user_id = ? AND team_name = ?", (user_id, team_name))
    conn.commit()
    conn.close()

def get_favorite_teams(user_id):
    """Get user's favorite teams"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT team_name FROM favorite_teams WHERE user_id = ?", (user_id,))
    teams = [row[0] for row in c.fetchall()]
    conn.close()
    return teams

def add_favorite_league(user_id, league_code):
    """Add favorite league (ignores if already exists)"""
    conn = get_db_connection()
    c = conn.cursor()
    # Check if already exists
    c.execute("SELECT 1 FROM favorite_leagues WHERE user_id = ? AND league_code = ?", (user_id, league_code))
    if not c.fetchone():
        c.execute("INSERT INTO favorite_leagues (user_id, league_code) VALUES (?, ?)", (user_id, league_code))
        conn.commit()
    conn.close()

def get_favorite_leagues(user_id):
    """Get user's favorite leagues"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT league_code FROM favorite_leagues WHERE user_id = ?", (user_id,))
    leagues = [row[0] for row in c.fetchall()]
    conn.close()
    return leagues


# ===== 1WIN POSTBACK & PREMIUM SYSTEM =====

# Deposit thresholds for premium (in USD as base)
# Will be converted from local currencies
PREMIUM_TIERS_USD = {
    10: "bonus_5",   # $10+ = +5 bonus predictions
    40: 7,           # $40+ = 7 days premium
    100: 30,         # $100+ = 30 days premium
    200: 36500       # $200+ = Lifetime (100 years)
}

# Currency conversion rates to USD
CURRENCY_TO_USD = {
    "USD": 1.0,
    "BRL": 0.20,      # 1 BRL = ~$0.20
    "EUR": 1.10,      # 1 EUR = ~$1.10
    "RUB": 0.011,     # 1 RUB = ~$0.011
    "NGN": 0.00065,   # 1 NGN = ~$0.00065
    "INR": 0.012,     # 1 INR = ~$0.012
    "KZT": 0.0022,    # 1 KZT = ~$0.0022
    "UAH": 0.027,     # 1 UAH = ~$0.027
    "TRY": 0.031,     # 1 TRY = ~$0.031
    "GBP": 1.27,      # 1 GBP = ~$1.27
    "PLN": 0.25,      # 1 PLN = ~$0.25
    "IDR": 0.000064,  # 1 IDR = ~$0.000064 (1 USD = ~15,600 IDR)
}

# For backwards compatibility
PREMIUM_TIERS = {
    200: 7,      # R$200+ = 7 days (legacy BRL)
    500: 30,     # R$500+ = 30 days
    1000: 36500  # R$1000+ = Lifetime
}

# ===== GEO-BASED PREMIUM TIERS =====
# Different countries have different purchasing power, so we adjust thresholds

# Nigeria (NG) - Lower thresholds, prices in Naira
PREMIUM_TIERS_NG = {
    3: "bonus_5",    # $3+ (~₦5,000) = +5 bonus predictions
    10: 7,           # $10+ (~₦15,000) = 7 days premium
    25: 30,          # $25+ (~₦40,000) = 30 days premium
    50: 36500        # $50+ (~₦80,000) = Lifetime
}

# Russia (RU) - Medium thresholds, prices in Rubles
PREMIUM_TIERS_RU = {
    5: "bonus_5",    # $5+ (~500₽) = +5 bonus predictions
    15: 7,           # $15+ (~1,500₽) = 7 days premium
    40: 30,          # $40+ (~4,000₽) = 30 days premium
    100: 36500       # $100+ (~10,000₽) = Lifetime
}

# Indonesia (ID) - Medium-low thresholds, prices in Rupiah
PREMIUM_TIERS_ID = {
    5: "bonus_5",    # $5+ (~Rp78K) = +5 bonus predictions
    20: 7,           # $20+ (~Rp312K) = 7 days premium
    50: 30,          # $50+ (~Rp780K) = 30 days premium
    100: 36500       # $100+ (~Rp1.56M) = Lifetime
}

# Geo-specific tier mapping
GEO_PREMIUM_TIERS = {
    "NG": PREMIUM_TIERS_NG,
    "RU": PREMIUM_TIERS_RU,
    "ID": PREMIUM_TIERS_ID,
    "DEFAULT": PREMIUM_TIERS_USD
}

# Geo-specific price display texts
GEO_PRICE_DISPLAY = {
    "NG": {
        "currency_symbol": "₦",
        "prices": [
            ("$3+", "~₦5,000", "+5 predictions"),
            ("$10+", "~₦15,000", "7 days"),
            ("$25+", "~₦40,000", "30 days"),
            ("$50+", "~₦80,000", "Lifetime"),
        ]
    },
    "RU": {
        "currency_symbol": "₽",
        "prices": [
            ("$5+", "~500₽", "+5 predictions"),
            ("$15+", "~1,500₽", "7 days"),
            ("$40+", "~4,000₽", "30 days"),
            ("$100+", "~10,000₽", "Lifetime"),
        ]
    },
    "ID": {
        "currency_symbol": "Rp",
        "prices": [
            ("$5+", "~Rp78K", "+5 predictions"),
            ("$20+", "~Rp312K", "7 days"),
            ("$50+", "~Rp780K", "30 days"),
            ("$100+", "~Rp1.56M", "Lifetime"),
        ]
    },
    "DEFAULT": {
        "currency_symbol": "$",
        "prices": [
            ("$10+", "~R$50/900₽", "+5 predictions"),
            ("$40+", "~R$200/3,600₽", "7 days"),
            ("$100+", "~R$500/9,000₽", "30 days"),
            ("$200+", "~R$1000/18,000₽", "Lifetime"),
        ]
    }
}


def get_user_geo(user_id: int) -> str:
    """Detect user's geo based on source field in database.

    Supports formats:
    - richads_ng_13563 → NG (with publisher ID)
    - richads_ng → NG (without publisher ID)
    - nigeria → NG (legacy)

    Returns:
        'NG' for Nigeria
        'RU' for Russia
        'ID' for Indonesia
        'DEFAULT' for others
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT source FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()

        if not row or not row[0]:
            return "DEFAULT"

        source = row[0].lower()

        # Split by underscore to check geo segments
        # e.g., "richads_ng_13563" → ["richads", "ng", "13563"]
        segments = source.split("_")

        # Check for Nigeria (ng segment or contains "nigeria")
        if "ng" in segments or "nigeria" in source:
            return "NG"

        # Check for Russia (ru segment or contains "russia")
        if "ru" in segments or "russia" in source:
            return "RU"

        # Check for Indonesia (id segment or contains "indonesia")
        # Note: checking segment to avoid false positives like "paid_user"
        if "id" in segments or "indonesia" in source:
            return "ID"

        return "DEFAULT"

    except Exception as e:
        logger.error(f"Error getting user geo: {e}")
        return "DEFAULT"


def get_premium_tiers_for_geo(geo: str) -> dict:
    """Get premium tiers for specific geo."""
    return GEO_PREMIUM_TIERS.get(geo, PREMIUM_TIERS_USD)


def convert_to_usd(amount: float, currency: str) -> float:
    """Convert amount from local currency to USD."""
    currency = currency.upper()
    rate = CURRENCY_TO_USD.get(currency, 0.20)  # Default to BRL rate if unknown
    return amount * rate


def calculate_premium_reward(amount: float, currency: str = "BRL", geo: str = "DEFAULT") -> dict:
    """Calculate premium reward based on deposit amount and user's geo.

    Args:
        amount: Deposit amount in local currency
        currency: Currency code (BRL, USD, RUB, NGN, etc.)
        geo: User's geo code (NG, RU, ID, DEFAULT)

    Returns dict with:
    - type: 'premium' or 'bonus_predictions' or 'none'
    - days: premium days (if premium)
    - predictions: bonus predictions (if bonus)
    - amount_usd: converted amount
    - geo: applied geo
    """
    amount_usd = convert_to_usd(amount, currency)

    # Get geo-specific tiers (falls back to DEFAULT if unknown)
    tiers = get_premium_tiers_for_geo(geo)

    # Check tiers from highest to lowest
    for threshold, reward in sorted(tiers.items(), reverse=True):
        if amount_usd >= threshold:
            if reward == "bonus_5":
                return {
                    "type": "bonus_predictions",
                    "predictions": 5,
                    "days": 0,
                    "amount_usd": amount_usd,
                    "geo": geo
                }
            else:
                return {
                    "type": "premium",
                    "days": reward,
                    "predictions": 0,
                    "amount_usd": amount_usd,
                    "geo": geo
                }

    return {"type": "none", "days": 0, "predictions": 0, "amount_usd": amount_usd, "geo": geo}


def calculate_premium_days(amount: float, currency: str = "BRL", geo: str = "DEFAULT") -> int:
    """Calculate premium days based on deposit amount (legacy function)."""
    reward = calculate_premium_reward(amount, currency, geo)
    return reward.get("days", 0)


def grant_premium(user_id: int, days: int) -> bool:
    """Grant premium to user for specified days."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check if user exists first
        c.execute("SELECT premium_expires FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

        if row is None:
            # User doesn't exist - create them first
            c.execute("INSERT INTO users (user_id, is_premium, daily_requests) VALUES (?, 0, 0)", (user_id,))
            conn.commit()
            logger.info(f"Created user {user_id} for premium grant")

        now = datetime.now()
        if row and row[0]:
            # Extend existing premium
            try:
                current_expiry = datetime.fromisoformat(row[0])
                if current_expiry > now:
                    new_expiry = current_expiry + timedelta(days=days)
                else:
                    new_expiry = now + timedelta(days=days)
            except:
                new_expiry = now + timedelta(days=days)
        else:
            new_expiry = now + timedelta(days=days)

        # Update premium status
        c.execute("""UPDATE users SET is_premium = 1, premium_expires = ?
                     WHERE user_id = ?""", (new_expiry.isoformat(), user_id))
        conn.commit()
        conn.close()

        logger.info(f"Granted {days} days premium to user {user_id}, expires {new_expiry}")
        return True
    except Exception as e:
        logger.error(f"Error granting premium: {e}")
        return False


def grant_bonus_predictions(user_id: int, count: int = 5) -> bool:
    """Grant bonus predictions to user (adds to daily limit).

    Stored as negative daily_requests (e.g., -5 means 5 extra requests available).
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get current daily_requests
        c.execute("SELECT daily_requests FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

        if row:
            current = row[0] or 0
            # Subtract count (negative means bonus available)
            new_count = current - count
            c.execute("UPDATE users SET daily_requests = ? WHERE user_id = ?", (new_count, user_id))
        else:
            # Create user with bonus
            c.execute("""INSERT INTO users (user_id, daily_requests)
                         VALUES (?, ?)""", (user_id, -count))

        conn.commit()
        conn.close()

        logger.info(f"Granted {count} bonus predictions to user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error granting bonus predictions: {e}")
        return False


def check_premium_expired(user_id: int) -> bool:
    """Check if user's premium has expired and update status if needed."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT is_premium, premium_expires FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()

        if not row or not row[0]:
            return True  # Not premium

        if not row[1]:
            return False  # Premium without expiry (legacy)

        expiry = datetime.fromisoformat(row[1])
        if expiry < datetime.now():
            # Premium expired - update status
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            logger.info(f"Premium expired for user {user_id}")
            return True

        return False  # Still premium
    except Exception as e:
        logger.error(f"Error checking premium: {e}")
        return True


# ===== REFERRAL SYSTEM =====
REFERRAL_BONUS_DAYS = 3  # Days given to referrer when referred user buys premium

def get_bot_username() -> str:
    """Get bot username from environment or default"""
    return os.getenv("BOT_USERNAME", "AIBettingProBot")

def get_referral_link(user_id: int) -> str:
    """Generate referral link for user"""
    bot_username = get_bot_username()
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def save_referral(referrer_id: int, referred_id: int) -> bool:
    """Save referral relationship"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Check if already exists
        c.execute("SELECT id FROM referrals WHERE referred_id = ?", (referred_id,))
        if c.fetchone():
            conn.close()
            return False  # Already referred by someone

        c.execute("""INSERT INTO referrals (referrer_id, referred_id)
                     VALUES (?, ?)""", (referrer_id, referred_id))
        c.execute("UPDATE users SET referred_by = ? WHERE user_id = ?",
                  (referrer_id, referred_id))
        conn.commit()
        conn.close()
        logger.info(f"Saved referral: {referrer_id} -> {referred_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving referral: {e}")
        return False

def get_referral_stats(user_id: int) -> dict:
    """Get referral statistics for user"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Count total referrals
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        total_invited = c.fetchone()[0]

        # Count referrals who bought premium (bonus_granted = 1)
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND bonus_granted = 1",
                  (user_id,))
        premium_count = c.fetchone()[0]

        # Calculate earned days
        earned_days = premium_count * REFERRAL_BONUS_DAYS

        conn.close()
        return {
            "invited": total_invited,
            "premium": premium_count,
            "earned_days": earned_days
        }
    except Exception as e:
        logger.error(f"Error getting referral stats: {e}")
        return {"invited": 0, "premium": 0, "earned_days": 0}

def grant_referral_bonus(referred_user_id: int) -> Optional[int]:
    """Grant bonus to referrer when referred user buys premium. Returns referrer_id if bonus granted."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find referrer and check if bonus already granted
        c.execute("""SELECT referrer_id, bonus_granted FROM referrals
                     WHERE referred_id = ?""", (referred_user_id,))
        row = c.fetchone()

        if not row:
            conn.close()
            return None  # No referrer

        referrer_id, bonus_granted = row

        if bonus_granted:
            conn.close()
            return None  # Bonus already granted

        # Grant bonus to referrer
        grant_premium(referrer_id, REFERRAL_BONUS_DAYS)

        # Mark bonus as granted
        c.execute("UPDATE referrals SET bonus_granted = 1 WHERE referred_id = ?",
                  (referred_user_id,))
        conn.commit()
        conn.close()

        logger.info(f"Granted {REFERRAL_BONUS_DAYS} days referral bonus to {referrer_id} for {referred_user_id}")
        return referrer_id
    except Exception as e:
        logger.error(f"Error granting referral bonus: {e}")
        return None


# ===== REFERRAL PREDICTIONS BONUS (2 friends = 3 free predictions) =====

REFERRAL_BONUS_PREDICTIONS = 3  # Bonus predictions for inviting 2 friends
REFERRAL_BONUS_THRESHOLD = 2   # Number of friends needed

def check_referral_bonus_eligible(user_id: int) -> dict:
    """Check if user is eligible for referral predictions bonus (2 friends = 3 predictions)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get referral count
        c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
        referral_count = c.fetchone()[0]

        # Check if bonus already claimed
        c.execute("SELECT referral_bonus_claimed, bonus_predictions FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

        conn.close()

        if not row:
            return {"eligible": False, "claimed": False, "progress": 0, "threshold": REFERRAL_BONUS_THRESHOLD}

        claimed = row[0] == 1
        bonus_predictions = row[1] or 0

        return {
            "eligible": referral_count >= REFERRAL_BONUS_THRESHOLD and not claimed,
            "claimed": claimed,
            "progress": referral_count,
            "threshold": REFERRAL_BONUS_THRESHOLD,
            "bonus_predictions": bonus_predictions
        }
    except Exception as e:
        logger.error(f"Error checking referral bonus eligibility: {e}")
        return {"eligible": False, "claimed": False, "progress": 0, "threshold": REFERRAL_BONUS_THRESHOLD}


def claim_referral_bonus(user_id: int) -> bool:
    """Claim referral predictions bonus. Returns True if successful."""
    try:
        # First check eligibility
        status = check_referral_bonus_eligible(user_id)
        if not status["eligible"]:
            logger.warning(f"User {user_id} not eligible for referral bonus")
            return False

        conn = get_db_connection()
        c = conn.cursor()

        # Grant bonus predictions and mark as claimed
        c.execute("""UPDATE users
                     SET referral_bonus_claimed = 1,
                         bonus_predictions = bonus_predictions + ?
                     WHERE user_id = ?""", (REFERRAL_BONUS_PREDICTIONS, user_id))
        conn.commit()
        conn.close()

        logger.info(f"Granted {REFERRAL_BONUS_PREDICTIONS} bonus predictions to user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error claiming referral bonus: {e}")
        return False


def grant_new_user_referral_bonus(user_id: int) -> bool:
    """Grant bonus predictions to new user who was referred (friend also gets bonus)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Grant bonus predictions to new user
        c.execute("""UPDATE users
                     SET bonus_predictions = bonus_predictions + ?
                     WHERE user_id = ?""", (REFERRAL_BONUS_PREDICTIONS, user_id))
        conn.commit()
        conn.close()

        logger.info(f"Granted {REFERRAL_BONUS_PREDICTIONS} welcome bonus predictions to referred user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error granting new user referral bonus: {e}")
        return False


def use_bonus_prediction(user_id: int) -> bool:
    """Use one bonus prediction. Returns True if successful."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check if user has bonus predictions
        c.execute("SELECT bonus_predictions FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

        if not row or (row[0] or 0) <= 0:
            conn.close()
            return False

        # Decrement bonus predictions
        c.execute("""UPDATE users
                     SET bonus_predictions = bonus_predictions - 1
                     WHERE user_id = ? AND bonus_predictions > 0""", (user_id,))
        conn.commit()
        conn.close()

        logger.info(f"Used 1 bonus prediction for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error using bonus prediction: {e}")
        return False


def get_bonus_predictions(user_id: int) -> int:
    """Get number of remaining bonus predictions"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT bonus_predictions FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] or 0 if row else 0
    except Exception as e:
        logger.error(f"Error getting bonus predictions: {e}")
        return 0


# ===== STREAK SYSTEM =====

def update_user_streak(user_id: int) -> dict:
    """Update user's daily streak. Returns streak info."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        c.execute("""SELECT streak_days, streak_record, last_streak_date
                     FROM users WHERE user_id = ?""", (user_id,))
        row = c.fetchone()

        if not row:
            conn.close()
            return {"streak": 0, "record": 0, "milestone": False}

        current_streak = row[0] or 0
        record = row[1] or 0
        last_date = row[2] or ""

        milestone = False

        if last_date == today:
            # Already updated today
            conn.close()
            return {"streak": current_streak, "record": record, "milestone": False}
        elif last_date == yesterday:
            # Continue streak
            current_streak += 1
            if current_streak > record:
                record = current_streak
            # Check for milestones (3, 7, 14, 30 days)
            if current_streak in [3, 7, 14, 30]:
                milestone = True
        else:
            # Streak broken
            current_streak = 1

        c.execute("""UPDATE users SET streak_days = ?, streak_record = ?, last_streak_date = ?
                     WHERE user_id = ?""", (current_streak, record, today, user_id))
        conn.commit()
        conn.close()

        return {"streak": current_streak, "record": record, "milestone": milestone}
    except Exception as e:
        logger.error(f"Error updating streak: {e}")
        return {"streak": 0, "record": 0, "milestone": False}


def get_user_streak(user_id: int) -> dict:
    """Get user's current streak without updating."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT streak_days, streak_record FROM users WHERE user_id = ?""", (user_id,))
        row = c.fetchone()
        conn.close()

        if row:
            return {"streak": row[0] or 0, "record": row[1] or 0}
        return {"streak": 0, "record": 0}
    except Exception as e:
        logger.error(f"Error getting streak: {e}")
        return {"streak": 0, "record": 0}


# ===== SOCIAL PROOF =====

def get_social_stats() -> dict:
    """Get social proof statistics."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        # Wins today
        c.execute("""SELECT COUNT(DISTINCT user_id) FROM predictions
                     WHERE is_correct = 1 AND date(checked_at) = ?""", (today,))
        wins_today = c.fetchone()[0] or 0

        # Total wins this week
        c.execute("""SELECT COUNT(*) FROM predictions
                     WHERE is_correct = 1 AND date(checked_at) >= ?""", (week_ago,))
        wins_week = c.fetchone()[0] or 0

        # Weekly accuracy
        c.execute("""SELECT COUNT(*) FROM predictions
                     WHERE is_correct IS NOT NULL AND date(checked_at) >= ?""", (week_ago,))
        total_checked = c.fetchone()[0] or 0

        c.execute("""SELECT COUNT(*) FROM predictions
                     WHERE is_correct = 1 AND date(checked_at) >= ?""", (week_ago,))
        correct = c.fetchone()[0] or 0

        accuracy = (correct / total_checked * 100) if total_checked > 0 else 0

        # Best win today (highest odds)
        c.execute("""SELECT home_team, away_team, odds FROM predictions
                     WHERE is_correct = 1 AND date(checked_at) = ?
                     ORDER BY odds DESC LIMIT 1""", (today,))
        best_win = c.fetchone()

        conn.close()

        return {
            "wins_today": wins_today,
            "wins_week": wins_week,
            "accuracy": round(accuracy, 1),
            "best_win": {
                "match": f"{best_win[0]} vs {best_win[1]}" if best_win else None,
                "odds": best_win[2] if best_win else None
            } if best_win else None
        }
    except Exception as e:
        logger.error(f"Error getting social stats: {e}")
        return {"wins_today": 0, "wins_week": 0, "accuracy": 0, "best_win": None}


def get_friend_wins(user_id: int, lang: str = "ru") -> list:
    """Get recent wins from user's referrals (friends)."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get user's referrals who won recently
        c.execute("""
            SELECT u.username, u.first_name, p.home_team, p.away_team, p.bet_type, p.odds
            FROM referrals r
            JOIN users u ON r.referred_id = u.user_id
            JOIN predictions p ON p.user_id = r.referred_id
            WHERE r.referrer_id = ?
            AND p.is_correct = 1
            AND p.checked_at >= datetime('now', '-24 hours')
            ORDER BY p.checked_at DESC
            LIMIT 3
        """, (user_id,))

        wins = []
        for row in c.fetchall():
            username, first_name, home, away, bet, odds = row
            name = username or first_name or get_text("friend_fallback", lang)
            wins.append({
                "name": name,
                "match": f"{home} vs {away}",
                "bet": bet,
                "odds": odds
            })

        conn.close()
        return wins
    except Exception as e:
        logger.error(f"Error getting friend wins: {e}")
        return []


# ===== MARKETING NOTIFICATIONS =====

# Track when last notification was sent per type
notification_cooldowns = {}

def should_send_notification(user_id: int, notif_type: str, cooldown_hours: int = 24) -> bool:
    """Check if we should send this notification type to user."""
    key = f"{user_id}_{notif_type}"
    last_sent = notification_cooldowns.get(key)

    if last_sent is None:
        return True

    hours_passed = (datetime.now() - last_sent).total_seconds() / 3600
    return hours_passed >= cooldown_hours


def mark_notification_sent(user_id: int, notif_type: str):
    """Mark notification as sent."""
    key = f"{user_id}_{notif_type}"
    notification_cooldowns[key] = datetime.now()


def process_1win_postback(data: dict) -> dict:
    """Process postback from 1win affiliate system."""
    try:
        event = data.get("event", "")
        amount = float(data.get("amount", 0))
        sub1 = data.get("sub1", "")  # Telegram user_id
        transaction_id = data.get("transaction_id", "")
        country = data.get("country", "")
        onewin_user_id = data.get("user_id", "")
        currency = data.get("currency", "BRL")

        logger.info(f"1win postback: event={event}, amount={amount}, sub1={sub1}, tx={transaction_id}")

        # Only process deposit events
        if event != "deposit" or not sub1:
            return {"status": "ignored", "reason": "not a deposit or no sub1"}

        # Parse telegram user_id from sub1
        try:
            telegram_user_id = int(sub1)
        except:
            return {"status": "error", "reason": "invalid sub1 (telegram user_id)"}

        # Check for duplicate transaction
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM deposits_1win WHERE transaction_id = ?", (transaction_id,))
        if c.fetchone():
            conn.close()
            return {"status": "duplicate", "reason": "transaction already processed"}

        # Get user's geo for personalized thresholds
        user_geo = get_user_geo(telegram_user_id)

        # Calculate reward (premium days OR bonus predictions) with geo-specific thresholds
        reward = calculate_premium_reward(amount, currency, user_geo)

        if reward["type"] == "none":
            conn.close()
            # Get minimum threshold for this geo
            min_threshold = min(get_premium_tiers_for_geo(user_geo).keys())
            return {"status": "ignored", "reason": f"deposit {amount} {currency} (${reward['amount_usd']:.2f}) below minimum ${min_threshold} for geo={user_geo}"}

        premium_days = reward.get("days", 0)
        bonus_predictions = reward.get("predictions", 0)

        # Save deposit record
        c.execute("""INSERT INTO deposits_1win
                     (user_id, onewin_user_id, amount, currency, event, transaction_id, country, premium_days)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (telegram_user_id, onewin_user_id, amount, currency, event, transaction_id, country, premium_days))
        conn.commit()
        conn.close()

        # Grant reward based on type
        if reward["type"] == "premium":
            grant_premium(telegram_user_id, premium_days)
            logger.info(f"Granted {premium_days} days premium to user {telegram_user_id} for ${reward['amount_usd']:.2f} deposit")
        elif reward["type"] == "bonus_predictions":
            grant_bonus_predictions(telegram_user_id, bonus_predictions)
            logger.info(f"Granted {bonus_predictions} bonus predictions to user {telegram_user_id} for ${reward['amount_usd']:.2f} deposit")

        # Grant referral bonus if user was referred
        referrer_id = grant_referral_bonus(telegram_user_id)
        if referrer_id:
            logger.info(f"Referral bonus granted to {referrer_id} for {telegram_user_id} 1win deposit")

        return {
            "status": "success",
            "user_id": telegram_user_id,
            "amount": amount,
            "amount_usd": reward["amount_usd"],
            "reward_type": reward["type"],
            "premium_days": premium_days,
            "bonus_predictions": bonus_predictions,
            "referrer_bonus": referrer_id
        }

    except Exception as e:
        logger.error(f"Error processing 1win postback: {e}")
        return {"status": "error", "reason": str(e)}


def get_affiliate_link(user_id: int) -> str:
    """Generate affiliate link with user tracking.
    Returns None when MONETIZATION_ENABLED=False.
    """
    if not MONETIZATION_ENABLED or not AFFILIATE_LINK:
        return None  # No link when monetization disabled
    # Base 1win affiliate link with sub1 parameter for tracking
    base_link = AFFILIATE_LINK.rstrip("/")
    if "?" in base_link:
        return f"{base_link}&sub1={user_id}"
    else:
        return f"{base_link}?sub1={user_id}"


# ===== CRYPTOBOT INTEGRATION =====

CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

async def create_crypto_invoice(user_id: int, days: int, currency: str = "USDT") -> dict:
    """Create invoice via CryptoBot API.

    Args:
        user_id: Telegram user ID
        days: Premium days (7, 30, 365)
        currency: USDT or TON

    Returns:
        dict with invoice_id and pay_url, or error
    """
    if not CRYPTOBOT_TOKEN:
        return {"error": "CryptoBot not configured"}

    amount = CRYPTO_PRICES.get(days, 15)

    # Payload for CryptoBot
    payload = {
        "currency_type": "crypto",
        "asset": currency,
        "amount": str(amount),
        "description": f"Premium {days} days - AI Betting Bot",
        "payload": f"{user_id}:{days}",  # Will be returned in webhook
        "expires_in": 3600,  # 1 hour to pay
        "allow_comments": False,
        "allow_anonymous": False
    }

    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
        "Content-Type": "application/json"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{CRYPTOBOT_API_URL}/createInvoice",
                json=payload,
                headers=headers
            ) as resp:
                data = await resp.json()

                if data.get("ok"):
                    invoice = data["result"]
                    invoice_id = str(invoice["invoice_id"])

                    # Save to database
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute("""
                        INSERT INTO crypto_payments (user_id, invoice_id, amount, currency, days, status)
                        VALUES (?, ?, ?, ?, ?, 'pending')
                    """, (user_id, invoice_id, amount, currency, days))
                    conn.commit()
                    conn.close()

                    return {
                        "invoice_id": invoice_id,
                        "pay_url": invoice["pay_url"],
                        "amount": amount,
                        "currency": currency
                    }
                else:
                    logger.error(f"CryptoBot error: {data}")
                    return {"error": data.get("error", {}).get("name", "Unknown error")}

    except Exception as e:
        logger.error(f"CryptoBot API error: {e}")
        return {"error": str(e)}


def process_crypto_webhook(data: dict) -> dict:
    """Process CryptoBot webhook when payment is completed.

    Args:
        data: Webhook payload from CryptoBot

    Returns:
        dict with status
    """
    try:
        update_type = data.get("update_type")
        if update_type != "invoice_paid":
            return {"status": "ignored", "reason": "not a payment"}

        payload = data.get("payload", {})
        invoice_id = str(payload.get("invoice_id", ""))
        custom_payload = payload.get("payload", "")  # Our "user_id:days" string

        if not invoice_id or not custom_payload:
            return {"status": "error", "reason": "missing data"}

        # Parse our payload
        parts = custom_payload.split(":")
        if len(parts) != 2:
            return {"status": "error", "reason": "invalid payload format"}

        user_id = int(parts[0])
        days = int(parts[1])

        # Check if already processed
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT status FROM crypto_payments WHERE invoice_id = ?", (invoice_id,))
        row = c.fetchone()

        if row and row[0] == "paid":
            conn.close()
            return {"status": "already_processed"}

        # Grant premium
        success = grant_premium(user_id, days)

        if success:
            # Update payment status
            c.execute("""
                UPDATE crypto_payments
                SET status = 'paid', paid_at = datetime('now')
                WHERE invoice_id = ?
            """, (invoice_id,))
            conn.commit()
            conn.close()

            # Grant referral bonus if user was referred
            referrer_id = grant_referral_bonus(user_id)
            if referrer_id:
                logger.info(f"Referral bonus granted to {referrer_id} for {user_id} crypto payment")

            logger.info(f"Crypto payment processed: user={user_id}, days={days}, invoice={invoice_id}")
            return {
                "status": "success",
                "user_id": user_id,
                "days": days,
                "referrer_bonus": referrer_id
            }
        else:
            conn.close()
            return {"status": "error", "reason": "failed to grant premium"}

    except Exception as e:
        logger.error(f"Crypto webhook error: {e}")
        return {"status": "error", "reason": str(e)}


# ===== LIVE SUBSCRIBERS PERSISTENCE =====

def load_live_subscribers() -> set[int]:
    """Load live subscribers from database"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM live_subscribers")
    subscribers = {row[0] for row in c.fetchall()}
    conn.close()
    logger.info(f"Loaded {len(subscribers)} live subscribers from DB")
    return subscribers


def add_live_subscriber(user_id: int) -> None:
    """Add user to live subscribers in DB"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO live_subscribers (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def remove_live_subscriber(user_id: int) -> None:
    """Remove user from live subscribers in DB"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM live_subscribers WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def categorize_bet(bet_type):
    """Categorize bet type for statistics"""
    if not bet_type:
        return "other"
    bet_lower = bet_type.lower()
    
    if "тб" in bet_lower or "тотал больше" in bet_lower or "over" in bet_lower:
        return "totals_over"
    elif "тм" in bet_lower or "тотал меньше" in bet_lower or "under" in bet_lower:
        return "totals_under"
    elif "п1" in bet_lower or "победа хозя" in bet_lower or "home win" in bet_lower:
        return "outcomes_home"
    elif "п2" in bet_lower or "победа гост" in bet_lower or "away win" in bet_lower:
        return "outcomes_away"
    elif "ничья" in bet_lower or "draw" in bet_lower or bet_lower == "х":
        return "outcomes_draw"
    elif "btts" in bet_lower or "обе забьют" in bet_lower:
        return "btts"
    elif "1x" in bet_lower or "x2" in bet_lower or "двойной шанс" in bet_lower:
        return "double_chance"
    elif "фора" in bet_lower or "handicap" in bet_lower:
        return "handicap"
    return "other"


def parse_bet_from_text(text: str) -> tuple:
    """Parse bet type, confidence and odds from text.

    Returns: (bet_type, confidence, odds) or (None, None, None) if parsing fails
    """
    text_lower = text.lower()

    # Default values
    bet_type = None
    confidence = 70
    odds = 1.5

    # Parse confidence
    conf_match = re.search(r'(\d+)\s*%', text)
    if conf_match:
        confidence = int(conf_match.group(1))

    # Parse odds
    odds_match = re.search(r'@\s*~?(\d+\.?\d*)', text)
    if odds_match:
        odds = float(odds_match.group(1))

    # Detect bet type - check double chances FIRST
    if "п1 или х" in text_lower or "1x" in text_lower or "п1/х" in text_lower:
        bet_type = "1X"
    elif "х или п2" in text_lower or "x2" in text_lower or "2x" in text_lower or "х/п2" in text_lower:
        bet_type = "X2"
    elif "п1 или п2" in text_lower or " 12 " in text_lower or "не ничья" in text_lower:
        bet_type = "12"
    elif "фора" in text_lower or "handicap" in text_lower:
        if "-1.5" in text_lower:
            bet_type = "Фора1(-1.5)"
        elif "-1" in text_lower:
            bet_type = "Фора1(-1)"
        elif "+1" in text_lower:
            bet_type = "Фора2(+1)"
        else:
            bet_type = "Фора"
    elif "тб 2.5" in text_lower or "тотал больше 2.5" in text_lower or "over 2.5" in text_lower:
        bet_type = "ТБ 2.5"
    elif "тм 2.5" in text_lower or "тотал меньше 2.5" in text_lower or "under 2.5" in text_lower:
        bet_type = "ТМ 2.5"
    elif "обе забьют" in text_lower or "btts" in text_lower:
        bet_type = "BTTS"
    elif "п2" in text_lower or "победа гостей" in text_lower:
        bet_type = "П2"
    elif "п1" in text_lower or "победа хозя" in text_lower:
        bet_type = "П1"
    elif "ничья" in text_lower or " х " in text_lower:
        bet_type = "Х"

    return (bet_type, confidence, odds)


def parse_alternative_bets(analysis: str) -> list:
    """Parse alternative bets from analysis text.

    Returns: list of (bet_type, confidence, odds) tuples
    """
    alternatives = []

    # Method 1: Look for [ALT1], [ALT2], [ALT3] format
    for i in range(1, 4):
        alt_match = re.search(rf'\[ALT{i}\]\s*(.+?)(?=\[ALT|\n⚠️|\n✅|$)', analysis, re.IGNORECASE | re.DOTALL)
        if alt_match:
            alt_text = alt_match.group(1).strip()
            bet_type, confidence, odds = parse_bet_from_text(alt_text)
            if bet_type:
                # Avoid duplicates
                if not any(alt[0] == bet_type for alt in alternatives):
                    alternatives.append((bet_type, confidence, odds))
                    logger.info(f"Parsed ALT{i}: {bet_type} @ {odds} ({confidence}%)")

    # Method 2: Look for "ДОПОЛНИТЕЛЬНЫЕ" section with better regex
    if len(alternatives) < 3:
        # Try multiple section header variations
        section_patterns = [
            r'📈\s*\**ДОПОЛНИТЕЛЬНЫЕ[^:]*:\**\s*\n(.*?)(?=\n⚠️|\n✅|\nРИСКИ|\nВЕРДИКТ|$)',
            r'ДОПОЛНИТЕЛЬНЫЕ[^:]*:\s*\n(.*?)(?=\n⚠️|\n✅|\nРИСКИ|\nВЕРДИКТ|$)',
            r'ДОПОЛНИТЕЛЬНЫЕ.*?\n((?:.*?\n)*?)(?=⚠️|✅|РИСКИ|ВЕРДИКТ|$)',
        ]

        dop_section = None
        for pattern in section_patterns:
            dop_match = re.search(pattern, analysis, re.IGNORECASE | re.DOTALL)
            if dop_match:
                dop_section = dop_match.group(1) if dop_match.lastindex else dop_match.group(0)
                break

        if dop_section:
            # Parse each line in alternatives section
            for line in dop_section.split('\n'):
                line = line.strip()
                # Skip empty lines and header lines
                if not line or 'ДОПОЛНИТЕЛЬНЫЕ' in line.upper() or line.startswith('📈'):
                    continue
                # Skip lines that are just markers or instructions
                if line in ['[ALT1]', '[ALT2]', '[ALT3]', '-', '•', '*'] or 'ОБЯЗАТЕЛЬНО' in line or 'ВСЕГДА' in line:
                    continue
                bet_type, confidence, odds = parse_bet_from_text(line)
                if bet_type and len(alternatives) < 3:
                    # Avoid duplicates
                    if not any(alt[0] == bet_type for alt in alternatives):
                        alternatives.append((bet_type, confidence, odds))
                        logger.info(f"Parsed ALT from section: {bet_type} @ {odds} ({confidence}%)")

    # Method 3: Fallback - bullet/numbered list after ДОПОЛНИТЕЛЬНЫЕ
    if len(alternatives) < 3:
        lines = analysis.split('\n')
        in_alt_section = False
        for line in lines:
            line_stripped = line.strip()
            # Start of alternatives section
            if 'ДОПОЛНИТЕЛЬНЫЕ' in line_stripped.upper():
                in_alt_section = True
                continue
            # End of section
            if in_alt_section and ('РИСКИ' in line_stripped.upper() or '⚠️' in line_stripped or '✅' in line_stripped):
                break
            # Parse lines in section
            if in_alt_section and line_stripped:
                # Skip instruction lines
                if 'ОБЯЗАТЕЛЬНО' in line_stripped or 'ВСЕГДА' in line_stripped:
                    continue
                # Match numbered (1. 2. 3.), bullet (• - *) or [ALT] formats
                if re.match(r'^[\d•\-\*\[\]]+\.?\s*', line_stripped) or '@' in line_stripped:
                    bet_type, confidence, odds = parse_bet_from_text(line_stripped)
                    if bet_type and len(alternatives) < 3:
                        # Avoid duplicating already found alternatives
                        if not any(alt[0] == bet_type for alt in alternatives):
                            alternatives.append((bet_type, confidence, odds))
                            logger.info(f"Parsed ALT (method 3): {bet_type} @ {odds} ({confidence}%)")

    # Method 4: Direct bet type search in alternatives section (most aggressive)
    if len(alternatives) < 3:
        bet_patterns = [
            (r'(?:1X|1Х)\s*[@|]\s*[\d.]+', '1X'),
            (r'(?:X2|Х2)\s*[@|]\s*[\d.]+', 'X2'),
            (r'(?:12)\s*[@|]\s*[\d.]+', '12'),
            (r'(?:BTTS|ОЗ|Обе забьют)\s*[@|]\s*[\d.]+', 'BTTS'),
            (r'(?:ТБ|Over)\s*2\.?5\s*[@|]\s*[\d.]+', 'ТБ 2.5'),
            (r'(?:ТМ|Under)\s*2\.?5\s*[@|]\s*[\d.]+', 'ТМ 2.5'),
            (r'(?:П1|P1|Home)\s*[@|]\s*[\d.]+', 'П1'),
            (r'(?:П2|P2|Away)\s*[@|]\s*[\d.]+', 'П2'),
            (r'(?:Ничья|Draw|X)\s*[@|]\s*[\d.]+', 'X'),
        ]

        # Only search in the alternatives section
        alt_section_match = re.search(r'ДОПОЛНИТЕЛЬНЫЕ.*?(⚠️|✅|РИСКИ|$)', analysis, re.IGNORECASE | re.DOTALL)
        if alt_section_match:
            alt_section = alt_section_match.group(0)
            for pattern, bet_name in bet_patterns:
                if len(alternatives) >= 3:
                    break
                match = re.search(pattern, alt_section, re.IGNORECASE)
                if match and not any(alt[0] == bet_name for alt in alternatives):
                    # Try to extract odds
                    odds_match = re.search(r'[@|]\s*([\d.]+)', match.group(0))
                    odds = float(odds_match.group(1)) if odds_match else 1.8
                    # Try to extract confidence
                    conf_match = re.search(r'(\d+)\s*%', alt_section[match.end():match.end()+50])
                    confidence = int(conf_match.group(1)) if conf_match else 65
                    alternatives.append((bet_name, confidence, odds))
                    logger.info(f"Parsed ALT (method 4): {bet_name} @ {odds} ({confidence}%)")

    if alternatives:
        logger.info(f"✅ Total alternatives found: {len(alternatives)}")
        if len(alternatives) < 3:
            logger.warning(f"⚠️ Only {len(alternatives)}/3 alternatives parsed - Claude may have generated fewer")
    else:
        logger.warning("⚠️ No alternatives found in analysis")

    return alternatives[:3]  # Max 3 alternatives


def save_prediction(user_id, match_id, home, away, bet_type, confidence, odds, ml_features=None, bet_rank=1, league_code=None, match_time=None):
    """Save prediction to database with category and ML features.

    Args:
        bet_rank: 1 = main bet, 2+ = alternatives
        league_code: League code for learning system (e.g. "PL", "SA", "BL1")
        match_time: ISO format datetime string when match starts (for smart result checking)

    Duplicate rules:
    - Main bet (rank=1): Only ONE main bet per match allowed (regardless of bet_type)
    - Alternative (rank>1): Max 3 per match, one per bet_type
    """
    # Safety check: ensure confidence is not None
    if confidence is None:
        logger.warning(f"Confidence is None for {home} vs {away}, defaulting to 65")
        confidence = 65

    category = categorize_bet(bet_type)

    # Calculate VALUE score for ROI optimization
    # VALUE = (confidence/100) × odds
    # For positive ROI long-term, we want VALUE > 1.0 (edge over bookmaker)
    try:
        odds_float = float(odds) if odds else 1.5
    except:
        odds_float = 1.5

    value_score = (confidence / 100) * odds_float
    has_value = value_score > 1.0  # Positive expected value

    # Log for analytics - but SAVE ALL predictions for learning
    logger.info(f"PREDICTION: {bet_type} ({category}) | conf={confidence}% | odds={odds_float:.2f} | VALUE={value_score:.2f} {'✓' if has_value else '✗'} | {home} vs {away}")

    conn = get_db_connection()
    c = conn.cursor()

    # For MAIN bets: check if ANY main bet exists for this match
    if bet_rank == 1:
        c.execute("""SELECT id, bet_type FROM predictions
                     WHERE user_id = ? AND match_id = ? AND bet_rank = 1
                     LIMIT 1""", (user_id, match_id))
        existing = c.fetchone()
    else:
        # For alternatives: check if already have 3 alts OR same bet_type exists
        c.execute("""SELECT COUNT(*) FROM predictions
                     WHERE user_id = ? AND match_id = ? AND bet_rank > 1""", (user_id, match_id))
        alt_count = c.fetchone()[0]

        if alt_count >= 3:
            conn.close()
            logger.info(f"Skipping ALT: match {match_id} already has 3 alternatives")
            return None

        c.execute("""SELECT id, bet_type FROM predictions
                     WHERE user_id = ? AND match_id = ? AND bet_type = ? AND bet_rank > 1
                     LIMIT 1""", (user_id, match_id, bet_type))
        existing = c.fetchone()

    if existing:
        # Already have this prediction
        existing_id = existing[0]
        existing_type = existing[1]
        conn.close()
        if bet_rank == 1:
            logger.info(f"Skipping duplicate MAIN: match {match_id} already has main bet {existing_type}")
        else:
            logger.info(f"Skipping duplicate ALT: match {match_id}, {bet_type}")

        # IMPORTANT: Still save ML data if features provided but not saved before
        if ml_features and category:
            # Check if ML data exists for this prediction
            conn2 = get_db_connection()
            c2 = conn2.cursor()
            c2.execute("SELECT id FROM ml_training_data WHERE prediction_id = ?", (existing_id,))
            ml_exists = c2.fetchone()
            conn2.close()

            if not ml_exists:
                save_ml_training_data(existing_id, category, ml_features, target=None, bet_rank=bet_rank)
                logger.info(f"Added missing ML data for existing prediction {existing_id}")

        return existing_id  # Return existing prediction ID

    # Serialize ml_features to JSON for smart learning
    ml_features_json = json.dumps(ml_features) if ml_features else None

    # Calculate Expected Value and recommended stake
    ev = calculate_expected_value(confidence, odds)
    stake = calculate_kelly_stake(confidence, odds)

    c.execute("""INSERT INTO predictions
                 (user_id, match_id, home_team, away_team, bet_type, bet_category, confidence, odds, bet_rank, league_code, ml_features_json, expected_value, stake_percent, match_time)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (user_id, match_id, home, away, bet_type, category, confidence, odds, bet_rank, league_code, ml_features_json, ev, stake, match_time))
    prediction_id = c.lastrowid
    conn.commit()
    conn.close()

    # Save ML training data if features provided (with bet_rank for MAIN vs ALT analysis)
    if ml_features and category:
        save_ml_training_data(prediction_id, category, ml_features, target=None, bet_rank=bet_rank)

    rank_label = "MAIN" if bet_rank == 1 else f"ALT{bet_rank-1}"
    logger.info(f"Saved prediction [{rank_label}]: {home} vs {away}, {bet_type} ({confidence}%)")

    return prediction_id

def get_pending_predictions():
    """Get predictions that haven't been checked yet.

    Sorted by match_time (oldest first) for smart result checking -
    matches that should have ended get checked first.
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, user_id, match_id, home_team, away_team, bet_type, confidence, odds, bet_rank, match_time
                 FROM predictions
                 WHERE is_correct IS NULL
                 AND predicted_at > datetime('now', '-7 days')
                 ORDER BY
                    CASE WHEN match_time IS NOT NULL
                         THEN match_time
                         ELSE predicted_at
                    END ASC""")
    rows = c.fetchall()
    conn.close()

    return [{"id": r[0], "user_id": r[1], "match_id": r[2], "home": r[3],
             "away": r[4], "bet_type": r[5], "confidence": r[6], "odds": r[7],
             "bet_rank": r[8] if len(r) > 8 else 1,
             "match_time": r[9] if len(r) > 9 else None} for r in rows]

def update_prediction_result(pred_id, result, is_correct):
    """Update prediction with result and ML training data + trigger learning"""
    conn = get_db_connection()
    c = conn.cursor()

    # Get prediction details for learning
    c.execute("""SELECT bet_category, confidence, bet_type FROM predictions WHERE id = ?""", (pred_id,))
    pred_row = c.fetchone()

    c.execute("""UPDATE predictions
                 SET result = ?, is_correct = ?, checked_at = CURRENT_TIMESTAMP
                 WHERE id = ?""", (result, is_correct, pred_id))
    conn.commit()
    conn.close()

    # Update ML training target (1 = correct, 0 = incorrect)
    if is_correct is not None:
        target = 1 if is_correct else 0
        update_ml_training_target(pred_id, target)

        # Check if we should train models
        check_and_train_models()

        # Trigger self-learning system
        if pred_row:
            bet_category, confidence, bet_type = pred_row
            # Get ML features, user_id, odds, and league_code
            conn2 = get_db_connection()
            c2 = conn2.cursor()
            c2.execute("SELECT features_json FROM ml_training_data WHERE prediction_id = ?", (pred_id,))
            features_row = c2.fetchone()
            c2.execute("SELECT user_id, odds, league_code FROM predictions WHERE id = ?", (pred_id,))
            pred_info = c2.fetchone()
            conn2.close()

            features = json.loads(features_row[0]) if features_row and features_row[0] else None
            league_code = pred_info[2] if pred_info and len(pred_info) > 2 else None
            learn_from_result(pred_id, bet_category, confidence or 70, is_correct, features, bet_type or "",
                              league_code=league_code, actual_result=result)

            # Update user personalization stats
            if pred_info and pred_info[0] and pred_info[0] > 0:  # user_id > 0 (not bot alerts)
                user_id, odds, _ = pred_info  # 3 values: user_id, odds, league_code
                update_user_bet_stats(user_id, bet_category, is_correct == 1, odds or 1.5)


def clean_duplicate_predictions() -> dict:
    """Remove duplicate predictions based on these rules:

    - Main bet (rank=1): Only ONE per (user_id, match_id) - keep oldest
    - Alternative (rank>1): Only ONE per (user_id, match_id, bet_type) - keep oldest
    """
    conn = get_db_connection()
    c = conn.cursor()

    deleted_count = 0
    affected_matches = 0

    # Step 1: Clean duplicate MAIN bets (keep oldest per user_id + match_id)
    c.execute("""
        SELECT user_id, match_id, COUNT(*) as cnt, MIN(id) as keep_id
        FROM predictions
        WHERE bet_rank = 1
        GROUP BY user_id, match_id
        HAVING cnt > 1
    """)
    main_duplicates = c.fetchall()

    for user_id, match_id, count, keep_id in main_duplicates:
        c.execute("""DELETE FROM predictions
                     WHERE user_id = ? AND match_id = ? AND bet_rank = 1 AND id != ?""",
                  (user_id, match_id, keep_id))
        deleted_count += c.rowcount
        affected_matches += 1

    # Step 2: Clean duplicate ALT bets (keep oldest per user_id + match_id + bet_type)
    c.execute("""
        SELECT user_id, match_id, bet_type, COUNT(*) as cnt, MIN(id) as keep_id
        FROM predictions
        WHERE bet_rank > 1
        GROUP BY user_id, match_id, bet_type
        HAVING cnt > 1
    """)
    alt_duplicates = c.fetchall()

    for user_id, match_id, bet_type, count, keep_id in alt_duplicates:
        c.execute("""DELETE FROM predictions
                     WHERE user_id = ? AND match_id = ? AND bet_type = ? AND bet_rank > 1 AND id != ?""",
                  (user_id, match_id, bet_type, keep_id))
        deleted_count += c.rowcount
        affected_matches += 1

    # Also clean orphaned ml_training_data
    c.execute("""DELETE FROM ml_training_data
                 WHERE prediction_id NOT IN (SELECT id FROM predictions)""")
    orphaned_ml = c.rowcount

    conn.commit()
    conn.close()

    logger.info(f"Cleaned {deleted_count} duplicates from {affected_matches} matches, {orphaned_ml} orphaned ML records")

    return {
        "deleted": deleted_count,
        "matches_affected": affected_matches,
        "orphaned_ml_cleaned": orphaned_ml
    }


def clean_duplicate_favorites() -> dict:
    """Remove duplicate entries from favorite_teams and favorite_leagues.

    Keeps the oldest entry for each (user_id, team_name/league_code) pair.
    Returns count of deleted duplicates.
    """
    conn = get_db_connection()
    c = conn.cursor()

    deleted_teams = 0
    deleted_leagues = 0

    # Clean duplicate favorite teams
    try:
        c.execute("""
            DELETE FROM favorite_teams
            WHERE id NOT IN (
                SELECT MIN(id) FROM favorite_teams GROUP BY user_id, team_name
            )
        """)
        deleted_teams = c.rowcount
    except Exception as e:
        logger.warning(f"Error cleaning favorite_teams: {e}")

    # Clean duplicate favorite leagues
    try:
        c.execute("""
            DELETE FROM favorite_leagues
            WHERE id NOT IN (
                SELECT MIN(id) FROM favorite_leagues GROUP BY user_id, league_code
            )
        """)
        deleted_leagues = c.rowcount
    except Exception as e:
        logger.warning(f"Error cleaning favorite_leagues: {e}")

    conn.commit()
    conn.close()

    if deleted_teams > 0 or deleted_leagues > 0:
        logger.info(f"Cleaned favorites: {deleted_teams} team dups, {deleted_leagues} league dups")

    return {
        "deleted_teams": deleted_teams,
        "deleted_leagues": deleted_leagues,
        "total": deleted_teams + deleted_leagues
    }


def get_clean_stats() -> dict:
    """Get accuracy stats and detect TRUE duplicates.

    A duplicate is: same (user_id, match_id, bet_type, bet_rank).
    Different bet types or ranks (main vs alt) are NOT duplicates.
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Count unique predictions (first per user+match+bet_type+bet_rank)
        c.execute("""
            WITH unique_preds AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY user_id, match_id, bet_type, bet_rank
                    ORDER BY predicted_at ASC
                ) as rn
                FROM predictions
                WHERE is_correct IS NOT NULL
            )
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct
            FROM unique_preds WHERE rn = 1
        """)
        row = c.fetchone()
        total = row[0] or 0
        correct = row[1] or 0

        # Current stats (with duplicates)
        c.execute("""SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
                     FROM predictions WHERE is_correct IS NOT NULL""")
        row2 = c.fetchone()
        total_with_dups = row2[0] or 0
        correct_with_dups = row2[1] or 0

        conn.close()

        return {
            "clean_total": total,
            "clean_correct": correct,
            "clean_accuracy": round(correct / total * 100, 1) if total > 0 else 0,
            "with_dups_total": total_with_dups,
            "with_dups_correct": correct_with_dups,
            "with_dups_accuracy": round(correct_with_dups / total_with_dups * 100, 1) if total_with_dups > 0 else 0,
            "duplicates_count": total_with_dups - total
        }
    except Exception as e:
        logger.error(f"get_clean_stats error: {e}")
        return {
            "clean_total": 0,
            "clean_correct": 0,
            "clean_accuracy": 0,
            "with_dups_total": 0,
            "with_dups_correct": 0,
            "with_dups_accuracy": 0,
            "duplicates_count": 0
        }


def get_roi_stats(user_id: int = None) -> dict:
    """Calculate ROI (Return on Investment) for predictions.
    Assumes flat betting (1 unit per bet)."""
    conn = get_db_connection()
    c = conn.cursor()

    where_clause = "WHERE is_correct IS NOT NULL"
    params = ()
    if user_id:
        where_clause += " AND user_id = ?"
        params = (user_id,)

    c.execute(f"""
        SELECT odds, is_correct FROM predictions
        {where_clause}
    """, params)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"total_bets": 0, "roi": 0, "profit": 0, "units_won": 0, "units_lost": 0}

    total_bets = len(rows)
    units_staked = total_bets  # 1 unit per bet
    units_won = 0
    units_lost = 0

    for odds, is_correct in rows:
        if is_correct == 1:  # Win
            units_won += (odds - 1) if odds else 0.8  # profit = odds - 1
        elif is_correct == 0:  # Loss
            units_lost += 1
        # is_correct == 2 is push (no profit/loss)

    profit = units_won - units_lost
    roi = (profit / units_staked * 100) if units_staked > 0 else 0

    return {
        "total_bets": total_bets,
        "units_staked": units_staked,
        "units_won": round(units_won, 2),
        "units_lost": units_lost,
        "profit": round(profit, 2),
        "roi": round(roi, 1)
    }


def get_streak_info(user_id: int = None) -> dict:
    """Get current streak and best/worst streaks."""
    conn = get_db_connection()
    c = conn.cursor()

    where_clause = "WHERE is_correct IS NOT NULL"
    params = ()
    if user_id:
        where_clause += " AND user_id = ?"
        params = (user_id,)

    c.execute(f"""
        SELECT is_correct FROM predictions
        {where_clause}
        ORDER BY checked_at DESC
    """, params)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"current_streak": 0, "streak_type": None, "best_win_streak": 0, "worst_lose_streak": 0}

    results = [r[0] for r in rows]

    # Current streak
    current_streak = 0
    streak_type = None
    if results:
        first = results[0]
        if first in (0, 1):
            streak_type = "win" if first == 1 else "lose"
            for r in results:
                if r == first:
                    current_streak += 1
                else:
                    break

    # Best win streak and worst lose streak
    best_win = 0
    worst_lose = 0
    temp_win = 0
    temp_lose = 0

    for r in results:
        if r == 1:
            temp_win += 1
            temp_lose = 0
            best_win = max(best_win, temp_win)
        elif r == 0:
            temp_lose += 1
            temp_win = 0
            worst_lose = max(worst_lose, temp_lose)
        else:
            temp_win = 0
            temp_lose = 0

    return {
        "current_streak": current_streak,
        "streak_type": streak_type,
        "best_win_streak": best_win,
        "worst_lose_streak": worst_lose
    }


def get_stats_by_league() -> dict:
    """Get accuracy statistics broken down by league/competition."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT
            CASE
                WHEN home_team LIKE '%Premier%' OR away_team LIKE '%Premier%' THEN 'Premier League'
                WHEN home_team LIKE '%Barcelona%' OR home_team LIKE '%Madrid%' OR home_team LIKE '%Atletico%' THEN 'La Liga'
                WHEN home_team LIKE '%Bayern%' OR home_team LIKE '%Dortmund%' THEN 'Bundesliga'
                WHEN home_team LIKE '%Juventus%' OR home_team LIKE '%Milan%' OR home_team LIKE '%Inter%' OR home_team LIKE '%Roma%' THEN 'Serie A'
                WHEN home_team LIKE '%PSG%' OR home_team LIKE '%Lyon%' OR home_team LIKE '%Marseille%' THEN 'Ligue 1'
                ELSE 'Other'
            END as league,
            COUNT(*) as total,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins,
            bet_category
        FROM predictions
        WHERE is_correct IS NOT NULL
        GROUP BY league, bet_category
        ORDER BY total DESC
    """)
    rows = c.fetchall()
    conn.close()

    stats = {}
    for league, total, wins, category in rows:
        if league not in stats:
            stats[league] = {"total": 0, "wins": 0, "by_type": {}}
        stats[league]["total"] += total
        stats[league]["wins"] += wins
        if category:
            if category not in stats[league]["by_type"]:
                stats[league]["by_type"][category] = {"total": 0, "wins": 0}
            stats[league]["by_type"][category]["total"] += total
            stats[league]["by_type"][category]["wins"] += wins

    # Calculate accuracies
    for league in stats:
        stats[league]["accuracy"] = round(stats[league]["wins"] / stats[league]["total"] * 100, 1) if stats[league]["total"] > 0 else 0
        for cat in stats[league]["by_type"]:
            cat_data = stats[league]["by_type"][cat]
            cat_data["accuracy"] = round(cat_data["wins"] / cat_data["total"] * 100, 1) if cat_data["total"] > 0 else 0

    return stats


def calculate_kelly(probability: float, odds: float) -> float:
    """Calculate Kelly Criterion stake size.
    Returns fraction of bankroll to bet (0-1)."""
    if odds <= 1 or probability <= 0 or probability >= 1:
        return 0

    # Kelly formula: (bp - q) / b
    # b = decimal odds - 1
    # p = probability of winning
    # q = probability of losing (1 - p)
    b = odds - 1
    p = probability / 100 if probability > 1 else probability
    q = 1 - p

    kelly = (b * p - q) / b

    # Never bet more than 25% (quarter Kelly is safer)
    return max(0, min(kelly / 4, 0.25))


# ===== xG (EXPECTED GOALS) DATA SYSTEM =====
# Real xG from statistics providers - CRITICAL for totals predictions

# Team name mapping for Understat (they use specific names)
UNDERSTAT_TEAM_NAMES = {
    # Premier League
    "Arsenal FC": "Arsenal", "Arsenal": "Arsenal",
    "Aston Villa FC": "Aston Villa", "Aston Villa": "Aston Villa",
    "AFC Bournemouth": "Bournemouth", "Bournemouth": "Bournemouth",
    "Brentford FC": "Brentford", "Brentford": "Brentford",
    "Brighton & Hove Albion FC": "Brighton", "Brighton": "Brighton",
    "Chelsea FC": "Chelsea", "Chelsea": "Chelsea",
    "Crystal Palace FC": "Crystal Palace", "Crystal Palace": "Crystal Palace",
    "Everton FC": "Everton", "Everton": "Everton",
    "Fulham FC": "Fulham", "Fulham": "Fulham",
    "Ipswich Town FC": "Ipswich", "Ipswich Town": "Ipswich",
    "Leicester City FC": "Leicester", "Leicester City": "Leicester",
    "Liverpool FC": "Liverpool", "Liverpool": "Liverpool",
    "Manchester City FC": "Manchester City", "Manchester City": "Manchester City",
    "Manchester United FC": "Manchester United", "Manchester United": "Manchester United",
    "Newcastle United FC": "Newcastle United", "Newcastle United": "Newcastle United",
    "Nottingham Forest FC": "Nottingham Forest", "Nottingham Forest": "Nottingham Forest",
    "Southampton FC": "Southampton", "Southampton": "Southampton",
    "Tottenham Hotspur FC": "Tottenham", "Tottenham Hotspur": "Tottenham",
    "West Ham United FC": "West Ham", "West Ham United": "West Ham",
    "Wolverhampton Wanderers FC": "Wolverhampton Wanderers", "Wolves": "Wolverhampton Wanderers",
    # La Liga
    "FC Barcelona": "Barcelona", "Barcelona": "Barcelona",
    "Real Madrid CF": "Real Madrid", "Real Madrid": "Real Madrid",
    "Club Atlético de Madrid": "Atletico Madrid", "Atletico Madrid": "Atletico Madrid", "Atlético Madrid": "Atletico Madrid",
    "Athletic Club": "Athletic Club", "Athletic Bilbao": "Athletic Club",
    "Real Sociedad de Fútbol": "Real Sociedad", "Real Sociedad": "Real Sociedad",
    "Real Betis Balompié": "Real Betis", "Real Betis": "Real Betis",
    "Villarreal CF": "Villarreal", "Villarreal": "Villarreal",
    "Sevilla FC": "Sevilla", "Sevilla": "Sevilla",
    "Valencia CF": "Valencia", "Valencia": "Valencia",
    "Getafe CF": "Getafe", "Getafe": "Getafe",
    "RC Celta de Vigo": "Celta Vigo", "Celta Vigo": "Celta Vigo",
    "RCD Espanyol de Barcelona": "Espanyol", "Espanyol": "Espanyol",
    "CA Osasuna": "Osasuna", "Osasuna": "Osasuna",
    "RCD Mallorca": "Mallorca", "Mallorca": "Mallorca",
    "Deportivo Alavés": "Alaves", "Alaves": "Alaves",
    "UD Las Palmas": "Las Palmas", "Las Palmas": "Las Palmas",
    "Girona FC": "Girona", "Girona": "Girona",
    "Rayo Vallecano de Madrid": "Rayo Vallecano", "Rayo Vallecano": "Rayo Vallecano",
    "CD Leganés": "Leganes", "Leganes": "Leganes",
    "Real Valladolid CF": "Valladolid", "Valladolid": "Valladolid",
    # Serie A
    "SSC Napoli": "Napoli", "Napoli": "Napoli",
    "FC Internazionale Milano": "Inter", "Inter Milan": "Inter", "Inter": "Inter",
    "AC Milan": "AC Milan", "Milan": "AC Milan",
    "Juventus FC": "Juventus", "Juventus": "Juventus",
    "Atalanta BC": "Atalanta", "Atalanta": "Atalanta",
    "AS Roma": "Roma", "Roma": "Roma",
    "SS Lazio": "Lazio", "Lazio": "Lazio",
    "ACF Fiorentina": "Fiorentina", "Fiorentina": "Fiorentina",
    "Bologna FC 1909": "Bologna", "Bologna": "Bologna",
    "Torino FC": "Torino", "Torino": "Torino",
    "Udinese Calcio": "Udinese", "Udinese": "Udinese",
    "US Sassuolo Calcio": "Sassuolo", "Sassuolo": "Sassuolo",
    "Empoli FC": "Empoli", "Empoli": "Empoli",
    "US Salernitana 1919": "Salernitana", "Salernitana": "Salernitana",
    "Hellas Verona FC": "Verona", "Verona": "Verona",
    "US Lecce": "Lecce", "Lecce": "Lecce",
    "Cagliari Calcio": "Cagliari", "Cagliari": "Cagliari",
    "Genoa CFC": "Genoa", "Genoa": "Genoa",
    "Frosinone Calcio": "Frosinone", "Frosinone": "Frosinone",
    "Parma Calcio 1913": "Parma", "Parma": "Parma",
    "Venezia FC": "Venezia", "Venezia": "Venezia",
    "AC Monza": "Monza", "Monza": "Monza",
    "Como 1907": "Como", "Como": "Como",
    # Bundesliga
    "FC Bayern München": "Bayern Munich", "Bayern Munich": "Bayern Munich", "Bayern München": "Bayern Munich",
    "Borussia Dortmund": "Borussia Dortmund", "Dortmund": "Borussia Dortmund",
    "RB Leipzig": "RasenBallsport Leipzig", "RB Leipzig": "RasenBallsport Leipzig",
    "Bayer 04 Leverkusen": "Bayer Leverkusen", "Bayer Leverkusen": "Bayer Leverkusen",
    "Eintracht Frankfurt": "Eintracht Frankfurt", "Frankfurt": "Eintracht Frankfurt",
    "VfB Stuttgart": "Stuttgart", "Stuttgart": "Stuttgart",
    "VfL Wolfsburg": "Wolfsburg", "Wolfsburg": "Wolfsburg",
    "SC Freiburg": "Freiburg", "Freiburg": "Freiburg",
    "TSG 1899 Hoffenheim": "Hoffenheim", "Hoffenheim": "Hoffenheim",
    "1. FC Union Berlin": "Union Berlin", "Union Berlin": "Union Berlin",
    "Borussia Mönchengladbach": "Borussia M.Gladbach", "Gladbach": "Borussia M.Gladbach",
    "1. FSV Mainz 05": "Mainz 05", "Mainz": "Mainz 05",
    "FC Augsburg": "Augsburg", "Augsburg": "Augsburg",
    "SV Werder Bremen": "Werder Bremen", "Werder Bremen": "Werder Bremen",
    "VfL Bochum 1848": "Bochum", "Bochum": "Bochum",
    "1. FC Köln": "FC Cologne", "Köln": "FC Cologne", "Cologne": "FC Cologne",
    "1. FC Heidenheim 1846": "Heidenheim", "Heidenheim": "Heidenheim",
    "SV Darmstadt 98": "Darmstadt", "Darmstadt": "Darmstadt",
    "FC St. Pauli": "St. Pauli", "St. Pauli": "St. Pauli",
    "Holstein Kiel": "Holstein Kiel", "Kiel": "Holstein Kiel",
    # Ligue 1
    "Paris Saint-Germain FC": "Paris Saint Germain", "PSG": "Paris Saint Germain", "Paris Saint-Germain": "Paris Saint Germain",
    "AS Monaco FC": "Monaco", "Monaco": "Monaco",
    "Olympique de Marseille": "Marseille", "Marseille": "Marseille",
    "Lille OSC": "Lille", "Lille": "Lille",
    "Olympique Lyonnais": "Lyon", "Lyon": "Lyon",
    "OGC Nice": "Nice", "Nice": "Nice",
    "RC Lens": "Lens", "Lens": "Lens",
    "Stade Rennais FC 1901": "Rennes", "Rennes": "Rennes",
    "RC Strasbourg Alsace": "Strasbourg", "Strasbourg": "Strasbourg",
    "Stade Brestois 29": "Brest", "Brest": "Brest",
    "Montpellier HSC": "Montpellier", "Montpellier": "Montpellier",
    "FC Nantes": "Nantes", "Nantes": "Nantes",
    "Toulouse FC": "Toulouse", "Toulouse": "Toulouse",
    "Stade de Reims": "Reims", "Reims": "Reims",
    "FC Lorient": "Lorient", "Lorient": "Lorient",
    "Clermont Foot 63": "Clermont Foot", "Clermont": "Clermont Foot",
    "FC Metz": "Metz", "Metz": "Metz",
    "Le Havre AC": "Le Havre", "Le Havre": "Le Havre",
    "AJ Auxerre": "Auxerre", "Auxerre": "Auxerre",
    "Angers SCO": "Angers", "Angers": "Angers",
    "AS Saint-Étienne": "Saint-Etienne", "Saint-Étienne": "Saint-Etienne",
}

# League code to Understat league name
UNDERSTAT_LEAGUES = {
    "PL": "EPL",      # English Premier League
    "PD": "La_liga",  # La Liga
    "SA": "Serie_A",  # Serie A
    "BL1": "Bundesliga",  # Bundesliga
    "FL1": "Ligue_1",  # Ligue 1
}

# Cache for xG data (to avoid repeated requests)
_xg_cache = {}
_xg_cache_time = {}
XG_CACHE_DURATION = 3600  # 1 hour cache


async def fetch_team_xg_understat(team_name: str, league_code: str, season: int = None) -> dict:
    """
    Fetch real xG data for a team from Understat.

    Returns dict with:
    - xg_for: expected goals scored
    - xg_against: expected goals conceded
    - goals: actual goals scored
    - goals_against: actual goals conceded
    - xg_diff: xG - actual goals (positive = unlucky/should score more)
    - xg_per_game: average xG per match
    - npxg: non-penalty xG
    """
    result = {
        "source": "understat",
        "available": False,
        "team": team_name,
    }

    if league_code not in UNDERSTAT_LEAGUES:
        result["error"] = f"League {league_code} not supported by Understat"
        return result

    # Get Understat team name
    understat_name = UNDERSTAT_TEAM_NAMES.get(team_name)
    if not understat_name:
        # Try to find partial match
        for key, value in UNDERSTAT_TEAM_NAMES.items():
            if team_name.lower() in key.lower() or key.lower() in team_name.lower():
                understat_name = value
                break

    if not understat_name:
        result["error"] = f"Team {team_name} not found in Understat mapping"
        return result

    # Check cache
    cache_key = f"xg_{understat_name}_{league_code}"
    if cache_key in _xg_cache:
        cache_age = (datetime.now() - _xg_cache_time.get(cache_key, datetime.min)).total_seconds()
        if cache_age < XG_CACHE_DURATION:
            return _xg_cache[cache_key]

    understat_league = UNDERSTAT_LEAGUES[league_code]
    if not season:
        season = datetime.now().year if datetime.now().month >= 8 else datetime.now().year - 1

    try:
        session = await get_http_session()

        # Fetch league page which contains team xG data
        url = f"https://understat.com/league/{understat_league}/{season}"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 200:
                result["error"] = f"Understat returned {response.status}"
                return result

            html = await response.text()

            # Extract teamsData JSON from page (it's embedded in script tags)
            teams_match = re.search(r"var teamsData\s*=\s*JSON\.parse\('(.+?)'\)", html)
            if not teams_match:
                result["error"] = "Could not find teamsData in Understat page"
                return result

            # Decode the escaped JSON
            teams_json = teams_match.group(1)
            teams_json = teams_json.encode().decode('unicode_escape')
            teams_data = json.loads(teams_json)

            # Find our team
            team_data = None
            for team_id, data in teams_data.items():
                if data.get("title", "").lower() == understat_name.lower():
                    team_data = data
                    break

            if not team_data:
                # Try partial match
                for team_id, data in teams_data.items():
                    title = data.get("title", "").lower()
                    if understat_name.lower() in title or title in understat_name.lower():
                        team_data = data
                        break

            if not team_data:
                result["error"] = f"Team {understat_name} not found in Understat data"
                return result

            # Parse history to calculate totals
            history = team_data.get("history", [])
            if not history:
                result["error"] = "No match history found"
                return result

            total_xg = 0
            total_xga = 0
            total_goals = 0
            total_goals_against = 0
            total_npxg = 0
            total_npxga = 0
            matches_played = len(history)

            # Recent form (last 5 matches)
            recent_xg = 0
            recent_goals = 0
            recent_xga = 0
            recent_goals_against = 0
            recent_matches = history[-5:] if len(history) >= 5 else history

            for match in history:
                total_xg += float(match.get("xG", 0))
                total_xga += float(match.get("xGA", 0))
                total_goals += int(match.get("scored", 0))
                total_goals_against += int(match.get("missed", 0))
                total_npxg += float(match.get("npxG", 0))
                total_npxga += float(match.get("npxGA", 0))

            for match in recent_matches:
                recent_xg += float(match.get("xG", 0))
                recent_xga += float(match.get("xGA", 0))
                recent_goals += int(match.get("scored", 0))
                recent_goals_against += int(match.get("missed", 0))

            # Calculate metrics
            result.update({
                "available": True,
                "matches_played": matches_played,
                "xg_for": round(total_xg, 2),
                "xg_against": round(total_xga, 2),
                "goals": total_goals,
                "goals_against": total_goals_against,
                "npxg": round(total_npxg, 2),  # Non-penalty xG
                "npxga": round(total_npxga, 2),
                "xg_per_game": round(total_xg / matches_played, 2) if matches_played else 0,
                "xga_per_game": round(total_xga / matches_played, 2) if matches_played else 0,
                "goals_per_game": round(total_goals / matches_played, 2) if matches_played else 0,
                # xG deviation: positive = underperforming (should score more)
                "xg_diff": round(total_xg - total_goals, 2),
                "xga_diff": round(total_xga - total_goals_against, 2),
                # Recent form xG (last 5)
                "recent_xg": round(recent_xg, 2),
                "recent_xga": round(recent_xga, 2),
                "recent_goals": recent_goals,
                "recent_xg_per_game": round(recent_xg / len(recent_matches), 2) if recent_matches else 0,
            })

            # Add interpretation
            xg_diff = result["xg_diff"]
            if xg_diff > 3:
                result["attacking_luck"] = "very_unlucky"
                result["attack_trend"] = "likely_to_improve"
            elif xg_diff > 1:
                result["attacking_luck"] = "unlucky"
                result["attack_trend"] = "should_improve"
            elif xg_diff < -3:
                result["attacking_luck"] = "very_lucky"
                result["attack_trend"] = "likely_to_regress"
            elif xg_diff < -1:
                result["attacking_luck"] = "lucky"
                result["attack_trend"] = "may_regress"
            else:
                result["attacking_luck"] = "normal"
                result["attack_trend"] = "stable"

            # Cache result
            _xg_cache[cache_key] = result
            _xg_cache_time[cache_key] = datetime.now()

            logger.info(f"📊 xG fetched for {team_name}: xG={result['xg_for']}, Goals={result['goals']}, Diff={xg_diff}")

            return result

    except asyncio.TimeoutError:
        result["error"] = "Understat request timed out"
        return result
    except Exception as e:
        result["error"] = f"Understat fetch error: {str(e)}"
        logger.error(f"xG fetch error for {team_name}: {e}")
        return result


async def get_match_xg_data(home_team: str, away_team: str, league_code: str,
                           home_form: dict = None, away_form: dict = None) -> dict:
    """
    Get xG data for both teams in a match.

    SUPPORTS ALL LEAGUES:
    - Top-5 leagues: Real xG from Understat
    - Other leagues: Calculated xG from form data + deviation analysis

    Returns comprehensive xG analysis including:
    - Individual team xG stats
    - Expected match total (based on real xG or calculated)
    - Deviation analysis (who's over/underperforming)
    - Totals betting insights
    """
    result = {
        "available": False,
        "source": "none",
        "home_xg": None,
        "away_xg": None,
        "expected_total": None,
        "analysis": None
    }

    # STEP 1: Try Understat for top-5 leagues
    if league_code in UNDERSTAT_LEAGUES:
        home_xg_task = fetch_team_xg_understat(home_team, league_code)
        away_xg_task = fetch_team_xg_understat(away_team, league_code)
        home_xg, away_xg = await asyncio.gather(home_xg_task, away_xg_task)

        if home_xg.get("available") and away_xg.get("available"):
            result["home_xg"] = home_xg
            result["away_xg"] = away_xg
            result["source"] = "understat"
            result["available"] = True
            logger.info(f"📊 xG from Understat for {home_team} vs {away_team}")

    # STEP 2: Fallback - Calculate xG from form data (ALL LEAGUES)
    if not result["available"] and home_form and away_form:
        result = calculate_xg_from_form(home_team, away_team, home_form, away_form, league_code)
        if result["available"]:
            logger.info(f"📊 xG calculated from form for {home_team} vs {away_team}")

    # STEP 3: Calculate match predictions if we have xG data
    if result["available"]:
        home_xg = result.get("home_xg", {})
        away_xg = result.get("away_xg", {})

        # Calculate expected goals for this match
        home_attack = home_xg.get("xg_per_game", 1.3)
        away_defense = away_xg.get("xga_per_game", 1.3)
        away_attack = away_xg.get("xg_per_game", 1.0)
        home_defense = home_xg.get("xga_per_game", 1.0)

        # Expected goals formula with home advantage
        expected_home_goals = (home_attack * 1.1 * 0.6 + away_defense * 0.4)
        expected_away_goals = (away_attack * 0.6 + home_defense * 0.4)
        expected_total = expected_home_goals + expected_away_goals

        result["expected_home_goals"] = round(expected_home_goals, 2)
        result["expected_away_goals"] = round(expected_away_goals, 2)
        result["expected_total"] = round(expected_total, 2)

        # Analysis based on xG deviations
        analysis = []

        home_diff = home_xg.get("xg_diff", 0)
        away_diff = away_xg.get("xg_diff", 0)

        # Home team analysis
        if home_diff > 2:
            analysis.append(f"⚠️ {home_team} НЕДОЗАБИВАЕТ (diff: +{home_diff:.1f}) - ожидай больше голов!")
        elif home_diff < -2:
            analysis.append(f"📉 {home_team} перезабивает (diff: {home_diff:.1f}) - может регрессировать")

        # Away team analysis
        if away_diff > 2:
            analysis.append(f"⚠️ {away_team} НЕДОЗАБИВАЕТ (diff: +{away_diff:.1f}) - ожидай больше голов!")
        elif away_diff < -2:
            analysis.append(f"📉 {away_team} перезабивает (diff: {away_diff:.1f}) - может регрессировать")

        # Recent form xG
        home_recent_xg = home_xg.get("recent_xg_per_game", home_attack)
        away_recent_xg = away_xg.get("recent_xg_per_game", away_attack)
        recent_total = home_recent_xg + away_recent_xg

        if recent_total > 3.0:
            analysis.append(f"🔥 Высокий xG последних матчей: {recent_total:.1f} → склонность к OVER")
        elif recent_total < 2.0:
            analysis.append(f"🛡️ Низкий xG последних матчей: {recent_total:.1f} → склонность к UNDER")

        # Combined deviation
        total_deviation = home_diff + away_diff
        if total_deviation > 3:
            analysis.append(f"💎 ОБЕ команды недозабивают! Суммарно +{total_deviation:.1f} → СИЛЬНЫЙ OVER сигнал!")
        elif total_deviation < -3:
            analysis.append(f"⚡ ОБЕ команды перезабивают! Суммарно {total_deviation:.1f} → регрессия к UNDER")

        result["analysis"] = analysis
        result["total_xg_deviation"] = round(total_deviation, 2)
        result["recent_xg_total"] = round(recent_total, 2)

        logger.info(f"📊 Match xG ({result['source']}): {home_team} vs {away_team} - Total: {expected_total:.2f}")

    return result


def calculate_xg_from_form(home_team: str, away_team: str,
                           home_form: dict, away_form: dict,
                           league_code: str = None) -> dict:
    """
    Calculate pseudo-xG from form data for leagues without Understat coverage.

    Uses:
    - Average goals scored/conceded
    - Home/away splits
    - Recent form trend
    - League average normalization

    Also calculates xG deviation by comparing:
    - Expected goals (based on chances created proxy) vs actual goals
    """
    result = {
        "available": False,
        "source": "form_calculation",
        "home_xg": None,
        "away_xg": None,
    }

    if not home_form or not away_form:
        return result

    try:
        # Get league average for context
        league_avg = LEAGUE_AVG_GOALS.get(league_code, LEAGUE_AVG_GOALS.get("default", 2.6))

        # HOME TEAM xG calculation
        home_overall = home_form.get("overall", {})
        home_home_stats = home_form.get("home", {})

        # Use home-specific stats when available, otherwise overall
        home_scored = home_home_stats.get("avg_goals_scored") or home_overall.get("avg_goals_scored", 1.3)
        home_conceded = home_home_stats.get("avg_goals_conceded") or home_overall.get("avg_goals_conceded", 1.0)

        # Calculate "expected" based on shot proxy (more goals = more chances usually)
        # xG deviation: compare what they "should" score vs what they score
        # Positive = underperforming (unlucky), Negative = overperforming (lucky)
        home_matches = home_overall.get("wins", 0) + home_overall.get("draws", 0) + home_overall.get("losses", 0)
        home_matches = max(home_matches, 5)  # minimum 5 for calculation

        # Estimate xG from goals + variance (regression to league mean)
        # Teams far from league average tend to regress
        home_xg_estimate = home_scored * 0.85 + (league_avg / 2) * 0.15  # Slight regression to mean
        home_xg_diff = (home_xg_estimate - home_scored) * home_matches * 0.5  # Scale by matches

        home_xg_data = {
            "available": True,
            "source": "form_calculation",
            "team": home_team,
            "xg_per_game": round(home_xg_estimate, 2),
            "xga_per_game": round(home_conceded, 2),
            "goals_per_game": round(home_scored, 2),
            "xg_diff": round(home_xg_diff, 2),
            "matches_played": home_matches,
            "recent_xg_per_game": round(home_scored, 2),  # Use actual as proxy for recent
        }

        # Check if team is over/under performing based on form trend
        form_str = home_overall.get("form", "")
        recent_wins = form_str[-5:].count("W") if len(form_str) >= 5 else 0
        recent_losses = form_str[-5:].count("L") if len(form_str) >= 5 else 0

        # Adjust xG diff based on recent trend
        if recent_wins >= 4 and home_scored > league_avg / 2 + 0.3:
            home_xg_data["xg_diff"] -= 1  # Hot streak, might be overperforming
            home_xg_data["attacking_luck"] = "lucky_streak"
        elif recent_losses >= 4 and home_scored < league_avg / 2:
            home_xg_data["xg_diff"] += 1  # Cold streak, might be underperforming
            home_xg_data["attacking_luck"] = "unlucky_streak"
        else:
            home_xg_data["attacking_luck"] = "normal"

        # AWAY TEAM xG calculation
        away_overall = away_form.get("overall", {})
        away_away_stats = away_form.get("away", {})

        away_scored = away_away_stats.get("avg_goals_scored") or away_overall.get("avg_goals_scored", 1.0)
        away_conceded = away_away_stats.get("avg_goals_conceded") or away_overall.get("avg_goals_conceded", 1.3)

        away_matches = away_overall.get("wins", 0) + away_overall.get("draws", 0) + away_overall.get("losses", 0)
        away_matches = max(away_matches, 5)

        away_xg_estimate = away_scored * 0.85 + (league_avg / 2) * 0.15
        away_xg_diff = (away_xg_estimate - away_scored) * away_matches * 0.5

        away_xg_data = {
            "available": True,
            "source": "form_calculation",
            "team": away_team,
            "xg_per_game": round(away_xg_estimate, 2),
            "xga_per_game": round(away_conceded, 2),
            "goals_per_game": round(away_scored, 2),
            "xg_diff": round(away_xg_diff, 2),
            "matches_played": away_matches,
            "recent_xg_per_game": round(away_scored, 2),
        }

        # Away team form trend
        away_form_str = away_overall.get("form", "")
        away_recent_wins = away_form_str[-5:].count("W") if len(away_form_str) >= 5 else 0
        away_recent_losses = away_form_str[-5:].count("L") if len(away_form_str) >= 5 else 0

        if away_recent_wins >= 4 and away_scored > league_avg / 2 + 0.2:
            away_xg_data["xg_diff"] -= 1
            away_xg_data["attacking_luck"] = "lucky_streak"
        elif away_recent_losses >= 4 and away_scored < league_avg / 2:
            away_xg_data["xg_diff"] += 1
            away_xg_data["attacking_luck"] = "unlucky_streak"
        else:
            away_xg_data["attacking_luck"] = "normal"

        result["home_xg"] = home_xg_data
        result["away_xg"] = away_xg_data
        result["available"] = True

        logger.debug(f"📊 Calculated xG: {home_team}={home_xg_estimate:.2f}, {away_team}={away_xg_estimate:.2f}")

    except Exception as e:
        logger.error(f"xG calculation error: {e}")

    return result


def format_xg_analysis(xg_data: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format xG analysis for display in match analysis."""
    if not xg_data or not xg_data.get("available"):
        return ""

    lines = []

    # Show source of xG data
    source = xg_data.get("source", "unknown")
    if source == "understat":
        source_text = "Understat" if lang != "ru" else "Understat (реальный xG)"
    else:
        source_text = "Form calculation" if lang != "ru" else "Расчёт по форме"

    if lang == "ru":
        lines.append(f"📊 **xG АНАЛИЗ** [{source_text}]")
    else:
        lines.append(f"📊 **xG ANALYSIS** [{source_text}]")

    home_xg = xg_data.get("home_xg", {})
    away_xg = xg_data.get("away_xg", {})

    if home_xg.get("available"):
        # Use xg_for for Understat, goals_per_game for calculated
        if source == "understat":
            xg = home_xg.get("xg_for", 0)
            goals = home_xg.get("goals", 0)
            lines.append(f"  {home_team}:")
            diff = home_xg.get("xg_diff", 0)
            diff_str = f"+{diff:.1f}" if diff > 0 else f"{diff:.1f}"
            trend = "↗️" if diff > 1 else ("↘️" if diff < -1 else "→")
            lines.append(f"    xG: {xg:.1f} | Голы: {goals} | Diff: {diff_str} {trend}")
        else:
            xg_pg = home_xg.get("xg_per_game", 0)
            goals_pg = home_xg.get("goals_per_game", 0)
            diff = home_xg.get("xg_diff", 0)
            diff_str = f"+{diff:.1f}" if diff > 0 else f"{diff:.1f}"
            trend = "↗️" if diff > 1 else ("↘️" if diff < -1 else "→")
            luck = home_xg.get("attacking_luck", "normal")
            luck_emoji = "🍀" if "lucky" in luck else ("😔" if "unlucky" in luck else "")
            lines.append(f"  {home_team}:")
            lines.append(f"    xG/игра: {xg_pg:.2f} | Голы/игра: {goals_pg:.2f} | Тренд: {diff_str} {trend} {luck_emoji}")

        matches = home_xg.get("matches_played", 0)
        xg_pg = home_xg.get("xg_per_game", 0)
        lines.append(f"    xG/game: {xg_pg:.2f} ({matches} матчей)")

    if away_xg.get("available"):
        if source == "understat":
            xg = away_xg.get("xg_for", 0)
            goals = away_xg.get("goals", 0)
            lines.append(f"  {away_team}:")
            diff = away_xg.get("xg_diff", 0)
            diff_str = f"+{diff:.1f}" if diff > 0 else f"{diff:.1f}"
            trend = "↗️" if diff > 1 else ("↘️" if diff < -1 else "→")
            lines.append(f"    xG: {xg:.1f} | Голы: {goals} | Diff: {diff_str} {trend}")
        else:
            xg_pg = away_xg.get("xg_per_game", 0)
            goals_pg = away_xg.get("goals_per_game", 0)
            diff = away_xg.get("xg_diff", 0)
            diff_str = f"+{diff:.1f}" if diff > 0 else f"{diff:.1f}"
            trend = "↗️" if diff > 1 else ("↘️" if diff < -1 else "→")
            luck = away_xg.get("attacking_luck", "normal")
            luck_emoji = "🍀" if "lucky" in luck else ("😔" if "unlucky" in luck else "")
            lines.append(f"  {away_team}:")
            lines.append(f"    xG/игра: {xg_pg:.2f} | Голы/игра: {goals_pg:.2f} | Тренд: {diff_str} {trend} {luck_emoji}")

        matches = away_xg.get("matches_played", 0)
        xg_pg = away_xg.get("xg_per_game", 0)
        lines.append(f"    xG/game: {xg_pg:.2f} ({matches} матчей)")

    # Expected match totals
    if xg_data.get("expected_total"):
        exp_total = xg_data["expected_total"]
        exp_home = xg_data.get("expected_home_goals", 0)
        exp_away = xg_data.get("expected_away_goals", 0)

        lines.append("")
        if lang == "ru":
            lines.append(f"  **Прогноз голов (xG-модель):**")
            lines.append(f"    {home_team}: ~{exp_home:.1f}")
            lines.append(f"    {away_team}: ~{exp_away:.1f}")
            lines.append(f"    **ИТОГО: ~{exp_total:.1f}**")
        else:
            lines.append(f"  **Expected Goals (xG model):**")
            lines.append(f"    {home_team}: ~{exp_home:.1f}")
            lines.append(f"    {away_team}: ~{exp_away:.1f}")
            lines.append(f"    **TOTAL: ~{exp_total:.1f}**")

    # Analysis insights
    analysis = xg_data.get("analysis", [])
    if analysis:
        lines.append("")
        for insight in analysis:
            lines.append(f"  {insight}")

    return "\n".join(lines)


# ===== PLAYER IMPACT SYSTEM =====
# Tracks key players and their impact on team performance
# Auto-updated from API (top scorers + squad data)

# Position impact weights (how much each position affects different aspects)
POSITION_IMPACT_WEIGHTS = {
    "Offence": {"attack": 0.8, "defense": 0.1, "creativity": 0.4, "goals": 0.9},
    "Forward": {"attack": 0.8, "defense": 0.1, "creativity": 0.4, "goals": 0.9},
    "Midfield": {"attack": 0.5, "defense": 0.4, "creativity": 0.8, "goals": 0.3},
    "Defence": {"attack": 0.2, "defense": 0.9, "creativity": 0.2, "goals": 0.1},
    "Defender": {"attack": 0.2, "defense": 0.9, "creativity": 0.2, "goals": 0.1},
    "Goalkeeper": {"attack": 0.0, "defense": 1.0, "creativity": 0.1, "goals": 0.0},
}

# Cache for player impact data
_player_impact_cache = {}
_player_impact_cache_time = {}
PLAYER_IMPACT_CACHE_DURATION = 3600 * 6  # 6 hours


async def update_key_players_from_api(league_code: str) -> int:
    """
    Fetch and update key players for a league from API.
    Uses top scorers + team squad data.

    Returns number of players updated.
    """
    if not FOOTBALL_API_KEY:
        return 0

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()
    updated_count = 0

    try:
        # Step 1: Get top scorers (these are definitely key players)
        url = f"{FOOTBALL_API_URL}/competitions/{league_code}/scorers"
        async with session.get(url, headers=headers, params={"limit": 30}) as r:
            if r.status == 200:
                data = await r.json()
                scorers = data.get("scorers", [])

                for scorer in scorers:
                    player = scorer.get("player", {})
                    team = scorer.get("team", {})

                    if not player.get("id") or not team.get("id"):
                        continue

                    goals = scorer.get("goals", 0)
                    assists = scorer.get("assists", 0)
                    played = scorer.get("playedMatches", 1)

                    # Calculate impact scores based on performance
                    goals_per_game = goals / max(played, 1)
                    assists_per_game = assists / max(played, 1)

                    # Impact calculation:
                    # - Top scorer with 1+ goals/game = 40-50 impact
                    # - Good scorer with 0.5 goals/game = 25-35 impact
                    # - Playmaker with high assists = 20-30 creativity impact

                    attack_impact = min(50, int(goals_per_game * 40 + assists_per_game * 15))
                    goals_impact = min(50, int(goals_per_game * 45))
                    creativity_impact = min(40, int(assists_per_game * 35 + goals_per_game * 10))

                    # Save to database
                    save_key_player(
                        team_id=team.get("id"),
                        team_name=team.get("name"),
                        player_id=player.get("id"),
                        player_name=player.get("name"),
                        position=player.get("position", "Offence"),
                        impact_attack=attack_impact,
                        impact_goals=goals_impact,
                        impact_creativity=creativity_impact,
                        goals_season=goals,
                        assists_season=assists,
                        league_code=league_code
                    )
                    updated_count += 1

                logger.info(f"🌟 Updated {updated_count} key players for {league_code} from top scorers")

        # Step 2: Get teams and their key defenders/goalkeepers
        url = f"{FOOTBALL_API_URL}/competitions/{league_code}/teams"
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                data = await r.json()
                teams = data.get("teams", [])

                for team in teams[:20]:  # Top 20 teams
                    team_id = team.get("id")
                    team_name = team.get("name")
                    squad = team.get("squad", [])

                    if not squad:
                        continue

                    # Find key defenders and GKs (usually most experienced/capped)
                    for player in squad:
                        position = player.get("position", "Unknown")

                        # We already have attackers from scorers, now add defenders/GKs
                        if position in ["Goalkeeper", "Defence", "Defender", "Centre-Back"]:
                            # Estimate impact based on position
                            # For defenders/GKs we don't have goal stats, so estimate
                            defense_impact = 30 if position == "Goalkeeper" else 25

                            save_key_player(
                                team_id=team_id,
                                team_name=team_name,
                                player_id=player.get("id"),
                                player_name=player.get("name"),
                                position=position,
                                impact_defense=defense_impact,
                                impact_attack=5,
                                league_code=league_code
                            )
                            updated_count += 1

        logger.info(f"🌟 Total {updated_count} key players updated for {league_code}")

    except Exception as e:
        logger.error(f"Error updating key players for {league_code}: {e}")

    return updated_count


def save_key_player(team_id: int, team_name: str, player_id: int, player_name: str,
                    position: str, impact_attack: int = 0, impact_defense: int = 0,
                    impact_creativity: int = 0, impact_goals: int = 0,
                    goals_season: int = 0, assists_season: int = 0,
                    league_code: str = None):
    """Save or update a key player in database."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            INSERT INTO key_players
            (team_id, team_name, player_id, player_name, position,
             impact_attack, impact_defense, impact_creativity, impact_goals,
             goals_season, assists_season, league_code, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(team_id, player_id) DO UPDATE SET
                player_name = excluded.player_name,
                position = excluded.position,
                impact_attack = MAX(impact_attack, excluded.impact_attack),
                impact_defense = MAX(impact_defense, excluded.impact_defense),
                impact_creativity = MAX(impact_creativity, excluded.impact_creativity),
                impact_goals = MAX(impact_goals, excluded.impact_goals),
                goals_season = excluded.goals_season,
                assists_season = excluded.assists_season,
                updated_at = CURRENT_TIMESTAMP
        """, (team_id, team_name, player_id, player_name, position,
              impact_attack, impact_defense, impact_creativity, impact_goals,
              goals_season, assists_season, league_code))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving key player {player_name}: {e}")


def get_team_key_players(team_name: str, team_id: int = None) -> list:
    """Get key players for a team from database."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        if team_id:
            c.execute("""
                SELECT * FROM key_players
                WHERE team_id = ? AND is_key_player = 1
                ORDER BY (impact_attack + impact_defense + impact_goals) DESC
                LIMIT 10
            """, (team_id,))
        else:
            # Try to match by team name
            c.execute("""
                SELECT * FROM key_players
                WHERE (team_name LIKE ? OR team_name LIKE ?) AND is_key_player = 1
                ORDER BY (impact_attack + impact_defense + impact_goals) DESC
                LIMIT 10
            """, (f"%{team_name}%", f"{team_name}%"))

        rows = c.fetchall()
        conn.close()

        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error getting key players for {team_name}: {e}")
        return []


def calculate_player_impact(team_name: str, injuries: list, team_id: int = None) -> dict:
    """
    Calculate the impact of missing players on team performance.

    Args:
        team_name: Name of the team
        injuries: List of injured/suspended players (from lineups API)
        team_id: Optional team ID for better matching

    Returns:
        dict with:
        - attack_modifier: % reduction in attack (negative)
        - defense_modifier: % reduction in defense (negative)
        - creativity_modifier: % reduction in creativity
        - goals_modifier: % reduction in expected goals
        - key_players_out: list of key players missing
        - total_impact: overall team strength modifier
        - impact_details: detailed breakdown
    """
    result = {
        "attack_modifier": 0,
        "defense_modifier": 0,
        "creativity_modifier": 0,
        "goals_modifier": 0,
        "key_players_out": [],
        "total_impact": 0,
        "impact_details": [],
        "available": False
    }

    if not injuries:
        result["available"] = True
        return result

    # Get key players for this team
    key_players = get_team_key_players(team_name, team_id)

    if not key_players:
        # No key players in DB - still mark as available but no impact calculation
        result["available"] = True
        return result

    # Create lookup for injured players (normalize names)
    injured_names = set()
    for injury in injuries:
        if isinstance(injury, dict):
            name = injury.get("player", injury.get("name", ""))
        else:
            name = str(injury)

        # Normalize: "E. Haaland" -> "haaland", "Erling Haaland" -> "haaland"
        name_parts = name.lower().replace(".", "").split()
        if name_parts:
            injured_names.add(name_parts[-1])  # Last name
            injured_names.add(" ".join(name_parts))  # Full name

    # Check which key players are injured
    total_attack = 0
    total_defense = 0
    total_creativity = 0
    total_goals = 0

    for player in key_players:
        player_name = player.get("player_name", "")
        name_parts = player_name.lower().replace(".", "").split()

        is_injured = False
        for part in name_parts:
            if part in injured_names:
                is_injured = True
                break

        if not is_injured and " ".join(name_parts) in injured_names:
            is_injured = True

        if is_injured:
            attack_impact = player.get("impact_attack", 0)
            defense_impact = player.get("impact_defense", 0)
            creativity_impact = player.get("impact_creativity", 0)
            goals_impact = player.get("impact_goals", 0)

            total_attack += attack_impact
            total_defense += defense_impact
            total_creativity += creativity_impact
            total_goals += goals_impact

            result["key_players_out"].append({
                "name": player_name,
                "position": player.get("position", "Unknown"),
                "impact_attack": attack_impact,
                "impact_defense": defense_impact,
                "impact_goals": goals_impact
            })

            # Add detail
            impact_desc = []
            if attack_impact > 15:
                impact_desc.append(f"атака -{attack_impact}%")
            if defense_impact > 15:
                impact_desc.append(f"защита -{defense_impact}%")
            if goals_impact > 15:
                impact_desc.append(f"голы -{goals_impact}%")

            if impact_desc:
                result["impact_details"].append(f"❌ {player_name}: {', '.join(impact_desc)}")

    # Cap modifiers at reasonable levels
    result["attack_modifier"] = -min(total_attack, 60)
    result["defense_modifier"] = -min(total_defense, 50)
    result["creativity_modifier"] = -min(total_creativity, 40)
    result["goals_modifier"] = -min(total_goals, 55)

    # Calculate total impact (weighted average)
    result["total_impact"] = round(
        (result["attack_modifier"] * 0.4 +
         result["defense_modifier"] * 0.3 +
         result["goals_modifier"] * 0.3), 1
    )

    result["available"] = True

    if result["key_players_out"]:
        logger.info(f"🏥 Player impact for {team_name}: {len(result['key_players_out'])} key players out, total impact: {result['total_impact']}%")

    return result


def get_opponent_class(position: int, total_teams: int = 20) -> str:
    """Classify opponent by table position."""
    if position <= 6:
        return "top6"
    elif position >= total_teams - 5:
        return "bottom6"
    else:
        return "mid"


def analyze_flat_track_bully(team_name: str, team_id: int = None) -> dict:
    """
    Analyze if team's key players are "flat track bullies" or "big game players".

    Flat track bully = scores lots vs weak teams, struggles vs strong
    Big game player = performs well in big matches vs top teams

    Returns analysis with scoring patterns by opponent strength.
    """
    result = {
        "available": False,
        "players": [],
        "team_has_flat_track_bullies": False,
        "team_has_big_game_players": False,
        "goals_vs_top6_per_game": 0,
        "goals_vs_bottom6_per_game": 0,
        "scoring_ratio": 1.0,  # bottom6/top6 ratio (>2 = flat track bully team)
    }

    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Get key players with their vs-opponent stats
        if team_id:
            c.execute("""
                SELECT * FROM key_players
                WHERE team_id = ? AND is_key_player = 1
                  AND (goals_vs_top6 > 0 OR goals_vs_bottom6 > 0)
                ORDER BY goals_season DESC
                LIMIT 5
            """, (team_id,))
        else:
            c.execute("""
                SELECT * FROM key_players
                WHERE (team_name LIKE ? OR team_name LIKE ?) AND is_key_player = 1
                  AND (goals_vs_top6 > 0 OR goals_vs_bottom6 > 0)
                ORDER BY goals_season DESC
                LIMIT 5
            """, (f"%{team_name}%", f"{team_name}%"))

        rows = c.fetchall()
        conn.close()

        if not rows:
            return result

        result["available"] = True

        total_goals_top6 = 0
        total_goals_bottom6 = 0
        total_games_top6 = 0
        total_games_bottom6 = 0

        for row in rows:
            player = dict(row)

            goals_top6 = player.get("goals_vs_top6", 0)
            goals_mid = player.get("goals_vs_mid", 0)
            goals_bottom6 = player.get("goals_vs_bottom6", 0)
            games_top6 = player.get("games_vs_top6", 1)
            games_bottom6 = player.get("games_vs_bottom6", 1)

            # Calculate per-game rates
            rate_top6 = goals_top6 / max(games_top6, 1)
            rate_bottom6 = goals_bottom6 / max(games_bottom6, 1)

            # Classify player
            is_flat_track = rate_bottom6 > rate_top6 * 2 and goals_bottom6 >= 3
            is_big_game = rate_top6 >= rate_bottom6 * 0.8 and goals_top6 >= 2

            player_analysis = {
                "name": player.get("player_name"),
                "goals_vs_top6": goals_top6,
                "goals_vs_mid": goals_mid,
                "goals_vs_bottom6": goals_bottom6,
                "rate_vs_top6": round(rate_top6, 2),
                "rate_vs_bottom6": round(rate_bottom6, 2),
                "is_flat_track_bully": is_flat_track,
                "is_big_game_player": is_big_game
            }

            result["players"].append(player_analysis)

            if is_flat_track:
                result["team_has_flat_track_bullies"] = True
            if is_big_game:
                result["team_has_big_game_players"] = True

            total_goals_top6 += goals_top6
            total_goals_bottom6 += goals_bottom6
            total_games_top6 += games_top6
            total_games_bottom6 += games_bottom6

        # Team-level stats
        if total_games_top6 > 0:
            result["goals_vs_top6_per_game"] = round(total_goals_top6 / total_games_top6, 2)
        if total_games_bottom6 > 0:
            result["goals_vs_bottom6_per_game"] = round(total_goals_bottom6 / total_games_bottom6, 2)

        # Scoring ratio (how much more they score vs weak teams)
        if result["goals_vs_top6_per_game"] > 0:
            result["scoring_ratio"] = round(
                result["goals_vs_bottom6_per_game"] / result["goals_vs_top6_per_game"], 2
            )

        logger.debug(f"📊 Flat track analysis for {team_name}: ratio={result['scoring_ratio']}")

    except Exception as e:
        logger.error(f"Error analyzing flat track bully for {team_name}: {e}")

    return result


def update_player_match_performance(player_id: int, player_name: str, team_id: int,
                                     match_id: int, opponent_id: int, opponent_name: str,
                                     opponent_position: int, goals: int, assists: int,
                                     league_code: str):
    """
    Update player's performance stats after a match.
    Called when checking match results.
    """
    try:
        opponent_class = get_opponent_class(opponent_position)

        conn = get_db_connection()
        c = conn.cursor()

        # Insert match stats
        c.execute("""
            INSERT INTO player_match_stats
            (player_id, player_name, team_id, match_id, opponent_id, opponent_name,
             opponent_position, opponent_class, goals, assists, league_code, match_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, DATE('now'))
            ON CONFLICT(player_id, match_id) DO UPDATE SET
                goals = excluded.goals,
                assists = excluded.assists
        """, (player_id, player_name, team_id, match_id, opponent_id, opponent_name,
              opponent_position, opponent_class, goals, assists, league_code))

        # Update aggregates in key_players table
        if opponent_class == "top6":
            c.execute("""
                UPDATE key_players SET
                    goals_vs_top6 = goals_vs_top6 + ?,
                    games_vs_top6 = games_vs_top6 + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE player_id = ?
            """, (goals, player_id))
        elif opponent_class == "bottom6":
            c.execute("""
                UPDATE key_players SET
                    goals_vs_bottom6 = goals_vs_bottom6 + ?,
                    games_vs_bottom6 = games_vs_bottom6 + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE player_id = ?
            """, (goals, player_id))
        else:
            c.execute("""
                UPDATE key_players SET
                    goals_vs_mid = goals_vs_mid + ?,
                    games_vs_mid = games_vs_mid + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE player_id = ?
            """, (goals, player_id))

        # Update is_flat_track_bully / is_big_game_player flags
        c.execute("""
            UPDATE key_players SET
                is_flat_track_bully = CASE
                    WHEN games_vs_bottom6 >= 3 AND games_vs_top6 >= 2 AND
                         (goals_vs_bottom6 * 1.0 / games_vs_bottom6) > (goals_vs_top6 * 2.0 / MAX(games_vs_top6, 1))
                    THEN 1 ELSE 0 END,
                is_big_game_player = CASE
                    WHEN games_vs_top6 >= 3 AND goals_vs_top6 >= 2 AND
                         (goals_vs_top6 * 1.0 / games_vs_top6) >= (goals_vs_bottom6 * 0.5 / MAX(games_vs_bottom6, 1))
                    THEN 1 ELSE 0 END
            WHERE player_id = ?
        """, (player_id,))

        conn.commit()
        conn.close()

        logger.debug(f"📊 Updated player stats: {player_name} vs {opponent_class} = {goals} goals")

    except Exception as e:
        logger.error(f"Error updating player match performance: {e}")


async def update_player_stats_from_finished_match(match_data: dict, league_code: str):
    """
    Extract goalscorers from finished match and update player_match_stats.
    Called after match results are confirmed.

    Football-data.org /matches/{id} returns goals in 'goals' array:
    {
        "goals": [
            {"minute": 23, "scorer": {"id": 123, "name": "Player"}, "team": {"id": 1, "name": "Team"}},
            ...
        ]
    }
    """
    try:
        match_id = match_data.get("id")
        if not match_id:
            return

        home_team = match_data.get("homeTeam", {})
        away_team = match_data.get("awayTeam", {})
        home_id = home_team.get("id")
        away_id = away_team.get("id")
        home_name = home_team.get("name", "")
        away_name = away_team.get("name", "")

        goals_data = match_data.get("goals", [])

        if not goals_data or not home_id or not away_id:
            return

        # Get standings to determine positions
        standings = await get_standings(league_code)

        # Build position lookup from standings
        team_positions = {}
        if standings and standings.get("total"):
            for idx, team in enumerate(standings["total"], 1):
                tid = team.get("team", {}).get("id")
                if tid:
                    team_positions[tid] = idx

        home_pos = team_positions.get(home_id, 10)
        away_pos = team_positions.get(away_id, 10)

        # Count goals per player
        player_goals = {}  # {player_id: {"name": str, "team_id": int, "goals": int}}

        for goal in goals_data:
            scorer = goal.get("scorer", {})
            player_id = scorer.get("id")
            player_name = scorer.get("name", "Unknown")
            team_info = goal.get("team", {})
            team_id = team_info.get("id")

            if not player_id or not team_id:
                continue

            if player_id not in player_goals:
                player_goals[player_id] = {
                    "name": player_name,
                    "team_id": team_id,
                    "goals": 0
                }
            player_goals[player_id]["goals"] += 1

        # Update stats for each scorer
        for player_id, data in player_goals.items():
            team_id = data["team_id"]

            # Determine opponent info based on which team scored
            if team_id == home_id:
                opponent_id = away_id
                opponent_name = away_name
                opponent_position = away_pos
            else:
                opponent_id = home_id
                opponent_name = home_name
                opponent_position = home_pos

            update_player_match_performance(
                player_id=player_id,
                player_name=data["name"],
                team_id=team_id,
                match_id=match_id,
                opponent_id=opponent_id,
                opponent_name=opponent_name,
                opponent_position=opponent_position,
                goals=data["goals"],
                assists=0,  # API doesn't provide assists in basic match data
                league_code=league_code
            )

        if player_goals:
            logger.info(f"📊 Updated stats for {len(player_goals)} goalscorers in match {match_id}")

    except Exception as e:
        logger.error(f"Error updating player stats from match: {e}")


def get_flat_track_context(home_team: str, away_team: str, home_id: int, away_id: int,
                           opponent_home_pos: int, opponent_away_pos: int) -> dict:
    """
    Get flat track bully context for a match.

    Analyzes if key players tend to score more/less based on opponent strength.
    Returns adjustments and warnings.
    """
    result = {
        "home_analysis": None,
        "away_analysis": None,
        "home_scoring_adjustment": 0,
        "away_scoring_adjustment": 0,
        "warnings": [],
        "available": False
    }

    # Analyze both teams
    home_analysis = analyze_flat_track_bully(home_team, home_id)
    away_analysis = analyze_flat_track_bully(away_team, away_id)

    result["home_analysis"] = home_analysis
    result["away_analysis"] = away_analysis

    if not home_analysis.get("available") and not away_analysis.get("available"):
        return result

    result["available"] = True

    # Determine opponent classes for this match
    home_opponent_class = get_opponent_class(opponent_away_pos)  # Home plays vs away
    away_opponent_class = get_opponent_class(opponent_home_pos)  # Away plays vs home

    # Home team scoring adjustment
    if home_analysis.get("available"):
        ratio = home_analysis.get("scoring_ratio", 1.0)

        if home_opponent_class == "top6" and ratio > 1.8:
            # Playing strong team, but scores more vs weak = reduce expected goals
            result["home_scoring_adjustment"] = -15
            if home_analysis.get("team_has_flat_track_bullies"):
                bullies = [p["name"] for p in home_analysis.get("players", []) if p.get("is_flat_track_bully")]
                if bullies:
                    result["warnings"].append(
                        f"⚠️ {home_team}: {', '.join(bullies[:2])} хуже играют против топов (flat track bully)"
                    )

        elif home_opponent_class == "bottom6" and ratio > 1.5:
            # Playing weak team, scores well vs weak = boost expected goals
            result["home_scoring_adjustment"] = 10
            result["warnings"].append(
                f"🎯 {home_team}: Ключевые игроки хорошо забивают слабым → больше голов"
            )

    # Away team scoring adjustment
    if away_analysis.get("available"):
        ratio = away_analysis.get("scoring_ratio", 1.0)

        if away_opponent_class == "top6" and ratio > 1.8:
            result["away_scoring_adjustment"] = -15
            if away_analysis.get("team_has_flat_track_bullies"):
                bullies = [p["name"] for p in away_analysis.get("players", []) if p.get("is_flat_track_bully")]
                if bullies:
                    result["warnings"].append(
                        f"⚠️ {away_team}: {', '.join(bullies[:2])} хуже играют против топов (flat track bully)"
                    )

        elif away_opponent_class == "bottom6" and ratio > 1.5:
            result["away_scoring_adjustment"] = 10
            result["warnings"].append(
                f"🎯 {away_team}: Ключевые игроки хорошо забивают слабым → больше голов"
            )

    # Big game player bonus
    if home_opponent_class == "top6" and home_analysis.get("team_has_big_game_players"):
        result["home_scoring_adjustment"] += 8
        result["warnings"].append(f"💪 {home_team}: Есть игроки для больших матчей")

    if away_opponent_class == "top6" and away_analysis.get("team_has_big_game_players"):
        result["away_scoring_adjustment"] += 8
        result["warnings"].append(f"💪 {away_team}: Есть игроки для больших матчей")

    return result


def format_flat_track_context(flat_track: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format flat track bully analysis for display."""
    if not flat_track.get("available"):
        return ""

    warnings = flat_track.get("warnings", [])
    if not warnings:
        return ""

    lines = []

    if lang == "ru":
        lines.append("🎯 **ЭФФЕКТИВНОСТЬ VS СОПЕРНИКИ**")
    else:
        lines.append("🎯 **PERFORMANCE VS OPPONENT TYPE**")

    for warning in warnings:
        lines.append(f"  {warning}")

    # Summary of scoring adjustments
    home_adj = flat_track.get("home_scoring_adjustment", 0)
    away_adj = flat_track.get("away_scoring_adjustment", 0)

    if home_adj != 0 or away_adj != 0:
        lines.append("")
        if lang == "ru":
            if home_adj < -10:
                lines.append(f"  📉 {home_team}: Ожидай меньше голов ({home_adj}%)")
            elif home_adj > 5:
                lines.append(f"  📈 {home_team}: Ожидай больше голов (+{home_adj}%)")

            if away_adj < -10:
                lines.append(f"  📉 {away_team}: Ожидай меньше голов ({away_adj}%)")
            elif away_adj > 5:
                lines.append(f"  📈 {away_team}: Ожидай больше голов (+{away_adj}%)")

    return "\n".join(lines)


def format_player_impact(home_impact: dict, away_impact: dict,
                         home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format player impact analysis for display."""
    if not home_impact.get("available") and not away_impact.get("available"):
        return ""

    lines = []
    has_impact = False

    # Check if there's any significant impact
    home_out = home_impact.get("key_players_out", [])
    away_out = away_impact.get("key_players_out", [])

    if not home_out and not away_out:
        return ""

    if lang == "ru":
        lines.append("🌟 **ВЛИЯНИЕ КЛЮЧЕВЫХ ИГРОКОВ**")
    else:
        lines.append("🌟 **KEY PLAYER IMPACT**")

    # Home team
    if home_out:
        has_impact = True
        total_impact = home_impact.get("total_impact", 0)
        lines.append(f"  {home_team}:")

        for detail in home_impact.get("impact_details", [])[:3]:
            lines.append(f"    {detail}")

        if total_impact < -20:
            lines.append(f"    ⚠️ **КРИТИЧНО: Общее влияние {total_impact}%**")
        elif total_impact < -10:
            lines.append(f"    ⚡ Заметное влияние: {total_impact}%")

    # Away team
    if away_out:
        has_impact = True
        total_impact = away_impact.get("total_impact", 0)
        lines.append(f"  {away_team}:")

        for detail in away_impact.get("impact_details", [])[:3]:
            lines.append(f"    {detail}")

        if total_impact < -20:
            lines.append(f"    ⚠️ **КРИТИЧНО: Общее влияние {total_impact}%**")
        elif total_impact < -10:
            lines.append(f"    ⚡ Заметное влияние: {total_impact}%")

    # Analysis summary
    if has_impact:
        lines.append("")
        home_attack_mod = home_impact.get("attack_modifier", 0)
        away_attack_mod = away_impact.get("attack_modifier", 0)
        home_defense_mod = home_impact.get("defense_modifier", 0)
        away_defense_mod = away_impact.get("defense_modifier", 0)

        # Totals impact
        if home_attack_mod < -25 or away_attack_mod < -25:
            if lang == "ru":
                lines.append("  📉 Потеря ключевого атакующего → меньше голов (UNDER)")
            else:
                lines.append("  📉 Key attacker out → fewer goals (UNDER)")

        if home_defense_mod < -25 or away_defense_mod < -25:
            if lang == "ru":
                lines.append("  📈 Потеря ключевого защитника → больше пропустят (OVER)")
            else:
                lines.append("  📈 Key defender out → more goals conceded (OVER)")

    return "\n".join(lines) if has_impact else ""


# ===== ENSEMBLE ML SYSTEM =====
# Multiple models vote for higher confidence predictions

# Available model types for ensemble
ENSEMBLE_MODEL_TYPES = {
    "random_forest": {
        "class": "RandomForestClassifier",
        "params": {"n_estimators": 100, "max_depth": 10, "min_samples_split": 5, "random_state": 42},
        "weight": 1.0
    },
    "gradient_boost": {
        "class": "GradientBoostingClassifier",
        "params": {"n_estimators": 100, "max_depth": 5, "learning_rate": 0.1, "random_state": 42},
        "weight": 1.2  # Slightly higher weight - often performs better
    },
    "logistic": {
        "class": "LogisticRegression",
        "params": {"max_iter": 1000, "random_state": 42},
        "weight": 0.8  # Lower weight - simpler model
    },
}

# Loaded ensemble models cache
_ensemble_models = {}


def train_ensemble_models(bet_category: str = "match_result") -> dict:
    """
    Train multiple ML models for ensemble voting.

    Returns dict with training results for each model.
    """
    if not ML_AVAILABLE:
        return {"error": "ML libraries not available"}

    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

    results = {}

    try:
        # Load training data
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT features_json, target FROM ml_training_data
            WHERE bet_category = ? AND target IS NOT NULL
        """, (bet_category,))
        rows = c.fetchall()
        conn.close()

        if len(rows) < ML_MIN_SAMPLES:
            return {"error": f"Not enough samples: {len(rows)} < {ML_MIN_SAMPLES}"}

        # Prepare data
        X = []
        y = []
        feature_names = None

        for features_json, target in rows:
            features = json.loads(features_json)
            if feature_names is None:
                feature_names = sorted(features.keys())

            feature_vector = [features.get(f, 0) for f in feature_names]
            X.append(feature_vector)
            y.append(target)

        X = np.array(X)
        y = np.array(y)

        # Split data
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

        # Train each model type
        model_classes = {
            "random_forest": RandomForestClassifier(**ENSEMBLE_MODEL_TYPES["random_forest"]["params"]),
            "gradient_boost": GradientBoostingClassifier(**ENSEMBLE_MODEL_TYPES["gradient_boost"]["params"]),
            "logistic": LogisticRegression(**ENSEMBLE_MODEL_TYPES["logistic"]["params"]),
        }

        for model_name, model in model_classes.items():
            try:
                # Train
                model.fit(X_train, y_train)

                # Evaluate
                y_pred = model.predict(X_test)
                accuracy = accuracy_score(y_test, y_pred)
                precision = precision_score(y_test, y_pred, average='weighted', zero_division=0)
                recall = recall_score(y_test, y_pred, average='weighted', zero_division=0)
                f1 = f1_score(y_test, y_pred, average='weighted', zero_division=0)

                # Get feature importance
                if hasattr(model, 'feature_importances_'):
                    importance = dict(zip(feature_names, model.feature_importances_.tolist()))
                    # Top 10 features
                    importance = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True)[:10])
                else:
                    importance = {}

                # Save model
                model_path = f"{ML_MODELS_DIR}/{model_name}_{bet_category}.joblib"
                os.makedirs(ML_MODELS_DIR, exist_ok=True)
                joblib.dump({"model": model, "feature_names": feature_names}, model_path)

                # Save to database
                save_ensemble_model(
                    model_name=model_name,
                    bet_category=bet_category,
                    accuracy=accuracy,
                    precision_score=precision,
                    recall_score=recall,
                    f1_score=f1,
                    samples_count=len(rows),
                    feature_importance=importance,
                    model_path=model_path
                )

                # Cache model
                cache_key = f"{model_name}_{bet_category}"
                _ensemble_models[cache_key] = {
                    "model": model,
                    "feature_names": feature_names,
                    "accuracy": accuracy
                }

                results[model_name] = {
                    "accuracy": round(accuracy * 100, 1),
                    "precision": round(precision * 100, 1),
                    "recall": round(recall * 100, 1),
                    "f1": round(f1 * 100, 1),
                    "samples": len(rows)
                }

                logger.info(f"🤖 Trained {model_name} for {bet_category}: accuracy={accuracy:.1%}")

            except Exception as e:
                logger.error(f"Error training {model_name}: {e}")
                results[model_name] = {"error": str(e)}

        return results

    except Exception as e:
        logger.error(f"Ensemble training error: {e}")
        return {"error": str(e)}


def save_ensemble_model(model_name: str, bet_category: str, accuracy: float,
                        precision_score: float, recall_score: float, f1_score: float,
                        samples_count: int, feature_importance: dict, model_path: str):
    """Save ensemble model info to database."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            INSERT INTO ensemble_models
            (model_name, model_type, bet_category, accuracy, precision_score,
             recall_score, f1_score, samples_count, feature_importance, model_path, trained_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(model_name, bet_category) DO UPDATE SET
                accuracy = excluded.accuracy,
                precision_score = excluded.precision_score,
                recall_score = excluded.recall_score,
                f1_score = excluded.f1_score,
                samples_count = excluded.samples_count,
                feature_importance = excluded.feature_importance,
                model_path = excluded.model_path,
                trained_at = CURRENT_TIMESTAMP
        """, (model_name, model_name, bet_category, accuracy, precision_score,
              recall_score, f1_score, samples_count, json.dumps(feature_importance), model_path))

        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error saving ensemble model: {e}")


def load_ensemble_models(bet_category: str = "match_result") -> dict:
    """Load all ensemble models for a bet category."""
    models = {}

    for model_name in ENSEMBLE_MODEL_TYPES.keys():
        cache_key = f"{model_name}_{bet_category}"

        # Check cache first
        if cache_key in _ensemble_models:
            models[model_name] = _ensemble_models[cache_key]
            continue

        # Try to load from file
        model_path = f"{ML_MODELS_DIR}/{model_name}_{bet_category}.joblib"
        if os.path.exists(model_path):
            try:
                data = joblib.load(model_path)
                models[model_name] = data
                _ensemble_models[cache_key] = data
            except Exception as e:
                logger.error(f"Error loading {model_name}: {e}")

    return models


def get_ensemble_prediction(features: dict, bet_category: str = "match_result") -> dict:
    """
    Get prediction from ensemble of models with voting.

    Returns:
    {
        "prediction": int (predicted class),
        "confidence": float (0-100),
        "votes": {model_name: {"pred": int, "prob": float, "class_name": str}},
        "agreement": float (0-1, how many models agree),
        "consensus_boost": int (confidence adjustment based on agreement),
        "available": bool
    }
    """
    result = {
        "prediction": None,
        "confidence": 50,
        "votes": {},
        "agreement": 0,
        "consensus_boost": 0,
        "available": False
    }

    if not ML_AVAILABLE:
        return result

    # Load models
    models = load_ensemble_models(bet_category)

    if not models:
        return result

    # Get predictions from each model
    predictions = []
    probabilities = []

    for model_name, model_data in models.items():
        try:
            model = model_data.get("model")
            feature_names = model_data.get("feature_names", [])

            if not model or not feature_names:
                continue

            # Prepare feature vector
            feature_vector = np.array([[features.get(f, 0) for f in feature_names]])

            # Get prediction and probability
            pred = model.predict(feature_vector)[0]
            prob = model.predict_proba(feature_vector)[0]

            # Get probability of predicted class
            pred_prob = prob[pred] if pred < len(prob) else 0.5

            # Map prediction to class name
            class_names = {0: "away", 1: "draw", 2: "home"} if bet_category == "match_result" else {0: "no", 1: "yes"}
            class_name = class_names.get(pred, str(pred))

            # Weight by model's historical accuracy
            weight = ENSEMBLE_MODEL_TYPES.get(model_name, {}).get("weight", 1.0)

            result["votes"][model_name] = {
                "pred": int(pred),
                "prob": round(pred_prob * 100, 1),
                "class_name": class_name,
                "weight": weight
            }

            predictions.append(pred)
            probabilities.append(pred_prob * weight)

        except Exception as e:
            logger.error(f"Error getting prediction from {model_name}: {e}")

    if not predictions:
        return result

    result["available"] = True

    # Calculate consensus
    from collections import Counter
    vote_counts = Counter(predictions)
    most_common_pred, most_common_count = vote_counts.most_common(1)[0]

    result["prediction"] = int(most_common_pred)
    result["agreement"] = most_common_count / len(predictions)

    # Calculate weighted average probability
    total_weight = sum(ENSEMBLE_MODEL_TYPES.get(m, {}).get("weight", 1.0) for m in result["votes"].keys())
    weighted_prob = sum(probabilities) / total_weight if total_weight > 0 else 0.5

    # Base confidence from weighted probability
    base_confidence = weighted_prob * 100

    # Consensus boost
    if result["agreement"] >= 1.0:  # All models agree
        result["consensus_boost"] = 15
    elif result["agreement"] >= 0.67:  # Most models agree
        result["consensus_boost"] = 8
    elif result["agreement"] >= 0.5:  # Half agree
        result["consensus_boost"] = 0
    else:  # Disagreement
        result["consensus_boost"] = -10

    result["confidence"] = min(95, max(30, base_confidence + result["consensus_boost"]))

    return result


def format_ensemble_prediction(ensemble_result: dict, lang: str = "ru") -> str:
    """Format ensemble ML prediction for display."""
    if not ensemble_result.get("available"):
        return ""

    lines = []

    if lang == "ru":
        lines.append("🤖 **ML ENSEMBLE** (голосование моделей)")
    else:
        lines.append("🤖 **ML ENSEMBLE** (model voting)")

    votes = ensemble_result.get("votes", {})

    # Display each model's vote
    model_display_names = {
        "random_forest": "RandomForest",
        "gradient_boost": "GradientBoost",
        "logistic": "Logistic"
    }

    for model_name, vote in votes.items():
        display_name = model_display_names.get(model_name, model_name)
        prob = vote.get("prob", 50)
        class_name = vote.get("class_name", "?")

        # Emoji for prediction
        if class_name == "home":
            emoji = "🏠"
        elif class_name == "away":
            emoji = "✈️"
        elif class_name == "draw":
            emoji = "🤝"
        elif class_name == "yes":
            emoji = "✅"
        else:
            emoji = "❌"

        lines.append(f"  {display_name:14} {prob:5.1f}% → {emoji} {class_name.upper()}")

    # Divider
    lines.append("  " + "─" * 30)

    # Consensus
    agreement = ensemble_result.get("agreement", 0)
    consensus_boost = ensemble_result.get("consensus_boost", 0)
    confidence = ensemble_result.get("confidence", 50)

    num_models = len(votes)
    agreeing = int(agreement * num_models)

    if agreement >= 1.0:
        consensus_emoji = "🟢"
        consensus_text = "ПОЛНЫЙ" if lang == "ru" else "FULL"
    elif agreement >= 0.67:
        consensus_emoji = "🟡"
        consensus_text = "ВЫСОКИЙ" if lang == "ru" else "HIGH"
    else:
        consensus_emoji = "🔴"
        consensus_text = "НИЗКИЙ" if lang == "ru" else "LOW"

    boost_str = f"+{consensus_boost}" if consensus_boost > 0 else str(consensus_boost)

    if lang == "ru":
        lines.append(f"  КОНСЕНСУС: {agreeing}/{num_models} {consensus_emoji} ({boost_str}% уверенность)")
        lines.append(f"  **ИТОГО: {confidence:.0f}%**")
    else:
        lines.append(f"  CONSENSUS: {agreeing}/{num_models} {consensus_emoji} ({boost_str}% confidence)")
        lines.append(f"  **TOTAL: {confidence:.0f}%**")

    return "\n".join(lines)


# ===== IMPROVED EXPECTED GOALS CALCULATION =====
# Uses home/away specific stats instead of overall averages

# Average goals per game in major leagues (for reference/normalization)
LEAGUE_AVG_GOALS = {
    "PL": 2.70,   # Premier League
    "BL1": 3.10,  # Bundesliga (higher scoring)
    "SA": 2.55,   # Serie A
    "PD": 2.50,   # La Liga
    "FL1": 2.60,  # Ligue 1
    "CL": 2.85,   # Champions League
    "EL": 2.70,   # Europa League
    "BSA": 2.40,  # Brasileirão
    "DED": 3.00,  # Eredivisie (high scoring)
    "PPL": 2.45,  # Liga Portugal
    "default": 2.60
}


def calculate_expected_goals(home_form: dict, away_form: dict, league_code: str = None) -> dict:
    """Calculate expected goals using HOME/AWAY specific stats.

    This is more accurate than using overall averages because:
    - Home teams play differently at home vs away
    - Away teams play differently at home vs away

    Formula:
    - expected_home = home_team_home_scored * 0.6 + away_team_away_conceded * 0.4
    - expected_away = away_team_away_scored * 0.6 + home_team_home_conceded * 0.4

    Weights: Attack (0.6) > Defense (0.4) because team's own attack matters more.

    Returns dict with expected_home, expected_away, expected_total, and method used.
    """
    result = {
        "expected_home": 1.3,  # Default
        "expected_away": 1.0,
        "expected_total": 2.3,
        "method": "default",
        "confidence": "low"
    }

    # Get league average for normalization
    league_avg = LEAGUE_AVG_GOALS.get(league_code, LEAGUE_AVG_GOALS["default"]) if league_code else 2.60

    try:
        # Try to use HOME/AWAY specific stats (best method)
        home_home = home_form.get("home", {}) if home_form else {}
        away_away = away_form.get("away", {}) if away_form else {}

        home_home_scored = home_home.get("avg_goals_scored")
        home_home_conceded = home_home.get("avg_goals_conceded")
        away_away_scored = away_away.get("avg_goals_scored")
        away_away_conceded = away_away.get("avg_goals_conceded")

        # Check if we have HOME/AWAY specific data
        if all([home_home_scored, home_home_conceded, away_away_scored, away_away_conceded]):
            # Best method: use home/away specific averages
            # Weight: team's attack (0.6) + opponent's defense weakness (0.4)
            expected_home = home_home_scored * 0.6 + away_away_conceded * 0.4
            expected_away = away_away_scored * 0.6 + home_home_conceded * 0.4

            result["expected_home"] = round(expected_home, 2)
            result["expected_away"] = round(expected_away, 2)
            result["expected_total"] = round(expected_home + expected_away, 2)
            result["method"] = "home_away_specific"
            result["confidence"] = "high"

            # Add breakdown for transparency
            result["breakdown"] = {
                "home_attack": home_home_scored,
                "home_defense": home_home_conceded,
                "away_attack": away_away_scored,
                "away_defense": away_away_conceded
            }

        else:
            # Fallback: use overall averages (less accurate)
            home_overall = home_form.get("overall", {}) if home_form else {}
            away_overall = away_form.get("overall", {}) if away_form else {}

            home_scored = home_overall.get("avg_goals_scored", 1.4)
            home_conceded = home_overall.get("avg_goals_conceded", 1.2)
            away_scored = away_overall.get("avg_goals_scored", 1.2)
            away_conceded = away_overall.get("avg_goals_conceded", 1.4)

            # Simple average method
            expected_home = (home_scored + away_conceded) / 2
            expected_away = (away_scored + home_conceded) / 2

            result["expected_home"] = round(expected_home, 2)
            result["expected_away"] = round(expected_away, 2)
            result["expected_total"] = round(expected_home + expected_away, 2)
            result["method"] = "overall_average"
            result["confidence"] = "medium"

        # Apply league normalization (optional boost/reduction)
        # If league is high-scoring (like Bundesliga), slightly increase expectation
        league_factor = league_avg / 2.60  # 2.60 is our baseline
        if league_factor > 1.05 or league_factor < 0.95:
            result["expected_total"] = round(result["expected_total"] * league_factor, 2)
            result["league_adjustment"] = round((league_factor - 1) * 100, 1)

    except Exception as e:
        logger.error(f"Expected goals calculation error: {e}")
        result["method"] = "error_fallback"
        result["confidence"] = "low"

    return result


def validate_totals_prediction(bet_type: str, confidence: int, home_form: dict, away_form: dict,
                                league_code: str = None) -> tuple:
    """Validate totals prediction against expected goals (using improved calculation).
    Returns (validated_bet_type, validated_confidence, warning_message)"""

    if not bet_type or not home_form or not away_form:
        return bet_type, confidence, None

    bet_lower = bet_type.lower()

    # Only validate totals bets
    if "тб" not in bet_lower and "тм" not in bet_lower and "over" not in bet_lower and "under" not in bet_lower:
        return bet_type, confidence, None

    # Use improved expected goals calculation
    try:
        exp_goals = calculate_expected_goals(home_form, away_form, league_code)
        expected_total = exp_goals["expected_total"]
        method = exp_goals["method"]

        logger.info(f"Totals validation: expected={expected_total:.2f} ({method}), bet={bet_type}, league={league_code}")

        is_over = "тб" in bet_lower or "over" in bet_lower or "больше" in bet_lower
        is_under = "тм" in bet_lower or "under" in bet_lower or "меньше" in bet_lower

        # STRICT VALIDATION
        if is_over and expected_total < 2.3:
            # Over recommended but expected goals too low!
            warning = f"⚠️ КОНТР-ПРОВЕРКА: ожидаемые голы={expected_total:.1f} < 2.5, ТБ рискован!"
            logger.warning(f"Totals mismatch: Over but expected={expected_total:.2f}")
            new_confidence = min(confidence, 60)
            return bet_type, new_confidence, warning

        if is_under and expected_total > 2.7:
            # Under recommended but expected goals too high!
            warning = f"⚠️ КОНТР-ПРОВЕРКА: ожидаемые голы={expected_total:.1f} > 2.5, ТМ рискован!"
            logger.warning(f"Totals mismatch: Under but expected={expected_total:.2f}")
            new_confidence = min(confidence, 60)
            return bet_type, new_confidence, warning

        # Good match - boost confidence slightly if strong signal
        if is_over and expected_total > 3.0:
            return bet_type, min(confidence + 5, 85), None
        if is_under and expected_total < 2.0:
            return bet_type, min(confidence + 5, 85), None

    except Exception as e:
        logger.error(f"Totals validation error: {e}")

    return bet_type, confidence, None


def check_bet_result(bet_type, home_score, away_score):
    """Check if bet was correct based on score"""
    total_goals = home_score + away_score
    bet_lower = bet_type.lower() if bet_type else ""
    bet_upper = bet_type.upper() if bet_type else ""
    
    # Handicaps (Фора)
    if "фора" in bet_lower or "handicap" in bet_lower:
        # Parse handicap value
        handicap_match = re.search(r'\(?([-+]?\d+\.?\d*)\)?', bet_type)
        if handicap_match:
            handicap = float(handicap_match.group(1))
            
            # Home team handicap (Фора1)
            if "1" in bet_type or "home" in bet_lower:
                adjusted_home = home_score + handicap
                if adjusted_home > away_score:
                    return True
                elif adjusted_home < away_score:
                    return False
                else:
                    return None  # Push/refund
            
            # Away team handicap (Фора2)
            elif "2" in bet_type or "away" in bet_lower:
                adjusted_away = away_score + handicap
                if adjusted_away > home_score:
                    return True
                elif adjusted_away < home_score:
                    return False
                else:
                    return None
        
        # Default: assume home -1 handicap
        return (home_score - 1) > away_score
    
    # Home win
    if bet_type == "П1" or "победа хозя" in bet_lower or "home win" in bet_lower or bet_type == "1":
        return home_score > away_score
    
    # Away win
    elif bet_type == "П2" or "победа гост" in bet_lower or "away win" in bet_lower or bet_type == "2":
        return away_score > home_score
    
    # Draw
    elif bet_type == "Х" or "ничья" in bet_lower or "draw" in bet_lower:
        return home_score == away_score
    
    # 12 (not draw)
    elif bet_type == "12" or "не ничья" in bet_lower:
        return home_score != away_score
    
    # Over 2.5
    elif "ТБ" in bet_upper or "тотал больше" in bet_lower or "over" in bet_lower or "больше 2" in bet_lower:
        return total_goals > 2.5
    
    # Under 2.5
    elif "ТМ" in bet_upper or "тотал меньше" in bet_lower or "under" in bet_lower or "меньше 2" in bet_lower:
        return total_goals < 2.5
    
    # BTTS
    elif "BTTS" in bet_upper or "обе забьют" in bet_lower or "both teams" in bet_lower:
        return home_score > 0 and away_score > 0
    
    # Double chance 1X
    elif "1X" in bet_upper or "двойной шанс 1" in bet_lower:
        return home_score >= away_score
    
    # Double chance X2
    elif "X2" in bet_upper or "двойной шанс 2" in bet_lower:
        return away_score >= home_score
    
    # If we can't determine bet type
    elif "analysis" in bet_lower or bet_type == "":
        return home_score > away_score
    
    return None


# ===== MACHINE LEARNING SYSTEM =====

# All ML features with default values - EASY TO EXTEND!
# When adding new features: just add them here and in extract_features()
ML_FEATURE_COLUMNS = {
    # Form features
    "home_wins": 0,
    "home_draws": 0,
    "home_losses": 0,
    "home_goals_scored": 1.5,
    "home_goals_conceded": 1.0,
    "home_home_win_rate": 50,
    "home_btts_pct": 50,
    "home_over25_pct": 50,
    "away_wins": 0,
    "away_draws": 0,
    "away_losses": 0,
    "away_goals_scored": 1.0,
    "away_goals_conceded": 1.5,
    "away_away_win_rate": 30,
    "away_btts_pct": 50,
    "away_over25_pct": 50,
    # Standings
    "home_position": 10,
    "away_position": 10,
    "position_diff": 0,
    # Odds
    "odds_home": 2.5,
    "odds_draw": 3.5,
    "odds_away": 3.0,
    "implied_home": 0.4,
    "implied_draw": 0.25,
    "implied_away": 0.35,
    # H2H
    "h2h_home_wins": 0,
    "h2h_draws": 0,
    "h2h_away_wins": 0,
    "h2h_total": 0,
    # Expected goals (improved calculation)
    "expected_goals": 2.5,
    "expected_home_goals": 1.3,
    "expected_away_goals": 1.0,
    "expected_goals_method": 0,  # 1 = home/away specific, 0 = overall
    # Aggregates
    "avg_btts_pct": 50,
    "avg_over25_pct": 50,
    # Referee features (NEW)
    "referee_cards_per_game": 4.0,
    "referee_penalties_per_game": 0.32,
    "referee_reds_per_game": 0.12,
    "referee_style": 2,  # 4=very_strict, 3=strict, 2=balanced, 1=lenient
    "referee_cards_vs_avg": 0,
    # Web news indicator
    "has_web_news": 0,
    # Fixture congestion (calendar load)
    "home_rest_days": 5,           # Days since last match
    "away_rest_days": 5,
    "home_congestion_score": 0,    # 0=fresh, 1=normal, 2=tired, 3=exhausted
    "away_congestion_score": 0,
    "rest_advantage": 0,           # Positive = home has more rest
    # Motivation factors
    "is_derby": 0,                 # 1 if derby match
    "home_motivation": 5,          # 1-10 scale
    "away_motivation": 5,
    "home_relegation_battle": 0,   # 1 if in bottom 4
    "away_relegation_battle": 0,
    "home_title_race": 0,          # 1 if in top 3
    "away_title_race": 0,
    "motivation_diff": 0,          # home_motivation - away_motivation
    # Team class (elite factor)
    "home_is_elite": 0,            # 1 if in TOP_CLUBS list (Real, Barca, Bayern, etc.)
    "away_is_elite": 0,
    "home_team_class": 2,          # 4=elite, 3=strong, 2=midtable, 1=weak, 0=relegation
    "away_team_class": 2,
    "class_diff": 0,               # Positive = home is higher class
    "elite_vs_underdog": 0,        # 1 if elite plays weak/relegation team
    "class_mismatch": 0,           # Absolute class difference (for upset detection)
    # Line movement (sharp money indicators)
    "home_odds_dropped": 0,        # 1 if home odds dropped significantly (sharp money)
    "away_odds_dropped": 0,        # 1 if away odds dropped (sharp money on away)
    "draw_odds_dropped": 0,        # 1 if draw odds dropped
    "over_odds_dropped": 0,        # 1 if over odds dropped (sharps on goals)
    "under_odds_dropped": 0,       # 1 if under odds dropped
    "sharp_money_detected": 0,     # 1 if any significant sharp money movement
    "line_movement_direction": 0,  # -1=away favored more, 0=stable, 1=home favored more
    # Coach change features
    "home_new_coach": 0,           # 1 if home team has new coach (<5 matches)
    "away_new_coach": 0,           # 1 if away team has new coach (<5 matches)
    "home_coach_boost": 0,         # 0-15 boost from new coach effect
    "away_coach_boost": 0,         # 0-15 boost from new coach effect
    # Lineup and injury features
    "home_injuries": 0,            # Number of injured players
    "away_injuries": 0,
    "total_injuries": 0,
    "home_lineup_confirmed": 0,    # 1 if lineup known
    "away_lineup_confirmed": 0,
    "home_injury_crisis": 0,       # 1 if 6+ injuries
    "away_injury_crisis": 0,
    "fatigue_risk": 0,             # 1 if any team is tired
    # xG (Expected Goals) features - CRITICAL for totals
    "home_xg_per_game": 1.3,
    "away_xg_per_game": 1.0,
    "home_xga_per_game": 1.0,
    "away_xga_per_game": 1.3,
    "home_xg_diff": 0,             # xG - actual goals (positive = underperforming)
    "away_xg_diff": 0,
    "total_xg_deviation": 0,
    "xg_expected_total": 2.5,
    "xg_expected_home": 1.3,
    "xg_expected_away": 1.0,
    "home_recent_xg": 1.3,         # Last 5 games xG
    "away_recent_xg": 1.0,
    "recent_xg_total": 2.3,
    "home_unlucky": 0,             # 1 if xG_diff > 2 (should score more)
    "away_unlucky": 0,
    "home_lucky": 0,               # 1 if xG_diff < -2 (overperforming)
    "away_lucky": 0,
    "both_underperforming": 0,     # 1 if total xG deviation > 3
    "both_overperforming": 0,
    "xg_data_available": 0,
    # Player Impact features - KEY for injuries of star players
    "home_attack_modifier": 0,     # Negative = weaker attack
    "away_attack_modifier": 0,
    "home_defense_modifier": 0,
    "away_defense_modifier": 0,
    "home_goals_modifier": 0,      # Direct goals impact
    "away_goals_modifier": 0,
    "home_total_impact": 0,        # Total team impact %
    "away_total_impact": 0,
    "home_key_players_out": 0,     # Count of key players injured
    "away_key_players_out": 0,
    "home_star_out": 0,            # 1 if attack modifier < -25%
    "away_star_out": 0,
    "home_defense_crisis": 0,      # 1 if defense modifier < -25%
    "away_defense_crisis": 0,
    "player_impact_available": 0,
    # Flat Track Bully features - players who score vs weak but not strong teams
    "home_scoring_ratio": 1.0,     # bottom6/top6 scoring ratio (>2 = flat track bully team)
    "away_scoring_ratio": 1.0,
    "home_has_flat_track_bully": 0,  # 1 if team has flat track bullies
    "away_has_flat_track_bully": 0,
    "home_has_big_game_player": 0,   # 1 if team has big game players
    "away_has_big_game_player": 0,
    "home_scoring_adjustment": 0,    # Expected goals adjustment based on opponent
    "away_scoring_adjustment": 0,
    "flat_track_available": 0,
}


def extract_features(home_form: dict, away_form: dict, standings: dict,
                     odds: dict, h2h: list, home_team: str, away_team: str,
                     referee_stats: dict = None, has_web_news: bool = False,
                     congestion: dict = None, motivation: dict = None,
                     team_class: dict = None, coach_factor: dict = None,
                     lineups: dict = None, xg_data: dict = None,
                     player_impact: dict = None, flat_track_context: dict = None) -> dict:
    """Extract numerical features for ML model including all factors + player impact + flat track bully"""
    features = {}

    # Home team form features
    if home_form:
        home_overall = home_form.get("overall", {})
        home_home = home_form.get("home", {})
        features["home_wins"] = home_overall.get("wins", 0)
        features["home_draws"] = home_overall.get("draws", 0)
        features["home_losses"] = home_overall.get("losses", 0)
        features["home_goals_scored"] = home_overall.get("avg_goals_scored", 1.5)
        features["home_goals_conceded"] = home_overall.get("avg_goals_conceded", 1.0)
        features["home_home_win_rate"] = home_home.get("win_rate", 50)
        features["home_btts_pct"] = home_form.get("btts_percent", 50)
        features["home_over25_pct"] = home_form.get("over25_percent", 50)
    else:
        features["home_wins"] = 0
        features["home_draws"] = 0
        features["home_losses"] = 0
        features["home_goals_scored"] = 1.5
        features["home_goals_conceded"] = 1.0
        features["home_home_win_rate"] = 50
        features["home_btts_pct"] = 50
        features["home_over25_pct"] = 50

    # Away team form features
    if away_form:
        away_overall = away_form.get("overall", {})
        away_away = away_form.get("away", {})
        features["away_wins"] = away_overall.get("wins", 0)
        features["away_draws"] = away_overall.get("draws", 0)
        features["away_losses"] = away_overall.get("losses", 0)
        features["away_goals_scored"] = away_overall.get("avg_goals_scored", 1.0)
        features["away_goals_conceded"] = away_overall.get("avg_goals_conceded", 1.5)
        features["away_away_win_rate"] = away_away.get("win_rate", 30)
        features["away_btts_pct"] = away_form.get("btts_percent", 50)
        features["away_over25_pct"] = away_form.get("over25_percent", 50)
    else:
        features["away_wins"] = 0
        features["away_draws"] = 0
        features["away_losses"] = 0
        features["away_goals_scored"] = 1.0
        features["away_goals_conceded"] = 1.5
        features["away_away_win_rate"] = 30
        features["away_btts_pct"] = 50
        features["away_over25_pct"] = 50

    # Standings features
    features["home_position"] = 10
    features["away_position"] = 10
    if standings:
        for team in standings.get("standings", []):
            team_name = team.get("team", {}).get("name", "").lower()
            if home_team.lower() in team_name or team_name in home_team.lower():
                features["home_position"] = team.get("position", 10)
            if away_team.lower() in team_name or team_name in away_team.lower():
                features["away_position"] = team.get("position", 10)

    features["position_diff"] = features["home_position"] - features["away_position"]

    # Odds features (implied probabilities)
    if odds:
        features["odds_home"] = odds.get("home", 2.5)
        features["odds_draw"] = odds.get("draw", 3.5)
        features["odds_away"] = odds.get("away", 3.0)
        # Implied probabilities
        features["implied_home"] = 1 / features["odds_home"] if features["odds_home"] > 0 else 0.4
        features["implied_draw"] = 1 / features["odds_draw"] if features["odds_draw"] > 0 else 0.25
        features["implied_away"] = 1 / features["odds_away"] if features["odds_away"] > 0 else 0.35

        # Line movement features (sharp money indicators)
        movements_raw = odds.get("_line_movements", {})
        # Filter out metadata keys (start with _)
        movements = {k: v for k, v in movements_raw.items()
                    if not k.startswith("_") and isinstance(v, dict)}

        if movements:
            # Check each outcome for sharp money (odds dropped)
            for outcome_key in ["Home", "home", "1"]:
                if outcome_key in movements and movements[outcome_key].get("sharp"):
                    features["home_odds_dropped"] = 1
                    break
            for outcome_key in ["Away", "away", "2"]:
                if outcome_key in movements and movements[outcome_key].get("sharp"):
                    features["away_odds_dropped"] = 1
                    break
            for outcome_key in ["Draw", "draw", "X"]:
                if outcome_key in movements and movements[outcome_key].get("sharp"):
                    features["draw_odds_dropped"] = 1
                    break
            for outcome_key in ["Over_2.5", "over", "Over 2.5"]:
                if outcome_key in movements and movements[outcome_key].get("sharp"):
                    features["over_odds_dropped"] = 1
                    break
            for outcome_key in ["Under_2.5", "under", "Under 2.5"]:
                if outcome_key in movements and movements[outcome_key].get("sharp"):
                    features["under_odds_dropped"] = 1
                    break

            # Overall sharp money detection
            sharp_count = sum(1 for m in movements.values() if isinstance(m, dict) and m.get("sharp"))
            features["sharp_money_detected"] = 1 if sharp_count > 0 else 0

            # Direction: positive if home favored more now, negative if away
            home_change = movements.get("Home", movements.get("home", {})).get("change", 0)
            away_change = movements.get("Away", movements.get("away", {})).get("change", 0)
            if home_change < -0.1 and away_change > 0.05:
                features["line_movement_direction"] = 1  # Sharp on home
            elif away_change < -0.1 and home_change > 0.05:
                features["line_movement_direction"] = -1  # Sharp on away
            else:
                features["line_movement_direction"] = 0  # Stable
    else:
        features["odds_home"] = 2.5
        features["odds_draw"] = 3.5
        features["odds_away"] = 3.0
        features["implied_home"] = 0.4
        features["implied_draw"] = 0.25
        features["implied_away"] = 0.35

    # H2H features
    h2h_home_wins = 0
    h2h_draws = 0
    h2h_away_wins = 0
    if h2h:
        for match in h2h[:10]:
            score = match.get("score", {}).get("fullTime", {})
            h_goals = score.get("home", 0) or 0
            a_goals = score.get("away", 0) or 0
            if h_goals > a_goals:
                h2h_home_wins += 1
            elif h_goals < a_goals:
                h2h_away_wins += 1
            else:
                h2h_draws += 1

    features["h2h_home_wins"] = h2h_home_wins
    features["h2h_draws"] = h2h_draws
    features["h2h_away_wins"] = h2h_away_wins
    features["h2h_total"] = h2h_home_wins + h2h_draws + h2h_away_wins

    # Calculated features - use improved expected goals calculation
    exp_goals = calculate_expected_goals(home_form, away_form)
    features["expected_goals"] = exp_goals["expected_total"]
    features["expected_home_goals"] = exp_goals["expected_home"]
    features["expected_away_goals"] = exp_goals["expected_away"]
    features["expected_goals_method"] = 1 if exp_goals["method"] == "home_away_specific" else 0

    features["avg_btts_pct"] = (features["home_btts_pct"] + features["away_btts_pct"]) / 2
    features["avg_over25_pct"] = (features["home_over25_pct"] + features["away_over25_pct"]) / 2

    # Referee features (for card/penalty predictions)
    if referee_stats:
        features["referee_cards_per_game"] = referee_stats.get("cards_per_game", 4.0)
        features["referee_penalties_per_game"] = referee_stats.get("penalties_per_game", 0.32)
        features["referee_reds_per_game"] = referee_stats.get("reds_per_game", 0.12)
        # Style as numeric: very_strict=4, strict=3, balanced=2, lenient=1
        style_map = {"very_strict": 4, "strict": 3, "balanced": 2, "lenient": 1}
        features["referee_style"] = style_map.get(referee_stats.get("style", "balanced"), 2)
        features["referee_cards_vs_avg"] = referee_stats.get("cards_vs_avg", 0)
    else:
        features["referee_cards_per_game"] = 4.0  # Default
        features["referee_penalties_per_game"] = 0.32
        features["referee_reds_per_game"] = 0.12
        features["referee_style"] = 2  # Balanced
        features["referee_cards_vs_avg"] = 0

    # Web news indicator (1 if we have fresh news)
    features["has_web_news"] = 1 if has_web_news else 0

    # Fixture congestion features (calendar load)
    if congestion:
        features["home_rest_days"] = congestion.get("home_rest_days", 5)
        features["away_rest_days"] = congestion.get("away_rest_days", 5)
        features["home_congestion_score"] = congestion.get("home_congestion", 0)
        features["away_congestion_score"] = congestion.get("away_congestion", 0)
        features["rest_advantage"] = congestion.get("rest_advantage", 0)
        features["fatigue_risk"] = 1 if (congestion.get("home_tired") or congestion.get("away_tired")) else 0
    else:
        features["home_rest_days"] = 5
        features["away_rest_days"] = 5
        features["home_congestion_score"] = 0
        features["away_congestion_score"] = 0
        features["rest_advantage"] = 0
        features["fatigue_risk"] = 0

    # Motivation features (derby, relegation, title race)
    if motivation:
        features["is_derby"] = 1 if motivation.get("is_derby") else 0
        features["home_motivation"] = motivation.get("home_motivation", 5)
        features["away_motivation"] = motivation.get("away_motivation", 5)
        features["motivation_diff"] = motivation.get("motivation_diff", 0)
        features["home_relegation_battle"] = 1 if motivation.get("home_relegation") else 0
        features["away_relegation_battle"] = 1 if motivation.get("away_relegation") else 0
        features["home_title_race"] = 1 if motivation.get("home_title_race") else 0
        features["away_title_race"] = 1 if motivation.get("away_title_race") else 0
    else:
        features["is_derby"] = 0
        features["home_motivation"] = 5
        features["away_motivation"] = 5
        features["motivation_diff"] = 0
        features["home_relegation_battle"] = 0
        features["away_relegation_battle"] = 0
        features["home_title_race"] = 0
        features["away_title_race"] = 0

    # Team class features (elite factor)
    if team_class:
        features["home_is_elite"] = 1 if team_class.get("home_is_elite") else 0
        features["away_is_elite"] = 1 if team_class.get("away_is_elite") else 0
        features["home_team_class"] = team_class.get("home_class", 2)
        features["away_team_class"] = team_class.get("away_class", 2)
        features["class_diff"] = team_class.get("class_diff", 0)
        features["elite_vs_underdog"] = team_class.get("elite_vs_underdog", 0)
        features["class_mismatch"] = team_class.get("class_mismatch", 0)
    else:
        features["home_is_elite"] = 0
        features["away_is_elite"] = 0
        features["home_team_class"] = 2
        features["away_team_class"] = 2
        features["class_diff"] = 0
        features["elite_vs_underdog"] = 0
        features["class_mismatch"] = 0

    # Coach change features (new coach boost)
    if coach_factor:
        features["home_new_coach"] = 1 if coach_factor.get("home_new_coach") else 0
        features["away_new_coach"] = 1 if coach_factor.get("away_new_coach") else 0
        features["home_coach_boost"] = coach_factor.get("home_coach_boost", 0)
        features["away_coach_boost"] = coach_factor.get("away_coach_boost", 0)
    else:
        features["home_new_coach"] = 0
        features["away_new_coach"] = 0
        features["home_coach_boost"] = 0
        features["away_coach_boost"] = 0

    # Lineups and injuries features (CRITICAL for prediction accuracy!)
    if lineups:
        home_injuries_list = lineups.get("home_injuries", [])
        away_injuries_list = lineups.get("away_injuries", [])
        home_lineup = lineups.get("home_lineup", [])
        away_lineup = lineups.get("away_lineup", [])

        features["home_injuries"] = len(home_injuries_list)
        features["away_injuries"] = len(away_injuries_list)
        features["total_injuries"] = len(home_injuries_list) + len(away_injuries_list)
        features["home_lineup_confirmed"] = 1 if home_lineup else 0
        features["away_lineup_confirmed"] = 1 if away_lineup else 0

        # High injury count is a risk factor
        features["home_injury_crisis"] = 1 if len(home_injuries_list) >= 6 else 0
        features["away_injury_crisis"] = 1 if len(away_injuries_list) >= 6 else 0

        logger.debug(f"🏥 Injuries extracted: home={len(home_injuries_list)}, away={len(away_injuries_list)}")
    else:
        features["home_injuries"] = 0
        features["away_injuries"] = 0
        features["total_injuries"] = 0
        features["home_lineup_confirmed"] = 0
        features["away_lineup_confirmed"] = 0
        features["home_injury_crisis"] = 0
        features["away_injury_crisis"] = 0

    # xG (Expected Goals) features - CRITICAL for totals predictions!
    if xg_data and xg_data.get("available"):
        home_xg = xg_data.get("home_xg", {})
        away_xg = xg_data.get("away_xg", {})

        # Per-game xG metrics
        features["home_xg_per_game"] = home_xg.get("xg_per_game", 1.3)
        features["away_xg_per_game"] = away_xg.get("xg_per_game", 1.0)
        features["home_xga_per_game"] = home_xg.get("xga_per_game", 1.0)
        features["away_xga_per_game"] = away_xg.get("xga_per_game", 1.3)

        # xG deviation (positive = underperforming, likely to score more)
        features["home_xg_diff"] = home_xg.get("xg_diff", 0)
        features["away_xg_diff"] = away_xg.get("xg_diff", 0)
        features["total_xg_deviation"] = xg_data.get("total_xg_deviation", 0)

        # Expected total from real xG data
        features["xg_expected_total"] = xg_data.get("expected_total", 2.5)
        features["xg_expected_home"] = xg_data.get("expected_home_goals", 1.3)
        features["xg_expected_away"] = xg_data.get("expected_away_goals", 1.0)

        # Recent form xG (last 5 games - more predictive)
        features["home_recent_xg"] = home_xg.get("recent_xg_per_game", 1.3)
        features["away_recent_xg"] = away_xg.get("recent_xg_per_game", 1.0)
        features["recent_xg_total"] = xg_data.get("recent_xg_total", 2.3)

        # Luck indicators (for regression predictions)
        features["home_unlucky"] = 1 if home_xg.get("xg_diff", 0) > 2 else 0
        features["away_unlucky"] = 1 if away_xg.get("xg_diff", 0) > 2 else 0
        features["home_lucky"] = 1 if home_xg.get("xg_diff", 0) < -2 else 0
        features["away_lucky"] = 1 if away_xg.get("xg_diff", 0) < -2 else 0

        # Combined signals
        features["both_underperforming"] = 1 if xg_data.get("total_xg_deviation", 0) > 3 else 0
        features["both_overperforming"] = 1 if xg_data.get("total_xg_deviation", 0) < -3 else 0

        features["xg_data_available"] = 1
        logger.debug(f"📊 xG features: home={features['home_xg_per_game']:.2f}, away={features['away_xg_per_game']:.2f}, total={features['xg_expected_total']:.2f}")
    else:
        # Default xG features when data not available
        features["home_xg_per_game"] = 1.3
        features["away_xg_per_game"] = 1.0
        features["home_xga_per_game"] = 1.0
        features["away_xga_per_game"] = 1.3
        features["home_xg_diff"] = 0
        features["away_xg_diff"] = 0
        features["total_xg_deviation"] = 0
        features["xg_expected_total"] = 2.5
        features["xg_expected_home"] = 1.3
        features["xg_expected_away"] = 1.0
        features["home_recent_xg"] = 1.3
        features["away_recent_xg"] = 1.0
        features["recent_xg_total"] = 2.3
        features["home_unlucky"] = 0
        features["away_unlucky"] = 0
        features["home_lucky"] = 0
        features["away_lucky"] = 0
        features["both_underperforming"] = 0
        features["both_overperforming"] = 0
        features["xg_data_available"] = 0

    # Player Impact features - KEY for predictions when stars are injured!
    if player_impact:
        home_impact = player_impact.get("home", {})
        away_impact = player_impact.get("away", {})

        # Attack/defense modifiers (negative values = weaker)
        features["home_attack_modifier"] = home_impact.get("attack_modifier", 0)
        features["away_attack_modifier"] = away_impact.get("attack_modifier", 0)
        features["home_defense_modifier"] = home_impact.get("defense_modifier", 0)
        features["away_defense_modifier"] = away_impact.get("defense_modifier", 0)

        # Goals modifier (direct impact on scoring)
        features["home_goals_modifier"] = home_impact.get("goals_modifier", 0)
        features["away_goals_modifier"] = away_impact.get("goals_modifier", 0)

        # Total impact
        features["home_total_impact"] = home_impact.get("total_impact", 0)
        features["away_total_impact"] = away_impact.get("total_impact", 0)

        # Number of key players out
        features["home_key_players_out"] = len(home_impact.get("key_players_out", []))
        features["away_key_players_out"] = len(away_impact.get("key_players_out", []))

        # Critical flags
        features["home_star_out"] = 1 if home_impact.get("attack_modifier", 0) < -25 else 0
        features["away_star_out"] = 1 if away_impact.get("attack_modifier", 0) < -25 else 0
        features["home_defense_crisis"] = 1 if home_impact.get("defense_modifier", 0) < -25 else 0
        features["away_defense_crisis"] = 1 if away_impact.get("defense_modifier", 0) < -25 else 0

        features["player_impact_available"] = 1
        logger.debug(f"🌟 Player impact: home={features['home_total_impact']}%, away={features['away_total_impact']}%")
    else:
        features["home_attack_modifier"] = 0
        features["away_attack_modifier"] = 0
        features["home_defense_modifier"] = 0
        features["away_defense_modifier"] = 0
        features["home_goals_modifier"] = 0
        features["away_goals_modifier"] = 0
        features["home_total_impact"] = 0
        features["away_total_impact"] = 0
        features["home_key_players_out"] = 0
        features["away_key_players_out"] = 0
        features["home_star_out"] = 0
        features["away_star_out"] = 0
        features["home_defense_crisis"] = 0
        features["away_defense_crisis"] = 0
        features["player_impact_available"] = 0

    # Flat Track Bully features - players scoring vs weak teams but not strong
    if flat_track_context and flat_track_context.get("available"):
        home_analysis = flat_track_context.get("home_analysis", {})
        away_analysis = flat_track_context.get("away_analysis", {})

        # Scoring ratio (bottom6/top6 goals - ratio > 2 = flat track bully team)
        features["home_scoring_ratio"] = home_analysis.get("scoring_ratio", 1.0)
        features["away_scoring_ratio"] = away_analysis.get("scoring_ratio", 1.0)

        # Team has flat track bullies / big game players
        features["home_has_flat_track_bully"] = 1 if home_analysis.get("team_has_flat_track_bullies") else 0
        features["away_has_flat_track_bully"] = 1 if away_analysis.get("team_has_flat_track_bullies") else 0
        features["home_has_big_game_player"] = 1 if home_analysis.get("team_has_big_game_players") else 0
        features["away_has_big_game_player"] = 1 if away_analysis.get("team_has_big_game_players") else 0

        # Scoring adjustment based on opponent class
        features["home_scoring_adjustment"] = flat_track_context.get("home_scoring_adjustment", 0)
        features["away_scoring_adjustment"] = flat_track_context.get("away_scoring_adjustment", 0)

        features["flat_track_available"] = 1
        logger.debug(f"🎯 Flat track: home_ratio={features['home_scoring_ratio']:.2f}, away_ratio={features['away_scoring_ratio']:.2f}")
    else:
        features["home_scoring_ratio"] = 1.0
        features["away_scoring_ratio"] = 1.0
        features["home_has_flat_track_bully"] = 0
        features["away_has_flat_track_bully"] = 0
        features["home_has_big_game_player"] = 0
        features["away_has_big_game_player"] = 0
        features["home_scoring_adjustment"] = 0
        features["away_scoring_adjustment"] = 0
        features["flat_track_available"] = 0

    return features


def save_ml_training_data(prediction_id: int, bet_category: str, features: dict, target: int = None, bet_rank: int = 1):
    """Save features for ML training with bet rank (1=MAIN, 2+=ALT)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""INSERT INTO ml_training_data (prediction_id, bet_category, features_json, target, bet_rank)
                     VALUES (?, ?, ?, ?, ?)""",
                  (prediction_id, bet_category, json.dumps(features), target, bet_rank))
        conn.commit()
        ml_id = c.lastrowid
        conn.close()
        logger.info(f"✅ ML data saved: id={ml_id}, pred={prediction_id}, cat={bet_category}, rank={bet_rank}, features={len(features)} keys")
    except Exception as e:
        logger.error(f"❌ Failed to save ML data: {e}")


def update_ml_training_target(prediction_id: int, target: int):
    """Update target (result) for ML training data"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE ml_training_data SET target = ? WHERE prediction_id = ?", (target, prediction_id))
    conn.commit()
    conn.close()


def get_ml_training_data(bet_category: str) -> tuple:
    """Get training data for specific bet category.

    Uses ML_FEATURE_COLUMNS for consistent feature ordering.
    Automatically uses all defined features - just add to ML_FEATURE_COLUMNS!
    """
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT features_json, target FROM ml_training_data
                 WHERE bet_category = ? AND target IS NOT NULL""", (bet_category,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None, None

    X = []
    y = []

    # Get feature names in consistent order
    feature_names = list(ML_FEATURE_COLUMNS.keys())

    for features_json, target in rows:
        try:
            features = json.loads(features_json)
            # Convert to list using ML_FEATURE_COLUMNS order and defaults
            feature_values = [
                features.get(name, default)
                for name, default in ML_FEATURE_COLUMNS.items()
            ]
            X.append(feature_values)
            y.append(target)
        except:
            continue

    logger.info(f"ML training data for {bet_category}: {len(X)} samples, {len(feature_names)} features")
    return X, y


def features_to_vector(features: dict) -> list:
    """Convert features dict to vector using ML_FEATURE_COLUMNS order.

    Used for predictions - ensures same order as training.
    """
    return [
        features.get(name, default)
        for name, default in ML_FEATURE_COLUMNS.items()
    ]


def train_ml_model(bet_category: str) -> Optional[dict]:
    """Train ML model for specific bet category"""
    if not ML_AVAILABLE:
        logger.warning("ML libraries not available")
        return None

    X, y = get_ml_training_data(bet_category)

    if X is None or len(X) < ML_MIN_SAMPLES:
        logger.info(f"Not enough data for {bet_category}: {len(X) if X else 0} samples")
        return None

    # Create models directory
    os.makedirs(ML_MODELS_DIR, exist_ok=True)

    # Split data
    X_train, X_test, y_train, y_test = train_test_split(
        np.array(X), np.array(y), test_size=0.2, random_state=42
    )

    # Train model (Gradient Boosting works well for tabular data)
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=5,
        learning_rate=0.1,
        random_state=42
    )
    model.fit(X_train, y_train)

    # Evaluate
    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    # Save model
    model_path = os.path.join(ML_MODELS_DIR, f"model_{bet_category}.pkl")
    joblib.dump(model, model_path)

    # Save metadata
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO ml_models (model_type, accuracy, samples_count, model_path)
                 VALUES (?, ?, ?, ?)""",
              (bet_category, accuracy, len(X), model_path))
    conn.commit()
    conn.close()

    logger.info(f"Trained {bet_category} model: accuracy={accuracy:.2%}, samples={len(X)}")

    return {
        "category": bet_category,
        "accuracy": accuracy,
        "samples": len(X),
        "model_path": model_path
    }


def train_all_models():
    """Train models for all bet categories with enough data"""
    categories = ["outcomes_home", "outcomes_away", "outcomes_draw",
                  "totals_over", "totals_under", "btts"]

    results = {}
    for cat in categories:
        result = train_ml_model(cat)
        if result:
            results[cat] = result

    return results


async def train_all_models_async(progress_callback=None):
    """Train models for all bet categories with progress updates.

    Args:
        progress_callback: async function(category, status, result) for progress updates

    Returns:
        dict with training results per category
    """
    categories = [
        ("outcomes_home", "П1 (победа хозяев)"),
        ("outcomes_away", "П2 (победа гостей)"),
        ("outcomes_draw", "X (ничья)"),
        ("totals_over", "ТБ 2.5 (тотал больше)"),
        ("totals_under", "ТМ 2.5 (тотал меньше)"),
        ("btts", "ОЗ (обе забьют)")
    ]

    results = {}

    for i, (cat_code, cat_name) in enumerate(categories):
        # Notify progress - starting category
        if progress_callback:
            await progress_callback(cat_name, "training", None, i + 1, len(categories))

        # Run training in thread pool to avoid blocking event loop
        result = await asyncio.to_thread(train_ml_model, cat_code)

        if result:
            results[cat_code] = result
            if progress_callback:
                await progress_callback(cat_name, "done", result, i + 1, len(categories))
        else:
            if progress_callback:
                await progress_callback(cat_name, "no_data", None, i + 1, len(categories))

    return results


def ml_predict(features: dict, bet_category: str) -> Optional[dict]:
    """Get ML prediction for a bet category.

    Uses features_to_vector() for consistent feature ordering with training.
    """
    if not ML_AVAILABLE:
        return None

    model_path = os.path.join(ML_MODELS_DIR, f"model_{bet_category}.pkl")

    if not os.path.exists(model_path):
        return None

    try:
        model = joblib.load(model_path)

        # Convert features to array using consistent ML_FEATURE_COLUMNS order
        feature_values = features_to_vector(features)
        X = np.array([feature_values])

        # Get probability
        proba = model.predict_proba(X)[0]
        prediction = model.predict(X)[0]

        return {
            "prediction": int(prediction),
            "confidence": float(max(proba) * 100),
            "probabilities": {
                "win": float(proba[1]) if len(proba) > 1 else float(proba[0]),
                "lose": float(proba[0]) if len(proba) > 1 else 0
            }
        }
    except Exception as e:
        logger.error(f"ML prediction error: {e}")
        return None


def get_all_ml_predictions(features: dict) -> dict:
    """Get ML predictions for all available bet types"""
    predictions = {}

    # Outcomes
    for cat in ["outcomes_home", "outcomes_away", "outcomes_draw"]:
        pred = ml_predict(features, cat)
        if pred:
            predictions[cat] = pred

    # Totals
    for cat in ["totals_over", "totals_under"]:
        pred = ml_predict(features, cat)
        if pred:
            predictions[cat] = pred

    # BTTS
    pred = ml_predict(features, "btts")
    if pred:
        predictions["btts"] = pred

    return predictions


def apply_ml_correction(bet_type: str, claude_confidence: int, ml_features: dict) -> tuple:
    """Apply ML correction to Claude's confidence.

    Returns: (adjusted_confidence, ml_status, ml_confidence)
    - ml_status: 'confirmed' | 'warning' | 'no_model' | None
    - ml_confidence: ML model's confidence or None
    """
    if not ML_AVAILABLE or not ml_features:
        return claude_confidence, None, None

    # Map bet_type to ML category
    bet_type_lower = bet_type.lower()
    ml_category = None

    if "п1" in bet_type_lower or bet_type_lower == "1x" or "победа хозя" in bet_type_lower:
        ml_category = "outcomes_home"
    elif "п2" in bet_type_lower or bet_type_lower == "x2" or "победа гост" in bet_type_lower:
        ml_category = "outcomes_away"
    elif bet_type_lower == "х" or "ничья" in bet_type_lower:
        ml_category = "outcomes_draw"
    elif "тб" in bet_type_lower or "over" in bet_type_lower:
        ml_category = "totals_over"
    elif "тм" in bet_type_lower or "under" in bet_type_lower:
        ml_category = "totals_under"
    elif "btts" in bet_type_lower or "обе забьют" in bet_type_lower:
        ml_category = "btts"

    if not ml_category:
        return claude_confidence, None, None

    # Get ML prediction
    ml_pred = ml_predict(ml_features, ml_category)

    if not ml_pred:
        return claude_confidence, "no_model", None

    ml_confidence = ml_pred["confidence"]

    # Calculate adjustment (half of the difference)
    diff = ml_confidence - claude_confidence
    adjustment = diff * 0.5

    # Apply adjustment (max ±15%)
    adjustment = max(-15, min(15, adjustment))
    adjusted_confidence = int(claude_confidence + adjustment)

    # Ensure bounds
    adjusted_confidence = max(30, min(95, adjusted_confidence))

    # Determine status
    if abs(diff) <= 10:
        ml_status = "confirmed"  # ML agrees
    elif diff < -15:
        ml_status = "warning"  # ML disagrees strongly
    else:
        ml_status = "adjusted"  # ML adjusted up

    # Apply self-learning adjustments (calibration + patterns)
    final_confidence, learning_adjustments = apply_learning_adjustments(bet_type, adjusted_confidence, ml_features)

    learning_info = f" | Learning: {', '.join(learning_adjustments)}" if learning_adjustments else ""
    logger.info(f"ML+Learning: {bet_type} | Claude {claude_confidence}% → ML {adjusted_confidence}% → Final {final_confidence}%{learning_info}")

    return final_confidence, ml_status, ml_confidence


def check_and_train_models():
    """Check if we have enough data and train models"""
    conn = get_db_connection()
    c = conn.cursor()

    # Check samples per category
    c.execute("""SELECT bet_category, COUNT(*) as cnt
                 FROM ml_training_data
                 WHERE target IS NOT NULL
                 GROUP BY bet_category""")
    counts = dict(c.fetchall())
    conn.close()

    trained = []
    for category, count in counts.items():
        if count >= ML_MIN_SAMPLES:
            # Check if model exists and is recent
            model_path = os.path.join(ML_MODELS_DIR, f"model_{category}.pkl")
            if not os.path.exists(model_path):
                result = train_ml_model(category)
                if result:
                    trained.append(result)

    return trained


def get_ml_status() -> dict:
    """Get ML system status"""
    conn = get_db_connection()
    c = conn.cursor()

    # Training data counts
    c.execute("""SELECT bet_category, COUNT(*) as total,
                 SUM(CASE WHEN target IS NOT NULL THEN 1 ELSE 0 END) as verified
                 FROM ml_training_data GROUP BY bet_category""")
    data_counts = {row[0]: {"total": row[1], "verified": row[2]} for row in c.fetchall()}

    # Model info
    c.execute("""SELECT model_type, accuracy, samples_count, trained_at
                 FROM ml_models ORDER BY trained_at DESC""")
    models = {row[0]: {"accuracy": row[1], "samples": row[2], "trained_at": row[3]}
              for row in c.fetchall()}

    conn.close()

    return {
        "ml_available": ML_AVAILABLE,
        "min_samples": ML_MIN_SAMPLES,
        "data_counts": data_counts,
        "models": models,
        "ready_to_train": [cat for cat, data in data_counts.items()
                          if data["verified"] >= ML_MIN_SAMPLES and cat not in models]
    }


# ===== ERROR ANALYSIS & LEARNING SYSTEM =====
# Analyzes WHY predictions fail and teaches Claude to improve

def analyze_prediction_error(prediction: dict, actual_result: str, features: dict) -> dict:
    """Analyze why a prediction failed and categorize the error.

    Returns error analysis with type, description, and lessons learned.
    """
    bet_type = prediction.get("bet_type", "").lower()
    bet_category = prediction.get("bet_category", "")
    confidence = prediction.get("confidence", 70)

    # Parse actual result (e.g., "2:1", "0:0")
    try:
        if ":" in actual_result:
            home_goals, away_goals = map(int, actual_result.split(":"))
            total_goals = home_goals + away_goals
        else:
            home_goals, away_goals, total_goals = 0, 0, 0
    except:
        home_goals, away_goals, total_goals = 0, 0, 0

    error_analysis = {
        "error_type": "unknown",
        "expected_value": None,
        "actual_value": None,
        "description": "",
        "lesson": ""
    }

    # Get expected values from features
    expected_goals = features.get("expected_goals", 2.5) if features else 2.5

    # Analyze by bet category
    if "totals_over" in bet_category or "тб" in bet_type or "over" in bet_type:
        error_analysis["error_type"] = "totals_overestimate"
        error_analysis["expected_value"] = expected_goals
        error_analysis["actual_value"] = total_goals
        diff = expected_goals - total_goals
        if diff > 1.5:
            error_analysis["description"] = f"Form suggested {expected_goals:.1f} goals, actual {total_goals}. Overestimated by {diff:.1f}"
            error_analysis["lesson"] = "Teams played more defensively than recent form suggested. This league may be lower-scoring than averages indicate."
        elif diff > 0.5:
            error_analysis["description"] = f"Form suggested {expected_goals:.1f} goals, actual {total_goals}. Close miss."
            error_analysis["lesson"] = "Slight overestimate. Be more conservative with Over bets in this league."
        else:
            error_analysis["description"] = f"Close call - form suggested {expected_goals:.1f}, got {total_goals}"
            error_analysis["lesson"] = "Borderline result - prediction was reasonable but variance."

    elif "totals_under" in bet_category or "тм" in bet_type or "under" in bet_type:
        error_analysis["error_type"] = "totals_underestimate"
        error_analysis["expected_value"] = expected_goals
        error_analysis["actual_value"] = total_goals
        diff = total_goals - expected_goals
        if diff > 1.5:
            error_analysis["description"] = f"Form suggested {expected_goals:.1f} goals, actual {total_goals}. Underestimated by {diff:.1f}"
            error_analysis["lesson"] = "Teams were more attacking than form suggested. This matchup type may produce more goals."
        else:
            error_analysis["description"] = f"Form suggested {expected_goals:.1f} goals, actual {total_goals}. Close miss."
            error_analysis["lesson"] = "Match was more open than form suggested."

    elif "outcomes_home" in bet_category or "п1" in bet_type:
        error_analysis["error_type"] = "home_overestimate"
        error_analysis["expected_value"] = confidence
        if home_goals < away_goals:
            error_analysis["actual_value"] = 0
            error_analysis["description"] = f"Home team lost {home_goals}:{away_goals} despite {confidence}% confidence"
            error_analysis["lesson"] = "Home advantage overestimated. Away team stronger than form indicated."
        else:
            error_analysis["actual_value"] = 50
            error_analysis["description"] = f"Draw {home_goals}:{away_goals} instead of home win"
            error_analysis["lesson"] = "Home team couldn't convert dominance. Consider double chance next time."

    elif "outcomes_away" in bet_category or "п2" in bet_type:
        error_analysis["error_type"] = "away_overestimate"
        error_analysis["expected_value"] = confidence
        if away_goals < home_goals:
            error_analysis["actual_value"] = 0
            error_analysis["description"] = f"Away team lost {home_goals}:{away_goals} despite {confidence}% confidence"
            error_analysis["lesson"] = "Away form didn't translate. Home advantage was stronger."
        else:
            error_analysis["actual_value"] = 50
            error_analysis["description"] = f"Draw {home_goals}:{away_goals} instead of away win"
            error_analysis["lesson"] = "Away team couldn't win despite chances. Consider double chance."

    elif "btts" in bet_category:
        both_scored = home_goals > 0 and away_goals > 0
        if not both_scored:
            error_analysis["error_type"] = "btts_overestimate"
            error_analysis["expected_value"] = confidence
            error_analysis["actual_value"] = 0
            if home_goals == 0 and away_goals == 0:
                error_analysis["description"] = f"0:0 draw - neither team scored"
                error_analysis["lesson"] = "Both teams more defensive than expected. Check recent clean sheets."
            else:
                error_analysis["description"] = f"Result {home_goals}:{away_goals} - one team failed to score"
                error_analysis["lesson"] = "One team's attack failed. Check goal-scoring consistency, not just average."

    return error_analysis


def save_prediction_error(prediction_id: int, league_code: str, bet_category: str,
                          error_analysis: dict, features: dict):
    """Save error analysis to database for learning."""
    conn = get_db_connection()
    c = conn.cursor()

    features_json = json.dumps(features) if features else "{}"

    c.execute("""INSERT INTO prediction_errors
                 (prediction_id, league_code, bet_category, error_type,
                  expected_value, actual_value, error_description, features_json)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (prediction_id, league_code, bet_category,
               error_analysis.get("error_type"),
               error_analysis.get("expected_value"),
               error_analysis.get("actual_value"),
               error_analysis.get("description", "") + " | " + error_analysis.get("lesson", ""),
               features_json))

    conn.commit()
    conn.close()
    logger.info(f"Saved error analysis for prediction {prediction_id}: {error_analysis.get('error_type')}")


def update_league_learning(league_code: str, bet_category: str, is_correct: bool, error_type: str = None):
    """Update league learning stats after each result."""
    conn = get_db_connection()
    c = conn.cursor()

    # Get or create record
    c.execute("""SELECT id, total_predictions, correct_predictions, lessons_json
                 FROM league_learning WHERE league_code = ? AND bet_category = ?""",
              (league_code, bet_category))
    row = c.fetchone()

    if row:
        total = row[1] + 1
        correct = row[2] + (1 if is_correct else 0)
        lessons = json.loads(row[3]) if row[3] else {}

        # Track error types
        if not is_correct and error_type:
            lessons[error_type] = lessons.get(error_type, 0) + 1

        # Find most common error
        common_error = max(lessons.keys(), key=lambda k: lessons[k]) if lessons else None

        c.execute("""UPDATE league_learning
                     SET total_predictions = ?, correct_predictions = ?,
                         common_error_type = ?, lessons_json = ?, updated_at = datetime('now')
                     WHERE id = ?""",
                  (total, correct, common_error, json.dumps(lessons), row[0]))
    else:
        lessons = {error_type: 1} if error_type and not is_correct else {}
        c.execute("""INSERT INTO league_learning
                     (league_code, bet_category, total_predictions, correct_predictions,
                      common_error_type, lessons_json)
                     VALUES (?, ?, 1, ?, ?, ?)""",
                  (league_code, bet_category, 1 if is_correct else 0,
                   error_type if not is_correct else None, json.dumps(lessons)))

    conn.commit()
    conn.close()


def get_learning_context(league_code: str, bet_category: str = None) -> str:
    """Get learning context for Claude prompt - what we learned from past errors."""
    conn = get_db_connection()
    c = conn.cursor()

    context_parts = []

    # Get league-specific learning
    if bet_category:
        c.execute("""SELECT total_predictions, correct_predictions, common_error_type, lessons_json
                     FROM league_learning WHERE league_code = ? AND bet_category = ?""",
                  (league_code, bet_category))
    else:
        c.execute("""SELECT bet_category, total_predictions, correct_predictions, common_error_type, lessons_json
                     FROM league_learning WHERE league_code = ?""",
                  (league_code,))

    rows = c.fetchall()

    if rows:
        context_parts.append(f"📚 LEARNING FROM PAST ERRORS IN {league_code}:")

        for row in rows:
            if bet_category:
                total, correct, common_error, lessons_json = row
                cat = bet_category
            else:
                cat, total, correct, common_error, lessons_json = row

            if total >= 5:  # Only show if enough data
                accuracy = correct / total * 100 if total > 0 else 0
                lessons = json.loads(lessons_json) if lessons_json else {}

                context_parts.append(f"\n• {cat}: {accuracy:.0f}% accuracy ({correct}/{total})")

                if accuracy < 50 and common_error:
                    context_parts.append(f"  ⚠️ Common error: {common_error}")

                    # Add specific lessons based on error type
                    if "overestimate" in common_error:
                        context_parts.append(f"  💡 Lesson: You tend to OVERESTIMATE in this category. Be more conservative.")
                    elif "underestimate" in common_error:
                        context_parts.append(f"  💡 Lesson: You tend to UNDERESTIMATE. Consider higher values.")

    # Get recent errors for this league (last 10)
    c.execute("""SELECT bet_category, error_type, error_description
                 FROM prediction_errors
                 WHERE league_code = ?
                 ORDER BY created_at DESC LIMIT 10""",
              (league_code,))
    recent_errors = c.fetchall()

    if recent_errors:
        context_parts.append(f"\n📋 RECENT ERRORS IN {league_code}:")
        error_summary = {}
        for cat, err_type, desc in recent_errors:
            key = f"{cat}:{err_type}"
            if key not in error_summary:
                error_summary[key] = {"count": 0, "desc": desc}
            error_summary[key]["count"] += 1

        for key, data in sorted(error_summary.items(), key=lambda x: -x[1]["count"])[:3]:
            cat, err_type = key.split(":")
            context_parts.append(f"  • {cat}: {err_type} (x{data['count']})")

    conn.close()

    return "\n".join(context_parts) if context_parts else ""


def get_category_learning_context(bet_category: str) -> str:
    """Get learning context for a specific bet category across all leagues."""
    conn = get_db_connection()
    c = conn.cursor()

    # Overall stats for this category
    c.execute("""SELECT SUM(total_predictions), SUM(correct_predictions),
                        GROUP_CONCAT(DISTINCT common_error_type)
                 FROM league_learning WHERE bet_category = ?""",
              (bet_category,))
    row = c.fetchone()

    context_parts = []

    if row and row[0] and row[0] >= 10:
        total, correct, common_errors = row
        accuracy = correct / total * 100 if total > 0 else 0

        context_parts.append(f"📊 YOUR {bet_category.upper()} PERFORMANCE:")
        context_parts.append(f"• Overall accuracy: {accuracy:.0f}% ({correct}/{total})")

        if accuracy < 50:
            context_parts.append(f"• ⚠️ BELOW 50% - Be extra careful with this bet type!")
            if common_errors:
                context_parts.append(f"• Common errors: {common_errors}")
        elif accuracy < 55:
            context_parts.append(f"• ⚡ Close to random (50%). Need stronger signals.")
        elif accuracy >= 60:
            context_parts.append(f"• ✅ Good performance! Trust your analysis here.")

    # Best and worst leagues for this category
    c.execute("""SELECT league_code, total_predictions, correct_predictions
                 FROM league_learning
                 WHERE bet_category = ? AND total_predictions >= 5
                 ORDER BY (correct_predictions * 1.0 / total_predictions) DESC""",
              (bet_category,))
    leagues = c.fetchall()

    if len(leagues) >= 2:
        best = leagues[0]
        worst = leagues[-1]
        best_acc = best[2] / best[1] * 100 if best[1] > 0 else 0
        worst_acc = worst[2] / worst[1] * 100 if worst[1] > 0 else 0

        if best_acc > worst_acc + 15:  # Significant difference
            context_parts.append(f"\n• Best league: {best[0]} ({best_acc:.0f}%)")
            context_parts.append(f"• Worst league: {worst[0]} ({worst_acc:.0f}%)")

    conn.close()

    return "\n".join(context_parts) if context_parts else ""


# ===== SELF-LEARNING SYSTEM =====
# System that improves predictions over time by learning from results

def get_confidence_band(confidence: int) -> str:
    """Convert confidence to band for calibration tracking"""
    if confidence >= 80:
        return "80-100"
    elif confidence >= 70:
        return "70-79"
    elif confidence >= 60:
        return "60-69"
    else:
        return "under-60"


def update_confidence_calibration(bet_category: str, confidence: int, is_win: bool):
    """Update calibration table after each verified result.

    Tracks: how often predictions at X% confidence actually win.
    This helps calibrate future predictions.
    """
    band = get_confidence_band(confidence)

    conn = get_db_connection()
    c = conn.cursor()

    # Get or create calibration record
    c.execute("""SELECT id, predicted_count, actual_wins
                 FROM confidence_calibration
                 WHERE bet_category = ? AND confidence_band = ?""",
              (bet_category, band))
    row = c.fetchone()

    if row:
        new_count = row[1] + 1
        new_wins = row[2] + (1 if is_win else 0)
        # Calculate new calibration factor
        actual_rate = new_wins / new_count if new_count > 0 else 0.5
        expected_rate = (int(band.split("-")[0]) + 5) / 100  # midpoint of band
        calibration = actual_rate / expected_rate if expected_rate > 0 else 1.0

        c.execute("""UPDATE confidence_calibration
                     SET predicted_count = ?, actual_wins = ?,
                         calibration_factor = ?, updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?""",
                  (new_count, new_wins, calibration, row[0]))
    else:
        c.execute("""INSERT INTO confidence_calibration
                     (bet_category, confidence_band, predicted_count, actual_wins, calibration_factor)
                     VALUES (?, ?, 1, ?, 1.0)""",
                  (bet_category, band, 1 if is_win else 0))

    conn.commit()
    conn.close()


def get_calibrated_confidence(bet_category: str, raw_confidence: int) -> int:
    """Adjust confidence based on historical accuracy.

    If 70% predictions actually win only 55% of time, reduce confidence.
    If 70% predictions win 80% of time, increase confidence.

    No hardcoded penalties - let the calibration system learn from actual data.
    This allows the model to improve over time for ALL bet types.
    """
    band = get_confidence_band(raw_confidence)
    confidence = raw_confidence

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""SELECT calibration_factor, predicted_count
                 FROM confidence_calibration
                 WHERE bet_category = ? AND confidence_band = ?""",
              (bet_category, band))
    row = c.fetchone()
    conn.close()

    if row and row[1] >= 10:  # Need at least 10 samples for reliable calibration
        calibration = row[0]
        # More aggressive calibration (capped at ±35% instead of 20%)
        calibration = max(0.65, min(1.35, calibration))
        confidence = int(confidence * calibration)

    # Keep within valid range
    return max(30, min(95, confidence))


def detect_pattern(features: dict, bet_type: str) -> str:
    """Detect pattern from match features for pattern learning.

    Returns a pattern key like:
    'home_strong_favorite|totals_over' or 'underdog_form_good|outcomes_home'
    """
    patterns = []

    # Home/Away strength pattern
    home_wins = features.get("home_wins", 0)
    away_wins = features.get("away_wins", 0)
    position_diff = features.get("position_diff", 0)

    if position_diff >= 10:
        patterns.append("home_much_higher")
    elif position_diff >= 5:
        patterns.append("home_higher")
    elif position_diff <= -10:
        patterns.append("away_much_higher")
    elif position_diff <= -5:
        patterns.append("away_higher")
    else:
        patterns.append("teams_equal")

    # Form pattern
    if home_wins >= 4:
        patterns.append("home_hot")
    elif home_wins <= 1:
        patterns.append("home_cold")

    if away_wins >= 4:
        patterns.append("away_hot")
    elif away_wins <= 1:
        patterns.append("away_cold")

    # H2H pattern
    h2h_home = features.get("h2h_home_wins", 0)
    h2h_away = features.get("h2h_away_wins", 0)
    if h2h_home >= 3:
        patterns.append("h2h_home_dominant")
    elif h2h_away >= 3:
        patterns.append("h2h_away_dominant")

    # Goals pattern
    expected_goals = features.get("expected_goals", 2.5)
    if expected_goals >= 3.0:
        patterns.append("high_scoring")
    elif expected_goals <= 2.0:
        patterns.append("low_scoring")

    # Categorize bet type
    category = categorize_bet(bet_type)

    # Create pattern key
    pattern_key = "|".join(sorted(patterns)) + f">{category}"
    return pattern_key


def update_pattern(pattern_key: str, is_win: bool):
    """Update pattern win/loss record."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT id, wins, losses FROM learning_patterns WHERE pattern_key = ?", (pattern_key,))
    row = c.fetchone()

    if row:
        if is_win:
            c.execute("UPDATE learning_patterns SET wins = wins + 1, last_updated = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
        else:
            c.execute("UPDATE learning_patterns SET losses = losses + 1, last_updated = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
    else:
        c.execute("""INSERT INTO learning_patterns (pattern_type, pattern_key, wins, losses)
                     VALUES ('match_pattern', ?, ?, ?)""",
                  (pattern_key, 1 if is_win else 0, 0 if is_win else 1))

    conn.commit()
    conn.close()


def get_pattern_adjustment(pattern_key: str) -> int:
    """Get confidence adjustment based on pattern history.

    Returns: adjustment in percentage points (-15 to +15)
    Positive = pattern historically wins
    Negative = pattern historically loses
    """
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT wins, losses FROM learning_patterns WHERE pattern_key = ?", (pattern_key,))
    row = c.fetchone()
    conn.close()

    if not row:
        return 0

    wins, losses = row
    total = wins + losses

    if total < 5:  # Need at least 5 samples
        return 0

    win_rate = wins / total

    # Calculate adjustment
    # 50% win rate = 0 adjustment
    # 70% win rate = +10 adjustment
    # 30% win rate = -10 adjustment
    adjustment = int((win_rate - 0.5) * 50)

    # Cap at ±15
    return max(-15, min(15, adjustment))


def learn_from_result(prediction_id: int, bet_category: str, confidence: int,
                      is_correct: bool, features: dict, bet_type: str,
                      league_code: str = None, actual_result: str = None):
    """Main learning function - called after each verified result.

    Updates:
    1. Confidence calibration
    2. Pattern learning
    3. Error analysis (NEW) - learns WHY predictions fail
    4. League learning (NEW) - tracks accuracy per league/category
    5. Triggers model retraining if needed
    """
    is_win = is_correct == True  # Handle 0, 1, 2 (push)

    # Skip push results for learning
    if is_correct == 2:  # Push
        logger.info(f"📚 LEARN: Skip push result for pred_id={prediction_id}")
        return

    # Log learning event for visibility
    result_emoji = "✅" if is_win else "❌"
    logger.info(f"📚 LEARNING: {result_emoji} {bet_category} | conf={confidence}% | pred_id={prediction_id} | features={'yes' if features else 'NO'}")

    # 1. Update confidence calibration
    if bet_category:
        update_confidence_calibration(bet_category, confidence, is_win)
        logger.debug(f"📚 Updated calibration for {bet_category}")

    # 2. Update pattern learning
    if features:
        pattern_key = detect_pattern(features, bet_type)
        update_pattern(pattern_key, is_win)

        # Log significant patterns
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT wins, losses FROM learning_patterns WHERE pattern_key = ?", (pattern_key,))
        row = c.fetchone()
        conn.close()

        if row:
            wins, losses = row
            total = wins + losses
            if total >= 10:  # Log after 10 samples
                win_rate = wins / total * 100
                if win_rate >= 70 or win_rate <= 30:
                    log_learning_event(
                        "pattern_significant",
                        f"Pattern '{pattern_key}' has {win_rate:.0f}% win rate ({total} samples)",
                        {"pattern": pattern_key, "wins": wins, "losses": losses}
                    )

    # 3. NEW: Error analysis - learn WHY predictions fail
    error_type = None
    if not is_win and actual_result and features:
        prediction = {
            "bet_type": bet_type,
            "bet_category": bet_category,
            "confidence": confidence
        }
        error_analysis = analyze_prediction_error(prediction, actual_result, features)
        error_type = error_analysis.get("error_type")

        if league_code and error_type != "unknown":
            save_prediction_error(prediction_id, league_code, bet_category, error_analysis, features)
            logger.info(f"📚 Error analyzed: {error_type} | {error_analysis.get('lesson', '')[:50]}")

    # 4. NEW: Update league learning
    if league_code and bet_category:
        update_league_learning(league_code, bet_category, is_win, error_type)

    # 5. NEW: Update feature-based error patterns (SMART LEARNING)
    # This is the key to learning WHEN specific bets fail
    if features and bet_category:
        conditions = extract_feature_conditions(features, bet_category)
        for condition in conditions:
            update_feature_pattern(bet_category, condition, is_win, confidence)

        # Log if we found significant risky conditions
        if not is_win and conditions:
            logger.info(f"📚 Feature learning: {bet_category} failed with conditions: {', '.join(conditions)}")

    # 6. NEW: Update ROI analytics - track PROFITABILITY not just win rate
    # Get prediction details for ROI calculation
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT odds, expected_value, stake_percent
                 FROM predictions WHERE id = ?""", (prediction_id,))
    pred_row = c.fetchone()
    conn.close()

    if pred_row and bet_category:
        odds, ev, stake = pred_row
        if odds and odds > 1:
            # Default stake to 1 unit if not set
            stake = stake if stake and stake > 0 else 1.0

            # Calculate and save profit
            if is_win:
                profit = stake * odds - stake
            else:
                profit = -stake

            # Update prediction with profit
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE predictions SET profit = ? WHERE id = ?", (profit, prediction_id))
            conn.commit()
            conn.close()

            # Update ROI analytics for each condition
            if features:
                conditions = extract_feature_conditions(features, bet_category)
                for condition in conditions:
                    condition_key = get_condition_key(bet_category, condition)
                    update_roi_analytics(bet_category, condition_key, is_win, odds, stake, ev or 0)

                # Also update overall category ROI
                update_roi_analytics(bet_category, "overall", is_win, odds, stake, ev or 0)

            logger.info(f"💰 ROI updated: {bet_category} | {'WIN' if is_win else 'LOSS'} | profit={profit:+.2f} | EV={ev or 0:.1f}%")

    # 7. Check if model needs retraining
    if bet_category and should_retrain_model(bet_category):
        logger.info(f"🔄 Triggering model retrain for {bet_category}")
        result = train_ml_model(bet_category)
        if result:
            log_learning_event(
                "model_retrained",
                f"Retrained {bet_category} model: {result['accuracy']:.1%} accuracy",
                result
            )


def should_retrain_model(bet_category: str) -> bool:
    """Check if model should be retrained.

    Retrain when:
    1. New data > 20% more than training data
    2. Recent accuracy significantly lower than model accuracy
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Get model info
    c.execute("""SELECT accuracy, samples_count, trained_at
                 FROM ml_models WHERE model_type = ?
                 ORDER BY trained_at DESC LIMIT 1""", (bet_category,))
    model = c.fetchone()

    if not model:
        conn.close()
        return False  # No model exists yet

    model_accuracy, model_samples, trained_at = model

    # Count current verified samples
    c.execute("""SELECT COUNT(*) FROM ml_training_data
                 WHERE bet_category = ? AND target IS NOT NULL""", (bet_category,))
    current_samples = c.fetchone()[0]

    # Check if we have 20% more data
    if current_samples > model_samples * 1.2:
        conn.close()
        logger.info(f"Retrain {bet_category}: {current_samples} samples vs {model_samples} trained")
        return True

    # Check recent accuracy (last 20 predictions)
    c.execute("""SELECT target FROM ml_training_data
                 WHERE bet_category = ? AND target IS NOT NULL
                 ORDER BY created_at DESC LIMIT 20""", (bet_category,))
    recent = c.fetchall()
    conn.close()

    if len(recent) >= 20:
        recent_accuracy = sum(1 for r in recent if r[0] == 1) / len(recent)
        # If recent accuracy is 15%+ lower than model accuracy, retrain
        if recent_accuracy < model_accuracy - 0.15:
            logger.info(f"Retrain {bet_category}: recent {recent_accuracy:.1%} vs model {model_accuracy:.1%}")
            return True

    return False


def log_learning_event(event_type: str, description: str, data: dict = None):
    """Log a learning event for tracking system improvement."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""INSERT INTO learning_log (event_type, description, data_json)
                 VALUES (?, ?, ?)""",
              (event_type, description, json.dumps(data) if data else None))
    conn.commit()
    conn.close()
    logger.info(f"📚 Learning: {description}")


def get_learning_stats() -> dict:
    """Get statistics about system learning progress."""
    conn = get_db_connection()
    c = conn.cursor()

    # Total counts for diagnostics
    try:
        c.execute("SELECT COUNT(*), SUM(predicted_count) FROM confidence_calibration")
        calib_row = c.fetchone()
        total_calibration_records = calib_row[0] if calib_row else 0
        total_calibration_samples = calib_row[1] if calib_row and calib_row[1] else 0
    except:
        total_calibration_records = 0
        total_calibration_samples = 0

    try:
        c.execute("SELECT COUNT(*), SUM(wins + losses) FROM learning_patterns")
        pattern_row = c.fetchone()
        total_pattern_records = pattern_row[0] if pattern_row else 0
        total_pattern_samples = pattern_row[1] if pattern_row and pattern_row[1] else 0
    except:
        total_pattern_records = 0
        total_pattern_samples = 0

    # Calibration stats
    c.execute("""SELECT bet_category, confidence_band, predicted_count, actual_wins, calibration_factor
                 FROM confidence_calibration WHERE predicted_count >= 5
                 ORDER BY bet_category, confidence_band""")
    calibrations = {}
    for row in c.fetchall():
        cat = row[0]
        if cat not in calibrations:
            calibrations[cat] = {}
        calibrations[cat][row[1]] = {
            "count": row[2],
            "wins": row[3],
            "rate": round(row[3] / row[2] * 100, 1) if row[2] > 0 else 0,
            "calibration": round(row[4], 2)
        }

    # Pattern stats - best and worst
    c.execute("""SELECT pattern_key, wins, losses
                 FROM learning_patterns
                 WHERE wins + losses >= 5
                 ORDER BY CAST(wins AS FLOAT) / (wins + losses) DESC
                 LIMIT 5""")
    best_patterns = [{"pattern": r[0], "wins": r[1], "losses": r[2],
                      "rate": round(r[1]/(r[1]+r[2])*100, 1)} for r in c.fetchall()]

    c.execute("""SELECT pattern_key, wins, losses
                 FROM learning_patterns
                 WHERE wins + losses >= 5
                 ORDER BY CAST(wins AS FLOAT) / (wins + losses) ASC
                 LIMIT 5""")
    worst_patterns = [{"pattern": r[0], "wins": r[1], "losses": r[2],
                       "rate": round(r[1]/(r[1]+r[2])*100, 1)} for r in c.fetchall()]

    # Recent learning events
    c.execute("""SELECT event_type, description, created_at
                 FROM learning_log ORDER BY created_at DESC LIMIT 10""")
    recent_events = [{"type": r[0], "desc": r[1], "time": r[2]} for r in c.fetchall()]

    # ROI by category
    try:
        c.execute("""SELECT bet_category, total_bets, wins, losses, roi_percent, avg_odds
                     FROM roi_analytics
                     WHERE condition_key = 'overall' AND total_bets >= 5
                     ORDER BY roi_percent DESC""")
        roi_by_category = {}
        for row in c.fetchall():
            cat, total, wins, losses, roi, avg_odds = row
            win_rate = wins / total * 100 if total > 0 else 0
            roi_by_category[cat] = {
                "total": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "roi": round(roi, 1),
                "avg_odds": round(avg_odds, 2)
            }
    except:
        roi_by_category = {}

    conn.close()

    return {
        "calibrations": calibrations,
        "best_patterns": best_patterns,
        "worst_patterns": worst_patterns,
        "recent_learning": recent_events,
        "totals": {
            "calibration_records": total_calibration_records,
            "calibration_samples": int(total_calibration_samples),
            "pattern_records": total_pattern_records,
            "pattern_samples": int(total_pattern_samples)
        },
        "roi_by_category": roi_by_category
    }


def get_roi_by_category() -> dict:
    """Get ROI statistics per bet category."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""SELECT bet_category, total_bets, wins, losses, roi_percent, avg_odds
                 FROM roi_analytics
                 WHERE condition_key = 'overall' AND total_bets >= 5
                 ORDER BY roi_percent DESC""")

    result = {}
    for row in c.fetchall():
        cat, total, wins, losses, roi, avg_odds = row
        win_rate = wins / total * 100 if total > 0 else 0
        result[cat] = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "roi": round(roi, 1),
            "avg_odds": round(avg_odds, 2)
        }

    conn.close()
    return result


# ===== SMART FEATURE-BASED LEARNING =====
# System that learns WHEN specific bet types fail based on match features

def extract_feature_conditions(features: dict, bet_category: str) -> list:
    """Extract relevant feature conditions for a bet type.

    Returns list of conditions like:
    - "high_injuries" (injuries > 8)
    - "away_higher_position" (away team higher in table)
    - "low_home_form" (home win rate < 50%)
    """
    conditions = []

    if not features:
        return conditions

    # Injury conditions
    home_injuries = features.get("home_injuries", 0)
    away_injuries = features.get("away_injuries", 0)
    if home_injuries > 8:
        conditions.append("home_many_injuries")
    if away_injuries > 8:
        conditions.append("away_many_injuries")

    # Position/class conditions
    position_diff = features.get("position_diff", 0)
    if position_diff < -5:  # Away team much higher
        conditions.append("away_higher_position")
    elif position_diff > 5:  # Home team much higher
        conditions.append("home_higher_position")

    class_diff = features.get("class_diff", 0)
    if class_diff < -1:
        conditions.append("away_higher_class")
    elif class_diff > 1:
        conditions.append("home_higher_class")

    # Form conditions
    home_wins = features.get("home_wins", 0)
    away_wins = features.get("away_wins", 0)
    if home_wins < 2:  # Out of 5
        conditions.append("poor_home_form")
    if home_wins >= 4:
        conditions.append("strong_home_form")
    if away_wins < 2:
        conditions.append("poor_away_form")
    if away_wins >= 4:
        conditions.append("strong_away_form")

    # Goals conditions (for totals)
    home_scored = features.get("home_scored", 0)
    away_scored = features.get("away_scored", 0)
    home_conceded = features.get("home_conceded", 0)
    away_conceded = features.get("away_conceded", 0)

    avg_goals = (home_scored + away_scored + home_conceded + away_conceded) / 4 if any([home_scored, away_scored]) else 0
    if avg_goals < 1.0:
        conditions.append("low_scoring_teams")
    elif avg_goals > 2.0:
        conditions.append("high_scoring_teams")

    # H2H conditions
    h2h_count = features.get("h2h_count", 0)
    if h2h_count == 0:
        conditions.append("no_h2h_data")

    # Motivation/fatigue
    home_rest_days = features.get("home_rest_days", 5)
    away_rest_days = features.get("away_rest_days", 5)
    if home_rest_days < 3:
        conditions.append("home_tired")
    if away_rest_days < 3:
        conditions.append("away_tired")

    # Cup match
    is_cup = features.get("is_cup", 0)
    if is_cup:
        conditions.append("cup_match")

    return conditions


def get_condition_key(bet_category: str, condition: str) -> str:
    """Generate unique key for bet_category + condition combination."""
    return f"{bet_category}|{condition}"


def update_feature_pattern(bet_category: str, condition: str, is_win: bool, confidence: int):
    """Update feature-based error pattern after a result."""
    condition_key = get_condition_key(bet_category, condition)

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""SELECT id, total_predictions, wins, losses, avg_confidence_when_failed
                 FROM feature_error_patterns WHERE condition_key = ?""", (condition_key,))
    row = c.fetchone()

    if row:
        total = row[1] + 1
        wins = row[2] + (1 if is_win else 0)
        losses = row[3] + (0 if is_win else 1)

        # Track average confidence when failed (to understand overconfidence)
        if not is_win:
            old_avg = row[4] or confidence
            old_losses = row[3]
            new_avg = (old_avg * old_losses + confidence) / losses if losses > 0 else confidence
        else:
            new_avg = row[4]

        # Calculate suggested adjustment based on win rate
        win_rate = wins / total if total > 0 else 0.5
        # If win rate is 40%, we should reduce confidence by ~10-15%
        # If win rate is 60%, we can slightly boost by ~5%
        suggested_adj = int((win_rate - 0.5) * 30)  # Range: -15 to +15
        suggested_adj = max(-20, min(10, suggested_adj))  # More aggressive penalty, conservative boost

        c.execute("""UPDATE feature_error_patterns
                     SET total_predictions = ?, wins = ?, losses = ?,
                         avg_confidence_when_failed = ?, suggested_adjustment = ?,
                         last_updated = CURRENT_TIMESTAMP
                     WHERE id = ?""",
                  (total, wins, losses, new_avg, suggested_adj, row[0]))
    else:
        c.execute("""INSERT INTO feature_error_patterns
                     (bet_category, feature_condition, condition_key, total_predictions,
                      wins, losses, avg_confidence_when_failed, suggested_adjustment)
                     VALUES (?, ?, ?, 1, ?, ?, ?, 0)""",
                  (bet_category, condition, condition_key,
                   1 if is_win else 0, 0 if is_win else 1,
                   None if is_win else confidence))

    conn.commit()
    conn.close()


def get_smart_adjustments(bet_category: str, features: dict) -> tuple:
    """Get smart adjustments based on learned feature patterns.

    Returns: (total_adjustment, list of applied adjustments with reasons)
    """
    conditions = extract_feature_conditions(features, bet_category)

    if not conditions:
        return 0, []

    conn = get_db_connection()
    c = conn.cursor()

    adjustments = []
    total_adjustment = 0

    for condition in conditions:
        condition_key = get_condition_key(bet_category, condition)

        c.execute("""SELECT wins, losses, suggested_adjustment, total_predictions
                     FROM feature_error_patterns
                     WHERE condition_key = ? AND total_predictions >= 5""",
                  (condition_key,))
        row = c.fetchone()

        if row:
            wins, losses, suggested_adj, total = row
            win_rate = wins / total if total > 0 else 0.5

            # Only apply adjustment if we have enough data and pattern is significant
            if total >= 5 and abs(suggested_adj) >= 3:
                # Weight by sample size (more samples = more confidence in adjustment)
                weight = min(1.0, total / 20)  # Full weight at 20+ samples
                weighted_adj = int(suggested_adj * weight)

                if weighted_adj != 0:
                    total_adjustment += weighted_adj
                    direction = "⬇️" if weighted_adj < 0 else "⬆️"
                    adjustments.append(f"{direction} {condition}: {weighted_adj:+d}% (win rate: {win_rate:.0%}, n={total})")

    conn.close()

    # Cap total adjustment
    total_adjustment = max(-25, min(15, total_adjustment))

    return total_adjustment, adjustments


def get_risky_conditions(bet_category: str, features: dict) -> list:
    """Check if current conditions are historically risky for this bet type.

    Returns list of risky conditions with their stats.
    """
    conditions = extract_feature_conditions(features, bet_category)
    risky = []

    if not conditions:
        return risky

    conn = get_db_connection()
    c = conn.cursor()

    for condition in conditions:
        condition_key = get_condition_key(bet_category, condition)

        c.execute("""SELECT wins, losses, total_predictions
                     FROM feature_error_patterns
                     WHERE condition_key = ? AND total_predictions >= 5""",
                  (condition_key,))
        row = c.fetchone()

        if row:
            wins, losses, total = row
            win_rate = wins / total if total > 0 else 0.5

            # Condition is risky if win rate < 45%
            if win_rate < 0.45 and total >= 5:
                risky.append({
                    "condition": condition,
                    "win_rate": win_rate,
                    "sample_size": total,
                    "wins": wins,
                    "losses": losses
                })

    conn.close()

    return sorted(risky, key=lambda x: x["win_rate"])


def suggest_alternative_bet(bet_category: str, features: dict, risky_conditions: list) -> dict:
    """Suggest a safer alternative bet based on risky conditions.

    Returns dict with alternative suggestion or None.
    """
    if not risky_conditions:
        return None

    # Map risky outcomes to safer alternatives
    alternatives = {
        "outcomes_home": [
            {"category": "double_chance_1x", "name": "1X (Double Chance)", "reason": "Safer - covers draw"},
            {"category": "handicap_home", "name": "Home +1.5", "reason": "Safer - home can lose by 1"},
        ],
        "outcomes_away": [
            {"category": "double_chance_x2", "name": "X2 (Double Chance)", "reason": "Safer - covers draw"},
            {"category": "handicap_away", "name": "Away +1.5", "reason": "Safer - away can lose by 1"},
        ],
        "totals_over": [
            {"category": "totals_over_1.5", "name": "Over 1.5", "reason": "Lower line - easier to hit"},
            {"category": "btts", "name": "BTTS Yes", "reason": "Both teams scoring = goals"},
        ],
        "totals_under": [
            {"category": "totals_under_3.5", "name": "Under 3.5", "reason": "Higher line - safer"},
        ],
    }

    if bet_category in alternatives:
        # Check which alternative has best historical performance
        best_alt = None
        best_win_rate = 0

        conn = get_db_connection()
        c = conn.cursor()

        for alt in alternatives[bet_category]:
            # Check if alternative performs better with same conditions
            total_wins = 0
            total_preds = 0

            for risky in risky_conditions:
                alt_key = get_condition_key(alt["category"], risky["condition"])
                c.execute("""SELECT wins, total_predictions FROM feature_error_patterns
                             WHERE condition_key = ?""", (alt_key,))
                row = c.fetchone()
                if row and row[1] > 0:
                    total_wins += row[0]
                    total_preds += row[1]

            if total_preds > 0:
                alt_win_rate = total_wins / total_preds
                if alt_win_rate > best_win_rate:
                    best_win_rate = alt_win_rate
                    best_alt = alt

        conn.close()

        if best_alt and best_win_rate > 0.5:
            return {
                "category": best_alt["category"],
                "name": best_alt["name"],
                "reason": best_alt["reason"],
                "expected_win_rate": best_win_rate
            }

    return None


def get_smart_learning_context_for_claude(features: dict, league_code: str = None) -> str:
    """Generate learning context for Claude prompt based on historical patterns.

    This tells Claude BEFORE analysis what conditions historically led to errors,
    so it can make better decisions (not just adjust confidence after).

    Returns formatted string for Claude prompt.
    """
    if not features:
        return ""

    conn = get_db_connection()
    c = conn.cursor()

    context_parts = []
    warnings = []
    recommendations = []

    # Categories to check
    bet_categories = [
        ("outcomes_home", "П1 (победа хозяев)"),
        ("outcomes_away", "П2 (победа гостей)"),
        ("outcomes_draw", "Ничья"),
        ("totals_over", "Тотал больше 2.5"),
        ("totals_under", "Тотал меньше 2.5"),
        ("btts", "Обе забьют")
    ]

    for category, category_name in bet_categories:
        conditions = extract_feature_conditions(features, category)

        if not conditions:
            continue

        category_issues = []
        category_strengths = []

        for condition in conditions:
            condition_key = get_condition_key(category, condition)

            c.execute("""SELECT wins, losses, total_predictions, suggested_adjustment
                         FROM feature_error_patterns
                         WHERE condition_key = ? AND total_predictions >= 5""",
                      (condition_key,))
            row = c.fetchone()

            if row:
                wins, losses, total, adj = row
                win_rate = wins / total if total > 0 else 0.5

                # Format condition name for readability
                condition_readable = condition.replace("_", " ")

                if win_rate < 0.40:  # Very risky
                    category_issues.append({
                        "condition": condition_readable,
                        "win_rate": win_rate,
                        "sample": total,
                        "severity": "high"
                    })
                elif win_rate < 0.48:  # Moderately risky
                    category_issues.append({
                        "condition": condition_readable,
                        "win_rate": win_rate,
                        "sample": total,
                        "severity": "medium"
                    })
                elif win_rate > 0.60:  # Strong pattern
                    category_strengths.append({
                        "condition": condition_readable,
                        "win_rate": win_rate,
                        "sample": total
                    })

        # Generate warnings for risky categories
        if category_issues:
            high_risk = [i for i in category_issues if i["severity"] == "high"]
            if high_risk:
                worst = min(high_risk, key=lambda x: x["win_rate"])
                warnings.append(
                    f"🔴 {category_name}: ОСТОРОЖНО! При условии '{worst['condition']}' "
                    f"винрейт только {worst['win_rate']:.0%} (n={worst['sample']}). "
                    f"Рассмотри альтернативу!"
                )
            elif category_issues:
                issue = category_issues[0]
                warnings.append(
                    f"🟡 {category_name}: При условии '{issue['condition']}' "
                    f"винрейт {issue['win_rate']:.0%} - ниже среднего."
                )

        # Generate recommendations for strong patterns
        if category_strengths:
            best = max(category_strengths, key=lambda x: x["win_rate"])
            recommendations.append(
                f"🟢 {category_name}: Условие '{best['condition']}' исторически хорошо работает "
                f"({best['win_rate']:.0%} винрейт, n={best['sample']})"
            )

    conn.close()

    # Build final context - INTERNAL FORMAT (not for direct display!)
    if not warnings and not recommendations:
        return ""

    # Use technical format that Claude should interpret, not copy directly
    context_parts.append("[INTERNAL_DATA: SMART_LEARNING]")
    context_parts.append("DO NOT show this section to user! Use for analysis only.")

    if warnings:
        context_parts.append("RISK_WARNINGS:")
        for w in warnings[:4]:  # Max 4 warnings
            # Strip emoji for cleaner internal format
            w_clean = w.replace("🔴 ", "HIGH_RISK: ").replace("🟡 ", "MED_RISK: ")
            context_parts.append(f"  - {w_clean}")

    if recommendations:
        context_parts.append("STRONG_PATTERNS:")
        for r in recommendations[:3]:  # Max 3 recommendations
            r_clean = r.replace("🟢 ", "GOOD: ")
            context_parts.append(f"  - {r_clean}")

    context_parts.append("ACTION: Factor these into confidence, but do NOT display this section in output!")
    context_parts.append("[/INTERNAL_DATA]")

    return "\n".join(context_parts)


def apply_learning_adjustments(bet_type: str, raw_confidence: int, features: dict) -> tuple:
    """Apply all learning adjustments to confidence.

    Returns: (adjusted_confidence, adjustments_applied)
    """
    adjustments = []
    confidence = raw_confidence

    category = categorize_bet(bet_type)

    # 1. Apply calibration
    if category:
        calibrated = get_calibrated_confidence(category, confidence)
        if calibrated != confidence:
            adjustments.append(f"calibration: {confidence}→{calibrated}")
            confidence = calibrated

    # 2. Apply pattern adjustment (basic patterns)
    if features:
        pattern_key = detect_pattern(features, bet_type)
        pattern_adj = get_pattern_adjustment(pattern_key)
        if pattern_adj != 0:
            new_conf = max(30, min(95, confidence + pattern_adj))
            adjustments.append(f"pattern: {'+' if pattern_adj > 0 else ''}{pattern_adj}")
            confidence = new_conf

    # 3. Apply smart feature-based adjustments
    # This is the KEY improvement - learns from specific conditions that cause failures
    if features and category:
        smart_adj, smart_reasons = get_smart_adjustments(category, features)
        if smart_adj != 0:
            old_conf = confidence
            confidence = max(30, min(95, confidence + smart_adj))
            adjustments.append(f"smart_learning: {old_conf}→{confidence}")
            # Add detailed reasons to log
            for reason in smart_reasons:
                adjustments.append(f"  └─ {reason}")

    # 4. NEW: Apply ROI-based adjustment
    # Win rate ≠ profitability! 60% win rate can still lose money at bad odds
    if category:
        roi_adj, roi_reason = get_roi_adjustment(category)
        if roi_adj != 0:
            old_conf = confidence
            confidence = max(30, min(95, confidence + roi_adj))
            adjustments.append(f"roi_learning: {old_conf}→{confidence}")
            if roi_reason:
                adjustments.append(f"  └─ {roi_reason}")

    return confidence, adjustments


# ===== ROI & EXPECTED VALUE TRACKING =====
# Track profitability, not just win rate

def get_roi_adjustment(bet_category: str) -> tuple:
    """Get confidence adjustment based on historical ROI for this category.

    ROI-based learning complements win rate calibration:
    - Win rate tells us HOW OFTEN we win
    - ROI tells us HOW MUCH we profit

    A category with 60% win rate but -10% ROI is actually losing money!
    A category with 45% win rate but +15% ROI is profitable!

    Returns: (adjustment: int, reason: str)
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Get overall ROI for this category
    c.execute("""SELECT total_bets, roi_percent, avg_odds, wins, losses
                 FROM roi_analytics
                 WHERE bet_category = ? AND condition_key = 'overall'""",
              (bet_category,))
    row = c.fetchone()
    conn.close()

    if not row or row[0] < 15:  # Need at least 15 bets for reliable ROI
        return 0, None

    total_bets, roi, avg_odds, wins, losses = row
    win_rate = wins / total_bets * 100 if total_bets > 0 else 50

    # ROI-based adjustment logic:
    # ROI < -20%: strong penalty (-12)
    # ROI -20% to -10%: medium penalty (-8)
    # ROI -10% to 0%: small penalty (-4)
    # ROI 0% to +10%: small boost (+3)
    # ROI +10% to +25%: medium boost (+6)
    # ROI > +25%: strong boost (+10)

    if roi < -20:
        adjustment = -12
        reason = f"💸 ROI: {roi:.1f}% ({total_bets} ставок) → -12%"
    elif roi < -10:
        adjustment = -8
        reason = f"📉 ROI: {roi:.1f}% ({total_bets} ставок) → -8%"
    elif roi < 0:
        adjustment = -4
        reason = f"⚠️ ROI: {roi:.1f}% ({total_bets} ставок) → -4%"
    elif roi < 10:
        adjustment = 3
        reason = f"📊 ROI: +{roi:.1f}% ({total_bets} ставок) → +3%"
    elif roi < 25:
        adjustment = 6
        reason = f"📈 ROI: +{roi:.1f}% ({total_bets} ставок) → +6%"
    else:
        adjustment = 10
        reason = f"🚀 ROI: +{roi:.1f}% ({total_bets} ставок) → +10%"

    logger.debug(f"ROI adjustment for {bet_category}: {adjustment} (ROI={roi:.1f}%, n={total_bets})")

    return adjustment, reason


def calculate_expected_value(confidence: int, odds: float) -> float:
    """Calculate Expected Value (EV) for a bet.

    EV = (win_probability × odds) - 1
    Positive EV = profitable in long run

    Returns EV as percentage (e.g., 15.0 means +15% EV)
    """
    if not odds or odds <= 1:
        return 0.0

    win_prob = confidence / 100.0
    ev = (win_prob * odds) - 1
    return round(ev * 100, 2)  # Return as percentage


def calculate_kelly_stake(confidence: int, odds: float, fraction: float = 0.25) -> float:
    """Calculate Kelly Criterion stake percentage.

    Kelly = (bp - q) / b
    where b = odds - 1, p = win probability, q = 1 - p

    Uses fractional Kelly (default 25%) for safer bankroll management.

    Returns stake as percentage of bankroll.
    """
    if not odds or odds <= 1:
        return 0.0

    b = odds - 1
    p = confidence / 100.0
    q = 1 - p

    kelly = (b * p - q) / b

    if kelly <= 0:
        return 0.0

    # Apply fractional Kelly and cap at 10%
    stake = min(kelly * fraction * 100, 10.0)
    return round(stake, 2)


def update_roi_analytics(bet_category: str, condition_key: str, is_win: bool,
                        odds: float, stake: float, ev: float):
    """Update ROI analytics after a prediction is verified.

    Tracks total ROI by category and condition for learning.
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Calculate profit/loss
    if is_win:
        returned = stake * odds
        profit = returned - stake
    else:
        returned = 0
        profit = -stake

    # Get existing record
    c.execute("""SELECT id, total_bets, wins, losses, total_staked, total_returned, avg_odds, avg_ev
                 FROM roi_analytics
                 WHERE bet_category = ? AND condition_key = ?""",
              (bet_category, condition_key))
    row = c.fetchone()

    if row:
        record_id, total_bets, wins, losses, total_staked, total_returned, avg_odds, avg_ev = row

        # Update averages
        new_total_bets = total_bets + 1
        new_wins = wins + (1 if is_win else 0)
        new_losses = losses + (0 if is_win else 1)
        new_total_staked = total_staked + stake
        new_total_returned = total_returned + returned
        new_avg_odds = (avg_odds * total_bets + odds) / new_total_bets
        new_avg_ev = (avg_ev * total_bets + ev) / new_total_bets

        # Calculate ROI
        roi = ((new_total_returned - new_total_staked) / new_total_staked * 100) if new_total_staked > 0 else 0

        c.execute("""UPDATE roi_analytics SET
                     total_bets = ?, wins = ?, losses = ?, total_staked = ?, total_returned = ?,
                     roi_percent = ?, avg_odds = ?, avg_ev = ?, last_updated = CURRENT_TIMESTAMP
                     WHERE id = ?""",
                  (new_total_bets, new_wins, new_losses, new_total_staked, new_total_returned,
                   roi, new_avg_odds, new_avg_ev, record_id))
    else:
        # Create new record
        roi = ((returned - stake) / stake * 100) if stake > 0 else 0
        c.execute("""INSERT INTO roi_analytics
                     (bet_category, condition_key, total_bets, wins, losses, total_staked, total_returned, roi_percent, avg_odds, avg_ev)
                     VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                  (bet_category, condition_key, 1 if is_win else 0, 0 if is_win else 1,
                   stake, returned, roi, odds, ev))

    conn.commit()
    conn.close()


def get_roi_based_recommendations(features: dict) -> str:
    """Get ROI-based recommendations for Claude prompt.

    Shows which bet types/conditions are actually PROFITABLE, not just winning.
    This is the key to 50%+ ROI.
    """
    if not features:
        return ""

    conn = get_db_connection()
    c = conn.cursor()

    context_parts = []
    profitable_bets = []
    unprofitable_bets = []

    # Get ROI data for each category
    bet_categories = [
        ("outcomes_home", "П1"),
        ("outcomes_away", "П2"),
        ("outcomes_draw", "Ничья"),
        ("totals_over", "ТБ 2.5"),
        ("totals_under", "ТМ 2.5"),
        ("btts", "Обе забьют")
    ]

    for category, name in bet_categories:
        conditions = extract_feature_conditions(features, category)

        for condition in conditions:
            condition_key = get_condition_key(category, condition)

            c.execute("""SELECT total_bets, wins, roi_percent, avg_odds, avg_ev
                         FROM roi_analytics
                         WHERE condition_key = ? AND total_bets >= 5""",
                      (condition_key,))
            row = c.fetchone()

            if row:
                total, wins, roi, avg_odds, avg_ev = row
                win_rate = wins / total * 100 if total > 0 else 0
                condition_readable = condition.replace("_", " ")

                if roi > 10:  # Profitable (ROI > 10%)
                    profitable_bets.append({
                        "name": name,
                        "condition": condition_readable,
                        "roi": roi,
                        "win_rate": win_rate,
                        "avg_odds": avg_odds,
                        "sample": total
                    })
                elif roi < -15:  # Losing money (ROI < -15%)
                    unprofitable_bets.append({
                        "name": name,
                        "condition": condition_readable,
                        "roi": roi,
                        "win_rate": win_rate,
                        "sample": total
                    })

    conn.close()

    if not profitable_bets and not unprofitable_bets:
        return ""

    # INTERNAL FORMAT - not for direct display!
    context_parts.append("[INTERNAL_DATA: ROI_ANALYSIS]")
    context_parts.append("DO NOT show this section to user! Use for bet selection only.")

    if profitable_bets:
        context_parts.append("PROFITABLE_BETS:")
        for bet in sorted(profitable_bets, key=lambda x: x["roi"], reverse=True)[:3]:
            context_parts.append(
                f"  - {bet['name']} + '{bet['condition']}': ROI +{bet['roi']:.0f}% "
                f"(WR {bet['win_rate']:.0f}%, odds {bet['avg_odds']:.2f}, n={bet['sample']})"
            )

    if unprofitable_bets:
        context_parts.append("UNPROFITABLE_BETS:")
        for bet in sorted(unprofitable_bets, key=lambda x: x["roi"])[:3]:
            context_parts.append(
                f"  - {bet['name']} + '{bet['condition']}': ROI {bet['roi']:.0f}% (LOSING MONEY!)"
            )

    context_parts.append("ACTION: Prioritize profitable bets, avoid unprofitable ones! Do NOT show this in output!")
    context_parts.append("[/INTERNAL_DATA]")

    return "\n".join(context_parts)


def get_overall_roi_stats() -> dict:
    """Get overall ROI statistics for admin dashboard."""
    conn = get_db_connection()
    c = conn.cursor()

    # Overall stats
    c.execute("""SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as losses,
                 AVG(odds) as avg_odds,
                 AVG(expected_value) as avg_ev,
                 AVG(confidence) as avg_confidence
                 FROM predictions
                 WHERE is_correct IS NOT NULL AND is_correct != 2
                 AND odds IS NOT NULL AND odds > 1""")
    overall = c.fetchone()

    # Calculate theoretical ROI
    total, wins, losses, avg_odds, avg_ev, avg_conf = overall if overall[0] else (0, 0, 0, 0, 0, 0)

    if total > 0 and wins is not None:
        # Simulated ROI if betting 1 unit per bet
        total_staked = total
        total_returned = wins * (avg_odds or 1.5)
        roi = ((total_returned - total_staked) / total_staked * 100) if total_staked > 0 else 0
        win_rate = wins / total * 100
    else:
        roi, win_rate, avg_ev = 0, 0, 0

    # ROI by category
    c.execute("""SELECT bet_category,
                 COUNT(*) as total,
                 SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins,
                 AVG(odds) as avg_odds
                 FROM predictions
                 WHERE is_correct IS NOT NULL AND is_correct != 2
                 AND bet_category IS NOT NULL AND odds > 1
                 GROUP BY bet_category
                 HAVING total >= 5
                 ORDER BY total DESC""")
    category_stats = []
    for row in c.fetchall():
        cat, cat_total, cat_wins, cat_avg_odds = row
        cat_roi = ((cat_wins * cat_avg_odds - cat_total) / cat_total * 100) if cat_total > 0 else 0
        cat_win_rate = cat_wins / cat_total * 100 if cat_total > 0 else 0
        category_stats.append({
            "category": cat,
            "total": cat_total,
            "wins": cat_wins,
            "win_rate": cat_win_rate,
            "avg_odds": cat_avg_odds,
            "roi": cat_roi
        })

    conn.close()

    return {
        "total_bets": total or 0,
        "wins": wins or 0,
        "losses": losses or 0,
        "win_rate": win_rate,
        "roi": roi,
        "avg_odds": avg_odds or 0,
        "avg_ev": avg_ev or 0,
        "by_category": sorted(category_stats, key=lambda x: x["roi"], reverse=True)
    }


# ===== USER PERSONALIZATION =====
# Analyzes user's betting history to provide personalized recommendations

def update_user_bet_stats(user_id: int, bet_category: str, is_correct: bool, odds: float):
    """Update user's betting statistics after each verified result"""
    if not bet_category or is_correct is None:
        return

    # is_correct: True = win, False = loss, 2 = push (skip)
    if is_correct == 2:  # Push
        return

    conn = get_db_connection()
    c = conn.cursor()

    # Get or create user stats for this category
    c.execute("""SELECT id, total_bets, wins, losses, avg_odds, roi
                 FROM user_bet_stats
                 WHERE user_id = ? AND bet_category = ?""",
              (user_id, bet_category))
    row = c.fetchone()

    if row:
        total = row[1] + 1
        wins = row[2] + (1 if is_correct else 0)
        losses = row[3] + (0 if is_correct else 1)
        # Update average odds
        old_avg = row[4] or 1.5
        new_avg = (old_avg * row[1] + (odds or 1.5)) / total if total > 0 else 1.5
        # Calculate ROI: (wins * avg_odds - total) / total * 100
        roi = ((wins * new_avg - total) / total * 100) if total > 0 else 0

        c.execute("""UPDATE user_bet_stats
                     SET total_bets = ?, wins = ?, losses = ?, avg_odds = ?, roi = ?,
                         updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?""",
                  (total, wins, losses, new_avg, roi, row[0]))
    else:
        wins = 1 if is_correct else 0
        losses = 0 if is_correct else 1
        odds_val = odds or 1.5
        roi = ((wins * odds_val - 1) / 1 * 100) if is_correct else -100

        c.execute("""INSERT INTO user_bet_stats
                     (user_id, bet_category, total_bets, wins, losses, avg_odds, roi)
                     VALUES (?, ?, 1, ?, ?, ?, ?)""",
                  (user_id, bet_category, wins, losses, odds_val, roi))

    conn.commit()
    conn.close()


def get_user_personalization(user_id: int) -> dict:
    """Get personalized insights for user based on their betting history"""
    conn = get_db_connection()
    c = conn.cursor()

    # Get user's stats by category
    c.execute("""SELECT bet_category, total_bets, wins, losses, roi
                 FROM user_bet_stats
                 WHERE user_id = ? AND total_bets >= 3
                 ORDER BY total_bets DESC""", (user_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {"has_data": False}

    best_categories = []
    worst_categories = []
    recommendations = []

    category_names = {
        "totals_over": "ТБ 2.5",
        "totals_under": "ТМ 2.5",
        "outcomes_home": "П1",
        "outcomes_away": "П2",
        "outcomes_draw": "Ничья",
        "btts": "Обе забьют",
        "double_chance": "Двойной шанс",
        "handicap": "Фора"
    }

    for cat, total, wins, losses, roi in rows:
        win_rate = (wins / total * 100) if total > 0 else 0
        cat_name = category_names.get(cat, cat)

        if win_rate >= 60 and total >= 5:
            best_categories.append({
                "category": cat,
                "name": cat_name,
                "win_rate": win_rate,
                "roi": roi,
                "total": total
            })
        elif win_rate <= 40 and total >= 5:
            worst_categories.append({
                "category": cat,
                "name": cat_name,
                "win_rate": win_rate,
                "roi": roi,
                "total": total
            })

    # Generate recommendations
    if best_categories:
        best = best_categories[0]
        recommendations.append({
            "type": "boost",
            "category": best["category"],
            "message_ru": f"🎯 {best['name']} — твой сильный тип! {best['win_rate']:.0f}% побед",
            "message_en": f"🎯 {best['name']} is your strength! {best['win_rate']:.0f}% win rate"
        })

    if worst_categories:
        worst = worst_categories[0]
        recommendations.append({
            "type": "warning",
            "category": worst["category"],
            "message_ru": f"⚠️ {worst['name']} — осторожно! Только {worst['win_rate']:.0f}% побед",
            "message_en": f"⚠️ {worst['name']} — be careful! Only {worst['win_rate']:.0f}% win rate"
        })

    return {
        "has_data": True,
        "best_categories": best_categories[:3],
        "worst_categories": worst_categories[:3],
        "recommendations": recommendations,
        "total_categories": len(rows)
    }


def get_personalized_advice(user_id: int, bet_category: str, lang: str = "ru") -> Optional[str]:
    """Get personalized advice for a specific bet type based on user's history"""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""SELECT total_bets, wins, losses, roi
                 FROM user_bet_stats
                 WHERE user_id = ? AND bet_category = ?""",
              (user_id, bet_category))
    row = c.fetchone()
    conn.close()

    if not row or row[0] < 5:  # Need at least 5 bets for advice
        return None

    total, wins, losses, roi = row
    win_rate = (wins / total * 100) if total > 0 else 0

    category_names = {
        "ru": {
            "totals_over": "ТБ 2.5",
            "totals_under": "ТМ 2.5",
            "outcomes_home": "П1",
            "outcomes_away": "П2",
            "outcomes_draw": "Ничья",
            "btts": "BTTS",
            "double_chance": "1X/X2/12",
            "handicap": "Фора"
        },
        "en": {
            "totals_over": "Over 2.5",
            "totals_under": "Under 2.5",
            "outcomes_home": "Home Win",
            "outcomes_away": "Away Win",
            "outcomes_draw": "Draw",
            "btts": "BTTS",
            "double_chance": "Double Chance",
            "handicap": "Handicap"
        },
        "pt": {
            "totals_over": "Mais 2.5",
            "totals_under": "Menos 2.5",
            "outcomes_home": "Vitória Casa",
            "outcomes_away": "Vitória Fora",
            "outcomes_draw": "Empate",
            "btts": "Ambas Marcam",
            "double_chance": "Dupla Chance",
            "handicap": "Handicap"
        },
        "es": {
            "totals_over": "Más 2.5",
            "totals_under": "Menos 2.5",
            "outcomes_home": "Victoria Local",
            "outcomes_away": "Victoria Visitante",
            "outcomes_draw": "Empate",
            "btts": "Ambos Marcan",
            "double_chance": "Doble Oportunidad",
            "handicap": "Hándicap"
        },
        "id": {
            "totals_over": "Over 2.5",
            "totals_under": "Under 2.5",
            "outcomes_home": "Tuan Rumah",
            "outcomes_away": "Tim Tamu",
            "outcomes_draw": "Seri",
            "btts": "Kedua Tim Cetak Gol",
            "double_chance": "Peluang Ganda",
            "handicap": "Voor"
        }
    }

    cat_name = category_names.get(lang, category_names["en"]).get(bet_category, bet_category)

    # Translations for personalized advice
    strength_texts = {
        "ru": f"🎯 **Твой конёк!** {cat_name}: {win_rate:.0f}% побед ({wins}/{total})",
        "en": f"🎯 **Your strength!** {cat_name}: {win_rate:.0f}% wins ({wins}/{total})",
        "pt": f"🎯 **Seu ponto forte!** {cat_name}: {win_rate:.0f}% vitórias ({wins}/{total})",
        "es": f"🎯 **Tu fuerte!** {cat_name}: {win_rate:.0f}% victorias ({wins}/{total})",
        "id": f"🎯 **Keunggulanmu!** {cat_name}: {win_rate:.0f}% kemenangan ({wins}/{total})"
    }

    careful_texts = {
        "ru": f"⚠️ **Осторожно!** {cat_name}: только {win_rate:.0f}% побед ({wins}/{total})",
        "en": f"⚠️ **Be careful!** {cat_name}: only {win_rate:.0f}% wins ({wins}/{total})",
        "pt": f"⚠️ **Cuidado!** {cat_name}: apenas {win_rate:.0f}% vitórias ({wins}/{total})",
        "es": f"⚠️ **¡Cuidado!** {cat_name}: solo {win_rate:.0f}% victorias ({wins}/{total})",
        "id": f"⚠️ **Hati-hati!** {cat_name}: hanya {win_rate:.0f}% kemenangan ({wins}/{total})"
    }

    if win_rate >= 65:
        return strength_texts.get(lang, strength_texts["en"])
    elif win_rate <= 40:
        return careful_texts.get(lang, careful_texts["en"])

    return None


def get_user_stats(user_id, page: int = 0, per_page: int = 7):
    """Get user's prediction statistics with categories and pagination"""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]

    # Total predictions count is already in 'total' variable
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 1", (user_id,))
    correct = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 0", (user_id,))
    incorrect = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 2", (user_id,))
    push = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct IS NOT NULL", (user_id,))
    checked = c.fetchone()[0]
    
    # Stats by category (excluding push from win rate calculation)
    categories = {}
    for cat in ["totals_over", "totals_under", "outcomes_home", "outcomes_away", "outcomes_draw", 
                "btts", "double_chance", "handicap", "other"]:
        c.execute("""SELECT 
                        COUNT(*),
                        SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END),
                        SUM(CASE WHEN is_correct = 2 THEN 1 ELSE 0 END)
                     FROM predictions 
                     WHERE user_id = ? AND bet_category = ? AND is_correct IS NOT NULL""", 
                  (user_id, cat))
        row = c.fetchone()
        cat_total = row[0] or 0
        cat_correct = row[1] or 0
        cat_push = row[2] or 0
        # Calculate rate excluding pushes
        cat_decided = cat_total - cat_push
        if cat_decided > 0:
            categories[cat] = {
                "total": cat_total,
                "correct": cat_correct,
                "push": cat_push,
                "rate": round(cat_correct / cat_decided * 100, 1)
            }
    
    # Recent predictions with pagination (all bets shown, no ALT marker in display)
    offset = page * per_page
    c.execute("""SELECT home_team, away_team, bet_type, confidence, result, is_correct, predicted_at, bet_rank
                 FROM predictions
                 WHERE user_id = ?
                 ORDER BY predicted_at DESC
                 LIMIT ? OFFSET ?""", (user_id, per_page, offset))
    recent = c.fetchall()

    # Stats by bet_rank (main vs alternatives)
    main_stats = {"total": 0, "correct": 0, "decided": 0}
    alt_stats = {"total": 0, "correct": 0, "decided": 0}

    c.execute("""SELECT
                    COUNT(*),
                    SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN is_correct IS NOT NULL AND is_correct != 2 THEN 1 ELSE 0 END)
                 FROM predictions
                 WHERE user_id = ? AND (bet_rank = 1 OR bet_rank IS NULL)""", (user_id,))
    row = c.fetchone()
    main_stats = {"total": row[0] or 0, "correct": row[1] or 0, "decided": row[2] or 0}

    c.execute("""SELECT
                    COUNT(*),
                    SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN is_correct IS NOT NULL AND is_correct != 2 THEN 1 ELSE 0 END)
                 FROM predictions
                 WHERE user_id = ? AND bet_rank > 1""", (user_id,))
    row = c.fetchone()
    alt_stats = {"total": row[0] or 0, "correct": row[1] or 0, "decided": row[2] or 0}

    conn.close()

    predictions = []
    for r in recent:
        predictions.append({
            "home": r[0],
            "away": r[1],
            "bet_type": r[2],
            "confidence": r[3],
            "result": r[4],
            "is_correct": r[5],
            "date": r[6],
            "bet_rank": r[7] if len(r) > 7 else 1
        })

    # Win rate excluding pushes
    decided = correct + incorrect
    win_rate = (correct / decided * 100) if decided > 0 else 0

    # Calculate rates for main/alt
    main_rate = (main_stats["correct"] / main_stats["decided"] * 100) if main_stats["decided"] > 0 else 0
    alt_rate = (alt_stats["correct"] / alt_stats["decided"] * 100) if alt_stats["decided"] > 0 else 0

    import math
    total_pages = math.ceil(total / per_page) if total > 0 else 1

    return {
        "total": total,
        "correct": correct,
        "incorrect": incorrect,
        "push": push,
        "checked": checked,
        "pending": total - checked,
        "win_rate": win_rate,
        "categories": categories,
        "predictions": predictions,
        "main_stats": {"total": main_stats["total"], "correct": main_stats["correct"],
                       "decided": main_stats["decided"], "rate": main_rate},
        "alt_stats": {"total": alt_stats["total"], "correct": alt_stats["correct"],
                      "decided": alt_stats["decided"], "rate": alt_rate},
        "page": page,
        "total_pages": total_pages
    }


# ===== CLAUDE PARSER =====

def parse_user_query(user_message):
    """Parse user query with Claude"""
    
    if not claude_client:
        return {"intent": "team_search", "teams": [user_message]}
    
    prompt = f"""Analyze this football betting message and return JSON.

Message: "{user_message}"

Return ONLY this JSON format:
{{"intent": "X", "teams": ["Y"], "league": "Z"}}

INTENT RULES (VERY IMPORTANT):
- "team_search" = mentions ANY specific team name OR asks about a match
  Examples: "Liverpool", "Арсенал", "что думаешь про Баварию", "Arsenal vs Brentford", "кто выиграет Реал"
- "recommend" = asks for general tips WITHOUT any team names
  Examples: "лучшие ставки", "что посоветуешь", "топ ставки сегодня"
- "matches_list" = wants to see list of matches (no specific team)
- "next_match" = asks for closest/next match
- "today" = asks about today's matches generally
- "tomorrow" = asks about tomorrow's matches generally
- "settings" = wants to change settings
- "favorites" = asks about favorites
- "stats" = asks about statistics
- "greeting" = just hello/hi
- "help" = asks how to use

CRITICAL: If user mentions ANY team name (even in a question like "what about Arsenal?") → intent = "team_search"

LEAGUE DETECTION:
- "немецкая лига" / "Bundesliga" / "бундеслига" = "BL1"
- "английская лига" / "Premier League" / "АПЛ" = "PL"  
- "испанская лига" / "La Liga" = "PD"
- "итальянская лига" / "Serie A" = "SA"
- "французская лига" / "Ligue 1" = "FL1"
- "лига чемпионов" / "Champions League" = "CL"
- "бразильская лига" / "Brasileirão" = "BSA"

Return ONLY valid JSON, no explanation."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response = message.content[0].text.strip()
        
        # Clean up response
        if response.startswith("```"):
            response = response.split("```")[1]
            if response.startswith("json"):
                response = response[4:]
        
        return json.loads(response)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return {"intent": "team_search", "teams": [user_message]}


# ===== FOOTBALL DATA API =====

async def get_matches(competition: Optional[str] = None, date_filter: Optional[str] = None,
                      days: int = 7, use_cache: bool = True) -> list[dict]:
    """Get matches from Football Data API - only upcoming matches (ASYNC)"""
    if not FOOTBALL_API_KEY:
        return []

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}

    # Check cache
    if use_cache and not competition and not date_filter and days == 7:
        if (matches_cache["updated_at"] and
            (datetime.now() - matches_cache["updated_at"]).total_seconds() < matches_cache["ttl_seconds"]):
            logger.info(f"Using cached matches: {len(matches_cache['data'])} matches")
            return matches_cache["data"]

    if date_filter == "today":
        date_from = datetime.now().strftime("%Y-%m-%d")
        date_to = date_from
    elif date_filter == "tomorrow":
        tomorrow = datetime.now() + timedelta(days=1)
        date_from = tomorrow.strftime("%Y-%m-%d")
        date_to = date_from
    else:
        date_from = datetime.now().strftime("%Y-%m-%d")
        date_to = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    # Only get SCHEDULED matches (not finished)
    params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED"}
    session = await get_http_session()

    if competition:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
            async with session.get(url, headers=headers, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    matches = data.get("matches", [])
                    matches = [m for m in matches if m.get("status") in ["SCHEDULED", "TIMED"]]
                    logger.info(f"Got {len(matches)} from {competition}")
                    return matches
                elif r.status == 429:
                    logger.warning(f"Rate limit hit for {competition}, waiting...")
                    await asyncio.sleep(6)
                    async with session.get(url, headers=headers, params=params) as r2:
                        if r2.status == 200:
                            data = await r2.json()
                            matches = data.get("matches", [])
                            return [m for m in matches if m.get("status") in ["SCHEDULED", "TIMED"]]
                else:
                    text = await r.text()
                    logger.error(f"API error {r.status} for {competition}: {text[:100]}")
        except Exception as e:
            logger.error(f"Error getting matches for {competition}: {e}")
        return []

    # Get from all leagues with rate limit awareness (Standard plan = 25 leagues, 60 req/min)
    all_matches = []
    leagues = list(COMPETITIONS.keys())

    for code in leagues:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{code}/matches"
            async with session.get(url, headers=headers, params=params) as r:
                if r.status == 200:
                    data = await r.json()
                    matches = data.get("matches", [])
                    matches = [m for m in matches if m.get("status") in ["SCHEDULED", "TIMED"]]
                    all_matches.extend(matches)
                    logger.info(f"Got {len(matches)} from {code}")
                elif r.status == 429:
                    logger.warning(f"Rate limit hit at {code}, waiting 6s...")
                    await asyncio.sleep(6)
                    async with session.get(url, headers=headers, params=params) as r2:
                        if r2.status == 200:
                            data = await r2.json()
                            matches = data.get("matches", [])
                            matches = [m for m in matches if m.get("status") in ["SCHEDULED", "TIMED"]]
                            all_matches.extend(matches)
                            logger.info(f"Retry got {len(matches)} from {code}")
                else:
                    text = await r.text()
                    logger.error(f"API error {r.status} for {code}: {text[:100]}")

            await asyncio.sleep(0.3)
            
        except Exception as e:
            logger.error(f"Error: {e}")
    
    logger.info(f"Total: {len(all_matches)} upcoming matches")
    
    # Update cache
    if not competition and not date_filter:
        matches_cache["data"] = all_matches
        matches_cache["updated_at"] = datetime.now()
        logger.info("Matches cache updated")
    
    return all_matches


async def get_standings(competition: str = "PL") -> Optional[dict]:
    """Get league standings with home/away stats (ASYNC)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/competitions/{competition}/standings"
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                data = await r.json()
                standings = data.get("standings", [])

                result = {"total": [], "home": [], "away": []}
                for s in standings:
                    table_type = s.get("type", "TOTAL").lower()
                    if table_type in result:
                        result[table_type] = s.get("table", [])

                return result
    except Exception as e:
        logger.error(f"Standings error: {e}")
    return None


async def get_team_form(team_id: int, limit: int = 5) -> Optional[dict]:
    """Get team's recent form (last N matches) (ASYNC)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}/matches"
        params = {"status": "FINISHED", "limit": limit}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                matches = data.get("matches", [])

                form = []
                goals_scored = 0
                goals_conceded = 0

                for m in matches[:limit]:
                    home_id = m.get("homeTeam", {}).get("id")
                    score = m.get("score", {}).get("fullTime", {})
                    home_goals = score.get("home", 0) or 0
                    away_goals = score.get("away", 0) or 0

                    if home_id == team_id:
                        goals_scored += home_goals
                        goals_conceded += away_goals
                        if home_goals > away_goals:
                            form.append("W")
                        elif home_goals < away_goals:
                            form.append("L")
                        else:
                            form.append("D")
                    else:
                        goals_scored += away_goals
                        goals_conceded += home_goals
                        if away_goals > home_goals:
                            form.append("W")
                        elif away_goals < home_goals:
                            form.append("L")
                        else:
                            form.append("D")

                return {
                    "form": "".join(form),
                    "wins": form.count("W"),
                    "draws": form.count("D"),
                    "losses": form.count("L"),
                    "goals_scored": goals_scored,
                    "goals_conceded": goals_conceded,
                    "matches": matches[:limit]
                }
    except Exception as e:
        logger.error(f"Form error: {e}")
    return None


async def get_h2h(match_id: int) -> Optional[dict]:
    """Get head-to-head history (ASYNC)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/matches/{match_id}/head2head"
        params = {"limit": 10}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                matches = data.get("matches", [])
                aggregates = data.get("aggregates", {})

                home_wins = 0
                away_wins = 0
                draws = 0
                total_goals = 0
                btts_count = 0
                over25_count = 0

                for m in matches:
                    score = m.get("score", {}).get("fullTime", {})
                    home_goals = score.get("home", 0) or 0
                    away_goals = score.get("away", 0) or 0

                    total_goals += home_goals + away_goals

                    if home_goals > 0 and away_goals > 0:
                        btts_count += 1

                    if home_goals + away_goals > 2.5:
                        over25_count += 1

                    if home_goals > away_goals:
                        home_wins += 1
                    elif away_goals > home_goals:
                        away_wins += 1
                    else:
                        draws += 1

                num_matches = len(matches)
                return {
                    "matches": matches,
                    "aggregates": aggregates,
                    "home_wins": home_wins,
                    "away_wins": away_wins,
                    "draws": draws,
                    "avg_goals": total_goals / num_matches if num_matches > 0 else 0,
                    "btts_percent": btts_count / num_matches * 100 if num_matches > 0 else 0,
                    "over25_percent": over25_count / num_matches * 100 if num_matches > 0 else 0
                }
    except Exception as e:
        logger.error(f"H2H error: {e}")
    return None


async def get_team_form_enhanced(team_id: int, limit: int = 10, upcoming_match_date: datetime = None) -> Optional[dict]:
    """Get enhanced team form with home/away split and average goals.

    Args:
        team_id: Football-data.org team ID
        limit: Number of past matches to analyze
        upcoming_match_date: Date of upcoming match (for accurate rest days calculation)
    """
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}/matches"
        params = {"status": "FINISHED", "limit": limit}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                matches = data.get("matches", [])

                # CRITICAL: Sort matches by date DESCENDING to get most recent first
                # API doesn't guarantee order, so we must sort explicitly
                matches.sort(key=lambda x: x.get("utcDate", ""), reverse=True)

                # Overall stats
                overall = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "form": []}
                # Home stats
                home = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "matches": 0}
                # Away stats
                away = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "matches": 0}
                # BTTS tracking
                btts_count = 0
                over25_count = 0

                # Rest days calculation - calculate days between last match and UPCOMING match
                last_match_date = None
                rest_days = None

                for m in matches[:limit]:
                    # Get last match date (first match in list is most recent)
                    if last_match_date is None:
                        match_date_str = m.get("utcDate", "")
                        if match_date_str:
                            try:
                                last_match_date = datetime.fromisoformat(match_date_str.replace("Z", "+00:00"))
                                # Calculate rest days to UPCOMING match, not to now
                                if upcoming_match_date:
                                    # Ensure both dates have timezone info
                                    if upcoming_match_date.tzinfo is None:
                                        upcoming_with_tz = upcoming_match_date.replace(tzinfo=timezone.utc)
                                    else:
                                        upcoming_with_tz = upcoming_match_date
                                    rest_days = (upcoming_with_tz - last_match_date).days
                                    logger.info(f"🔄 Team {team_id}: last match {last_match_date.date()}, upcoming {upcoming_with_tz.date()}, rest_days={rest_days}")
                                else:
                                    # Fallback to now if upcoming date not provided
                                    rest_days = (datetime.now(last_match_date.tzinfo) - last_match_date).days
                                    logger.warning(f"⚠️ Team {team_id}: no upcoming date provided, using now. last match {last_match_date.date()}, rest_days={rest_days}")
                            except Exception as e:
                                logger.warning(f"Rest days calculation error: {e}")
                    home_id = m.get("homeTeam", {}).get("id")
                    score = m.get("score", {}).get("fullTime", {})
                    home_goals = score.get("home", 0) or 0
                    away_goals = score.get("away", 0) or 0

                    # BTTS and totals
                    if home_goals > 0 and away_goals > 0:
                        btts_count += 1
                    if home_goals + away_goals > 2.5:
                        over25_count += 1

                    is_home = (home_id == team_id)
                    team_goals = home_goals if is_home else away_goals
                    opp_goals = away_goals if is_home else home_goals

                    # Overall
                    overall["gf"] += team_goals
                    overall["ga"] += opp_goals

                    if team_goals > opp_goals:
                        overall["w"] += 1
                        overall["form"].append("W")
                    elif team_goals < opp_goals:
                        overall["l"] += 1
                        overall["form"].append("L")
                    else:
                        overall["d"] += 1
                        overall["form"].append("D")

                    # Home/Away split
                    if is_home:
                        home["matches"] += 1
                        home["gf"] += team_goals
                        home["ga"] += opp_goals
                        if team_goals > opp_goals:
                            home["w"] += 1
                        elif team_goals < opp_goals:
                            home["l"] += 1
                        else:
                            home["d"] += 1
                    else:
                        away["matches"] += 1
                        away["gf"] += team_goals
                        away["ga"] += opp_goals
                        if team_goals > opp_goals:
                            away["w"] += 1
                        elif team_goals < opp_goals:
                            away["l"] += 1
                        else:
                            away["d"] += 1

                num_matches = len(matches[:limit])
                home_matches = home["matches"] or 1
                away_matches = away["matches"] or 1

                return {
                    "overall": {
                        "form": "".join(overall["form"][:5]),
                        "wins": overall["w"],
                        "draws": overall["d"],
                        "losses": overall["l"],
                        "goals_scored": overall["gf"],
                        "goals_conceded": overall["ga"],
                        "avg_goals_scored": round(overall["gf"] / num_matches, 2) if num_matches > 0 else 0,
                        "avg_goals_conceded": round(overall["ga"] / num_matches, 2) if num_matches > 0 else 0,
                    },
                    "home": {
                        "matches": home["matches"],
                        "wins": home["w"],
                        "draws": home["d"],
                        "losses": home["l"],
                        "goals_scored": home["gf"],
                        "goals_conceded": home["ga"],
                        "avg_goals_scored": round(home["gf"] / home_matches, 2),
                        "avg_goals_conceded": round(home["ga"] / home_matches, 2),
                        "win_rate": round(home["w"] / home_matches * 100, 1),
                    },
                    "away": {
                        "matches": away["matches"],
                        "wins": away["w"],
                        "draws": away["d"],
                        "losses": away["l"],
                        "goals_scored": away["gf"],
                        "goals_conceded": away["ga"],
                        "avg_goals_scored": round(away["gf"] / away_matches, 2),
                        "avg_goals_conceded": round(away["ga"] / away_matches, 2),
                        "win_rate": round(away["w"] / away_matches * 100, 1),
                    },
                    "btts_percent": round(btts_count / num_matches * 100, 1) if num_matches > 0 else 0,
                    "over25_percent": round(over25_count / num_matches * 100, 1) if num_matches > 0 else 0,
                    "rest_days": rest_days,
                    "last_match_date": last_match_date.isoformat() if last_match_date else None,
                }
    except Exception as e:
        logger.error(f"Enhanced form error: {e}")
    return None


# ===== WEB SEARCH FOR MATCH CONTEXT =====

def extract_referee_name_from_text(text: str) -> Optional[str]:
    """Extract referee name from article title/text using patterns.

    Looks for patterns like:
    - "referee: Name Surname"
    - "appointed referee Name"
    - "Name Surname will referee"
    - "official Name Surname"
    """
    import re
    text_lower = text.lower()

    # Common referee name patterns
    patterns = [
        r'referee[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)',
        r'appointed\s+(?:referee\s+)?([A-Z][a-z]+\s+[A-Z][a-z]+)',
        r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:will\s+)?referee',
        r'official[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)',
        r'(?:match\s+)?official[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)',
        r'([A-Z][a-z]+\s+[A-Z][a-z]+)\s+to\s+officiate',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name = match.group(1).strip()
            # Filter out common false positives
            false_positives = ['match preview', 'team news', 'starting lineup', 'injury update']
            if name.lower() not in false_positives and len(name) > 5:
                return name

    return None


def extract_odds_movement_from_text(text: str) -> Optional[dict]:
    """Extract odds movement information from article title/text.

    Looks for patterns like:
    - "odds drop" / "odds fall"
    - "price shortened"
    - "1.50 to 1.40"
    - "heavy backing"
    """
    import re
    text_lower = text.lower()

    movement = {"detected": False, "direction": None, "details": None}

    # Patterns indicating odds dropped (sharp money on that outcome)
    drop_patterns = ['odds drop', 'odds fall', 'price short', 'odds shorten',
                     'heavy backing', 'money coming in', 'being backed']
    rise_patterns = ['odds rise', 'odds drift', 'price lengthen', 'odds lengthen']

    for pattern in drop_patterns:
        if pattern in text_lower:
            movement["detected"] = True
            movement["direction"] = "drop"
            movement["details"] = text[:100]
            return movement

    for pattern in rise_patterns:
        if pattern in text_lower:
            movement["detected"] = True
            movement["direction"] = "rise"
            movement["details"] = text[:100]
            return movement

    # Try to find numerical odds changes like "1.80 to 1.60"
    odds_pattern = r'(\d+\.\d+)\s*(?:to|→|->)\s*(\d+\.\d+)'
    match = re.search(odds_pattern, text)
    if match:
        old_odds = float(match.group(1))
        new_odds = float(match.group(2))
        if abs(old_odds - new_odds) >= 0.05:
            movement["detected"] = True
            movement["direction"] = "drop" if new_odds < old_odds else "rise"
            movement["details"] = f"{old_odds} → {new_odds}"
            return movement

    return None


async def search_match_news(home_team: str, away_team: str, competition: str = "") -> dict:
    """Search for real-time news about the match: injuries, lineups, team news, referee, odds.

    Uses Google News RSS (free, no API key required).

    Returns dict with:
    - injuries: list of injury news
    - lineups: lineup information
    - news: general team news
    - referee: referee name if found
    - odds_movement: odds movement info if detected
    - raw_articles: raw article titles for Claude context
    """
    result = {
        "injuries": [],
        "lineups": [],
        "news": [],
        "referee": None,  # Referee name if found in news
        "odds_movement": None,  # Odds movement info
        "raw_articles": [],
        "searched": False,
        "error": None
    }

    session = await get_http_session()

    # Queries to search - more specific for match context
    # Clean team names (remove FC, CF, etc. for better search)
    home_clean = home_team.replace(" FC", "").replace(" CF", "").replace("FC ", "").strip()
    away_clean = away_team.replace(" FC", "").replace(" CF", "").replace("FC ", "").strip()

    queries = [
        f"{home_clean} vs {away_clean} match preview",
        f"{home_clean} team news injury",
        f"{away_clean} team news injury",
        f"{home_clean} {away_clean} predicted lineup",
        f"{home_clean} vs {away_clean} referee",  # More generic referee search
        f"{home_clean} {away_clean} odds betting",  # Odds movement search
    ]

    # Add competition-specific query if we have competition name
    if competition:
        comp_clean = competition.replace("Primera Division", "La Liga").replace("Serie A", "Serie A")
        queries.insert(0, f"{home_clean} {away_clean} {comp_clean}")
        # Add referee search with competition
        queries.append(f"{comp_clean} referee {home_clean} {away_clean}")

    all_articles = []

    for query in queries:
        try:
            # Google News RSS feed (free, no API key)
            encoded_query = quote_plus(query)
            url = f"https://news.google.com/rss/search?q={encoded_query}&hl=en&gl=US&ceid=US:en"

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    # Parse RSS XML
                    root = ET.fromstring(text)

                    # Find all items (articles)
                    for item in root.findall('.//item')[:3]:  # Take top 3 per query
                        title = item.find('title')
                        pub_date = item.find('pubDate')

                        if title is not None:
                            title_text = title.text or ""
                            # Check if article is from last 48 hours
                            is_recent = True
                            if pub_date is not None and pub_date.text:
                                try:
                                    # Parse date like "Sat, 07 Dec 2024 10:00:00 GMT"
                                    from email.utils import parsedate_to_datetime
                                    article_date = parsedate_to_datetime(pub_date.text)
                                    if datetime.now(timezone.utc) - article_date > timedelta(hours=48):
                                        is_recent = False
                                except:
                                    pass

                            if is_recent and title_text:
                                all_articles.append({
                                    "title": title_text,
                                    "query": query
                                })

                                # Categorize
                                title_lower = title_text.lower()
                                if any(kw in title_lower for kw in ['injur', 'doubt', 'miss', 'out', 'ruled out', 'sidelined', 'absent']):
                                    result["injuries"].append(title_text)
                                elif any(kw in title_lower for kw in ['lineup', 'line-up', 'starting', 'team news', 'squad']):
                                    result["lineups"].append(title_text)
                                else:
                                    result["news"].append(title_text)

                                # Try to extract referee name if mentioned
                                if ('referee' in query or 'referee' in title_lower or 'official' in title_lower) and result["referee"] is None:
                                    # First check known referees
                                    for ref_name in REFEREE_STATS.keys():
                                        if ref_name.lower() in title_lower:
                                            result["referee"] = ref_name
                                            logger.info(f"🔍 Found known referee in news: {ref_name}")
                                            break

                                    # If not found, try to extract name from text
                                    if result["referee"] is None:
                                        extracted_ref = extract_referee_name_from_text(title_text)
                                        if extracted_ref:
                                            result["referee"] = extracted_ref
                                            logger.info(f"🔍 Extracted referee from news: {extracted_ref}")

                                # Try to extract odds movement info
                                if ('odds' in query or 'betting' in query or 'odds' in title_lower) and result["odds_movement"] is None:
                                    movement = extract_odds_movement_from_text(title_text)
                                    if movement and movement.get("detected"):
                                        result["odds_movement"] = movement
                                        logger.info(f"🔍 Found odds movement in news: {movement}")
        except asyncio.TimeoutError:
            logger.warning(f"Web search timeout for: {query}")
        except Exception as e:
            logger.warning(f"Web search error for '{query}': {e}")

    # Filter news for relevance - must mention at least one team
    def is_relevant(text):
        text_lower = text.lower()
        return home_clean.lower() in text_lower or away_clean.lower() in text_lower

    # Deduplicate and filter for relevance
    result["injuries"] = list(dict.fromkeys([n for n in result["injuries"] if is_relevant(n)]))[:5]
    result["lineups"] = list(dict.fromkeys([n for n in result["lineups"] if is_relevant(n)]))[:4]
    # For general news, prefer relevant ones but keep some if nothing relevant found
    relevant_news = [n for n in result["news"] if is_relevant(n)]
    result["news"] = list(dict.fromkeys(relevant_news if relevant_news else result["news"][:3]))[:6]
    result["raw_articles"] = [a["title"] for a in all_articles if is_relevant(a["title"])][:15]
    result["searched"] = len(all_articles) > 0

    logger.info(f"🔍 Web search for {home_team} vs {away_team}: {len(all_articles)} articles found, {len(result['injuries'])} injuries, {len(result['news'])} news")

    return result


async def get_weather_for_match(venue: str, match_date: datetime = None) -> Optional[dict]:
    """Get weather for match venue (basic implementation using wttr.in - free, no key)"""
    if not venue:
        return None

    try:
        session = await get_http_session()
        # wttr.in is a free weather service
        # Extract city from venue name (rough heuristic)
        city = venue.split(',')[0].strip() if ',' in venue else venue.split()[0]
        url = f"https://wttr.in/{quote(city)}?format=j1"

        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                current = data.get("current_condition", [{}])[0]
                return {
                    "temp_c": current.get("temp_C", "?"),
                    "feels_like": current.get("FeelsLikeC", "?"),
                    "weather": current.get("weatherDesc", [{}])[0].get("value", "Unknown"),
                    "humidity": current.get("humidity", "?"),
                    "wind_kmph": current.get("windspeedKmph", "?"),
                    "precipitation": current.get("precipMM", "0"),
                }
    except Exception as e:
        logger.warning(f"Weather fetch error for {venue}: {e}")

    return None


def format_web_context_for_claude(web_news: dict, weather: dict = None, lang: str = "ru") -> str:
    """Format web search results for Claude's context"""
    if not web_news.get("searched"):
        return ""

    context = "\n🌐 АКТУАЛЬНЫЕ НОВОСТИ (веб-поиск):\n"

    # Referee from web search
    if web_news.get("referee"):
        context += f"\n👨‍⚖️ СУДЬЯ (из новостей): {web_news['referee']}\n"

    # Odds movement from web search
    if web_news.get("odds_movement") and web_news["odds_movement"].get("detected"):
        movement = web_news["odds_movement"]
        direction = "⬇️ ПАДАЮТ" if movement["direction"] == "drop" else "⬆️ РАСТУТ"
        context += f"\n📉 ДВИЖЕНИЕ КОЭФФИЦИЕНТОВ (из новостей): Коэффициенты {direction}\n"
        if movement.get("details"):
            context += f"  • {movement['details']}\n"
        if movement["direction"] == "drop":
            context += "  • ⚡ Признаки SHARP MONEY - профессионалы ставят!\n"

    if web_news.get("injuries"):
        context += "\n⚠️ ТРАВМЫ И ПРОПУСКИ:\n"
        for inj in web_news["injuries"][:5]:
            context += f"  • {inj}\n"

    if web_news.get("lineups"):
        context += "\n📋 СОСТАВЫ И ЗАЯВКИ:\n"
        for lineup in web_news["lineups"][:4]:
            context += f"  • {lineup}\n"

    if web_news.get("news"):
        context += "\n📰 НОВОСТИ:\n"
        for news in web_news["news"][:5]:
            context += f"  • {news}\n"

    if weather:
        context += f"\n🌤️ ПОГОДА НА СТАДИОНЕ:\n"
        context += f"  • Температура: {weather['temp_c']}°C (ощущается {weather['feels_like']}°C)\n"
        context += f"  • Условия: {weather['weather']}\n"
        if float(weather.get('precipitation', 0)) > 0:
            context += f"  • ⚠️ Осадки: {weather['precipitation']}mm\n"
        if float(weather.get('wind_kmph', 0)) > 30:
            context += f"  • ⚠️ Сильный ветер: {weather['wind_kmph']} км/ч\n"

    if context.strip() == "🌐 АКТУАЛЬНЫЕ НОВОСТИ (веб-поиск):":
        return ""  # Nothing found

    context += "\n"
    return context


def format_lineups_from_api(lineups: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format lineup and injury data from Football API (PRIMARY SOURCE).

    This is more reliable than web search - use this data first!
    """
    if not lineups:
        return ""

    context = ""

    # Official lineups from API
    home_lineup = lineups.get("home_lineup", [])
    away_lineup = lineups.get("away_lineup", [])

    if home_lineup or away_lineup:
        context += "\n📋 ОФИЦИАЛЬНЫЕ СОСТАВЫ (из API):\n"
        if home_lineup:
            players = [p.get("name", p) if isinstance(p, dict) else str(p) for p in home_lineup[:11]]
            context += f"  {home_team}: {', '.join(players[:5])}...\n"
        if away_lineup:
            players = [p.get("name", p) if isinstance(p, dict) else str(p) for p in away_lineup[:11]]
            context += f"  {away_team}: {', '.join(players[:5])}...\n"

    # Injuries from API (CRITICAL DATA!)
    home_injuries = lineups.get("home_injuries", [])
    away_injuries = lineups.get("away_injuries", [])

    total_injuries = len(home_injuries) + len(away_injuries)

    if home_injuries or away_injuries:
        context += f"\n🏥 ТРАВМЫ/ДИСКВАЛИФИКАЦИИ (из API) - ВСЕГО: {total_injuries}:\n"

        if home_injuries:
            context += f"  {home_team} ({len(home_injuries)} игроков):\n"
            for inj in home_injuries[:5]:
                if isinstance(inj, dict):
                    name = inj.get("name", "Unknown")
                    reason = inj.get("type", inj.get("reason", ""))
                    context += f"    ❌ {name}"
                    if reason:
                        context += f" ({reason})"
                    context += "\n"
                else:
                    context += f"    ❌ {inj}\n"
            if len(home_injuries) > 5:
                context += f"    ... и еще {len(home_injuries) - 5}\n"

        if away_injuries:
            context += f"  {away_team} ({len(away_injuries)} игроков):\n"
            for inj in away_injuries[:5]:
                if isinstance(inj, dict):
                    name = inj.get("name", "Unknown")
                    reason = inj.get("type", inj.get("reason", ""))
                    context += f"    ❌ {name}"
                    if reason:
                        context += f" ({reason})"
                    context += "\n"
                else:
                    context += f"    ❌ {inj}\n"
            if len(away_injuries) > 5:
                context += f"    ... и еще {len(away_injuries) - 5}\n"

        # Add impact assessment
        if total_injuries >= 8:
            context += "  ⚠️ КРИТИЧНО: Много травм - высокая неопределенность!\n"
        elif total_injuries >= 4:
            context += "  ⚠️ Умеренные потери - учитывай в анализе\n"

    # Match status
    status = lineups.get("status", "")
    if status and status != "SCHEDULED":
        context += f"\n📌 Статус матча: {status}\n"

    return context


# ===== REFEREE STATISTICS =====
# Average stats per game for top European referees
# Data source: Transfermarkt, WhoScored (manually compiled, update periodically)

REFEREE_STATS = {
    # Premier League referees
    "Anthony Taylor": {"cards_per_game": 4.2, "yellows_per_game": 3.8, "reds_per_game": 0.15, "penalties_per_game": 0.35, "fouls_per_game": 24, "style": "strict"},
    "Michael Oliver": {"cards_per_game": 3.8, "yellows_per_game": 3.5, "reds_per_game": 0.12, "penalties_per_game": 0.42, "fouls_per_game": 22, "style": "balanced"},
    "Paul Tierney": {"cards_per_game": 4.5, "yellows_per_game": 4.1, "reds_per_game": 0.18, "penalties_per_game": 0.28, "fouls_per_game": 26, "style": "strict"},
    "Simon Hooper": {"cards_per_game": 4.0, "yellows_per_game": 3.7, "reds_per_game": 0.10, "penalties_per_game": 0.32, "fouls_per_game": 23, "style": "balanced"},
    "Chris Kavanagh": {"cards_per_game": 3.6, "yellows_per_game": 3.3, "reds_per_game": 0.12, "penalties_per_game": 0.38, "fouls_per_game": 21, "style": "lenient"},
    "Robert Jones": {"cards_per_game": 4.3, "yellows_per_game": 3.9, "reds_per_game": 0.14, "penalties_per_game": 0.30, "fouls_per_game": 25, "style": "strict"},
    "John Brooks": {"cards_per_game": 3.9, "yellows_per_game": 3.6, "reds_per_game": 0.11, "penalties_per_game": 0.33, "fouls_per_game": 22, "style": "balanced"},
    "Andy Madley": {"cards_per_game": 4.1, "yellows_per_game": 3.8, "reds_per_game": 0.13, "penalties_per_game": 0.29, "fouls_per_game": 24, "style": "balanced"},
    "Stuart Attwell": {"cards_per_game": 3.5, "yellows_per_game": 3.2, "reds_per_game": 0.10, "penalties_per_game": 0.36, "fouls_per_game": 20, "style": "lenient"},
    "David Coote": {"cards_per_game": 4.0, "yellows_per_game": 3.7, "reds_per_game": 0.12, "penalties_per_game": 0.34, "fouls_per_game": 23, "style": "balanced"},
    "Peter Bankes": {"cards_per_game": 4.4, "yellows_per_game": 4.0, "reds_per_game": 0.16, "penalties_per_game": 0.31, "fouls_per_game": 25, "style": "strict"},
    "Darren England": {"cards_per_game": 3.7, "yellows_per_game": 3.4, "reds_per_game": 0.11, "penalties_per_game": 0.35, "fouls_per_game": 21, "style": "balanced"},
    "Tony Harrington": {"cards_per_game": 4.2, "yellows_per_game": 3.9, "reds_per_game": 0.14, "penalties_per_game": 0.27, "fouls_per_game": 24, "style": "strict"},
    "Sam Barrott": {"cards_per_game": 3.8, "yellows_per_game": 3.5, "reds_per_game": 0.10, "penalties_per_game": 0.32, "fouls_per_game": 22, "style": "balanced"},

    # La Liga referees
    "Mateu Lahoz": {"cards_per_game": 5.2, "yellows_per_game": 4.8, "reds_per_game": 0.22, "penalties_per_game": 0.25, "fouls_per_game": 28, "style": "very_strict"},
    "Gil Manzano": {"cards_per_game": 4.8, "yellows_per_game": 4.4, "reds_per_game": 0.18, "penalties_per_game": 0.30, "fouls_per_game": 26, "style": "strict"},
    "Del Cerro Grande": {"cards_per_game": 4.5, "yellows_per_game": 4.1, "reds_per_game": 0.16, "penalties_per_game": 0.35, "fouls_per_game": 25, "style": "strict"},
    "Hernández Hernández": {"cards_per_game": 5.0, "yellows_per_game": 4.6, "reds_per_game": 0.20, "penalties_per_game": 0.28, "fouls_per_game": 27, "style": "very_strict"},
    "Jesús Gil Manzano": {"cards_per_game": 4.8, "yellows_per_game": 4.4, "reds_per_game": 0.18, "penalties_per_game": 0.30, "fouls_per_game": 26, "style": "strict"},

    # Serie A referees
    "Daniele Orsato": {"cards_per_game": 4.6, "yellows_per_game": 4.2, "reds_per_game": 0.17, "penalties_per_game": 0.38, "fouls_per_game": 27, "style": "strict"},
    "Marco Guida": {"cards_per_game": 4.3, "yellows_per_game": 3.9, "reds_per_game": 0.15, "penalties_per_game": 0.35, "fouls_per_game": 25, "style": "balanced"},
    "Davide Massa": {"cards_per_game": 4.4, "yellows_per_game": 4.0, "reds_per_game": 0.16, "penalties_per_game": 0.32, "fouls_per_game": 26, "style": "strict"},
    "Gianluca Rocchi": {"cards_per_game": 4.1, "yellows_per_game": 3.8, "reds_per_game": 0.13, "penalties_per_game": 0.40, "fouls_per_game": 24, "style": "balanced"},
    "Maurizio Mariani": {"cards_per_game": 4.5, "yellows_per_game": 4.1, "reds_per_game": 0.16, "penalties_per_game": 0.33, "fouls_per_game": 26, "style": "strict"},

    # Bundesliga referees
    "Felix Zwayer": {"cards_per_game": 3.8, "yellows_per_game": 3.5, "reds_per_game": 0.12, "penalties_per_game": 0.30, "fouls_per_game": 22, "style": "balanced"},
    "Daniel Siebert": {"cards_per_game": 3.6, "yellows_per_game": 3.3, "reds_per_game": 0.10, "penalties_per_game": 0.32, "fouls_per_game": 21, "style": "lenient"},
    "Deniz Aytekin": {"cards_per_game": 3.5, "yellows_per_game": 3.2, "reds_per_game": 0.09, "penalties_per_game": 0.28, "fouls_per_game": 20, "style": "lenient"},
    "Sascha Stegemann": {"cards_per_game": 3.9, "yellows_per_game": 3.6, "reds_per_game": 0.11, "penalties_per_game": 0.34, "fouls_per_game": 23, "style": "balanced"},
    "Tobias Welz": {"cards_per_game": 4.0, "yellows_per_game": 3.7, "reds_per_game": 0.12, "penalties_per_game": 0.31, "fouls_per_game": 23, "style": "balanced"},

    # Ligue 1 referees
    "Clément Turpin": {"cards_per_game": 4.2, "yellows_per_game": 3.8, "reds_per_game": 0.15, "penalties_per_game": 0.33, "fouls_per_game": 25, "style": "balanced"},
    "François Letexier": {"cards_per_game": 3.9, "yellows_per_game": 3.6, "reds_per_game": 0.12, "penalties_per_game": 0.35, "fouls_per_game": 23, "style": "balanced"},
    "Benoît Bastien": {"cards_per_game": 4.4, "yellows_per_game": 4.0, "reds_per_game": 0.16, "penalties_per_game": 0.30, "fouls_per_game": 26, "style": "strict"},
    "Jérôme Brisard": {"cards_per_game": 4.1, "yellows_per_game": 3.8, "reds_per_game": 0.13, "penalties_per_game": 0.32, "fouls_per_game": 24, "style": "balanced"},

    # UEFA/Champions League referees
    "Szymon Marciniak": {"cards_per_game": 3.7, "yellows_per_game": 3.4, "reds_per_game": 0.11, "penalties_per_game": 0.30, "fouls_per_game": 22, "style": "balanced"},
    "Danny Makkelie": {"cards_per_game": 3.5, "yellows_per_game": 3.2, "reds_per_game": 0.10, "penalties_per_game": 0.35, "fouls_per_game": 21, "style": "lenient"},
    "Slavko Vinčić": {"cards_per_game": 4.0, "yellows_per_game": 3.7, "reds_per_game": 0.12, "penalties_per_game": 0.32, "fouls_per_game": 23, "style": "balanced"},
    "Artur Dias": {"cards_per_game": 4.3, "yellows_per_game": 3.9, "reds_per_game": 0.15, "penalties_per_game": 0.28, "fouls_per_game": 25, "style": "strict"},
    "Istvan Kovacs": {"cards_per_game": 4.1, "yellows_per_game": 3.8, "reds_per_game": 0.13, "penalties_per_game": 0.34, "fouls_per_game": 24, "style": "balanced"},
    "Jesús Gil Manzano": {"cards_per_game": 4.8, "yellows_per_game": 4.4, "reds_per_game": 0.18, "penalties_per_game": 0.30, "fouls_per_game": 26, "style": "strict"},

    # Brazilian Serie A referees (BSA) - tend to be stricter with cards
    "Raphael Claus": {"cards_per_game": 5.2, "yellows_per_game": 4.8, "reds_per_game": 0.20, "penalties_per_game": 0.35, "fouls_per_game": 28, "style": "strict"},
    "Wilton Pereira Sampaio": {"cards_per_game": 5.0, "yellows_per_game": 4.6, "reds_per_game": 0.18, "penalties_per_game": 0.32, "fouls_per_game": 27, "style": "strict"},
    "Anderson Daronco": {"cards_per_game": 5.5, "yellows_per_game": 5.0, "reds_per_game": 0.25, "penalties_per_game": 0.38, "fouls_per_game": 30, "style": "very_strict"},
    "Braulio da Silva Machado": {"cards_per_game": 4.8, "yellows_per_game": 4.4, "reds_per_game": 0.17, "penalties_per_game": 0.30, "fouls_per_game": 26, "style": "strict"},
    "Flavio Rodrigues de Souza": {"cards_per_game": 4.6, "yellows_per_game": 4.2, "reds_per_game": 0.16, "penalties_per_game": 0.33, "fouls_per_game": 25, "style": "balanced"},
    "Ramon Abatti Abel": {"cards_per_game": 5.1, "yellows_per_game": 4.7, "reds_per_game": 0.19, "penalties_per_game": 0.34, "fouls_per_game": 28, "style": "strict"},
    "Bruno Arleu de Araujo": {"cards_per_game": 4.9, "yellows_per_game": 4.5, "reds_per_game": 0.18, "penalties_per_game": 0.31, "fouls_per_game": 27, "style": "strict"},
    "Luiz Flavio de Oliveira": {"cards_per_game": 4.7, "yellows_per_game": 4.3, "reds_per_game": 0.17, "penalties_per_game": 0.35, "fouls_per_game": 26, "style": "balanced"},
    "Wagner do Nascimento Magalhaes": {"cards_per_game": 5.3, "yellows_per_game": 4.9, "reds_per_game": 0.21, "penalties_per_game": 0.30, "fouls_per_game": 29, "style": "very_strict"},
    "Leandro Pedro Vuaden": {"cards_per_game": 4.5, "yellows_per_game": 4.1, "reds_per_game": 0.15, "penalties_per_game": 0.32, "fouls_per_game": 25, "style": "balanced"},
    "Marcelo de Lima Henrique": {"cards_per_game": 4.4, "yellows_per_game": 4.0, "reds_per_game": 0.14, "penalties_per_game": 0.28, "fouls_per_game": 24, "style": "balanced"},
    "Paulo Roberto Alves Junior": {"cards_per_game": 4.8, "yellows_per_game": 4.4, "reds_per_game": 0.17, "penalties_per_game": 0.33, "fouls_per_game": 26, "style": "strict"},
    "Savio Pereira Sampaio": {"cards_per_game": 5.0, "yellows_per_game": 4.6, "reds_per_game": 0.18, "penalties_per_game": 0.31, "fouls_per_game": 27, "style": "strict"},
    "Jean Pierre Goncalves Lima": {"cards_per_game": 4.6, "yellows_per_game": 4.2, "reds_per_game": 0.16, "penalties_per_game": 0.34, "fouls_per_game": 25, "style": "balanced"},
    "Denis da Silva Ribeiro Serafim": {"cards_per_game": 4.9, "yellows_per_game": 4.5, "reds_per_game": 0.18, "penalties_per_game": 0.30, "fouls_per_game": 27, "style": "strict"},

    # Portuguese Liga referees (PPL)
    "Artur Soares Dias": {"cards_per_game": 4.5, "yellows_per_game": 4.1, "reds_per_game": 0.16, "penalties_per_game": 0.32, "fouls_per_game": 26, "style": "strict"},
    "Joao Pinheiro": {"cards_per_game": 4.2, "yellows_per_game": 3.8, "reds_per_game": 0.14, "penalties_per_game": 0.35, "fouls_per_game": 24, "style": "balanced"},
    "Tiago Martins": {"cards_per_game": 4.4, "yellows_per_game": 4.0, "reds_per_game": 0.15, "penalties_per_game": 0.30, "fouls_per_game": 25, "style": "balanced"},
    "Nuno Almeida": {"cards_per_game": 4.6, "yellows_per_game": 4.2, "reds_per_game": 0.17, "penalties_per_game": 0.33, "fouls_per_game": 26, "style": "strict"},
    "Luis Godinho": {"cards_per_game": 4.3, "yellows_per_game": 3.9, "reds_per_game": 0.15, "penalties_per_game": 0.31, "fouls_per_game": 25, "style": "balanced"},

    # Eredivisie referees (DED)
    "Serdar Gözübüyük": {"cards_per_game": 4.0, "yellows_per_game": 3.7, "reds_per_game": 0.12, "penalties_per_game": 0.35, "fouls_per_game": 23, "style": "balanced"},
    "Bas Nijhuis": {"cards_per_game": 3.8, "yellows_per_game": 3.5, "reds_per_game": 0.11, "penalties_per_game": 0.38, "fouls_per_game": 22, "style": "lenient"},
    "Allard Lindhout": {"cards_per_game": 4.1, "yellows_per_game": 3.8, "reds_per_game": 0.13, "penalties_per_game": 0.32, "fouls_per_game": 24, "style": "balanced"},
    "Dennis Higler": {"cards_per_game": 3.9, "yellows_per_game": 3.6, "reds_per_game": 0.12, "penalties_per_game": 0.34, "fouls_per_game": 23, "style": "balanced"},
}

# League average stats for comparison
LEAGUE_REFEREE_AVERAGES = {
    "PL": {"cards_per_game": 3.9, "penalties_per_game": 0.33},
    "PD": {"cards_per_game": 4.8, "penalties_per_game": 0.30},  # La Liga - more cards
    "SA": {"cards_per_game": 4.4, "penalties_per_game": 0.36},  # Serie A
    "BL1": {"cards_per_game": 3.7, "penalties_per_game": 0.31},  # Bundesliga - fewer cards
    "FL1": {"cards_per_game": 4.1, "penalties_per_game": 0.32},  # Ligue 1
    "CL": {"cards_per_game": 3.8, "penalties_per_game": 0.32},   # Champions League
    "EL": {"cards_per_game": 4.0, "penalties_per_game": 0.30},   # Europa League
    "BSA": {"cards_per_game": 4.9, "penalties_per_game": 0.33},  # Brazilian Serie A - very strict
    "PPL": {"cards_per_game": 4.4, "penalties_per_game": 0.32},  # Portuguese Liga
    "DED": {"cards_per_game": 3.9, "penalties_per_game": 0.35},  # Eredivisie
    "default": {"cards_per_game": 4.0, "penalties_per_game": 0.32}
}


def get_referee_stats(referee_name: str, league_code: str = None) -> Optional[dict]:
    """Get referee statistics and compare to league average"""
    if not referee_name:
        return None

    # Try exact match first
    stats = REFEREE_STATS.get(referee_name)

    # Try partial match if exact not found
    if not stats:
        referee_lower = referee_name.lower()
        for name, s in REFEREE_STATS.items():
            if name.lower() in referee_lower or referee_lower in name.lower():
                stats = s
                referee_name = name
                break

    if not stats:
        return None

    # Get league average for comparison
    league_avg = LEAGUE_REFEREE_AVERAGES.get(league_code, LEAGUE_REFEREE_AVERAGES["default"])

    # Calculate deviation from average
    cards_vs_avg = stats["cards_per_game"] - league_avg["cards_per_game"]
    penalties_vs_avg = stats["penalties_per_game"] - league_avg["penalties_per_game"]

    return {
        "name": referee_name,
        "cards_per_game": stats["cards_per_game"],
        "yellows_per_game": stats["yellows_per_game"],
        "reds_per_game": stats["reds_per_game"],
        "penalties_per_game": stats["penalties_per_game"],
        "fouls_per_game": stats["fouls_per_game"],
        "style": stats["style"],
        "cards_vs_avg": round(cards_vs_avg, 1),
        "penalties_vs_avg": round(penalties_vs_avg, 2),
        "league_avg_cards": league_avg["cards_per_game"],
        "league_avg_penalties": league_avg["penalties_per_game"],
    }


def format_referee_context(referee_stats: dict, lang: str = "ru") -> str:
    """Format referee stats for Claude's context (multilingual)"""
    if not referee_stats:
        return ""

    r = referee_stats

    # Multilingual style names
    style_map = {
        "ru": {
            "very_strict": "очень строгий 🔴",
            "strict": "строгий 🟡",
            "balanced": "сбалансированный ⚖️",
            "lenient": "мягкий 🟢"
        },
        "en": {
            "very_strict": "very strict 🔴",
            "strict": "strict 🟡",
            "balanced": "balanced ⚖️",
            "lenient": "lenient 🟢"
        },
        "es": {
            "very_strict": "muy estricto 🔴",
            "strict": "estricto 🟡",
            "balanced": "equilibrado ⚖️",
            "lenient": "permisivo 🟢"
        }
    }

    # Multilingual labels
    labels = {
        "ru": {
            "referee": "СУДЬЯ",
            "style": "Стиль",
            "cards_per_game": "Карточек за игру",
            "penalties_per_game": "Пенальти за игру",
            "vs_league_avg": "vs среднее по лиге",
            "normal": "в норме",
            "betting_impact": "Влияние на ставки",
            "over_cards": "ТБ карточек - ВЫСОКАЯ вероятность",
            "under_cards": "ТМ карточек - рассмотреть",
            "penalties_likely": "Пенальти вероятны - учитывать в тоталах",
            "red_cards_risk": "Возможны удаления - осторожно с исходами"
        },
        "en": {
            "referee": "REFEREE",
            "style": "Style",
            "cards_per_game": "Cards per game",
            "penalties_per_game": "Penalties per game",
            "vs_league_avg": "vs league avg",
            "normal": "normal",
            "betting_impact": "Betting impact",
            "over_cards": "Over cards - HIGH probability",
            "under_cards": "Under cards - consider",
            "penalties_likely": "Penalties likely - factor into totals",
            "red_cards_risk": "Red cards possible - beware of outcomes"
        },
        "pt": {
            "referee": "ÁRBITRO",
            "style": "Estilo",
            "cards_per_game": "Cartões por jogo",
            "penalties_per_game": "Pênaltis por jogo",
            "vs_league_avg": "vs média da liga",
            "normal": "normal",
            "betting_impact": "Impacto nas apostas",
            "over_cards": "Mais cartões - ALTA probabilidade",
            "under_cards": "Menos cartões - considerar",
            "penalties_likely": "Pênaltis prováveis - considerar nos totais",
            "red_cards_risk": "Expulsões possíveis - cuidado com resultados"
        },
        "es": {
            "referee": "ÁRBITRO",
            "style": "Estilo",
            "cards_per_game": "Tarjetas por partido",
            "penalties_per_game": "Penales por partido",
            "vs_league_avg": "vs promedio de liga",
            "normal": "normal",
            "betting_impact": "Impacto en apuestas",
            "over_cards": "Más tarjetas - ALTA probabilidad",
            "under_cards": "Menos tarjetas - considerar",
            "penalties_likely": "Penales probables - considerar en totales",
            "red_cards_risk": "Posibles expulsiones - cuidado con resultados"
        },
        "id": {
            "referee": "WASIT",
            "style": "Gaya",
            "cards_per_game": "Kartu per pertandingan",
            "penalties_per_game": "Penalti per pertandingan",
            "vs_league_avg": "vs rata-rata liga",
            "normal": "normal",
            "betting_impact": "Dampak taruhan",
            "over_cards": "Lebih banyak kartu - probabilitas TINGGI",
            "under_cards": "Lebih sedikit kartu - pertimbangkan",
            "penalties_likely": "Penalti mungkin - pertimbangkan di total",
            "red_cards_risk": "Kartu merah mungkin - hati-hati dengan hasil"
        }
    }

    # Use English as fallback
    styles = style_map.get(lang, style_map["en"])
    l = labels.get(lang, labels["en"])

    style_text = styles.get(r["style"], r["style"])

    context = f"\n👨‍⚖️ {l['referee']}: {r['name']}\n"
    context += f"  • {l['style']}: {style_text}\n"
    context += f"  • {l['cards_per_game']}: {r['cards_per_game']} "

    if r["cards_vs_avg"] > 0.3:
        context += f"(+{r['cards_vs_avg']} {l['vs_league_avg']} ⚠️)\n"
    elif r["cards_vs_avg"] < -0.3:
        context += f"({r['cards_vs_avg']} {l['vs_league_avg']} ✅)\n"
    else:
        context += f"({l['normal']})\n"

    context += f"  • {l['penalties_per_game']}: {r['penalties_per_game']} "
    if r["penalties_vs_avg"] > 0.05:
        context += f"(+{r['penalties_vs_avg']} {l['vs_league_avg']} ⚠️)\n"
    elif r["penalties_vs_avg"] < -0.05:
        context += f"({r['penalties_vs_avg']} {l['vs_league_avg']})\n"
    else:
        context += f"({l['normal']})\n"

    # Betting implications
    context += f"  💡 {l['betting_impact']}:\n"
    if r["cards_per_game"] >= 4.3:
        context += f"     • {l['over_cards']}\n"
    elif r["cards_per_game"] <= 3.6:
        context += f"     • {l['under_cards']}\n"

    if r["penalties_per_game"] >= 0.38:
        context += f"     • {l['penalties_likely']}\n"

    if r["style"] in ["very_strict", "strict"]:
        context += f"     • {l['red_cards_risk']}\n"

    context += "\n"
    return context


# ===== FIXTURE CONGESTION (CALENDAR LOAD) =====

def calculate_congestion_score(rest_days: int) -> int:
    """Calculate congestion score from rest days.

    Returns: 0=fresh (7+ days), 1=normal (5-6), 2=tired (3-4), 3=exhausted (0-2)
    """
    if rest_days is None:
        return 1  # Default to normal
    # Sanity check: if rest_days > 14, data is likely wrong (no team rests 2+ weeks mid-season)
    if rest_days > 14:
        return 1  # Default to normal - data anomaly
    if rest_days >= 7:
        return 0  # Fresh
    elif rest_days >= 5:
        return 1  # Normal
    elif rest_days >= 3:
        return 2  # Tired
    else:
        return 3  # Exhausted


def sanitize_rest_days(rest_days: int) -> int:
    """Sanitize rest days value - cap unrealistic values.

    If rest_days > 14, it's likely a data error (international break, new team, etc.)
    Cap at reasonable maximum of 10 days.
    """
    if rest_days is None:
        return 5  # Default
    if rest_days > 14:
        return 7  # Cap at "fresh" - likely data anomaly
    if rest_days < 0:
        return 3  # Shouldn't happen, but safe default
    return rest_days


def get_congestion_analysis(home_form: dict, away_form: dict) -> dict:
    """Analyze fixture congestion for both teams.

    Returns dict with rest days, congestion scores, and advantage.
    """
    home_rest = home_form.get("rest_days") if home_form else None
    away_rest = away_form.get("rest_days") if away_form else None

    # Sanitize unrealistic values (e.g., 48 days = data error)
    home_rest = sanitize_rest_days(home_rest)
    away_rest = sanitize_rest_days(away_rest)

    home_congestion = calculate_congestion_score(home_rest)
    away_congestion = calculate_congestion_score(away_rest)

    # Rest advantage (positive = home has more rest)
    rest_advantage = home_rest - away_rest

    return {
        "home_rest_days": home_rest,
        "away_rest_days": away_rest,
        "home_congestion": home_congestion,
        "away_congestion": away_congestion,
        "rest_advantage": rest_advantage,
        "home_tired": home_congestion >= 2,
        "away_tired": away_congestion >= 2,
    }


def format_congestion_context(congestion: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format congestion analysis for Claude (multilingual)"""

    labels = {
        "ru": {
            "title": "ЗАГРУЖЕННОСТЬ КАЛЕНДАРЯ",
            "rest_days": "дней отдыха",
            "fresh": "свежие ✅",
            "normal": "нормально",
            "tired": "устали ⚠️",
            "exhausted": "измотаны 🔴",
            "advantage": "Преимущество в отдыхе",
            "days": "дней",
            "rotation_risk": "⚠️ Риск ротации состава!",
            "fatigue_warning": "⚠️ Усталость может повлиять на результат!"
        },
        "en": {
            "title": "FIXTURE CONGESTION",
            "rest_days": "days rest",
            "fresh": "fresh ✅",
            "normal": "normal",
            "tired": "tired ⚠️",
            "exhausted": "exhausted 🔴",
            "advantage": "Rest advantage",
            "days": "days",
            "rotation_risk": "⚠️ Squad rotation risk!",
            "fatigue_warning": "⚠️ Fatigue may affect result!"
        },
        "es": {
            "title": "CONGESTIÓN DE PARTIDOS",
            "rest_days": "días de descanso",
            "fresh": "frescos ✅",
            "normal": "normal",
            "tired": "cansados ⚠️",
            "exhausted": "agotados 🔴",
            "advantage": "Ventaja de descanso",
            "days": "días",
            "rotation_risk": "⚠️ Riesgo de rotación!",
            "fatigue_warning": "⚠️ La fatiga puede afectar!"
        },
        "pt": {
            "title": "CONGESTÃO DE JOGOS",
            "rest_days": "dias de descanso",
            "fresh": "descansados ✅",
            "normal": "normal",
            "tired": "cansados ⚠️",
            "exhausted": "exaustos 🔴",
            "advantage": "Vantagem de descanso",
            "days": "dias",
            "rotation_risk": "⚠️ Risco de rotação!",
            "fatigue_warning": "⚠️ Fadiga pode afetar!"
        },
        "id": {
            "title": "KEPADATAN JADWAL",
            "rest_days": "hari istirahat",
            "fresh": "segar ✅",
            "normal": "normal",
            "tired": "lelah ⚠️",
            "exhausted": "kelelahan 🔴",
            "advantage": "Keunggulan istirahat",
            "days": "hari",
            "rotation_risk": "⚠️ Risiko rotasi pemain!",
            "fatigue_warning": "⚠️ Kelelahan bisa mempengaruhi!"
        }
    }

    l = labels.get(lang, labels["en"])

    # Status text based on congestion score
    status_map = {0: l["fresh"], 1: l["normal"], 2: l["tired"], 3: l["exhausted"]}

    context = f"\n📅 {l['title']}:\n"
    context += f"  • {home_team}: {congestion['home_rest_days']} {l['rest_days']} - {status_map[congestion['home_congestion']]}\n"
    context += f"  • {away_team}: {congestion['away_rest_days']} {l['rest_days']} - {status_map[congestion['away_congestion']]}\n"

    if abs(congestion['rest_advantage']) >= 2:
        better_team = home_team if congestion['rest_advantage'] > 0 else away_team
        context += f"  📊 {l['advantage']}: {better_team} (+{abs(congestion['rest_advantage'])} {l['days']})\n"

    if congestion['home_congestion'] >= 3 or congestion['away_congestion'] >= 3:
        context += f"  {l['rotation_risk']}\n"
    elif congestion['home_tired'] or congestion['away_tired']:
        context += f"  {l['fatigue_warning']}\n"

    context += "\n"
    return context


# ===== MOTIVATION FACTORS =====

# Known derby matches (team name patterns)
DERBY_PAIRS = [
    # England
    ("arsenal", "tottenham"),       # North London Derby
    ("arsenal", "chelsea"),         # London Derby
    ("liverpool", "everton"),       # Merseyside Derby
    ("liverpool", "manchester united"), # Classic rivalry
    ("manchester united", "manchester city"), # Manchester Derby
    ("manchester city", "liverpool"),  # Title rivals
    ("chelsea", "tottenham"),       # London Derby
    ("newcastle", "sunderland"),    # Tyne-Wear Derby
    ("west ham", "millwall"),       # East London Derby
    ("aston villa", "birmingham"),  # Second City Derby
    # Spain
    ("real madrid", "barcelona"),   # El Clásico
    ("real madrid", "atlético"),    # Madrid Derby
    ("atletico madrid", "real madrid"),
    ("barcelona", "espanyol"),      # Barcelona Derby
    ("sevilla", "real betis"),      # Seville Derby
    ("athletic", "real sociedad"),  # Basque Derby
    # Italy
    ("inter", "milan"),             # Derby della Madonnina
    ("ac milan", "inter"),
    ("juventus", "torino"),         # Turin Derby
    ("roma", "lazio"),              # Derby della Capitale
    ("napoli", "roma"),             # Derby del Sole
    # Germany
    ("dortmund", "schalke"),        # Revierderby
    ("bayern", "dortmund"),         # Der Klassiker
    ("hamburg", "werder"),          # Nordderby
    # France
    ("paris saint-germain", "marseille"), # Le Classique
    ("psg", "marseille"),
    ("lyon", "saint-étienne"),      # Derby Rhône-Alpes
    # Others
    ("benfica", "porto"),           # O Clássico
    ("ajax", "feyenoord"),          # De Klassieker
    ("galatasaray", "fenerbahçe"),  # Intercontinental Derby
]


def is_derby_match(home_team: str, away_team: str) -> bool:
    """Check if match is a derby"""
    home_lower = home_team.lower()
    away_lower = away_team.lower()

    for team1, team2 in DERBY_PAIRS:
        if (team1 in home_lower or home_lower in team1) and \
           (team2 in away_lower or away_lower in team2):
            return True
        if (team2 in home_lower or home_lower in team2) and \
           (team1 in away_lower or away_lower in team1):
            return True
    return False


# ===== COACH CHANGE TRACKING (API-BASED) =====
# Automatically detects new coaches by comparing with history in database
# No manual updates needed - data comes from Football Data API


async def get_coach_from_api(team_id: int, team_name: str) -> Optional[dict]:
    """Get coach info from Football Data API and detect if coach is new.

    Returns dict with coach info and is_new flag if new coach detected.
    """
    if not team_id or not FOOTBALL_API_KEY:
        return None

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}"
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                data = await r.json()
                coach_data = data.get("coach")

                if not coach_data:
                    return None

                coach_id = coach_data.get("id")
                coach_name = coach_data.get("name", "Unknown")
                contract = coach_data.get("contract", {})
                contract_start = contract.get("start", "")

                # Check if this is a new coach by comparing with history
                is_new_coach = False
                matches_tracked = 0

                conn = get_db_connection()
                c = conn.cursor()

                # Get previous coach record for this team
                c.execute("""SELECT coach_id, coach_name, matches_tracked
                            FROM coach_history
                            WHERE team_id = ?
                            ORDER BY first_seen DESC LIMIT 1""", (team_id,))
                prev_coach = c.fetchone()

                if prev_coach:
                    prev_coach_id, prev_coach_name, prev_matches = prev_coach

                    if prev_coach_id != coach_id:
                        # Different coach! This is a NEW coach
                        is_new_coach = True
                        matches_tracked = 0
                        logger.info(f"🆕 New coach detected: {team_name} - {coach_name} (prev: {prev_coach_name})")

                        # Insert new coach record
                        c.execute("""INSERT OR REPLACE INTO coach_history
                                    (team_id, team_name, coach_id, coach_name, contract_start, matches_tracked)
                                    VALUES (?, ?, ?, ?, ?, 0)""",
                                 (team_id, team_name, coach_id, coach_name, contract_start))
                    else:
                        # Same coach - increment matches tracked
                        matches_tracked = prev_matches + 1
                        c.execute("""UPDATE coach_history SET matches_tracked = ?
                                    WHERE team_id = ? AND coach_id = ?""",
                                 (matches_tracked, team_id, coach_id))

                        # Still "new" if < 5 matches
                        if matches_tracked < 5:
                            is_new_coach = True
                else:
                    # First time seeing this team - save coach as baseline
                    c.execute("""INSERT INTO coach_history
                                (team_id, team_name, coach_id, coach_name, contract_start, matches_tracked)
                                VALUES (?, ?, ?, ?, ?, 0)""",
                             (team_id, team_name, coach_id, coach_name, contract_start))

                    # Check if contract started recently (within 3 months)
                    if contract_start:
                        try:
                            start_date = datetime.fromisoformat(contract_start)
                            days_since = (datetime.now() - start_date).days
                            if days_since < 90:  # Less than 3 months
                                is_new_coach = True
                                matches_tracked = max(0, days_since // 7)  # Estimate ~1 match/week
                                logger.info(f"📋 First seen coach, recent contract: {team_name} - {coach_name} ({days_since} days)")
                        except:
                            pass

                conn.commit()
                conn.close()

                if is_new_coach and matches_tracked < 5:
                    # Calculate boost based on how new the coach is
                    boost = 15 if matches_tracked <= 2 else 10 if matches_tracked <= 4 else 5

                    return {
                        "coach": coach_name,
                        "coach_id": coach_id,
                        "matches_since": matches_tracked,
                        "is_new": matches_tracked <= 2,
                        "boost": boost,
                        "contract_start": contract_start
                    }

                return None

    except Exception as e:
        logger.error(f"Coach API error for {team_name}: {e}")

    return None


async def calculate_coach_factor(home_team: str, away_team: str,
                                  home_id: int = None, away_id: int = None) -> dict:
    """Calculate coach change factor for both teams using API data.

    New coach typically brings:
    - First 2 matches: +15% motivation boost (honeymoon period)
    - Matches 3-4: +10% boost
    - Match 5+: Effect fades

    Returns dict with home_new_coach, away_new_coach, and boost values.
    """
    home_coach = None
    away_coach = None

    # Fetch coach data from API if team IDs are provided
    if home_id:
        home_coach = await get_coach_from_api(home_id, home_team)
    if away_id:
        away_coach = await get_coach_from_api(away_id, away_team)

    return {
        "home_new_coach": home_coach is not None,
        "away_new_coach": away_coach is not None,
        "home_coach_info": home_coach,
        "away_coach_info": away_coach,
        "home_coach_boost": home_coach.get("boost", 0) if home_coach else 0,
        "away_coach_boost": away_coach.get("boost", 0) if away_coach else 0,
    }


def format_coach_context(coach_factor: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format coach change info for Claude analysis."""
    if not coach_factor.get("home_new_coach") and not coach_factor.get("away_new_coach"):
        return ""

    labels = {
        "ru": {
            "title": "СМЕНА ТРЕНЕРА",
            "new_coach": "Новый тренер",
            "matches": "матчей",
            "honeymoon": "🔥 Эффект нового тренера! (+{boost}% мотивация)",
            "warning": "⚠️ Новый тренер = тактическая перестройка",
        },
        "en": {
            "title": "COACH CHANGE",
            "new_coach": "New coach",
            "matches": "matches",
            "honeymoon": "🔥 New coach effect! (+{boost}% motivation)",
            "warning": "⚠️ New coach = tactical changes",
        },
        "pt": {
            "title": "MUDANÇA DE TÉCNICO",
            "new_coach": "Novo técnico",
            "matches": "jogos",
            "honeymoon": "🔥 Efeito novo técnico! (+{boost}% motivação)",
            "warning": "⚠️ Novo técnico = mudanças táticas",
        },
        "es": {
            "title": "CAMBIO DE ENTRENADOR",
            "new_coach": "Nuevo entrenador",
            "matches": "partidos",
            "honeymoon": "🔥 Efecto nuevo DT! (+{boost}% motivación)",
            "warning": "⚠️ Nuevo DT = cambios tácticos",
        },
        "id": {
            "title": "PERGANTIAN PELATIH",
            "new_coach": "Pelatih baru",
            "matches": "pertandingan",
            "honeymoon": "🔥 Efek pelatih baru! (+{boost}% motivasi)",
            "warning": "⚠️ Pelatih baru = perubahan taktik",
        },
    }

    l = labels.get(lang, labels["en"])
    context = f"\n👔 {l['title']}:\n"

    if coach_factor.get("home_new_coach"):
        info = coach_factor["home_coach_info"]
        context += f"  • {home_team}: {l['new_coach']} ({info['coach']}) - {info['matches_since']} {l['matches']}\n"
        context += f"    {l['honeymoon'].format(boost=info['boost'])}\n"

    if coach_factor.get("away_new_coach"):
        info = coach_factor["away_coach_info"]
        context += f"  • {away_team}: {l['new_coach']} ({info['coach']}) - {info['matches_since']} {l['matches']}\n"
        context += f"    {l['honeymoon'].format(boost=info['boost'])}\n"

    if coach_factor.get("home_new_coach") or coach_factor.get("away_new_coach"):
        context += f"  {l['warning']}\n"

    return context


def normalize_team_name(name: str) -> str:
    """Normalize team name for matching - remove common suffixes, lowercase."""
    if not name:
        return ""
    name = name.lower().strip()
    # Remove common suffixes
    for suffix in [" fc", " cf", " sc", " ac", " fk", " sk", " bk", " if",
                   " united", " city", " town", " county", " athletic",
                   " sporting", " real", " club", " futbol"]:
        name = name.replace(suffix, "")
    return name.strip()


def find_team_in_standings(team_name: str, standings: list) -> dict:
    """Find team in standings using fuzzy matching.

    Returns dict with position, points, won, drawn, lost, goalsFor, goalsAgainst
    or None if not found.
    """
    if not standings or not team_name:
        return None

    team_normalized = normalize_team_name(team_name)

    # Try exact match first
    for team_data in standings:
        standing_name = team_data.get("team", {}).get("name", "")
        standing_normalized = normalize_team_name(standing_name)

        if team_normalized == standing_normalized:
            return {
                "position": team_data.get("position", 10),
                "points": team_data.get("points", 0),
                "won": team_data.get("won", 0),
                "drawn": team_data.get("drawn", 0),
                "lost": team_data.get("lost", 0),
                "goals_for": team_data.get("goalsFor", 0),
                "goals_against": team_data.get("goalsAgainst", 0),
                "played": team_data.get("playedGames", 0),
            }

    # Try partial match
    for team_data in standings:
        standing_name = team_data.get("team", {}).get("name", "")
        standing_normalized = normalize_team_name(standing_name)

        # Check if one contains the other
        if team_normalized in standing_normalized or standing_normalized in team_normalized:
            return {
                "position": team_data.get("position", 10),
                "points": team_data.get("points", 0),
                "won": team_data.get("won", 0),
                "drawn": team_data.get("drawn", 0),
                "lost": team_data.get("lost", 0),
                "goals_for": team_data.get("goalsFor", 0),
                "goals_against": team_data.get("goalsAgainst", 0),
                "played": team_data.get("playedGames", 0),
            }

        # Check individual words
        team_words = set(team_normalized.split())
        standing_words = set(standing_normalized.split())
        if team_words & standing_words:  # If any word matches
            return {
                "position": team_data.get("position", 10),
                "points": team_data.get("points", 0),
                "won": team_data.get("won", 0),
                "drawn": team_data.get("drawn", 0),
                "lost": team_data.get("lost", 0),
                "goals_for": team_data.get("goalsFor", 0),
                "goals_against": team_data.get("goalsAgainst", 0),
                "played": team_data.get("playedGames", 0),
            }

    return None


def calculate_motivation(position: int, total_teams: int = 20, is_derby: bool = False,
                         is_cup: bool = False, points_from_top: int = None,
                         points_from_relegation: int = None, recent_form: str = None) -> dict:
    """Calculate motivation score based on position and context.

    Returns dict with motivation score (1-10) and factors.
    """
    motivation = 5  # Base motivation
    factors = []

    # Derby boost
    if is_derby:
        motivation += 2
        factors.append("derby")

    # Cup matches - always high motivation
    if is_cup:
        motivation += 1
        factors.append("cup")

    # Position-based motivation
    relegation_zone = max(3, int(total_teams * 0.2))  # Bottom 20%
    title_zone = max(3, int(total_teams * 0.15))      # Top 15%

    if position is not None:
        if position <= title_zone:
            motivation += 2
            factors.append("title_race")
        elif position <= title_zone + 2:
            motivation += 1
            factors.append("european_spots")
        elif position >= total_teams - relegation_zone + 1:
            motivation += 3  # Survival is strongest motivator!
            factors.append("relegation_battle")
        elif position >= total_teams - relegation_zone - 2:
            motivation += 1
            factors.append("relegation_risk")

    # Points-based motivation boost
    if points_from_top is not None and points_from_top <= 3:
        motivation += 1  # Close to leader
        if "title_race" not in factors:
            factors.append("close_to_top")

    if points_from_relegation is not None and points_from_relegation <= 3:
        motivation += 1  # Close to danger zone
        if "relegation_battle" not in factors and "relegation_risk" not in factors:
            factors.append("close_to_relegation")

    # Recent form momentum
    if recent_form:
        wins = recent_form.upper().count('W')
        losses = recent_form.upper().count('L')
        if wins >= 3:  # Hot streak
            motivation += 1
            factors.append("hot_streak")
        elif losses >= 3:  # Cold streak - desperate
            motivation += 1
            factors.append("desperate")

    # Cap at 10
    motivation = min(10, motivation)

    return {
        "score": motivation,
        "factors": factors,
        "in_title_race": "title_race" in factors,
        "in_relegation": "relegation_battle" in factors or "relegation_risk" in factors,
    }


def get_motivation_analysis(home_team: str, away_team: str,
                            home_position: int, away_position: int,
                            is_cup: bool = False, total_teams: int = 20,
                            home_standings: dict = None, away_standings: dict = None,
                            home_form: str = None, away_form: str = None) -> dict:
    """Full motivation analysis for both teams with enhanced data."""

    derby = is_derby_match(home_team, away_team)

    # Calculate points from top and relegation zone
    home_pts_from_top = None
    home_pts_from_rel = None
    away_pts_from_top = None
    away_pts_from_rel = None

    if home_standings:
        home_position = home_standings.get("position", home_position)
        # Calculate gaps if we have points data
        if "points" in home_standings:
            home_pts = home_standings["points"]
            # Assume leader has ~2.2 points per game average
            games_played = home_standings.get("played", 10)
            if games_played > 0:
                # Rough estimate - in reality we'd need leader's points
                leader_pts_estimate = int(games_played * 2.2)
                home_pts_from_top = max(0, leader_pts_estimate - home_pts)

    if away_standings:
        away_position = away_standings.get("position", away_position)
        if "points" in away_standings:
            away_pts = away_standings["points"]
            games_played = away_standings.get("played", 10)
            if games_played > 0:
                leader_pts_estimate = int(games_played * 2.2)
                away_pts_from_top = max(0, leader_pts_estimate - away_pts)

    home_motivation = calculate_motivation(
        home_position, total_teams, derby, is_cup,
        points_from_top=home_pts_from_top,
        points_from_relegation=home_pts_from_rel,
        recent_form=home_form
    )
    away_motivation = calculate_motivation(
        away_position, total_teams, derby, is_cup,
        points_from_top=away_pts_from_top,
        points_from_relegation=away_pts_from_rel,
        recent_form=away_form
    )

    return {
        "is_derby": derby,
        "home_motivation": home_motivation["score"],
        "away_motivation": away_motivation["score"],
        "home_factors": home_motivation["factors"],
        "away_factors": away_motivation["factors"],
        "home_title_race": home_motivation["in_title_race"],
        "away_title_race": away_motivation["in_title_race"],
        "home_relegation": home_motivation["in_relegation"],
        "away_relegation": away_motivation["in_relegation"],
        "motivation_diff": home_motivation["score"] - away_motivation["score"],
        "home_position": home_position,
        "away_position": away_position,
    }


def format_motivation_context(motivation: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format motivation analysis for Claude (multilingual)"""

    labels = {
        "ru": {
            "title": "МОТИВАЦИЯ",
            "derby": "🔥 ДЕРБИ!",
            "score": "Мотивация",
            "title_race": "борьба за титул 🏆",
            "european_spots": "борьба за еврокубки",
            "relegation_battle": "борьба за выживание ⚠️",
            "relegation_risk": "риск вылета",
            "cup": "кубковый матч",
            "advantage": "Преимущество в мотивации",
            "high_stakes": "💥 Матч с высокими ставками!",
        },
        "en": {
            "title": "MOTIVATION",
            "derby": "🔥 DERBY!",
            "score": "Motivation",
            "title_race": "title race 🏆",
            "european_spots": "European spots battle",
            "relegation_battle": "relegation battle ⚠️",
            "relegation_risk": "relegation risk",
            "cup": "cup match",
            "advantage": "Motivation advantage",
            "high_stakes": "💥 High stakes match!",
        },
        "es": {
            "title": "MOTIVACIÓN",
            "derby": "🔥 ¡DERBI!",
            "score": "Motivación",
            "title_race": "lucha por el título 🏆",
            "european_spots": "lucha por Europa",
            "relegation_battle": "lucha por salvación ⚠️",
            "relegation_risk": "riesgo de descenso",
            "cup": "partido de copa",
            "advantage": "Ventaja motivacional",
            "high_stakes": "💥 ¡Partido de alto riesgo!",
        },
        "pt": {
            "title": "MOTIVAÇÃO",
            "derby": "🔥 CLÁSSICO!",
            "score": "Motivação",
            "title_race": "briga pelo título 🏆",
            "european_spots": "briga por vaga europeia",
            "relegation_battle": "luta contra rebaixamento ⚠️",
            "relegation_risk": "risco de rebaixamento",
            "cup": "jogo de copa",
            "advantage": "Vantagem motivacional",
            "high_stakes": "💥 Jogo de alto risco!",
        },
        "id": {
            "title": "MOTIVASI",
            "derby": "🔥 DERBY!",
            "score": "Motivasi",
            "title_race": "perebutan gelar 🏆",
            "european_spots": "perebutan Eropa",
            "relegation_battle": "zona degradasi ⚠️",
            "relegation_risk": "risiko degradasi",
            "cup": "pertandingan piala",
            "advantage": "Keunggulan motivasi",
            "high_stakes": "💥 Pertandingan penting!",
        }
    }

    l = labels.get(lang, labels["en"])

    factor_map = {
        "derby": l["derby"],
        "title_race": l["title_race"],
        "european_spots": l["european_spots"],
        "relegation_battle": l["relegation_battle"],
        "relegation_risk": l["relegation_risk"],
        "cup": l["cup"],
    }

    context = f"\n🔥 {l['title']}:\n"

    if motivation["is_derby"]:
        context += f"  {l['derby']}\n"

    # Home team
    home_factors_text = ", ".join([factor_map.get(f, f) for f in motivation["home_factors"] if f != "derby"])
    context += f"  • {home_team}: {l['score']} {motivation['home_motivation']}/10"
    if home_factors_text:
        context += f" ({home_factors_text})"
    context += "\n"

    # Away team
    away_factors_text = ", ".join([factor_map.get(f, f) for f in motivation["away_factors"] if f != "derby"])
    context += f"  • {away_team}: {l['score']} {motivation['away_motivation']}/10"
    if away_factors_text:
        context += f" ({away_factors_text})"
    context += "\n"

    # Motivation difference
    if abs(motivation["motivation_diff"]) >= 2:
        better_team = home_team if motivation["motivation_diff"] > 0 else away_team
        context += f"  📊 {l['advantage']}: {better_team} (+{abs(motivation['motivation_diff'])})\n"

    # High stakes warning
    if motivation["is_derby"] or motivation["home_relegation"] or motivation["away_relegation"] or \
       motivation["home_title_race"] or motivation["away_title_race"]:
        context += f"  {l['high_stakes']}\n"

    context += "\n"
    return context


# ===== TEAM CLASS (ELITE FACTOR) =====

def is_elite_team(team_name: str) -> bool:
    """Check if team is in TOP_CLUBS (elite tier)"""
    if not team_name:
        return False
    team_lower = team_name.lower()
    return any(club.lower() in team_lower or team_lower in club.lower() for club in TOP_CLUBS)


def calculate_team_class(team_name: str, position: int, total_teams: int = 20) -> int:
    """Calculate team class based on elite status and position.

    Returns:
        4 = Elite (TOP_CLUBS regardless of position)
        3 = Strong (top 4 or champions league spots)
        2 = Midtable (5-13)
        1 = Weak (14-17)
        0 = Relegation zone (bottom 3)
    """
    # Elite teams always class 4 (unless in relegation - then still 3)
    if is_elite_team(team_name):
        if position and position > total_teams - 3:  # In relegation zone
            return 3  # Even elite in trouble is strong
        return 4

    # Position-based class for non-elite
    if not position or position == 0:
        return 2  # Unknown = midtable

    relegation_zone = total_teams - 3  # Bottom 3

    if position <= 4:
        return 3  # Strong (CL spots)
    elif position <= 7:
        return 3  # Europa/Conference spots = still strong
    elif position <= 13:
        return 2  # Midtable
    elif position <= relegation_zone:
        return 1  # Weak
    else:
        return 0  # Relegation zone


def get_team_class_analysis(home_team: str, away_team: str,
                            home_position: int, away_position: int,
                            total_teams: int = 20) -> dict:
    """Full team class analysis for both teams."""

    home_elite = is_elite_team(home_team)
    away_elite = is_elite_team(away_team)

    home_class = calculate_team_class(home_team, home_position, total_teams)
    away_class = calculate_team_class(away_team, away_position, total_teams)

    class_diff = home_class - away_class
    class_mismatch = abs(class_diff)

    # Elite vs underdog: elite (4) playing weak (1) or relegation (0)
    elite_vs_underdog = 0
    if home_elite and away_class <= 1:
        elite_vs_underdog = 1
    elif away_elite and home_class <= 1:
        elite_vs_underdog = 1

    return {
        "home_is_elite": home_elite,
        "away_is_elite": away_elite,
        "home_class": home_class,
        "away_class": away_class,
        "class_diff": class_diff,
        "elite_vs_underdog": elite_vs_underdog,
        "class_mismatch": class_mismatch,
    }


def format_team_class_context(class_analysis: dict, home_team: str, away_team: str, lang: str = "ru") -> str:
    """Format team class analysis for Claude (multilingual)"""

    labels = {
        "ru": {
            "title": "КЛАСС КОМАНД",
            "elite": "элита 👑",
            "strong": "сильная",
            "midtable": "середняк",
            "weak": "слабая",
            "relegation": "аутсайдер ⚠️",
            "class": "Класс",
            "advantage": "Преимущество в классе",
            "elite_warning": "👑 ЭЛИТНЫЙ КЛУБ — не недооценивай!",
            "mismatch_warning": "⚡ Большая разница в классе — фаворит может доминировать!",
        },
        "en": {
            "title": "TEAM CLASS",
            "elite": "elite 👑",
            "strong": "strong",
            "midtable": "midtable",
            "weak": "weak",
            "relegation": "relegation ⚠️",
            "class": "Class",
            "advantage": "Class advantage",
            "elite_warning": "👑 ELITE CLUB — don't underestimate!",
            "mismatch_warning": "⚡ Big class difference — favorite may dominate!",
        },
        "es": {
            "title": "CLASE DE EQUIPOS",
            "elite": "élite 👑",
            "strong": "fuerte",
            "midtable": "media tabla",
            "weak": "débil",
            "relegation": "descenso ⚠️",
            "class": "Clase",
            "advantage": "Ventaja de clase",
            "elite_warning": "👑 CLUB DE ÉLITE — ¡no subestimes!",
            "mismatch_warning": "⚡ Gran diferencia de clase — ¡el favorito puede dominar!",
        },
        "pt": {
            "title": "CLASSE DAS EQUIPES",
            "elite": "elite 👑",
            "strong": "forte",
            "midtable": "meio da tabela",
            "weak": "fraca",
            "relegation": "rebaixamento ⚠️",
            "class": "Classe",
            "advantage": "Vantagem de classe",
            "elite_warning": "👑 CLUBE DE ELITE — não subestime!",
            "mismatch_warning": "⚡ Grande diferença de classe — favorito pode dominar!",
        },
        "id": {
            "title": "KELAS TIM",
            "elite": "elit 👑",
            "strong": "kuat",
            "midtable": "papan tengah",
            "weak": "lemah",
            "relegation": "degradasi ⚠️",
            "class": "Kelas",
            "advantage": "Keunggulan kelas",
            "elite_warning": "👑 KLUB ELIT — jangan remehkan!",
            "mismatch_warning": "⚡ Perbedaan kelas besar — favorit bisa mendominasi!",
        }
    }

    l = labels.get(lang, labels["en"])

    class_names = {
        4: l["elite"],
        3: l["strong"],
        2: l["midtable"],
        1: l["weak"],
        0: l["relegation"],
    }

    home_class_name = class_names.get(class_analysis["home_class"], l["midtable"])
    away_class_name = class_names.get(class_analysis["away_class"], l["midtable"])

    # Only show context if there's something notable
    if not class_analysis["home_is_elite"] and not class_analysis["away_is_elite"] and \
       class_analysis["class_mismatch"] < 2:
        return ""  # Skip if both midtable-ish

    context = f"\n👑 {l['title']}:\n"

    # Show team classes
    context += f"  • {home_team}: {l['class']} — {home_class_name}\n"
    context += f"  • {away_team}: {l['class']} — {away_class_name}\n"

    # Elite warning
    if class_analysis["home_is_elite"] or class_analysis["away_is_elite"]:
        elite_team = home_team if class_analysis["home_is_elite"] else away_team
        context += f"  {l['elite_warning']} ({elite_team})\n"

    # Class mismatch warning (2+ levels)
    if class_analysis["class_mismatch"] >= 2:
        better_team = home_team if class_analysis["class_diff"] > 0 else away_team
        context += f"  {l['mismatch_warning']} ({better_team})\n"

    context += "\n"
    return context


async def get_top_scorers(competition: str = "PL", limit: int = 10) -> Optional[list]:
    """Get top scorers of the competition (Standard plan feature)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/competitions/{competition}/scorers"
        params = {"limit": limit}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                scorers = data.get("scorers", [])

                return [{
                    "name": s.get("player", {}).get("name", "?"),
                    "team": s.get("team", {}).get("name", "?"),
                    "team_id": s.get("team", {}).get("id"),
                    "goals": s.get("goals", 0),
                    "assists": s.get("assists", 0),
                    "played": s.get("playedMatches", 0),
                    "goals_per_match": round(s.get("goals", 0) / max(s.get("playedMatches", 1), 1), 2)
                } for s in scorers]
    except Exception as e:
        logger.error(f"Top scorers error: {e}")
    return None


def calculate_value_bet(confidence: float, odds: float) -> dict:
    """Calculate if a bet has value based on confidence and odds"""
    implied_prob = 1 / odds if odds > 0 else 0
    our_prob = confidence / 100

    value = our_prob - implied_prob
    value_percent = round(value * 100, 1)

    # Expected value calculation
    ev = (our_prob * (odds - 1)) - (1 - our_prob)
    ev_percent = round(ev * 100, 1)

    return {
        "implied_prob": round(implied_prob * 100, 1),
        "our_prob": round(our_prob * 100, 1),
        "value": value_percent,
        "ev": ev_percent,
        "is_value_bet": value > 0.05,  # 5%+ edge
        "recommendation": "✅ VALUE" if value > 0.05 else "⚠️ FAIR" if value > -0.05 else "❌ NO VALUE"
    }


def get_bot_accuracy_stats() -> dict:
    """Analyze historical predictions to find what works best"""
    conn = get_db_connection()
    c = conn.cursor()

    stats = {
        "total": 0,
        "correct": 0,
        "overall_accuracy": 0,
        "by_bet_type": {},
        "by_confidence": {},
        "by_league": {},
        "best_bet_types": [],
        "recommendations": []
    }

    try:
        # Overall accuracy
        c.execute("""
            SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions WHERE is_correct IS NOT NULL
        """)
        row = c.fetchone()
        if row and row[0] > 0:
            stats["total"] = row[0]
            stats["correct"] = row[1] or 0
            stats["overall_accuracy"] = round(stats["correct"] / stats["total"] * 100, 1)

        # Accuracy by bet category (grouped properly)
        c.execute("""
            SELECT bet_category, COUNT(*) as total,
                   SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
            FROM predictions
            WHERE is_correct IS NOT NULL AND bet_category IS NOT NULL
            GROUP BY bet_category
            HAVING total >= 3
            ORDER BY (wins * 1.0 / total) DESC
        """)
        # Human-readable category names
        category_names = {
            "totals_over": "ТБ (Тотал больше)",
            "totals_under": "ТМ (Тотал меньше)",
            "outcomes_home": "П1 (Победа хозяев)",
            "outcomes_away": "П2 (Победа гостей)",
            "outcomes_draw": "Ничья (X)",
            "btts": "ОЗ (Обе забьют)",
            "double_chance": "Двойной шанс",
            "handicap": "Фора",
            "other": "Другое"
        }

        for row in c.fetchall():
            category, total, wins = row
            accuracy = round((wins or 0) / total * 100, 1)
            display_name = category_names.get(category, category)
            stats["by_bet_type"][display_name] = {
                "total": total,
                "wins": wins or 0,
                "accuracy": accuracy
            }
            if accuracy >= 55:
                stats["best_bet_types"].append(display_name)

        # Accuracy by confidence range
        c.execute("""
            SELECT
                CASE
                    WHEN confidence >= 80 THEN '80-100%'
                    WHEN confidence >= 70 THEN '70-79%'
                    WHEN confidence >= 60 THEN '60-69%'
                    ELSE 'under 60%'
                END as conf_range,
                COUNT(*) as total,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
            FROM predictions
            WHERE is_correct IS NOT NULL AND confidence IS NOT NULL
            GROUP BY conf_range
        """)
        for row in c.fetchall():
            conf_range, total, wins = row
            stats["by_confidence"][conf_range] = {
                "total": total,
                "wins": wins or 0,
                "accuracy": round((wins or 0) / total * 100, 1) if total > 0 else 0
            }

        # Generate recommendations
        if stats["best_bet_types"]:
            stats["recommendations"].append(f"Best performing: {', '.join(stats['best_bet_types'][:3])}")

        if stats["by_confidence"].get("80-100%", {}).get("accuracy", 0) > 65:
            stats["recommendations"].append("High confidence (80%+) predictions are reliable")

        if stats["by_confidence"].get("under 60%", {}).get("accuracy", 0) < 45:
            stats["recommendations"].append("Avoid predictions under 60% confidence")

    except Exception as e:
        logger.error(f"Accuracy stats error: {e}")
    finally:
        conn.close()

    return stats


async def get_lineups(match_id: int) -> Optional[dict]:
    """Get match lineups (Standard plan feature) (ASYNC)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/matches/{match_id}"
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                data = await r.json()

                home_team = data.get("homeTeam", {}).get("name", "?")
                away_team = data.get("awayTeam", {}).get("name", "?")

                # Get lineups if available
                home_lineup = []
                away_lineup = []

                home_data = data.get("homeTeam", {})
                away_data = data.get("awayTeam", {})

                # Try to get lineup from match data
                if "lineup" in home_data:
                    home_lineup = home_data.get("lineup", [])
                if "lineup" in away_data:
                    away_lineup = away_data.get("lineup", [])

                # Get injured/suspended players
                home_injuries = []
                away_injuries = []

                # Check for injuries in team data
                if home_data.get("injuries"):
                    home_injuries = home_data.get("injuries", [])
                if away_data.get("injuries"):
                    away_injuries = away_data.get("injuries", [])

                # Get referee info - try multiple fields
                referees = data.get("referees", [])
                main_referee = None

                # Log referee data for debugging
                if referees:
                    logger.info(f"👨‍⚖️ API returned {len(referees)} referees: {referees}")
                else:
                    logger.warning(f"👨‍⚖️ No referees in API response for match {match_id}")

                # Try to find main referee by type
                for ref in referees:
                    ref_type = ref.get("type", "").upper()
                    if ref_type in ["REFEREE", "MAIN_REFEREE", "CENTRE_REFEREE"]:
                        main_referee = ref.get("name")
                        logger.info(f"👨‍⚖️ Found main referee: {main_referee}")
                        break

                # Fallback to first referee if no main found
                if not main_referee and referees:
                    main_referee = referees[0].get("name")
                    logger.info(f"👨‍⚖️ Using first referee as fallback: {main_referee}")

                # Try alternate fields in API response
                if not main_referee:
                    # Some API responses have referee directly
                    main_referee = data.get("referee")
                    if main_referee:
                        logger.info(f"👨‍⚖️ Found referee in alternate field: {main_referee}")

                return {
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_lineup": home_lineup,
                    "away_lineup": away_lineup,
                    "home_injuries": home_injuries,
                    "away_injuries": away_injuries,
                    "status": data.get("status", "SCHEDULED"),
                    "venue": data.get("venue", "Unknown"),
                    "referee": main_referee,
                }
    except Exception as e:
        logger.error(f"Lineups error: {e}")
    return None


async def get_team_squad(team_id: int) -> Optional[dict]:
    """Get team squad with player details (ASYNC)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}"
        async with session.get(url, headers=headers) as r:
            if r.status == 200:
                data = await r.json()
                squad = data.get("squad", [])

                players_by_position = {
                    "Goalkeeper": [],
                    "Defence": [],
                    "Midfield": [],
                    "Offence": []
                }

                key_players = []

                for player in squad:
                    position = player.get("position", "Unknown")
                    name = player.get("name", "?")
                    nationality = player.get("nationality", "?")

                    if position in players_by_position:
                        players_by_position[position].append({
                            "name": name,
                            "nationality": nationality,
                            "id": player.get("id")
                        })

                    # Mark experienced players as key
                    if player.get("dateOfBirth"):
                        try:
                            birth = datetime.fromisoformat(player["dateOfBirth"].replace("Z", "+00:00"))
                            age = (datetime.now(birth.tzinfo) - birth).days // 365
                            if age > 28:  # Experienced player
                                key_players.append(name)
                        except:
                            pass

                return {
                    "team_name": data.get("name", "?"),
                    "coach": data.get("coach", {}).get("name", "Unknown"),
                    "squad_size": len(squad),
                    "players_by_position": players_by_position,
                    "key_players": key_players[:5]  # Top 5 key players
                }
    except Exception as e:
        logger.error(f"Squad error: {e}")
    return None


def save_odds_history(match_key: str, bookmaker: str, odds_data: dict):
    """Save odds to history for line movement tracking"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        for market_outcome, price in odds_data.items():
            # Parse market and outcome from key like "Over_2.5" or "Home"
            c.execute("""INSERT INTO odds_history (match_key, bookmaker, market, outcome, odds)
                         VALUES (?, ?, ?, ?, ?)""",
                      (match_key, bookmaker, "general", market_outcome, price))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Save odds history error: {e}")


def get_line_movement(match_key: str, current_odds: dict) -> dict:
    """Compare current odds with historical to detect line movement.

    Returns dict with movements and metadata:
    - '_has_history': True if we have historical data for comparison
    - '_first_seen': timestamp of first recorded odds
    - '_hours_tracked': how many hours we've been tracking
    """
    movements = {"_has_history": False, "_hours_tracked": 0}
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get oldest and count of records for this match
        c.execute("""SELECT outcome, odds, recorded_at,
                     MIN(recorded_at) OVER () as first_seen
                     FROM odds_history
                     WHERE match_key = ?
                     ORDER BY recorded_at ASC""", (match_key,))
        rows = c.fetchall()
        conn.close()

        if not rows:
            return movements

        first_odds = {}
        first_seen = None

        for outcome, odds, recorded_at, first_ts in rows:
            if outcome not in first_odds:
                first_odds[outcome] = odds
            if first_seen is None and first_ts:
                first_seen = first_ts

        # Calculate how long we've been tracking
        if first_seen:
            try:
                first_dt = datetime.fromisoformat(first_seen)
                hours_tracked = (datetime.now() - first_dt).total_seconds() / 3600
                movements["_hours_tracked"] = round(hours_tracked, 1)
                movements["_first_seen"] = first_seen

                # Only show movements if we have at least 1 hour of tracking
                if hours_tracked >= 0.5:  # 30+ minutes of data
                    movements["_has_history"] = True
            except:
                pass

        # Compare with current
        for outcome, current in current_odds.items():
            if outcome in first_odds:
                first = first_odds[outcome]
                diff = current - first
                if abs(diff) >= 0.05:  # Significant movement
                    pct_change = (diff / first) * 100
                    direction = "↓" if diff < 0 else "↑"
                    movements[outcome] = {
                        "first": first,
                        "current": current,
                        "change": diff,
                        "pct": pct_change,
                        "direction": direction,
                        "sharp": diff < -0.15  # Sharp money indicator (odds dropped significantly)
                    }

    except Exception as e:
        logger.error(f"Line movement error: {e}")
    return movements


async def get_odds(home_team: str, away_team: str) -> Optional[dict]:
    """Get betting odds with 1win priority and line movement tracking (ASYNC)"""
    if not ODDS_API_KEY:
        return None

    session = await get_http_session()

    # Priority bookmakers (1win first, then others)
    PRIORITY_BOOKMAKERS = ["1win", "1xbet", "betway", "pinnacle", "bet365", "unibet", "williamhill"]

    try:
        url = f"{ODDS_API_URL}/sports/soccer/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu,uk",  # Extended regions
            "markets": "h2h,spreads,totals,btts",
            "oddsFormat": "decimal"
        }
        async with session.get(url, params=params) as r:
            if r.status == 200:
                events = await r.json()

                home_lower = (home_team or "").lower()
                away_lower = (away_team or "").lower()

                for event in events:
                    event_home = (event.get("home_team") or "").lower()
                    event_away = (event.get("away_team") or "").lower()

                    if (home_lower in event_home or away_lower in event_away) or \
                       (home_lower in event_away or away_lower in event_home):

                        match_key = f"{event.get('home_team')}_{event.get('away_team')}_{event.get('commence_time', '')[:10]}"
                        bookmakers = event.get("bookmakers", [])

                        # Sort bookmakers by priority
                        def bookmaker_priority(bm):
                            name = bm.get("key", "").lower()
                            for i, priority in enumerate(PRIORITY_BOOKMAKERS):
                                if priority in name:
                                    return i
                            return 999

                        bookmakers_sorted = sorted(bookmakers, key=bookmaker_priority)

                        odds = {}
                        all_bookmaker_odds = {}  # For comparison
                        selected_bookmaker = None

                        for bookmaker in bookmakers_sorted:
                            bm_name = bookmaker.get("key", "unknown")
                            bm_odds = {}

                            for market in bookmaker.get("markets", []):
                                if market.get("key") == "h2h":
                                    for outcome in market.get("outcomes", []):
                                        bm_odds[outcome.get("name")] = outcome.get("price")
                                elif market.get("key") == "totals":
                                    for outcome in market.get("outcomes", []):
                                        name = outcome.get("name")
                                        point = outcome.get("point", 2.5)
                                        bm_odds[f"{name}_{point}"] = outcome.get("price")
                                elif market.get("key") == "spreads":
                                    for outcome in market.get("outcomes", []):
                                        name = outcome.get("name")
                                        point = outcome.get("point", 0)
                                        sign = "+" if point > 0 else ""
                                        bm_odds[f"{name} ({sign}{point})"] = outcome.get("price")
                                elif market.get("key") == "btts":
                                    for outcome in market.get("outcomes", []):
                                        name = outcome.get("name")
                                        bm_odds[f"BTTS_{name}"] = outcome.get("price")

                            all_bookmaker_odds[bm_name] = bm_odds

                            # Use first bookmaker (highest priority) as main odds
                            if not odds and bm_odds:
                                odds = bm_odds.copy()
                                selected_bookmaker = bm_name

                        if odds:
                            # Save to history for line tracking
                            save_odds_history(match_key, selected_bookmaker, odds)

                            # Get line movement
                            movements = get_line_movement(match_key, odds)

                            # Calculate average odds across bookmakers for value detection
                            avg_odds = {}
                            for outcome in odds.keys():
                                values = [bm_odds.get(outcome) for bm_odds in all_bookmaker_odds.values() if bm_odds.get(outcome)]
                                if values:
                                    avg_odds[outcome] = sum(values) / len(values)

                            # Add metadata
                            odds["_bookmaker"] = selected_bookmaker
                            odds["_bookmakers_count"] = len(all_bookmaker_odds)
                            odds["_line_movements"] = movements
                            odds["_avg_odds"] = avg_odds

                            # Detect value (our odds vs average)
                            value_bets = {}
                            for outcome, price in odds.items():
                                if outcome.startswith("_"):
                                    continue
                                avg = avg_odds.get(outcome)
                                if avg and price > avg * 1.02:  # 2%+ above average
                                    value_bets[outcome] = {
                                        "odds": price,
                                        "avg": avg,
                                        "value_pct": ((price / avg) - 1) * 100
                                    }
                            odds["_value_bets"] = value_bets

                            logger.info(f"Odds from {selected_bookmaker}: {len(odds)-5} markets, {len(movements)} movements, {len(value_bets)} value")
                            return odds
    except Exception as e:
        logger.error(f"Odds error: {e}")
    return None


def find_match(team_names, matches):
    """Find match by team names - flexible matching"""
    if not matches or not team_names:
        return None
    
    for team in team_names:
        if not team:
            continue
            
        team_lower = team.lower().strip()
        
        if len(team_lower) < 3:
            continue
        
        for m in matches:
            home = (m.get("homeTeam", {}).get("name") or "").lower()
            away = (m.get("awayTeam", {}).get("name") or "").lower()
            home_short = (m.get("homeTeam", {}).get("shortName") or "").lower()
            away_short = (m.get("awayTeam", {}).get("shortName") or "").lower()
            home_tla = (m.get("homeTeam", {}).get("tla") or "").lower()
            away_tla = (m.get("awayTeam", {}).get("tla") or "").lower()
            
            # Skip if no team names
            if not home and not away:
                continue
            
            if (team_lower in home or team_lower in away or
                team_lower in home_short or team_lower in away_short or
                team_lower == home_tla or team_lower == away_tla or
                (home and home in team_lower) or (away and away in team_lower)):
                logger.info(f"Found match: {home} vs {away} for query '{team}'")
                return m
    
    return None


# ===== MATCH WARNINGS =====

def get_match_warnings(match, home_form, away_form, lang="ru"):
    """Get warnings for a match (cup, top club, rotation)"""
    warnings = []
    
    home_team = match.get("homeTeam", {}).get("name") or ""
    away_team = match.get("awayTeam", {}).get("name") or ""
    competition = match.get("competition", {}).get("name") or ""
    
    # Check if cup match
    is_cup = any(kw in competition for kw in CUP_KEYWORDS)
    if is_cup:
        warnings.append(get_text("cup_warning", lang))
    
    # Check if playing against top club
    home_is_top = any(club.lower() in home_team.lower() for club in TOP_CLUBS) if home_team else False
    away_is_top = any(club.lower() in away_team.lower() for club in TOP_CLUBS) if away_team else False
    
    if home_is_top or away_is_top:
        top_club = home_team if home_is_top else away_team
        warnings.append(f"{get_text('top_club_warning', lang)} ({top_club})")
    
    # Check form for rotation risk (3+ losses)
    if home_form and home_form.get("losses", 0) >= 3:
        warnings.append(f"{get_text('rotation_warning', lang)} ({home_team})")
    if away_form and away_form.get("losses", 0) >= 3:
        warnings.append(f"{get_text('rotation_warning', lang)} ({away_team})")
    
    return warnings


# ===== ENHANCED ANALYSIS v2 =====

async def analyze_match_enhanced(match: dict, user_settings: Optional[dict] = None,
                                 lang: str = "ru") -> tuple:
    """Enhanced match analysis with form, H2H, home/away stats, top scorers, and value betting (ASYNC)

    Returns:
        tuple: (analysis_text, ml_features) - analysis text and features dict for ML training
    """

    if not claude_client:
        return "AI unavailable", None

    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "?")
    comp_code = match.get("competition", {}).get("code", "PL")

    # Parse upcoming match date for accurate rest days calculation
    match_date = None
    match_date_str = match.get("utcDate", "")
    if match_date_str:
        try:
            match_date = datetime.fromisoformat(match_date_str.replace("Z", "+00:00"))
        except Exception as e:
            logger.warning(f"Could not parse match date: {e}")

    # Get all data (async) - using ENHANCED form function with match date for rest days
    home_form = await get_team_form_enhanced(home_id, upcoming_match_date=match_date) if home_id else None
    away_form = await get_team_form_enhanced(away_id, upcoming_match_date=match_date) if away_id else None
    h2h = await get_h2h(match_id) if match_id else None
    odds = await get_odds(home, away)
    standings = await get_standings(comp_code)
    lineups = await get_lineups(match_id) if match_id else None
    top_scorers = await get_top_scorers(comp_code, 15)

    # 📊 xG DATA: Real xG from Understat (top-5) OR calculated from form (all 25 leagues)
    xg_data = await get_match_xg_data(home, away, comp_code, home_form, away_form)

    # 🌐 WEB SEARCH: Get real-time news about injuries, lineups, team news
    web_news = await search_match_news(home, away, comp)
    # Get weather if we have venue
    venue = lineups.get('venue') if lineups else None
    weather = await get_weather_for_match(venue) if venue else None

    # 👨‍⚖️ REFEREE STATS: Get referee statistics for card/penalty predictions
    referee_name = lineups.get('referee') if lineups else None
    # Fallback: try to get referee from web news if API doesn't have it
    if not referee_name and web_news and web_news.get("referee"):
        referee_name = web_news.get("referee")
        logger.info(f"Using referee from web news: {referee_name}")
    referee_stats = get_referee_stats(referee_name, comp_code) if referee_name else None

    # Get bot's historical accuracy stats
    bot_stats = get_bot_accuracy_stats()

    # Get warnings (using overall form for compatibility)
    home_form_simple = {"losses": home_form["overall"]["losses"]} if home_form else None
    away_form_simple = {"losses": away_form["overall"]["losses"]} if away_form else None
    warnings = get_match_warnings(match, home_form_simple, away_form_simple, lang)

    # Build analysis context
    analysis_data = f"Match: {home} vs {away}\nCompetition: {comp}\n\n"

    # Add warnings to context
    if warnings:
        analysis_data += "⚠️ WARNINGS:\n"
        for w in warnings:
            analysis_data += f"  {w}\n"
        analysis_data += "\n"

    # ENHANCED Form analysis with HOME/AWAY split
    if home_form:
        hf = home_form
        analysis_data += f"📊 {home} ФОРМА (последние 10 матчей):\n"
        analysis_data += f"  Общая: {hf['overall']['form']} ({hf['overall']['wins']}W-{hf['overall']['draws']}D-{hf['overall']['losses']}L)\n"
        analysis_data += f"  🏠 ДОМА: {hf['home']['wins']}W-{hf['home']['draws']}D-{hf['home']['losses']}L (винрейт {hf['home']['win_rate']}%)\n"
        analysis_data += f"      Средние голы: забито {hf['home']['avg_goals_scored']}, пропущено {hf['home']['avg_goals_conceded']}\n"
        analysis_data += f"  ✈️ В гостях: {hf['away']['wins']}W-{hf['away']['draws']}D-{hf['away']['losses']}L (винрейт {hf['away']['win_rate']}%)\n"
        analysis_data += f"  📈 BTTS: {hf['btts_percent']}% | Тотал >2.5: {hf['over25_percent']}%\n"
        # Rest days
        if hf.get('rest_days') is not None:
            rest = hf['rest_days']
            if rest <= 2:
                analysis_data += f"  ⚠️ УСТАЛОСТЬ: только {rest} дней отдыха!\n"
            elif rest >= 7:
                analysis_data += f"  ✅ Свежие: {rest} дней отдыха\n"
            else:
                analysis_data += f"  ⏱️ Отдых: {rest} дней\n"
        analysis_data += "\n"

    if away_form:
        af = away_form
        analysis_data += f"📊 {away} ФОРМА (последние 10 матчей):\n"
        analysis_data += f"  Общая: {af['overall']['form']} ({af['overall']['wins']}W-{af['overall']['draws']}D-{af['overall']['losses']}L)\n"
        analysis_data += f"  🏠 Дома: {af['home']['wins']}W-{af['home']['draws']}D-{af['home']['losses']}L (винрейт {af['home']['win_rate']}%)\n"
        analysis_data += f"  ✈️ В ГОСТЯХ: {af['away']['wins']}W-{af['away']['draws']}D-{af['away']['losses']}L (винрейт {af['away']['win_rate']}%)\n"
        analysis_data += f"      Средние голы: забито {af['away']['avg_goals_scored']}, пропущено {af['away']['avg_goals_conceded']}\n"
        analysis_data += f"  📈 BTTS: {af['btts_percent']}% | Тотал >2.5: {af['over25_percent']}%\n"
        # Rest days
        if af.get('rest_days') is not None:
            rest = af['rest_days']
            if rest <= 2:
                analysis_data += f"  ⚠️ УСТАЛОСТЬ: только {rest} дней отдыха!\n"
            elif rest >= 7:
                analysis_data += f"  ✅ Свежие: {rest} дней отдыха\n"
            else:
                analysis_data += f"  ⏱️ Отдых: {rest} дней\n"
        analysis_data += "\n"

    # EXPECTED GOALS calculation (using improved home/away specific method)
    if home_form and away_form:
        exp_goals = calculate_expected_goals(home_form, away_form, comp_code)
        expected_home = exp_goals["expected_home"]
        expected_away = exp_goals["expected_away"]
        expected_total = exp_goals["expected_total"]
        method = exp_goals["method"]

        analysis_data += f"🎯 ОЖИДАЕМЫЕ ГОЛЫ (расчёт на основе формы):\n"
        analysis_data += f"  {home}: ~{expected_home:.1f} голов\n"
        analysis_data += f"  {away}: ~{expected_away:.1f} голов\n"
        analysis_data += f"  Ожидаемый тотал: ~{expected_total:.1f}\n"
        if method == "home_away_specific":
            analysis_data += f"  📊 Метод: домашняя/гостевая статистика (точный)\n\n"
        else:
            analysis_data += f"  📊 Метод: общая статистика (приблизительный)\n\n"

    # 📊 xG ANALYSIS - Real Expected Goals from statistics (CRITICAL for totals!)
    xg_context = format_xg_analysis(xg_data, home, away, lang)
    if xg_context:
        analysis_data += xg_context + "\n\n"

    # H2H analysis with reliability warning
    if h2h:
        h2h_matches_count = len(h2h.get('matches', []))
        analysis_data += f"⚔️ H2H (последние {h2h_matches_count} матчей):\n"
        analysis_data += f"  {home}: {h2h['home_wins']} побед | Ничьи: {h2h['draws']} | {away}: {h2h['away_wins']} побед\n"
        analysis_data += f"  Средние голы: {h2h['avg_goals']:.1f} за матч\n"
        analysis_data += f"  Обе забьют: {h2h['btts_percent']:.0f}%\n"
        analysis_data += f"  Тотал >2.5: {h2h['over25_percent']:.0f}%\n"
        # Warning for small sample size
        if h2h_matches_count < 5:
            analysis_data += f"  ⚠️ ВНИМАНИЕ: Малая выборка ({h2h_matches_count} матчей) - H2H ненадёжен! Приоритет → текущая форма.\n"
        analysis_data += "\n"

    # 🌐 WEB SEARCH RESULTS - Real-time news (injuries, lineups, team news)
    web_context = format_web_context_for_claude(web_news, weather, lang)
    if web_context:
        analysis_data += web_context

    # 👨‍⚖️ REFEREE STATS - for card and penalty predictions
    referee_context = format_referee_context(referee_stats, lang)
    if referee_context:
        analysis_data += referee_context
    else:
        analysis_data += "\n👨‍⚖️ СУДЬЯ: Назначение ещё не объявлено (обычно за 24-48ч до матча)\n"

    # 📅 FIXTURE CONGESTION - calendar load analysis
    congestion = get_congestion_analysis(home_form, away_form)
    congestion_context = format_congestion_context(congestion, home, away, lang)
    if congestion_context:
        analysis_data += congestion_context

    # 🔥 MOTIVATION - derby, relegation, title race analysis
    home_pos = 10
    away_pos = 10
    total_teams = 20
    home_standings_data = None
    away_standings_data = None

    if standings:
        standings_list = standings.get("standings", [])
        total_teams = len(standings_list) or 20

        # Use improved team matching
        home_standings_data = find_team_in_standings(home, standings_list)
        away_standings_data = find_team_in_standings(away, standings_list)

        if home_standings_data:
            home_pos = home_standings_data.get("position", 10)
        if away_standings_data:
            away_pos = away_standings_data.get("position", 10)

    is_cup = "cup" in comp.lower() or "copa" in comp.lower() or "coupe" in comp.lower()

    # Get recent form strings for motivation
    home_form_str = home_form.get("overall", {}).get("form", "") if home_form else ""
    away_form_str = away_form.get("overall", {}).get("form", "") if away_form else ""

    motivation = get_motivation_analysis(
        home, away, home_pos, away_pos, is_cup, total_teams,
        home_standings=home_standings_data,
        away_standings=away_standings_data,
        home_form=home_form_str,
        away_form=away_form_str
    )
    motivation_context = format_motivation_context(motivation, home, away, lang)
    if motivation_context:
        analysis_data += motivation_context

    # 👔 COACH CHANGE - new coach boost factor (now API-based!)
    coach_factor = await calculate_coach_factor(home, away, home_id, away_id)
    coach_context = format_coach_context(coach_factor, home, away, lang)
    if coach_context:
        analysis_data += coach_context

    # 👑 TEAM CLASS - elite factor analysis
    team_class = get_team_class_analysis(home, away, home_pos, away_pos, total_teams)
    team_class_context = format_team_class_context(team_class, home, away, lang)
    if team_class_context:
        analysis_data += team_class_context

    # TOP SCORERS in this match
    if top_scorers:
        home_scorers = [s for s in top_scorers if s['team'].lower() in home.lower() or home.lower() in s['team'].lower()]
        away_scorers = [s for s in top_scorers if s['team'].lower() in away.lower() or away.lower() in s['team'].lower()]

        if home_scorers or away_scorers:
            analysis_data += "⭐ ТОП-БОМБАРДИРЫ В ЭТОМ МАТЧЕ:\n"
            for s in home_scorers[:2]:
                analysis_data += f"  {home}: {s['name']} - {s['goals']} голов ({s['goals_per_match']} за матч)\n"
            for s in away_scorers[:2]:
                analysis_data += f"  {away}: {s['name']} - {s['goals']} голов ({s['goals_per_match']} за матч)\n"
            analysis_data += "\n"

    # Save overall table positions for flat track bully analysis (before reset)
    home_table_pos = home_pos  # Overall league table position
    away_table_pos = away_pos  # Overall league table position

    # Home/Away standings from league table
    if standings:
        home_pos = None
        away_pos = None

        for team in standings.get("home", []):
            if home.lower() in team.get("team", {}).get("name", "").lower():
                home_pos = team.get('position')

        for team in standings.get("away", []):
            if away.lower() in team.get("team", {}).get("name", "").lower():
                away_pos = team.get('position')

        if home_pos and away_pos:
            analysis_data += f"📋 ПОЗИЦИИ В ТАБЛИЦЕ:\n"
            analysis_data += f"  {home} (дома): {home_pos}-е место\n"
            analysis_data += f"  {away} (в гостях): {away_pos}-е место\n"
            analysis_data += f"  Разница: {abs(home_pos - away_pos)} позиций\n\n"

    if lineups and lineups.get('venue'):
        analysis_data += f"🏟️ Стадион: {lineups['venue']}\n\n"

    # 📋 OFFICIAL LINEUPS AND INJURIES FROM API (PRIMARY SOURCE!)
    lineups_context = format_lineups_from_api(lineups, home, away, lang)
    if lineups_context:
        analysis_data += lineups_context
        analysis_data += "\n"

    # 🌟 PLAYER IMPACT ANALYSIS - How missing key players affect the team
    home_injuries = lineups.get("home_injuries", []) if lineups else []
    away_injuries = lineups.get("away_injuries", []) if lineups else []

    home_player_impact = calculate_player_impact(home, home_injuries, home_id)
    away_player_impact = calculate_player_impact(away, away_injuries, away_id)

    # Combined player impact for ML
    player_impact_data = {
        "home": home_player_impact,
        "away": away_player_impact
    }

    # Format and add to analysis
    player_impact_context = format_player_impact(home_player_impact, away_player_impact, home, away, lang)
    if player_impact_context:
        analysis_data += player_impact_context
        analysis_data += "\n\n"

    # 🎯 FLAT TRACK BULLY ANALYSIS - Players who score vs weak but not strong teams
    # Get opponent positions for flat track analysis (use saved table positions)
    opponent_home_pos = away_table_pos if away_table_pos else 10  # Home team faces away team
    opponent_away_pos = home_table_pos if home_table_pos else 10  # Away team faces home team

    flat_track_context = get_flat_track_context(
        home_team=home,
        away_team=away,
        home_id=home_id,
        away_id=away_id,
        opponent_home_pos=opponent_home_pos,
        opponent_away_pos=opponent_away_pos
    )

    flat_track_output = format_flat_track_context(flat_track_context, home, away, lang)
    if flat_track_output:
        analysis_data += flat_track_output
        analysis_data += "\n\n"

    # Odds with VALUE calculation, line movements, and bookmaker info
    if odds:
        bookmaker = odds.get("_bookmaker", "unknown")
        bm_count = odds.get("_bookmakers_count", 1)
        analysis_data += f"💰 КОЭФФИЦИЕНТЫ ({bookmaker}, из {bm_count} букмекеров):\n"

        for k, v in odds.items():
            if k.startswith("_"):  # Skip metadata
                continue
            if isinstance(v, (int, float)) and v > 1:
                implied = round(1 / v * 100, 1)
                analysis_data += f"  {k}: {v} (prob: {implied}%)\n"

        # Line movements (sharp money indicator)
        movements = odds.get("_line_movements", {})
        has_history = movements.get("_has_history", False)
        hours_tracked = movements.get("_hours_tracked", 0)

        # Filter out metadata keys to get actual movements
        actual_movements = {k: v for k, v in movements.items()
                          if not k.startswith("_") and isinstance(v, dict)}

        # Also check web search for odds movement info
        web_odds_movement = web_news.get("odds_movement") if web_news else None

        if actual_movements:
            analysis_data += f"\n📉 ДВИЖЕНИЕ ЛИНИЙ (за {hours_tracked:.1f}ч):\n"
            for outcome, mv in actual_movements.items():
                sharp_icon = "🔥" if mv.get("sharp") else ""
                analysis_data += f"  {outcome}: {mv['first']} → {mv['current']} ({mv['direction']}{abs(mv['pct']):.1f}%) {sharp_icon}\n"
            sharp_moves = [m for m in actual_movements.values() if m.get("sharp")]
            if sharp_moves:
                analysis_data += "  ⚡ SHARP MONEY DETECTED - линия упала значительно!\n"
        elif web_odds_movement and web_odds_movement.get("detected"):
            # Use web search data if no DB history
            direction = "⬇️ ПАДАЮТ" if web_odds_movement["direction"] == "drop" else "⬆️ РАСТУТ"
            analysis_data += f"\n📉 ДВИЖЕНИЕ ЛИНИЙ (из новостей): Коэффициенты {direction}\n"
            if web_odds_movement.get("details"):
                analysis_data += f"  • {web_odds_movement['details']}\n"
            if web_odds_movement["direction"] == "drop":
                analysis_data += "  ⚡ SHARP MONEY - профессионалы ставят на эту сторону!\n"
        elif has_history:
            # We have history but no significant movements
            analysis_data += f"\n📉 ДВИЖЕНИЕ ЛИНИЙ (за {hours_tracked:.1f}ч): Стабильно ✓\n"
        else:
            # First time seeing this match - explain
            analysis_data += "\n📉 ДВИЖЕНИЕ ЛИНИЙ: 📊 Начато отслеживание\n"
            analysis_data += "  ℹ️ При повторном анализе покажем изменения\n"

        # Value bets (our odds vs average)
        value_bets = odds.get("_value_bets", {})
        if value_bets:
            analysis_data += "\n💎 VALUE BETS (коэфф выше среднего):\n"
            for outcome, vb in value_bets.items():
                analysis_data += f"  {outcome}: {vb['odds']} vs avg {vb['avg']:.2f} (+{vb['value_pct']:.1f}% value)\n"

        analysis_data += "\n"

    # Bot's historical performance (to inform AI)
    if bot_stats["total"] >= 10:
        analysis_data += "📈 ИСТОРИЧЕСКАЯ ТОЧНОСТЬ БОТА:\n"
        analysis_data += f"  Общая: {bot_stats['overall_accuracy']}% ({bot_stats['correct']}/{bot_stats['total']})\n"
        if bot_stats["best_bet_types"]:
            analysis_data += f"  Лучшие типы ставок: {', '.join(bot_stats['best_bet_types'][:3])}\n"
        for rec in bot_stats["recommendations"][:2]:
            analysis_data += f"  💡 {rec}\n"
        analysis_data += "\n"

    # ===== ML PREDICTIONS =====
    # Extract features for ML (referee, web news, congestion, motivation, coach, lineups, xG, flat track!)
    ml_features = extract_features(
        home_form=home_form,
        away_form=away_form,
        standings=standings,
        odds=odds,
        h2h=h2h.get("matches", []) if h2h else [],
        home_team=home,
        away_team=away,
        referee_stats=referee_stats,
        has_web_news=web_news.get("searched", False) if web_news else False,
        congestion=congestion,
        motivation=motivation,
        team_class=team_class,
        coach_factor=coach_factor,
        lineups=lineups,  # Injuries data for ML
        xg_data=xg_data,  # Real xG data for totals
        player_impact=player_impact_data,  # KEY PLAYER IMPACT!
        flat_track_context=flat_track_context  # Flat track bully analysis
    )

    # Get ML predictions if models are trained
    ml_predictions = get_all_ml_predictions(ml_features)

    if ml_predictions:
        analysis_data += "🤖 ML МОДЕЛЬ ПРЕДСКАЗЫВАЕТ:\n"
        ml_names = {
            "outcomes_home": "П1 (победа хозяев)",
            "outcomes_away": "П2 (победа гостей)",
            "outcomes_draw": "Ничья",
            "totals_over": "ТБ 2.5",
            "totals_under": "ТМ 2.5",
            "btts": "Обе забьют"
        }
        for cat, pred in ml_predictions.items():
            name = ml_names.get(cat, cat)
            conf = pred["confidence"]
            analysis_data += f"  {name}: {conf:.0f}% вероятность\n"
        analysis_data += "  ⚠️ ML модель обучена на исторических данных бота\n\n"

    # 🤖 ENSEMBLE ML - Multiple models voting
    ensemble_result = get_ensemble_prediction(ml_features, "match_result")
    ensemble_context = format_ensemble_prediction(ensemble_result, lang)
    if ensemble_context:
        analysis_data += ensemble_context
        analysis_data += "\n\n"

    # Store features for future ML training (will be linked to prediction later)
    # Features are stored in match context for saving after Claude response

    # ===== LEARNING FROM PAST ERRORS =====
    # Get lessons from past prediction errors for this league
    learning_context = get_learning_context(comp_code)
    if learning_context:
        analysis_data += f"\n{learning_context}\n\n"
        analysis_data += "⚠️ ВАЖНО: Учти эти уроки при анализе! Не повторяй прошлые ошибки.\n\n"

    # ===== SMART LEARNING - CONDITION-BASED FEEDBACK =====
    # Tell Claude about specific conditions that historically led to errors/successes
    # This influences HOW Claude analyzes, not just adjusts confidence after
    smart_learning_context = get_smart_learning_context_for_claude(ml_features, comp_code)
    if smart_learning_context:
        analysis_data += f"{smart_learning_context}\n\n"

    # ===== ROI-BASED RECOMMENDATIONS =====
    # Show which bets are actually PROFITABLE, not just winning
    roi_context = get_roi_based_recommendations(ml_features)
    if roi_context:
        analysis_data += f"{roi_context}\n\n"

    # User settings for filtering
    filter_info = ""
    if user_settings:
        filter_info = f"""
User preferences:
- Min odds: {user_settings.get('min_odds', 1.3)}
- Max odds: {user_settings.get('max_odds', 3.0)}
- Risk level: {user_settings.get('risk_level', 'medium')}
"""

    # Language instruction
    lang_map = {
        "ru": "Отвечай на русском языке.",
        "en": "Respond in English.",
        "pt": "Responda em português.",
        "es": "Responde en español.",
        "id": "Jawab dalam Bahasa Indonesia."
    }
    lang_instruction = lang_map.get(lang, lang_map["ru"])

    prompt = f"""{lang_instruction}

You are an expert betting analyst. Analyze this match using ALL provided data:

{analysis_data}

{filter_info}

CRITICAL ANALYSIS RULES:

1. HOME/AWAY FORM IS KEY:
   - If home team has 80%+ win rate at HOME → П1 confidence +15%
   - If away team has <30% win rate AWAY → П1 confidence +10%
   - Always compare HOME form vs AWAY form, not overall

2. EXPECTED GOALS FOR TOTALS (STRICT RULES!):
   - CALCULATE expected_total = (home_avg_scored + away_avg_conceded)/2 + (away_avg_scored + home_avg_conceded)/2
   - If expected_total > 2.8 → ONLY then recommend Over 2.5
   - If expected_total < 2.2 → ONLY then recommend Under 2.5
   - If expected_total is 2.2-2.8 → DO NOT recommend totals! Too risky.
   - NEVER recommend Over 2.5 if expected_total < 2.5 (this is a HARD RULE!)
   - NEVER recommend Under 2.5 if expected_total > 2.5 (this is a HARD RULE!)
   - When in doubt about totals → recommend BTTS or outcomes instead

3. H2H RELIABILITY CHECK (CRITICAL!):
   - If H2H has < 5 matches → IGNORE H2H for totals prediction!
   - Small H2H sample is UNRELIABLE - prioritize current form instead
   - Only trust H2H data when 5+ matches available
   - Current form (10 matches) > H2H (2-3 matches)

4. VALUE BETTING (MANDATORY):
   - Calculate: your_confidence - implied_probability
   - Only recommend bets with VALUE > 5%
   - Show value calculation in analysis

5. TOP SCORERS MATTER:
   - If team has top-3 league scorer → +10% goal probability
   - Factor this into BTTS and totals

6. 🌐 REAL-TIME NEWS (CRITICAL!):
   - If injury news mentions key player OUT → ADJUST confidence significantly!
   - Star striker injured → Lower totals confidence, lower team win confidence
   - Key defender out → Higher opponent goal probability
   - "Rotation" news before big game → Team may rest players, lower win confidence
   - Bad weather (rain, wind) → Lower totals expected
   - Always mention significant news in your analysis!

7. 👨‍⚖️ REFEREE IMPACT (for cards/penalties):
   - Strict referee (4.3+ cards/game) → Consider over cards bet
   - Lenient referee (3.6- cards/game) → Consider under cards bet
   - High penalty referee (0.38+ pen/game) → Factor into totals (more goals likely)
   - Very strict referee with red card history → Beware of outcomes (man down changes game)
   - Always mention referee style if data available!

8. 📅 FIXTURE CONGESTION (CALENDAR LOAD):
   - Team with 0-2 days rest = EXHAUSTED → Lower win confidence (-10-15%)
   - Team with 3-4 days rest = TIRED → Slight confidence reduction (-5%)
   - Team with 7+ days rest = FRESH → Can handle physical battles better
   - BIG rest advantage (3+ days more) → Significant edge for fresher team!
   - If both teams tired → Consider Under totals (less energy = fewer goals)
   - Congested calendar → Higher rotation risk, check lineups!
   - Always mention fatigue if one team has <3 days rest!

9. 🔥 MOTIVATION FACTOR (CRITICAL FOR ACCURACY!):
   - DERBY MATCH → Expect unpredictable result! Lower main bet confidence, consider X or BTTS
   - Relegation battle (17-20 position) → Team fights for survival, higher motivation (+10%)
   - Title race (1-3 position) → Maximum motivation, reliable performance
   - Nothing to play for (mid-table, season ending) → Lower motivation, upset risk
   - Cup match → Extra motivation, but rotation possible
   - Motivation mismatch (high vs low) → Advantage for motivated team!
   - Always factor motivation into confidence calculation!

10. 👑 TEAM CLASS (ELITE FACTOR - CRITICAL!):
   - ELITE CLUBS (Real Madrid, Barcelona, Bayern, Man City, etc.) → NEVER bet against them!
   - Elite teams often WIN despite bad recent form — individual class decides!
   - Elite vs weak team → Stats of weak team are LESS relevant, elite will dominate
   - Big class mismatch (2+ levels) → Favorite will likely dominate, consider handicaps
   - Class levels: 4=Elite, 3=Strong (CL spots), 2=Midtable, 1=Weak, 0=Relegation
   - When elite plays away at weak team → Elite still favorite despite away stats!
   - Exception: Elite in relegation zone or crisis → class drops to 3 (still strong)
   - YOUR BARÇA EXAMPLE: Elite team (class 4) beats weak team regardless of form!

11. 🎯 EDGE STACKING (KEY TO 70%+ ACCURACY!):
   - Single factor = 55% confidence MAX
   - 2 aligned factors = 65% confidence
   - 3+ aligned factors = 75%+ confidence
   - EXAMPLE: Elite team (factor 1) + home (factor 2) + opponent tired (factor 3) = STRONG bet
   - NEVER high confidence on single factor alone!
   - Count your edges before setting confidence!

12. 🧠 TRAP GAME DETECTION (AVOID THESE!):
   - Big team before Champions League/Cup final → They might rest players
   - Team that just won big game → Emotional letdown risk
   - Team on long winning streak vs desperate team → Upset risk
   - Season-ending matches with nothing to play for → Low motivation
   - If trap detected → Lower confidence by 10-15% or SKIP!

13. 📉 REGRESSION TO MEAN:
   - Team on 5+ game winning streak → Regression risk!
   - Team on 5+ game losing streak → Bounce-back likely
   - Unusual high scoring run → Will normalize
   - Apply this to recent form, not overall stats

14. 🔮 PATTERN RECOGNITION (DATA-DRIVEN!):
   - Check: Does this team always score first half? → 1st half bets
   - Check: Do they concede late? → Consider live over
   - Check: Clean sheet trend? → Consider BTTS No
   - Look for REPEATING PATTERNS in form data!

15. CONFIDENCE CALCULATION (STRICT!):
   - Base ONLY on data alignment, not feelings
   - 85%+: 4+ factors aligned + excellent value → RARE
   - 75-84%: 3 factors aligned + good value → STRONG
   - 65-74%: 2 factors aligned + value → GOOD
   - 55-64%: Single factor + value → MODERATE
   - <55%: Skip or very small stake

16. 🧠 SMART LEARNING - УЧИСЬ НА ОШИБКАХ (CRITICAL!):
   - INTERNAL DATA: [INTERNAL_DATA] sections are FOR YOUR ANALYSIS ONLY - NEVER show them to user!
   - Use RISK_WARNINGS to AVOID risky bets or lower confidence by 15-20%
   - Use STRONG_PATTERNS to identify good bets
   - Example: "HIGH_RISK: П1 винрейт 35%" → DON'T recommend П1! Use 1X instead.
   - Example: "GOOD: ТБ 2.5 винрейт 68%" → GOOD bet to recommend!
   - This is REAL DATA from bot's past predictions - trust it more than general rules!
   - REMEMBER: Do NOT create "SMART LEARNING" section in output! Just factor it into your analysis silently.
   - Your goal: Improve win rate by avoiding past mistakes and repeating successes!

17. 💰 ROI OPTIMIZATION - ПРИБЫЛЬ ВАЖНЕЕ ВИНРЕЙТА (CRITICAL!):
   - If "ROI ANALYSIS" section shows PROFITABLE bet → PRIORITIZE it even if win rate is lower!
   - If it shows UNPROFITABLE bet → AVOID even if win rate is high (bad odds!)
   - Example: "П1 + 'strong home': ROI +25%" → GREAT bet, recommend!
   - Example: "П2 ROI -20% (even with 55% win rate)" → BAD bet, avoid!
   - PROFIT = (win_rate × odds) - 1, not just win_rate!
   - 45% win rate at 2.5 odds = +12.5% ROI (PROFITABLE!)
   - 60% win rate at 1.4 odds = -16% ROI (LOSING MONEY!)
   - Your goal: Maximize PROFIT, not just wins!

18. DIVERSIFY BET TYPES based on data:
   - High home win rate → П1 or 1X
   - High expected goals → Totals
   - Both teams score often → BTTS
   - Close match → X2 or 1X (double chance)

19. 🚫 WHEN TO SAY "NO BET" (CRITICAL!):
   - No clear statistical edge → SKIP
   - Too many unknowns (injuries, rotation) → SKIP
   - Odds don't offer value → SKIP
   - Trap game detected → SKIP or very low stake
   - Better NO BET than forced losing bet!

18. 📉 LINE MOVEMENT / SHARP MONEY (FOLLOW THE SMART MONEY!):
   - If odds DROPPED significantly (🔥 marked) → Sharp bettors are on this!
   - Sharp money on Home (home odds dropped 10%+) → Consider П1, increase confidence +10%
   - Sharp money on Away (away odds dropped) → Consider П2, sharps see value
   - Sharp money on Over (over odds dropped) → Sharps expect goals, consider ТБ
   - Sharp money on Under → Sharps expect defensive match, consider ТМ
   - STEAM MOVE (multiple odds dropped fast) → STRONG signal, follow the move!
   - If YOUR analysis + Sharp money align → Extra edge! +15% confidence
   - If YOUR analysis conflicts with sharp money → Be cautious, reduce confidence
   - Sharp money is an ADDITIONAL factor in edge stacking!
   - No line movement = neutral (doesn't help or hurt)

19. 👔 COACH CHANGE FACTOR (NEW COACH BOOST!):
   - NEW coach (first 2 matches) → +15% motivation boost (honeymoon period)
   - Coach with 3-4 matches → +10% boost (still adapting)
   - After 5+ matches → Effect fades, normal analysis
   - ⚠️ IMPORTANT: Only mention coach change if "СМЕНА ТРЕНЕРА" section is in data!
   - If no coach data provided → DO NOT invent or assume new coach!

20. ⛔ DATA INTEGRITY - DO NOT INVENT DATA:
   - Only use data that is ACTUALLY provided in the analysis context
   - If referee not specified → write "Судья: Не назначен" or skip
   - If no weather data → skip weather in analysis
   - If no line movements → write "Данных нет" or "Начато отслеживание"
   - If no coach change data → DO NOT mention "new coach" or coach boost!
   - NEVER invent injuries, lineups, or statistics not in the data

⛔ DO NOT INCLUDE IN OUTPUT:
   - [INTERNAL_DATA] sections (Smart Learning, ROI analysis) - use for analysis only!
   - "SMART LEARNING ПРЕДУПРЕЖДЕНИЯ" section - NEVER create this!
   - Any raw data you received - only show conclusions
   - Technical warnings about win rates - factor into confidence silently
   - Your reasoning about internal data - just apply it

RESPONSE FORMAT:

📊 **АНАЛИЗ ДАННЫХ:**
• Форма {home} ДОМА: [конкретные цифры]
• Форма {away} В ГОСТЯХ: [конкретные цифры]
• Ожидаемые голы: [расчёт по формуле]
• H2H тренд: [если есть]
• 🌐 Новости: [травмы/составы - если есть]
• 👨‍⚖️ Судья: [стиль, влияние]
• 📅 Усталость: [дни отдыха, преимущество]
• 🔥 Мотивация: [дерби/титул/вылет]
• 👑 Класс: [elite/strong/mid/weak]
• 📉 Движение линий: [sharp money куда идёт - если есть]

🎯 **EDGE STACKING (подсчёт факторов):**
✓ Фактор 1: [описание] → в пользу [ставки]
✓ Фактор 2: [описание] → в пользу [ставки]
✓ Фактор 3: [описание] → в пользу [ставки]
✗ Против: [что может помешать]
**ИТОГО: X факторов ЗА, Y факторов ПРОТИВ**

🎯 **ОСНОВНАЯ СТАВКА** (Уверенность: X%):
[Тип ставки] @ [коэфф]
📊 Value: [твоя вероятность]% - [implied]% = [+X% VALUE]
💰 Банк: X%
📝 Почему: [основано на edge stacking выше]

📈 **АЛЬТЕРНАТИВЫ (3 шт):**
[ALT1] [Ставка] @ [коэфф] | [X]%
[ALT2] [Ставка] @ [коэфф] | [X]%
[ALT3] [Ставка] @ [коэфф] | [X]%

⚠️ **РИСКИ / TRAP GAMES:**
[Конкретные риски + есть ли признаки trap game]

✅ **ВЕРДИКТ:**
[🔥 СИЛЬНАЯ (3+ факторов) / ⚡ ХОРОШАЯ (2 фактора) / ⚠️ РИСКОВАННАЯ (1 фактор) / 🚫 ПРОПУСТИТЬ]

Bank: 85%+=5%, 75-84%=4%, 65-74%=3%, 55-64%=2%, <55%=skip"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        # Add league_code to features for learning system
        if ml_features:
            ml_features["league_code"] = comp_code
        return message.content[0].text, ml_features
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return f"Error: {e}", None


async def get_recommendations_enhanced(matches: list, user_query: str = "",
                                       user_settings: Optional[dict] = None,
                                       league_filter: Optional[str] = None,
                                       lang: str = "ru",
                                       min_confidence: int = 0,
                                       user_tz: str = "Europe/Moscow") -> Optional[str]:
    """Enhanced recommendations with user preferences (ASYNC)

    Args:
        min_confidence: Minimum confidence threshold (0 = no filter, 75 = only high confidence)
        user_tz: User's timezone for displaying match times
    """

    logger.info(f"Getting recommendations for {len(matches) if matches else 0} matches")

    if not claude_client:
        return None

    if not matches:
        return "❌ Нет доступных матчей." if lang == "ru" else "❌ No matches available."

    # Filter by league
    if league_filter:
        league_names = {
            "PL": "Premier League",
            "PD": "Primera Division",
            "BL1": "Bundesliga",
            "SA": "Serie A",
            "FL1": "Ligue 1",
            "CL": "UEFA Champions League",
            "BSA": "Brasileirão"
        }
        target_league = league_names.get(league_filter, league_filter) or ""
        matches = [m for m in matches if target_league.lower() in (m.get("competition", {}).get("name") or "").lower()]

    if not matches:
        return "❌ Нет матчей для выбранной лиги." if lang == "ru" else "❌ No matches for selected league."

    # Get form data for top matches (async)
    matches_data = []
    for m in matches[:8]:
        home = m.get("homeTeam", {}).get("name", "?")
        away = m.get("awayTeam", {}).get("name", "?")
        comp = m.get("competition", {}).get("name", "?")
        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")
        utc_date = m.get("utcDate", "")

        home_form = await get_team_form(home_id) if home_id else None
        away_form = await get_team_form(away_id) if away_id else None

        # Get warnings
        warnings = get_match_warnings(m, home_form, away_form, lang)

        # Format match time for user's timezone
        match_time = format_match_datetime(utc_date, user_tz, lang) if utc_date else ""

        match_info = f"{home} vs {away} ({comp})"
        if match_time:
            match_info += f"\n  {match_time}"
        if warnings:
            match_info += f"\n  ⚠️ " + ", ".join(warnings)
        if home_form:
            match_info += f"\n  {home} форма: {home_form['form']}"
        if away_form:
            match_info += f"\n  {away} форма: {away_form['form']}"

        matches_data.append(match_info)

    matches_text = "\n\n".join(matches_data)
    
    # User preferences
    filter_info = ""
    if user_settings:
        filter_info = f"""
FILTER BY USER PREFERENCES:
- Min odds: {user_settings.get('min_odds', 1.3)} (ignore bets with lower odds)
- Max odds: {user_settings.get('max_odds', 3.0)} (ignore bets with higher odds)
- Risk level: {user_settings.get('risk_level', 'medium')}
  * low = only 75%+ confidence, safe bets
  * medium = 65-80% confidence, balanced
  * high = can include riskier bets with good value
"""
    
    # Language instruction
    lang_map = {
        "ru": "Отвечай на русском языке.",
        "en": "Respond in English.",
        "pt": "Responda em português.",
        "es": "Responde en español.",
        "id": "Jawab dalam Bahasa Indonesia."
    }
    lang_instruction = lang_map.get(lang, lang_map["ru"])
    
    prompt = f"""{lang_instruction}

User asked: "{user_query}"

Analyze these matches with form data and give TOP 3-4 picks:

{matches_text}

{filter_info}

RULES:
1. VALUE BETTING FOR ROI - find bets where confidence × odds > 1.2 (20% edge)
   Example: 65% confidence × 2.0 odds = 1.30 VALUE ✓ (good bet)
   Example: 80% confidence × 1.3 odds = 1.04 VALUE ✗ (waste of confidence)

2. PREFER HIGHER ODDS with solid confidence over "safe" low odds bets
   - A 60% bet @ 2.2 odds (VALUE=1.32) beats 80% bet @ 1.3 odds (VALUE=1.04)
   - Target odds range: 1.7 - 3.0 for best ROI potential
   - Low odds (<1.5) only if confidence is 85%+

3. ANALYZE ALL BET TYPES fairly - each has its place:
   - П1/П2: good when there's clear class difference + form advantage
   - Double Chance: safer but need decent odds (1.4+)
   - Over/Under: check actual goals averages and H2H totals
   - BTTS: check if both teams score regularly (>60% matches)
   - Draw: only when teams are truly equal AND low-scoring history

4. For TOP CLUBS - they rarely lose but draws happen, consider 1X or X2
5. Cup matches = more upsets, adjust confidence down 10%
6. If warnings present - lower confidence by 10-15%
7. CRITICAL: Include 📅 date/time for EVERY match!
{f'8. ONLY recommend bets with {min_confidence}%+ confidence!' if min_confidence > 0 else ''}

FORMAT (STRICTLY follow this format, including the 📅 line with date/time):
🔥 **ТОП СТАВКИ:**

1️⃣ **[Home] vs [Away]** ([Competition])
   📅 [REQUIRED: Copy the exact date/time from match data, e.g. "📅 Сегодня 21:00"]
   ⚡ [Bet type] @ ~X.XX
   📊 Уверенность: X%
   📝 [1-2 sentences why]

2️⃣ ...

💡 **Общий совет:** [1 sentence]"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Recommendations error: {e}")
        return None


# ===== TELEGRAM HANDLERS =====

async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's Telegram ID - useful for admin setup"""
    user_id = update.effective_user.id
    username = update.effective_user.username or "N/A"

    text = f"""🆔 **Твой Telegram ID:**
`{user_id}`

👤 Username: @{username}

📋 Скопируй ID и добавь в ADMIN_IDS переменную окружения,
или в config.py для админских команд."""

    await update.message.reply_text(text, parse_mode="Markdown")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - first launch with language selection or regular menu"""
    user = update.effective_user
    existing_user = get_user(user.id)

    # Check for referral link (t.me/bot?start=ref_12345) or UTM source (t.me/bot?start=push_ai)
    referrer_id = None
    utm_source = "organic"
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
                # Don't allow self-referral
                if referrer_id == user.id:
                    referrer_id = None
                # Store in context for later use
                context.user_data["referrer_id"] = referrer_id
                logger.info(f"Referral detected: {referrer_id} -> {user.id}")
            except ValueError:
                pass
        else:
            # Non-referral start parameter is treated as UTM source
            utm_source = arg[:50]  # Limit length for safety
            logger.info(f"UTM source detected: {utm_source} for user {user.id}")

    # Store UTM source for later use when creating user
    context.user_data["utm_source"] = utm_source

    # IMPORTANT: Also save to database in case bot restarts before user creation
    if not existing_user and (utm_source != "organic" or referrer_id):
        save_pending_utm(user.id, utm_source, referrer_id)

    if not existing_user:
        # NEW USER - show language selection first
        detected_lang = detect_language(user)

        text = """🌍 **Welcome / Добро пожаловать!**

Please select your language:
Пожалуйста, выберите язык:
Por favor, selecione seu idioma:
Por favor, selecciona tu idioma:
Silakan pilih bahasa Anda:"""

        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data=f"set_initial_lang_ru"),
             InlineKeyboardButton("🇬🇧 English", callback_data=f"set_initial_lang_en")],
            [InlineKeyboardButton("🇧🇷 Português", callback_data=f"set_initial_lang_pt"),
             InlineKeyboardButton("🇪🇸 Español", callback_data=f"set_initial_lang_es")],
            [InlineKeyboardButton("🇮🇩 Indonesia", callback_data=f"set_initial_lang_id")]
        ]

        # Pre-select detected language hint
        hint = f"\n\n💡 _Detected / Определён: {LANGUAGE_NAMES.get(detected_lang, detected_lang)}_"

        await update.message.reply_text(
            text + hint,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        # Existing user - show main menu
        lang = existing_user.get("language", "ru")
        await show_main_menu(update, context, lang)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu (can be called anytime)"""
    user_data = get_user(update.effective_user.id)
    if not user_data:
        lang = detect_language(update.effective_user)
        is_new = create_user(update.effective_user.id, update.effective_user.username, lang)
        if is_new:
            await notify_admins_new_user(
                context.bot,
                update.effective_user.id,
                update.effective_user.username,
                lang,
                "organic"
            )
    else:
        lang = user_data.get("language", "ru")

    await show_main_menu(update, context, lang)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str):
    """Show the main inline menu"""
    keyboard = [
        [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend"),
         InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")],
        [InlineKeyboardButton(get_text("tomorrow", lang), callback_data="cmd_tomorrow"),
         InlineKeyboardButton(get_text("leagues", lang), callback_data="cmd_leagues")],
        [InlineKeyboardButton(get_text("live_alerts", lang), callback_data="cmd_live"),
         InlineKeyboardButton(get_text("settings", lang), callback_data="cmd_settings")],
        [InlineKeyboardButton(get_text("favorites", lang), callback_data="cmd_favorites"),
         InlineKeyboardButton(get_text("stats", lang), callback_data="cmd_stats")],
        [InlineKeyboardButton(get_text("premium_btn", lang), callback_data="cmd_premium"),
         InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")],
        [InlineKeyboardButton(get_text("help", lang), callback_data="cmd_help")]
    ]

    text = f"""⚽ **AI Betting Bot v14**

{get_text('welcome', lang)}

{get_text('free_predictions', lang).format(limit=FREE_DAILY_LIMIT)}
{get_text('unlimited_deposit', lang)}"""

    await update.message.reply_text(
        text,
        reply_markup=get_main_keyboard(lang),
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        get_text("choose_action", lang),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's matches"""
    user = get_user(update.effective_user.id)
    lang = user.get("language", "ru") if user else "ru"
    user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
    exclude_cups = user.get("exclude_cups", 0) if user else 0

    status = await update.message.reply_text(get_text("analyzing", lang))

    matches = await get_matches(date_filter="today")
    matches = filter_cup_matches(matches, exclude=bool(exclude_cups))

    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    by_comp = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(m)
    
    tz_info = get_tz_offset_str(user_tz)
    text = f"{get_text('matches_today', lang)} ({tz_info}):\n\n"

    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
            text += f"  ⏰ {time_str} | {home} vs {away}\n"
        text += "\n"

    keyboard = [
        [InlineKeyboardButton(get_text("recs_today", lang), callback_data="rec_today")],
        [InlineKeyboardButton(get_text("tomorrow", lang), callback_data="cmd_tomorrow")]
    ]
    
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tomorrow's matches"""
    user = get_user(update.effective_user.id)
    lang = user.get("language", "ru") if user else "ru"
    user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
    
    status = await update.message.reply_text(get_text("analyzing", lang))
    
    matches = await get_matches(date_filter="tomorrow")
    
    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    by_comp = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(m)
    
    tz_info = get_tz_offset_str(user_tz)
    text = f"{get_text('matches_tomorrow', lang)} ({tz_info}):\n\n"

    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
            text += f"  ⏰ {time_str} | {home} vs {away}\n"
        text += "\n"

    keyboard = [
        [InlineKeyboardButton(get_text("recs_tomorrow", lang), callback_data="rec_tomorrow")],
        [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")]
    ]
    
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu"""
    user_id = update.effective_user.id
    user = get_user(user_id)

    if not user:
        lang = detect_language(update.effective_user)
        is_new = create_user(user_id, update.effective_user.username, lang)
        if is_new:
            await notify_admins_new_user(
                context.bot,
                user_id,
                update.effective_user.username,
                lang,
                "organic"
            )
        user = get_user(user_id)
    
    lang = user.get("language", "ru")
    user_tz = user.get("timezone", "Europe/Moscow")
    tz_display = get_tz_offset_str(user_tz)
    
    # Localized settings labels
    settings_labels = {
        "ru": {"title": "⚙️ **НАСТРОЙКИ**", "min": "Мин. коэфф", "max": "Макс. коэфф", "risk": "Риск", "tz": "Часовой пояс", "premium": "Премиум", "yes": "Да", "no": "Нет", "tap_to_change": "Нажми на параметр чтобы изменить:", "exclude_cups": "Исключить кубки"},
        "en": {"title": "⚙️ **SETTINGS**", "min": "Min odds", "max": "Max odds", "risk": "Risk", "tz": "Timezone", "premium": "Premium", "yes": "Yes", "no": "No", "tap_to_change": "Tap to change:", "exclude_cups": "Exclude cups"},
        "pt": {"title": "⚙️ **CONFIGURAÇÕES**", "min": "Odds mín", "max": "Odds máx", "risk": "Risco", "tz": "Fuso horário", "premium": "Premium", "yes": "Sim", "no": "Não", "tap_to_change": "Toque para alterar:", "exclude_cups": "Excluir copas"},
        "es": {"title": "⚙️ **AJUSTES**", "min": "Cuota mín", "max": "Cuota máx", "risk": "Riesgo", "tz": "Zona horaria", "premium": "Premium", "yes": "Sí", "no": "No", "tap_to_change": "Toca para cambiar:", "exclude_cups": "Excluir copas"},
        "id": {"title": "⚙️ **PENGATURAN**", "min": "Odds min", "max": "Odds maks", "risk": "Risiko", "tz": "Zona waktu", "premium": "Premium", "yes": "Ya", "no": "Tidak", "tap_to_change": "Ketuk untuk mengubah:", "exclude_cups": "Kecualikan piala"},
    }
    sl = settings_labels.get(lang, settings_labels["ru"])

    # Exclude cups toggle
    exclude_cups = user.get('exclude_cups', 0)
    cups_status = f"✅ {sl['yes']}" if exclude_cups else f"❌ {sl['no']}"

    keyboard = [
        [InlineKeyboardButton(f"📉 {sl['min']}: {user['min_odds']}", callback_data="set_min_odds")],
        [InlineKeyboardButton(f"📈 {sl['max']}: {user['max_odds']}", callback_data="set_max_odds")],
        [InlineKeyboardButton(f"⚠️ {sl['risk']}: {user['risk_level']}", callback_data="set_risk")],
        [InlineKeyboardButton(f"🏆 {sl['exclude_cups']}: {cups_status}", callback_data="toggle_exclude_cups")],
        [InlineKeyboardButton("🌍 Language", callback_data="set_language")],
        [InlineKeyboardButton(f"🕐 {sl['tz']}: {tz_display}", callback_data="set_timezone")],
        [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
    ]

    premium_status = f"✅ {sl['yes']}" if user.get('is_premium') else f"❌ {sl['no']}"
    text = f"""{sl['title']}

📉 **{sl['min']}:** {user['min_odds']}
📈 **{sl['max']}:** {user['max_odds']}
⚠️ **{sl['risk']}:** {user['risk_level']}
🏆 **{sl['exclude_cups']}:** {cups_status}
🌍 **Language:** {lang.upper()}
🕐 **{sl['tz']}:** {tz_display}
💎 **{sl['premium']}:** {premium_status}

{sl['tap_to_change']}"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show favorites menu"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    
    teams = get_favorite_teams(user_id)
    leagues = get_favorite_leagues(user_id)
    
    text = "⭐ **ИЗБРАННОЕ**\n\n" if lang == "ru" else "⭐ **FAVORITES**\n\n"
    
    if teams:
        text += "**Команды:**\n" if lang == "ru" else "**Teams:**\n"
        for t in teams:
            text += f"  • {t}\n"
    else:
        text += "_Нет избранных команд_\n" if lang == "ru" else "_No favorite teams_\n"
    
    text += "\n"
    
    if leagues:
        text += "**Лиги:**\n" if lang == "ru" else "**Leagues:**\n"
        for l in leagues:
            text += f"  • {COMPETITIONS.get(l, l)}\n"
    else:
        text += "_Нет избранных лиг_\n" if lang == "ru" else "_No favorite leagues_\n"
    
    text += "\n💡 Напиши название команды и нажми ⭐" if lang == "ru" else "\n💡 Type team name and tap ⭐"
    
    add_league_label = {"ru": "➕ Добавить лигу", "en": "➕ Add league", "pt": "➕ Adicionar liga", "es": "➕ Añadir liga", "id": "➕ Tambah liga"}
    keyboard = [
        [InlineKeyboardButton(add_league_label.get(lang, add_league_label["en"]), callback_data="add_fav_league")],
        [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Show user statistics with categories and pagination"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    stats = get_user_stats(user_id, page=page)

    if stats["total"] == 0:
        text = "📈 **СТАТИСТИКА**\n\nПока нет данных. Напиши название команды!" if lang == "ru" else "📈 **STATS**\n\nNo data yet. Type a team name!"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
        return

    win_emoji = "🔥" if stats["win_rate"] >= 70 else "✅" if stats["win_rate"] >= 50 else "📉"

    # Get ROI and streak info
    roi = get_roi_stats(user_id)
    streak = get_streak_info(user_id)

    # Format streak
    streak_text = ""
    if streak["current_streak"] > 0:
        if streak["streak_type"] == "win":
            streak_text = f"🔥 Серия: {streak['current_streak']} побед!"
        else:
            streak_text = f"❄️ Серия: {streak['current_streak']} поражений"

    # Format ROI
    roi_emoji = "💰" if roi["roi"] > 0 else "📉" if roi["roi"] < 0 else "➖"
    roi_text = f"{roi_emoji} ROI: {roi['roi']:+.1f}% (профит: {roi['profit']:+.1f} ед.)"

    # Build stats string with push
    decided = stats['correct'] + stats.get('incorrect', 0)
    push_str = f"\n🔄 Возвраты: {stats['push']}" if stats.get('push', 0) > 0 else ""

    # Main vs Alt stats display
    main_s = stats.get("main_stats", {})
    alt_s = stats.get("alt_stats", {})

    main_display = ""
    alt_display = ""

    # Show main stats if there are any results
    if main_s.get("decided", 0) > 0:
        main_emoji = "🎯" if main_s["rate"] >= 50 else "📊"
        main_display = f"{main_emoji} Основные: {main_s['correct']}/{main_s['decided']} ({main_s['rate']:.1f}%)"

    # Show alternatives: either with results or pending count
    if alt_s.get("decided", 0) > 0:
        alt_emoji = "📈" if alt_s["rate"] >= 50 else "📉"
        alt_display = f"{alt_emoji} Альтернативы: {alt_s['correct']}/{alt_s['decided']} ({alt_s['rate']:.1f}%)"
    elif alt_s.get("total", 0) > 0:
        # Show pending alternatives count if no results yet
        pending_alts = alt_s["total"] - alt_s.get("decided", 0)
        alt_display = f"📈 Альтернативы: ⏳ {pending_alts} ожидают"

    stats_by_rank = ""
    if main_display or alt_display:
        stats_by_rank = f"\n{main_display}\n{alt_display}" if alt_display else f"\n{main_display}"

    text = f"""📈 СТАТИСТИКА

{win_emoji} Точность: {stats['correct']}/{decided} ({stats['win_rate']:.1f}%)
{roi_text}
{streak_text}
{stats_by_rank}

📊 Всего прогнозов: {stats['total']}
✅ Верных: {stats['correct']}
❌ Неверных: {stats.get('incorrect', 0)}{push_str}
⏳ Ожидают: {stats['pending']}

🏆 Рекорды: лучшая серия {streak['best_win_streak']}W | худшая {streak['worst_lose_streak']}L

"""

    # Stats by category
    if stats["categories"]:
        cat_names = {
            "totals_over": "ТБ 2.5",
            "totals_under": "ТМ 2.5",
            "outcomes_home": "П1",
            "outcomes_away": "П2",
            "outcomes_draw": "Ничья",
            "btts": "Обе забьют",
            "double_chance": "Двойной шанс",
            "handicap": "Форы",
            "other": "Другое"
        }

        text += "📋 По типам ставок:\n"
        for cat, data in stats["categories"].items():
            cat_name = cat_names.get(cat, cat)
            push_info = f" (+{data['push']}🔄)" if data.get('push', 0) > 0 else ""
            text += f"  • {cat_name}: {data['correct']}/{data['total'] - data.get('push', 0)} ({data['rate']}%){push_info}\n"
        text += "\n"

    # Recent predictions with pagination info
    current_page = stats.get("page", 0)
    total_pages = stats.get("total_pages", 1)
    page_info = f" (стр. {current_page + 1}/{total_pages})" if total_pages > 1 else ""

    text += f"{'─'*25}\n📝 Последние прогнозы{page_info}:\n"
    for p in stats.get("predictions", []):
        if p["is_correct"] is None:
            emoji = "⏳"
            result_text = "ожидаем"
        elif p["is_correct"] == 1:
            emoji = "✅"
            result_text = p["result"] or "выиграл"
        elif p["is_correct"] == 2:
            emoji = "🔄"
            result_text = f"{p['result']} (возврат)"
        else:
            emoji = "❌"
            result_text = p["result"] or "проиграл"

        home_short = p["home"][:10] + ".." if len(p["home"]) > 12 else p["home"]
        away_short = p["away"][:10] + ".." if len(p["away"]) > 12 else p["away"]

        text += f"{emoji} {home_short} - {away_short}\n"
        text += f"    📊 {p['bet_type']} ({p['confidence']}%) → {result_text}\n"

    # Build keyboard with pagination
    refresh_label = {"ru": "🔄 Обновить", "en": "🔄 Refresh", "pt": "🔄 Atualizar", "es": "🔄 Actualizar", "id": "🔄 Perbarui"}

    # Pagination buttons
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"stats_page_{current_page - 1}"))
    if current_page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"stats_page_{current_page + 1}"))

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([InlineKeyboardButton(refresh_label.get(lang, refresh_label["en"]), callback_data="cmd_stats")])
    keyboard.append([InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug command to check user status and limits (ADMIN ONLY)"""
    user_id = update.effective_user.id

    # Check admin permission
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Эта команда доступна только администраторам.")
        return

    user = get_user(user_id)
    
    if not user:
        await update.message.reply_text(f"User {user_id} not found in DB")
        return
    
    can_use, remaining, use_bonus = check_daily_limit(user_id)
    bonus_predictions = user.get('bonus_predictions', 0)

    text = f"""🔧 DEBUG INFO

👤 User ID: {user_id}
📛 Username: {user.get('username', 'N/A')}

📊 Limits:
- Daily requests: {user.get('daily_requests', 0)}/{FREE_DAILY_LIMIT}
- Last request date: {user.get('last_request_date', 'Never')}
- Can use: {'Yes' if can_use else 'No'}
- Remaining: {remaining}
- Bonus predictions: {bonus_predictions}
- Using bonus: {'Yes' if use_bonus else 'No'}

💎 Premium: {'Yes' if user.get('is_premium') else 'No'}

⚙️ Settings:
- Min odds: {user.get('min_odds', 1.3)}
- Max odds: {user.get('max_odds', 3.0)}
- Risk: {user.get('risk_level', 'medium')}
- Language: {user.get('language', 'ru')}
- Timezone: {user.get('timezone', 'Europe/Moscow')}

🏆 Leagues: {len(COMPETITIONS)} configured
"""
    
    keyboard = [
        [InlineKeyboardButton("🔄 Reset Limit", callback_data="debug_reset_limit")],
        [InlineKeyboardButton("❌ Remove Premium", callback_data="debug_remove_premium")],
        [InlineKeyboardButton("🔙 Back", callback_data="cmd_start")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get recommendations with user preferences"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    exclude_cups = user.get("exclude_cups", 0) if user else 0

    # Check daily limit
    can_use, remaining, use_bonus = check_daily_limit(user_id)
    if not can_use:
        # Check if user can claim referral bonus
        ref_bonus = check_referral_bonus_eligible(user_id)
        if ref_bonus["eligible"]:
            text = get_limit_text(lang)
            text += f"\n\n🎁 {get_text('referral_bonus_title', lang)}\n{get_text('referral_bonus_progress', lang).format(current=ref_bonus['progress'])}"
            keyboard = [[InlineKeyboardButton("🎁 Получить бонус", callback_data="claim_ref_bonus")]]
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
        else:
            text = get_limit_text(lang)
            keyboard = []
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    status = await update.message.reply_text(get_text("analyzing", lang))

    matches = await get_matches(days=7)
    matches = filter_cup_matches(matches, exclude=bool(exclude_cups))

    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    user_query = update.message.text or ""
    user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
    recs = await get_recommendations_enhanced(matches, user_query, user, lang=lang, user_tz=user_tz)
    
    if recs:
        # Add social proof header
        social_stats = get_social_stats()
        streak_info = get_user_streak(user_id)

        social_header = ""
        if social_stats["wins_today"] > 0:
            social_header = f"🏆 {get_text('social_wins_today', lang).format(count=social_stats['wins_today'])}\n"
        if streak_info["streak"] > 1:
            social_header += f"{get_text('streak_title', lang).format(days=streak_info['streak'])}\n"
        if social_header:
            social_header += "\n"

        # Add affiliate button with referral (if monetization enabled)
        keyboard = []
        bet_btn = get_bet_button(user_id, lang)
        if bet_btn:
            keyboard.append(bet_btn)
        keyboard.append([InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today"),
             InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")])
        increment_daily_usage(user_id)
        try:
            await status.edit_text(social_header + recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Markdown error in recommend: {e}")
            # Fallback to plain text
            await status.edit_text(social_header + recs, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await status.edit_text(get_text("analysis_error", lang))


async def sure_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get only HIGH CONFIDENCE (75%+) recommendations"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    exclude_cups = user.get("exclude_cups", 0) if user else 0

    # Check daily limit
    can_use, remaining, use_bonus = check_daily_limit(user_id)
    if not can_use:
        text = get_limit_text(lang)
        keyboard = []
        premium_btns = get_premium_buttons(user_id, lang)
        if premium_btns:
            keyboard.append(premium_btns)
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        return

    status = await update.message.reply_text(get_text("sure_searching", lang))

    matches = await get_matches(days=7)
    matches = filter_cup_matches(matches, exclude=bool(exclude_cups))

    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return

    user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
    recs = await get_recommendations_enhanced(matches, "", user, lang=lang, min_confidence=75, user_tz=user_tz)

    if recs:
        # Add social proof
        social_stats = get_social_stats()
        accuracy_text = ""
        if social_stats["accuracy"] > 0:
            accuracy_text = f"\n{get_text('social_accuracy', lang).format(accuracy=social_stats['accuracy'])}\n"

        header = f"🎯 **УВЕРЕННЫЕ СТАВКИ (75%+)**{accuracy_text}\n"
        keyboard = []
        bet_btn = get_bet_button(user_id, lang)
        if bet_btn:
            keyboard.append(bet_btn)
        keyboard.append([InlineKeyboardButton("📊 Все ставки", callback_data="cmd_recommend"),
             InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")])
        increment_daily_usage(user_id)
        await status.edit_text(header + recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status.edit_text(get_text("no_sure_bets", lang))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    user = get_user(update.effective_user.id)
    lang = user.get("language", "ru") if user else "ru"

    text = f"""❓ **ПОМОЩЬ**

**Основные команды:**
• /start - Главное меню
• /recommend - Лучшие ставки
• /sure - 🎯 Только 75%+ уверенность
• /today - Матчи сегодня
• /tomorrow - Матчи завтра
• /live - 🔔 Включить алерты
• /premium - 💎 Получить премиум
• /ref - 👥 Пригласи друзей (+3 дня бесплатно!)
• /settings - Настройки
• /stats - Статистика

**Как пользоваться:**
1. Напиши название команды (напр. "Ливерпуль")
2. Получи анализ с формой, H2H и рекомендациями
3. Настрой фильтры под свой стиль

**Лимиты:**
• Бесплатно: {FREE_DAILY_LIMIT} прогноза/день
• Премиум: безлимит (/premium)

**Live-алерты:**
Каждые 10 минут бот проверяет матчи.
Если найдёт ставку 70%+ — пришлёт алерт!

**Типы ставок:**
• П1/Х/П2 - Исход
• ТБ/ТМ 2.5 - Тоталы
• BTTS - Обе забьют
• 1X/X2 - Двойной шанс"""

    keyboard = [[InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


def get_geo_prices_text(geo: str) -> str:
    """Generate price list text based on user's geo."""
    display = GEO_PRICE_DISPLAY.get(geo, GEO_PRICE_DISPLAY["DEFAULT"])
    prices = display["prices"]

    lines = []
    for usd, local, reward in prices:
        lines.append(f"• {usd} ({local}) → {reward}")

    return "\n".join(lines)


async def premium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show premium options - 1win deposit or crypto payment"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    # Check if monetization is disabled - show "coming soon"
    if not MONETIZATION_ENABLED:
        coming_soon_text = get_text("premium_coming_soon", lang)
        keyboard = [[InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]]
        if update.callback_query:
            await update.callback_query.edit_message_text(
                coming_soon_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                coming_soon_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        return

    # Get user's geo for personalized prices
    user_geo = get_user_geo(user_id)

    # Check if already premium
    is_prem = user.get("is_premium", 0) if user else 0
    expires = user.get("premium_expires") if user else None

    if is_prem and expires:
        status_text = get_text("premium_status", lang).format(date=expires[:10]) + "\n\n"
    else:
        status_text = ""

    # Check if CryptoBot is configured
    crypto_enabled = bool(CRYPTOBOT_TOKEN)

    # Get referral stats
    ref_stats = get_referral_stats(user_id)

    # Build option 2 text
    if crypto_enabled:
        option2_text = get_text("premium_option2_crypto", lang)
    else:
        option2_text = get_text("premium_option2_manual", lang).format(support=SUPPORT_USERNAME)

    # Build earned/click text
    if ref_stats['earned_days'] > 0:
        earned_text = get_text("premium_earned", lang).format(days=ref_stats['earned_days'])
    else:
        earned_text = get_text("premium_click_below", lang)

    # Get geo-personalized prices
    geo_prices = get_geo_prices_text(user_geo)

    text = f"""{get_text("premium_title", lang)}

{status_text}{get_text("premium_unlimited", lang)}

━━━━━━━━━━━━━━━━━━━━

{get_text("premium_option1_title", lang)}
{get_text("premium_option1_desc", lang)}

{geo_prices}

━━━━━━━━━━━━━━━━━━━━

{get_text("premium_option2_title", lang)}
{option2_text}

• $15 → 7 days
• $40 → 30 days
• $100 → 1 year

━━━━━━━━━━━━━━━━━━━━

{get_text("premium_free_title", lang)}
{get_text("premium_free_desc", lang)}
{earned_text}"""

    if crypto_enabled:
        keyboard = [
            [InlineKeyboardButton(get_text("premium_deposit_btn", lang), url=get_affiliate_link(user_id))],
            [InlineKeyboardButton("💳 $15 / 7 days", callback_data="pay_crypto_7"),
             InlineKeyboardButton("💳 $40 / 30 days", callback_data="pay_crypto_30")],
            [InlineKeyboardButton("💳 $100 / 1 year", callback_data="pay_crypto_365")],
            [InlineKeyboardButton(get_text("premium_friends_btn", lang), callback_data="cmd_referral")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]
    else:
        # Fallback to manual payment
        text += f"""

**USDT (TRC20):**
`{CRYPTO_WALLETS['USDT_TRC20']}`

**TON:**
`{CRYPTO_WALLETS['TON']}`

{get_text("premium_after_payment", lang).format(support=SUPPORT_USERNAME)}"""
        keyboard = [
            [InlineKeyboardButton(get_text("premium_deposit_btn", lang), url=get_affiliate_link(user_id))],
            [InlineKeyboardButton(get_text("premium_contact_btn", lang).format(support=SUPPORT_USERNAME), url=f"https://t.me/{SUPPORT_USERNAME}")],
            [InlineKeyboardButton(get_text("premium_friends_btn", lang), callback_data="cmd_referral")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def referral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show referral program info and stats"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    # Get referral stats
    stats = get_referral_stats(user_id)
    ref_link = get_referral_link(user_id)

    # Check referral bonus eligibility
    ref_bonus = check_referral_bonus_eligible(user_id)
    bonus_predictions = get_bonus_predictions(user_id)

    text = f"""{get_text('referral_title', lang)}

{get_text('referral_desc', lang)}

{get_text('referral_link', lang)}
`{ref_link}`

{get_text('referral_copy', lang)}

{get_text('referral_stats', lang)}
• {get_text('referral_invited', lang)}: **{stats['invited']}**
• {get_text('referral_premium', lang)}: **{stats['premium']}**
• {get_text('referral_earned', lang)}: **{stats['earned_days']}**"""

    # Add bonus section
    if bonus_predictions > 0:
        bonus_text = {
            "ru": f"\n\n🎁 **Бонусные прогнозы:** {bonus_predictions}",
            "en": f"\n\n🎁 **Bonus predictions:** {bonus_predictions}",
            "pt": f"\n\n🎁 **Previsões bônus:** {bonus_predictions}",
            "es": f"\n\n🎁 **Predicciones bonus:** {bonus_predictions}",
            "id": f"\n\n🎁 **Prediksi bonus:** {bonus_predictions}"
        }
        text += bonus_text.get(lang, bonus_text["en"])

    if ref_bonus["eligible"]:
        # Can claim bonus
        text += f"\n\n🎉 {get_text('referral_bonus_title', lang)}\n{get_text('referral_bonus_progress', lang).format(current=ref_bonus['progress'])}"
        text += f"\n✅ {get_text('referral_bonus_desc', lang)}"
    elif not ref_bonus["claimed"]:
        # Show progress toward bonus
        text += f"\n\n🎁 {get_text('referral_bonus_desc', lang)}\n{get_text('referral_bonus_progress', lang).format(current=ref_bonus['progress'])}"
    else:
        # Already claimed
        text += f"\n\n✅ {get_text('referral_bonus_claimed', lang)}"

    text += f"\n\n{get_text('referral_rules', lang)}"

    keyboard = []
    if ref_bonus["eligible"]:
        claim_btn_text = {
            "ru": "🎁 Получить +3 прогноза",
            "en": "🎁 Claim +3 predictions",
            "pt": "🎁 Resgatar +3 previsões",
            "es": "🎁 Reclamar +3 predicciones",
            "id": "🎁 Klaim +3 prediksi"
        }
        keyboard.append([InlineKeyboardButton(claim_btn_text.get(lang, claim_btn_text["en"]), callback_data="claim_ref_bonus")])
    keyboard.append([InlineKeyboardButton(get_text("referral_invite_btn", lang), url=f"https://t.me/share/url?url={ref_link}&text=🔥")])
    keyboard.append([InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show prediction history with filters"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    # Parse filter from arguments: /history [all|wins|losses|pending] [count]
    args = context.args if context.args else []
    filter_type = "all"
    limit = 10

    for arg in args:
        if arg in ["all", "wins", "losses", "pending"]:
            filter_type = arg
        elif arg.isdigit():
            limit = min(int(arg), 50)  # Max 50

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Build query based on filter
    if filter_type == "wins":
        c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct = 1
                     ORDER BY predicted_at DESC LIMIT ?""", (user_id, limit))
    elif filter_type == "losses":
        c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct = 0
                     ORDER BY predicted_at DESC LIMIT ?""", (user_id, limit))
    elif filter_type == "pending":
        c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct IS NULL
                     ORDER BY predicted_at DESC LIMIT ?""", (user_id, limit))
    else:
        c.execute("""SELECT * FROM predictions WHERE user_id = ?
                     ORDER BY predicted_at DESC LIMIT ?""", (user_id, limit))

    predictions = c.fetchall()
    conn.close()

    if not predictions:
        no_history = {
            "ru": "📜 История пуста. Сделайте прогноз!",
            "en": "📜 No history yet. Make a prediction!",
            "pt": "📜 Histórico vazio. Faça uma previsão!",
            "es": "📜 Sin historial. ¡Haz una predicción!",
            "id": "📜 Riwayat kosong. Buat prediksi!"
        }
        await update.message.reply_text(no_history.get(lang, no_history["ru"]))
        return

    # Build history text
    filter_labels = {
        "all": {"ru": "ВСЕ", "en": "ALL", "pt": "TODOS", "es": "TODOS", "id": "SEMUA"},
        "wins": {"ru": "ПОБЕДЫ", "en": "WINS", "pt": "VITÓRIAS", "es": "VICTORIAS", "id": "MENANG"},
        "losses": {"ru": "ПОРАЖЕНИЯ", "en": "LOSSES", "pt": "DERROTAS", "es": "DERROTAS", "id": "KALAH"},
        "pending": {"ru": "ОЖИДАЮТ", "en": "PENDING", "pt": "PENDENTES", "es": "PENDIENTES", "id": "MENUNGGU"}
    }
    filter_label = filter_labels[filter_type].get(lang, filter_labels[filter_type]["en"])

    text = f"📜 **ИСТОРИЯ ПРОГНОЗОВ** ({filter_label})\n\n"

    for p in predictions:
        date_str = p["predicted_at"][:10] if p["predicted_at"] else "?"
        home = p["home_team"] or "?"
        away = p["away_team"] or "?"
        bet = p["bet_type"] or "?"
        conf = p["confidence"] or 0
        odds = p["odds"] or 0

        # Result emoji
        if p["is_correct"] is None:
            result_emoji = "⏳"
            result_text = "Ожидает"
        elif p["is_correct"] == 1:
            result_emoji = "✅"
            result_text = "WIN"
        else:
            result_emoji = "❌"
            result_text = "LOSE"

        text += f"{result_emoji} **{home}** vs **{away}**\n"
        text += f"   📅 {date_str} | {bet} @ {odds:.2f} ({conf}%)\n"
        if p["result"]:
            text += f"   📊 Счёт: {p['result']}\n"
        text += "\n"

    # Add filter buttons
    keyboard = [
        [InlineKeyboardButton("🔄 Все", callback_data="history_all"),
         InlineKeyboardButton("✅ Победы", callback_data="history_wins")],
        [InlineKeyboardButton("❌ Поражения", callback_data="history_losses"),
         InlineKeyboardButton("⏳ Ожидают", callback_data="history_pending")],
        [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel - only for admins"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    # Get stats
    conn = get_db_connection()
    c = conn.cursor()

    # Total users
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]

    # Active today (safe query - column may not exist)
    try:
        c.execute("SELECT COUNT(*) FROM users WHERE last_active > datetime('now', '-1 day')")
        active_today = c.fetchone()[0]
    except:
        active_today = "N/A"

    # Premium users (safe query)
    try:
        c.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        premium_users = c.fetchone()[0]
    except:
        premium_users = 0

    # Total predictions
    c.execute("SELECT COUNT(*) FROM predictions")
    total_predictions = c.fetchone()[0]

    # Verified predictions
    c.execute("SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) FROM predictions WHERE is_correct IS NOT NULL")
    row = c.fetchone()
    verified = row[0] or 0
    correct = row[1] or 0
    accuracy = round(correct / verified * 100, 1) if verified > 0 else 0

    # Live subscribers (from live_subscribers table)
    c.execute("SELECT COUNT(*) FROM live_subscribers")
    live_subs = c.fetchone()[0]

    # Pending predictions (waiting for results)
    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NULL")
    pending_count = c.fetchone()[0]

    conn.close()

    # Get clean stats (without duplicates)
    clean = get_clean_stats()
    duplicates_info = ""
    if clean["duplicates_count"] > 0:
        duplicates_info = f"\n⚠️ **Дубликаты:** {clean['duplicates_count']} (искажают статистику!)"

    text = f"""👑 **АДМИН-ПАНЕЛЬ**

📊 **Статистика бота:**
├ Всего юзеров: {total_users}
├ Активных сегодня: {active_today}
├ Premium: {premium_users}
└ Live подписчики: {live_subs}

🎯 **Прогнозы:**
├ Всего: {total_predictions}
├ Проверенных: {verified}
├ Верных: {correct}
├ Точность (сырая): {accuracy}%
└ ⏳ Pending: {pending_count}

📈 **Чистая статистика (без дубликатов):**
├ Уникальных: {clean['clean_total']}
├ Верных: {clean['clean_correct']}
└ **Реальная точность: {clean['clean_accuracy']}%**{duplicates_info}

⚙️ **Админ-команды:**
• /broadcast текст - Рассылка всем
• /addpremium ID - Дать премиум (дни)
• /removepremium ID - Убрать премиум
• /userinfo ID - Инфо о юзере
• /forcecheck - Проверить ВСЕ pending
• /analyzeall - Анализ всех матчей
• /mlstatus - Статус ML моделей
• /mltrain - Обучить ML модели
• /learnhistory - Обучить на истории
• /accuracy - Детальный анализ точности
• /roi - ROI статистика (прибыльность)
• /cleanfavs - Очистить дубликаты избранного
• /debug - Отладочная информация

🔧 **Система:**
├ Админов: {len(ADMIN_IDS)}
└ Твой ID: {user_id}"""

    keyboard = [
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 Юзеры", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Детальная статистика", callback_data="admin_stats"),
         InlineKeyboardButton("📈 Источники", callback_data="admin_sources")],
        [InlineKeyboardButton("🎯 Анализ точности", callback_data="admin_accuracy"),
         InlineKeyboardButton("🤖 ML статистика", callback_data="admin_ml_stats")],
        [InlineKeyboardButton("🧠 Обучение", callback_data="admin_learning"),
         InlineKeyboardButton("🔔 Live-алерты", callback_data="admin_live_status")],
        [InlineKeyboardButton("🧹 Очистить дубликаты", callback_data="admin_clean_dups"),
         InlineKeyboardButton("🔙 В меню", callback_data="cmd_start")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def accuracy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detailed accuracy analysis - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    await update.message.reply_text("📊 Собираю статистику...")

    conn = get_db_connection()
    c = conn.cursor()

    # Check for pending predictions first
    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NULL")
    pending_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NOT NULL")
    verified_count = c.fetchone()[0]

    if verified_count == 0:
        await update.message.reply_text(
            f"⚠️ Нет проверенных predictions!\n\n"
            f"📊 Pending: {pending_count}\n"
            f"📊 Verified: {verified_count}\n\n"
            f"💡 Используй /forcecheck для проверки всех pending"
        )
        conn.close()
        return

    text = "📊 **ДЕТАЛЬНЫЙ АНАЛИЗ ТОЧНОСТИ**\n" + "=" * 35 + "\n\n"

    if pending_count > 0:
        text += f"⚠️ Pending predictions: {pending_count}\n\n"

    # Overall stats
    c.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
        FROM predictions WHERE is_correct IS NOT NULL
    """)
    total, wins = c.fetchone()
    wins = wins or 0
    accuracy = round(wins / total * 100, 1) if total > 0 else 0

    text += f"🎯 **ОБЩАЯ СТАТИСТИКА:**\n"
    text += f"├ Всего проверенных: {total}\n"
    text += f"├ Правильных: {wins}\n"
    text += f"└ **Точность: {accuracy}%**\n\n"

    # Industry benchmark
    if accuracy >= 57:
        verdict = "🏆 ОТЛИЧНО! Уровень топ-типстеров"
    elif accuracy >= 53:
        verdict = "✅ ХОРОШО! В плюсе на дистанции"
    elif accuracy >= 50:
        verdict = "⚠️ СРЕДНЕ. Около безубытка"
    else:
        verdict = "❌ СЛАБО. Нужна оптимизация"
    text += f"📈 **Оценка:** {verdict}\n\n"

    # By confidence level
    text += f"📈 **ПО УВЕРЕННОСТИ:**\n"
    c.execute("""
        SELECT
            CASE
                WHEN confidence >= 80 THEN '80-100%'
                WHEN confidence >= 70 THEN '70-79%'
                WHEN confidence >= 60 THEN '60-69%'
                ELSE '<60%'
            END as conf_range,
            COUNT(*) as total,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
        FROM predictions
        WHERE is_correct IS NOT NULL AND confidence IS NOT NULL
        GROUP BY conf_range
        ORDER BY conf_range DESC
    """)
    conf_rows = c.fetchall()
    for row in conf_rows:
        conf_range, cnt, w = row
        w = w or 0
        acc = round(w / cnt * 100, 1) if cnt > 0 else 0
        emoji = "✅" if acc >= 55 else "⚠️" if acc >= 50 else "❌"
        text += f"├ {emoji} {conf_range}: {w}/{cnt} = **{acc}%**\n"
    text += "\n"

    # By bet category
    text += f"🏷️ **ПО ТИПАМ СТАВОК:**\n"
    c.execute("""
        SELECT
            bet_category,
            COUNT(*) as total,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
        FROM predictions
        WHERE is_correct IS NOT NULL AND bet_category IS NOT NULL
        GROUP BY bet_category
        HAVING total >= 3
        ORDER BY (wins * 1.0 / total) DESC
    """)
    category_names = {
        "totals_over": "ТБ (больше)",
        "totals_under": "ТМ (меньше)",
        "outcomes_home": "П1",
        "outcomes_away": "П2",
        "outcomes_draw": "Ничья",
        "btts": "ОЗ",
        "double_chance": "Двойной шанс",
        "handicap": "Фора"
    }
    cat_rows = c.fetchall()
    for row in cat_rows:
        cat, cnt, w = row
        w = w or 0
        acc = round(w / cnt * 100, 1) if cnt > 0 else 0
        name = category_names.get(cat, cat or "Другое")
        emoji = "✅" if acc >= 55 else "⚠️" if acc >= 50 else "❌"
        text += f"├ {emoji} {name}: {w}/{cnt} = **{acc}%**\n"
    text += "\n"

    # Recent trends
    text += f"📅 **ТРЕНДЫ:**\n"
    for days, label in [(7, "7 дней"), (14, "14 дней"), (30, "30 дней")]:
        c.execute(f"""
            SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions
            WHERE is_correct IS NOT NULL
            AND predicted_at >= datetime('now', '-{days} days')
        """)
        row = c.fetchone()
        cnt, w = row[0] or 0, row[1] or 0
        if cnt > 0:
            acc = round(w / cnt * 100, 1)
            emoji = "📈" if acc >= 53 else "📉"
            text += f"├ {emoji} {label}: {w}/{cnt} = **{acc}%**\n"

    # By league (top 5)
    text += f"\n🏆 **ТОП ЛИГИ:**\n"
    c.execute("""
        SELECT
            league_code,
            COUNT(*) as total,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
        FROM predictions
        WHERE is_correct IS NOT NULL AND league_code IS NOT NULL
        GROUP BY league_code
        HAVING total >= 5
        ORDER BY (wins * 1.0 / total) DESC
        LIMIT 5
    """)
    league_rows = c.fetchall()
    for row in league_rows:
        league_code, cnt, w = row
        w = w or 0
        acc = round(w / cnt * 100, 1) if cnt > 0 else 0
        emoji = "✅" if acc >= 55 else "⚠️"
        # Shorten league name
        short_league = league_code[:20] + "..." if len(league_code) > 20 else league_code
        text += f"├ {emoji} {short_league}: **{acc}%** ({cnt})\n"

    # Advanced ROI analysis (TARGET: 50%+)
    text += f"\n💰 **ROI АНАЛИЗ (ЦЕЛЬ: 50%+):**\n"
    c.execute("""
        SELECT
            SUM(CASE WHEN is_correct = 1 THEN odds - 1 ELSE -1 END) as profit,
            COUNT(*) as bets,
            AVG(odds) as avg_odds,
            SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as wins
        FROM predictions
        WHERE is_correct IS NOT NULL AND odds IS NOT NULL
    """)
    row = c.fetchone()
    if row and row[1] and row[1] > 0:
        profit = row[0] or 0
        bets = row[1]
        avg_odds = row[2] or 1.5
        wins = row[3] or 0
        roi = round(profit / bets * 100, 1)
        win_rate = round(wins / bets * 100, 1)

        # ROI status
        if roi >= 50:
            roi_status = "🏆 ЦЕЛЬ ДОСТИГНУТА!"
        elif roi >= 20:
            roi_status = "🔥 Отличный результат"
        elif roi >= 5:
            roi_status = "✅ В плюсе"
        elif roi >= 0:
            roi_status = "⚠️ Около нуля"
        else:
            roi_status = "❌ В минусе"

        text += f"├ **ROI: {roi}%** {roi_status}\n"
        text += f"├ Средний коэфф: {round(avg_odds, 2)}\n"
        text += f"├ Точность: {win_rate}%\n"

        # Calculate what's needed for 50% ROI
        # ROI = (avg_odds * win_rate - 1) * 100
        # 50 = (avg_odds * win_rate - 1) * 100
        # 0.5 = avg_odds * win_rate - 1
        # win_rate_needed = 1.5 / avg_odds
        win_rate_needed = round(1.5 / avg_odds * 100, 1)
        odds_needed = round(1.5 / (wins / bets) if wins > 0 else 3.0, 2)

        text += f"├ 📊 Для ROI 50%:\n"
        text += f"│  • При коэфф {round(avg_odds, 2)} нужна точность: **{win_rate_needed}%**\n"
        text += f"│  • При точности {win_rate}% нужен коэфф: **{odds_needed}**\n"

        # Progress bar to 50% ROI
        progress = min(100, max(0, (roi / 50) * 100))
        bar_filled = int(progress / 10)
        bar_empty = 10 - bar_filled
        progress_bar = "█" * bar_filled + "░" * bar_empty
        text += f"└ Прогресс к 50%: [{progress_bar}] {round(progress)}%\n"
    else:
        text += f"└ Недостаточно данных\n"

    # ROI by confidence level
    text += f"\n📈 **ROI ПО УВЕРЕННОСТИ:**\n"
    c.execute("""
        SELECT
            CASE
                WHEN confidence >= 80 THEN '80%+'
                WHEN confidence >= 70 THEN '70-79%'
                ELSE '<70%'
            END as conf_range,
            SUM(CASE WHEN is_correct = 1 THEN odds - 1 ELSE -1 END) as profit,
            COUNT(*) as bets,
            AVG(odds) as avg_odds
        FROM predictions
        WHERE is_correct IS NOT NULL AND odds IS NOT NULL AND confidence IS NOT NULL
        GROUP BY conf_range
        ORDER BY conf_range DESC
    """)
    roi_by_conf = c.fetchall()
    for row in roi_by_conf:
        conf_range, profit, bets, avg_o = row
        if bets and bets > 0:
            roi_val = round((profit or 0) / bets * 100, 1)
            emoji = "🔥" if roi_val >= 50 else "✅" if roi_val > 0 else "❌"
            text += f"├ {emoji} {conf_range}: ROI **{roi_val}%** (коэфф ~{round(avg_o or 1.5, 2)})\n"

    conn.close()

    # Add recommendations
    text += f"\n💡 **РЕКОМЕНДАЦИИ ДЛЯ ROI 50%+:**\n"
    if total < 100:
        text += "• Мало данных — нужно минимум 100-200 прогнозов\n"
    if conf_rows:
        # Find best confidence range
        best_conf = max(conf_rows, key=lambda x: (x[2] or 0) / x[1] if x[1] > 0 else 0)
        text += f"• Лучший диапазон: {best_conf[0]}\n"
    if cat_rows:
        # Find worst category
        worst_cat = min(cat_rows, key=lambda x: (x[2] or 0) / x[1] if x[1] > 0 else 0)
        worst_name = category_names.get(worst_cat[0], worst_cat[0])
        worst_acc = round((worst_cat[2] or 0) / worst_cat[1] * 100, 1) if worst_cat[1] > 0 else 0
        if worst_acc < 50:
            text += f"• ⚠️ Проблемный тип: {worst_name} ({worst_acc}%)\n"

    # Tips for 50%+ ROI
    text += "• 🎯 Фокус на ставках с VALUE >10%\n"
    text += "• 📊 Ставить только при 3+ факторов (Edge Stacking)\n"
    text += "• 🚫 Пропускать матчи без явного преимущества\n"

    # Split message if too long
    try:
        if len(text) > 4000:
            await update.message.reply_text(text[:4000], parse_mode="Markdown")
            await update.message.reply_text(text[4000:], parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Markdown error in accuracy: {e}")
        # Fallback to plain text
        if len(text) > 4000:
            await update.message.reply_text(text[:4000])
            await update.message.reply_text(text[4000:])
        else:
            await update.message.reply_text(text)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    if not context.args:
        await update.message.reply_text("❌ Использование: /broadcast <текст сообщения>")
        return

    message = " ".join(context.args)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = c.fetchall()
    conn.close()

    sent = 0
    failed = 0

    await update.message.reply_text(f"📢 Начинаю рассылку {len(users)} юзерам...")

    for (uid,) in users:
        try:
            await context.bot.send_message(uid, f"📢 **Объявление:**\n\n{message}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)  # Rate limiting
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Рассылка завершена!\n├ Отправлено: {sent}\n└ Ошибок: {failed}")


async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add premium to user - admin only
    Usage: /addpremium <user_id> [days]
    Examples:
        /addpremium 123456789 30  - 30 days
        /addpremium 123456789 7   - 7 days
        /addpremium 123456789 365 - 1 year
        /addpremium 123456789     - 30 days (default)
    """
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "❌ Использование: /addpremium <user_id> [дней]\n\n"
            "Примеры:\n"
            "• /addpremium 123456 7 — 7 дней\n"
            "• /addpremium 123456 30 — 30 дней\n"
            "• /addpremium 123456 365 — 1 год\n"
            "• /addpremium 123456 — 30 дней (по умолчанию)"
        )
        return

    target_id = int(context.args[0])
    days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30

    # Use grant_premium function for proper expiry handling
    success = grant_premium(target_id, days)

    if success:
        expires_text = "навсегда" if days >= 36500 else f"на {days} дней"
        await update.message.reply_text(f"✅ Премиум выдан юзеру {target_id} {expires_text}")
        try:
            user_msg = f"🎉 Вам выдан Premium-статус {expires_text}!\n\nБезлимитные прогнозы активированы."
            await context.bot.send_message(target_id, user_msg)
        except Exception:
            pass
    else:
        await update.message.reply_text(f"❌ Юзер {target_id} не найден. Попросите его сначала запустить бота (/start)")


async def removepremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove premium from user - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Использование: /removepremium <user_id>")
        return

    target_id = int(context.args[0])

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (target_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
        await update.message.reply_text(f"✅ Премиум убран у юзера {target_id}")
    else:
        await update.message.reply_text(f"❌ Юзер {target_id} не найден")


async def cleanfavs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clean duplicate favorites - admin only.
    Removes duplicate entries from favorite_teams and favorite_leagues tables.
    """
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    # Get status BEFORE cleanup
    conn = get_db_connection()
    c = conn.cursor()

    # Count total favorite entries
    c.execute("SELECT COUNT(*) FROM favorite_teams")
    total_teams = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM favorite_leagues")
    total_leagues = c.fetchone()[0]

    # Count unique entries (without duplicates)
    c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT user_id, team_name FROM favorite_teams)")
    unique_teams = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT user_id, league_code FROM favorite_leagues)")
    unique_leagues = c.fetchone()[0]

    # Count users with favorites
    c.execute("SELECT COUNT(DISTINCT user_id) FROM favorite_teams")
    users_with_teams = c.fetchone()[0]
    c.execute("SELECT COUNT(DISTINCT user_id) FROM favorite_leagues")
    users_with_leagues = c.fetchone()[0]

    conn.close()

    dup_teams = total_teams - unique_teams
    dup_leagues = total_leagues - unique_leagues

    status_text = f"""🔍 **Статус избранного ДО очистки:**

📊 **Команды:**
├ Всего записей: {total_teams}
├ Уникальных: {unique_teams}
├ Дубликатов: {dup_teams}
└ Юзеров с командами: {users_with_teams}

📊 **Лиги:**
├ Всего записей: {total_leagues}
├ Уникальных: {unique_leagues}
├ Дубликатов: {dup_leagues}
└ Юзеров с лигами: {users_with_leagues}

🧹 Очищаю..."""

    await update.message.reply_text(status_text, parse_mode="Markdown")

    # Run cleanup
    result = clean_duplicate_favorites()

    if result["total"] > 0:
        text = f"""✅ **Дубликаты очищены!**

├ Удалено команд: {result['deleted_teams']}
└ Удалено лиг: {result['deleted_leagues']}

Всего удалено: {result['total']} записей"""
    else:
        text = "✅ Дубликатов не найдено! База чистая."

    await update.message.reply_text(text, parse_mode="Markdown")


async def userinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get user info - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Использование: /userinfo <user_id>")
        return

    target_id = int(context.args[0])

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
    row = c.fetchone()

    if not row:
        await update.message.reply_text(f"❌ Юзер {target_id} не найден")
        conn.close()
        return

    # Get prediction count
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (target_id,))
    pred_count = c.fetchone()[0]

    conn.close()

    # Parse user data safely
    username = row['username'] if 'username' in row.keys() else None
    first_name = row['first_name'] if 'first_name' in row.keys() else None
    language = row['language'] if 'language' in row.keys() else 'ru'
    is_premium = row['is_premium'] if 'is_premium' in row.keys() else 0
    live_alerts = row['live_alerts'] if 'live_alerts' in row.keys() else 0
    created_at = row['created_at'] if 'created_at' in row.keys() else 'N/A'
    last_active = row['last_active'] if 'last_active' in row.keys() else 'N/A'

    text = f"""👤 **Информация о юзере {target_id}**

├ Username: @{username or 'нет'}
├ Имя: {first_name or 'нет'}
├ Язык: {language}
├ Premium: {'✅' if is_premium else '❌'}
├ Live-алерты: {'✅' if live_alerts else '❌'}
├ Прогнозов: {pred_count}
├ Зарегистрирован: {created_at}
└ Последняя активность: {last_active}"""

    await update.message.reply_text(text, parse_mode="Markdown")


async def mlstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show ML system status - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    status = get_ml_status()

    text = f"""🤖 **ML СИСТЕМА**

🔧 **Статус:**
├ ML доступен: {'✅' if status['ml_available'] else '❌'}
└ Мин. данных для обучения: {status['min_samples']}

📊 **Данные для обучения:**
"""

    if status["data_counts"]:
        category_names = {
            "outcomes_home": "П1",
            "outcomes_away": "П2",
            "outcomes_draw": "Ничья",
            "totals_over": "ТБ 2.5",
            "totals_under": "ТМ 2.5",
            "btts": "BTTS",
            "double_chance": "Двойной шанс",
            "handicap": "Фора"
        }
        for cat, data in status["data_counts"].items():
            name = category_names.get(cat, cat)
            ready = "✅" if data["verified"] >= status["min_samples"] else f"⏳ {data['verified']}/{status['min_samples']}"
            text += f"├ {name}: {data['total']} всего, {data['verified']} проверено {ready}\n"
    else:
        text += "├ Пока нет данных\n"

    text += "\n🎯 **Обученные модели:**\n"

    if status["models"]:
        for cat, info in status["models"].items():
            name = category_names.get(cat, cat)
            text += f"├ {name}: {info['accuracy']:.1%} точность ({info['samples']} samples)\n"
    else:
        text += "├ Модели ещё не обучены\n"
        text += f"└ Нужно {status['min_samples']}+ проверенных прогнозов\n"

    if status["ready_to_train"]:
        text += f"\n⚡ **Готовы к обучению:** {', '.join(status['ready_to_train'])}"

    keyboard = [
        [InlineKeyboardButton("🔄 Обучить модели", callback_data="ml_train")],
        [InlineKeyboardButton("🔙 В админку", callback_data="cmd_admin")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def mltrain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force train ML models - admin only with live progress updates"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    # Send initial message that we'll update with progress
    status_msg = await update.message.reply_text(
        "🔄 **Обучение ML моделей**\n\n"
        "⏳ Подготовка...\n\n"
        "_Это может занять несколько минут_",
        parse_mode="Markdown"
    )

    # Track progress for display
    progress_lines = []
    start_time = datetime.now()

    async def progress_callback(cat_name: str, status: str, result: dict, current: int, total: int):
        """Update status message with training progress"""
        nonlocal progress_lines

        if status == "training":
            progress_lines.append(f"⏳ {cat_name}...")
        elif status == "done":
            # Remove "training" line and add completed
            progress_lines = [l for l in progress_lines if cat_name not in l]
            accuracy = result.get('accuracy', 0) * 100
            samples = result.get('samples', 0)
            progress_lines.append(f"✅ {cat_name}: {accuracy:.1f}% ({samples} примеров)")
        elif status == "no_data":
            progress_lines = [l for l in progress_lines if cat_name not in l]
            progress_lines.append(f"⚠️ {cat_name}: мало данных")

        # Build progress bar
        filled = "█" * current
        empty = "░" * (total - current)
        progress_bar = f"[{filled}{empty}] {current}/{total}"

        # Update message
        elapsed = (datetime.now() - start_time).seconds
        text = (
            f"🔄 **Обучение ML моделей**\n\n"
            f"{progress_bar}\n\n"
            + "\n".join(progress_lines) +
            f"\n\n_⏱ {elapsed} сек._"
        )

        try:
            await status_msg.edit_text(text, parse_mode="Markdown")
        except Exception:
            pass  # Ignore edit errors (message unchanged)

    # Run async training with progress
    results = await train_all_models_async(progress_callback)

    # Final summary
    elapsed = (datetime.now() - start_time).seconds
    if results:
        text = f"✅ **Обучение завершено за {elapsed} сек!**\n\n"
        total_samples = 0
        avg_accuracy = 0

        for cat, info in results.items():
            cat_names = {
                "outcomes_home": "П1",
                "outcomes_away": "П2",
                "outcomes_draw": "X",
                "totals_over": "ТБ 2.5",
                "totals_under": "ТМ 2.5",
                "btts": "ОЗ"
            }
            name = cat_names.get(cat, cat)
            text += f"• {name}: **{info['accuracy']:.1%}** ({info['samples']} примеров)\n"
            total_samples += info['samples']
            avg_accuracy += info['accuracy']

        if results:
            avg_accuracy = avg_accuracy / len(results) * 100
            text += f"\n📊 Средняя точность: {avg_accuracy:.1f}%"
            text += f"\n📚 Всего примеров: {total_samples}"
    else:
        text = (
            "❌ Недостаточно данных для обучения\n\n"
            f"Нужно минимум {ML_MIN_SAMPLES} проверенных прогнозов на категорию.\n\n"
            "💡 Продолжайте использовать бота, данные накапливаются автоматически."
        )

    try:
        await status_msg.edit_text(text, parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text(text)


async def learnhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Learn from historical predictions - admin only.

    Goes through all verified predictions and updates feature_error_patterns
    to bootstrap the smart learning system.
    """
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    status_msg = await update.message.reply_text("🧠 Запускаю обучение на исторических данных...")

    conn = get_db_connection()
    c = conn.cursor()

    # Get all verified predictions with their ML features
    # Use COALESCE to get features from predictions table or ml_training_data
    c.execute("""SELECT p.id, p.bet_type, p.confidence, p.is_correct,
                        COALESCE(p.ml_features_json, m.features_json) as features_json,
                        p.league_code
                 FROM predictions p
                 LEFT JOIN ml_training_data m ON m.prediction_id = p.id
                 WHERE p.is_correct IS NOT NULL
                 AND p.is_correct != 2
                 AND (p.ml_features_json IS NOT NULL OR m.features_json IS NOT NULL)
                 ORDER BY p.predicted_at""")

    predictions = c.fetchall()
    conn.close()

    total_count = len(predictions)

    if not predictions:
        await status_msg.edit_text("❌ Нет верифицированных прогнозов с ML фичами для обучения.")
        return

    await status_msg.edit_text(f"🧠 Обучение на {total_count} прогнозах...\n\n⏳ Прогресс: 0%")

    processed = 0
    errors = 0
    patterns_updated = 0
    last_update = 0

    for pred_id, bet_type, confidence, is_correct, features_json, league_code in predictions:
        try:
            if not features_json:
                continue

            features = json.loads(features_json)
            bet_category = categorize_bet(bet_type)

            if not bet_category:
                continue

            is_win = is_correct == 1

            # Extract conditions and update patterns
            conditions = extract_feature_conditions(features, bet_category)
            for condition in conditions:
                update_feature_pattern(bet_category, condition, is_win, confidence)
                patterns_updated += 1

            processed += 1

            # Update progress every 50 predictions
            if processed - last_update >= 50:
                percent = int((processed / total_count) * 100)
                try:
                    await status_msg.edit_text(
                        f"🧠 Обучение на {total_count} прогнозах...\n\n"
                        f"⏳ Прогресс: {processed}/{total_count} ({percent}%)\n"
                        f"📊 Паттернов: {patterns_updated}"
                    )
                except Exception:
                    pass  # Ignore rate limit errors
                last_update = processed

        except Exception as e:
            errors += 1
            logger.error(f"Error processing prediction {pred_id}: {e}")

    # Get stats about learned patterns
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT COUNT(*),
                 SUM(CASE WHEN suggested_adjustment < -5 THEN 1 ELSE 0 END),
                 SUM(CASE WHEN suggested_adjustment > 5 THEN 1 ELSE 0 END)
                 FROM feature_error_patterns WHERE total_predictions >= 5""")
    pattern_stats = c.fetchone()
    conn.close()

    total_patterns = pattern_stats[0] or 0
    risky_patterns = pattern_stats[1] or 0
    good_patterns = pattern_stats[2] or 0

    text = f"""✅ **Обучение на истории завершено!**

📊 **Обработано:**
├ Прогнозов: {processed}
├ Ошибок: {errors}
└ Паттернов обновлено: {patterns_updated}

🧠 **Выученные паттерны (≥5 примеров):**
├ Всего: {total_patterns}
├ 🔴 Рисковых (снижают conf): {risky_patterns}
└ 🟢 Хороших (повышают conf): {good_patterns}

Теперь система будет корректировать уверенность на основе выученных паттернов!"""

    await update.message.reply_text(text, parse_mode="Markdown")


async def roi_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show ROI statistics - admin only."""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    stats = get_overall_roi_stats()

    # Format ROI with color
    roi = stats["roi"]
    if roi > 20:
        roi_emoji = "🚀"
    elif roi > 0:
        roi_emoji = "🟢"
    elif roi > -10:
        roi_emoji = "🟡"
    else:
        roi_emoji = "🔴"

    text = f"""💰 **ROI СТАТИСТИКА**

📊 **Общие показатели:**
├ Всего ставок: {stats['total_bets']}
├ Побед: {stats['wins']} ({stats['win_rate']:.1f}%)
├ Поражений: {stats['losses']}
├ Средний коэфф: {stats['avg_odds']:.2f}
├ Средний EV: {stats['avg_ev'] or 0:.1f}%
└ {roi_emoji} **ROI: {roi:+.1f}%**

📈 **ROI по категориям:**
"""

    for cat in stats["by_category"][:8]:
        cat_roi = cat["roi"]
        if cat_roi > 10:
            cat_emoji = "🟢"
        elif cat_roi > -10:
            cat_emoji = "🟡"
        else:
            cat_emoji = "🔴"

        cat_names = {
            "outcomes_home": "П1",
            "outcomes_away": "П2",
            "outcomes_draw": "Ничья",
            "totals_over": "ТБ 2.5",
            "totals_under": "ТМ 2.5",
            "btts": "Обе забьют",
            "double_chance": "Двойной шанс",
            "handicap": "Фора"
        }
        name = cat_names.get(cat["category"], cat["category"])

        text += f"{cat_emoji} {name}: {cat_roi:+.1f}% ROI ({cat['win_rate']:.0f}% винрейт, n={cat['total']})\n"

    text += f"""
💡 **Понимание ROI:**
• Положительный ROI = прибыль
• 60% винрейт × 1.4 коэфф = -16% ROI (убыток!)
• 45% винрейт × 2.5 коэфф = +12% ROI (прибыль!)

🎯 Цель: ROI 30%+ при винрейте 55%+"""

    await update.message.reply_text(text, parse_mode="Markdown")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    # Initial language selection for new users
    if data.startswith("set_initial_lang_"):
        selected_lang = data.replace("set_initial_lang_", "")
        tg_user = query.from_user
        detected_tz = detect_timezone(tg_user)

        # Get UTM source - first from context, then from pending_utm (survives bot restart)
        utm_source = context.user_data.get("utm_source")
        pending_data = get_pending_utm(user_id)
        if not utm_source or utm_source == "organic":
            utm_source = pending_data["utm_source"]

        # Create user with selected language and source
        is_new_user = create_user(user_id, tg_user.username, selected_lang, source=utm_source)
        update_user_settings(user_id, timezone=detected_tz)

        if is_new_user:
            logger.info(f"New user created: {user_id}, lang={selected_lang}, source={utm_source}")
            # Notify admins about new user
            await notify_admins_new_user(
                context.bot,
                user_id,
                tg_user.username,
                selected_lang,
                utm_source
            )

        # Save referral if exists - check both context and pending_utm
        referrer_id = context.user_data.get("referrer_id") or pending_data.get("referrer_id")
        referral_msg = ""
        if referrer_id:
            if save_referral(referrer_id, user_id):
                # Grant bonus predictions to new user (friend also gets bonus!)
                grant_new_user_referral_bonus(user_id)
                referral_msg = f"\n\n{get_text('referral_welcome', selected_lang)}"
                referral_msg += f"\n🎁 {get_text('referral_bonus_friend_gets', selected_lang)}"
                logger.info(f"Saved referral from context: {referrer_id} -> {user_id}")

                # Check if referrer now has 2 referrals and can claim bonus
                ref_status = check_referral_bonus_eligible(referrer_id)
                if ref_status["eligible"]:
                    # Notify referrer that they can claim bonus
                    try:
                        referrer_user = get_user(referrer_id)
                        referrer_lang = referrer_user.get("language", "ru") if referrer_user else "ru"
                        notify_text = f"🎉 {get_text('referral_bonus_title', referrer_lang)}\n\n"
                        notify_text += get_text('referral_bonus_progress', referrer_lang).format(current=ref_status['progress'])
                        notify_text += f"\n\n✅ {get_text('referral_bonus_desc', referrer_lang)}"
                        notify_text += f"\n\n👉 /ref"
                        await context.bot.send_message(chat_id=referrer_id, text=notify_text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Failed to notify referrer {referrer_id}: {e}")

        # Show welcome message with 1win partner info
        tz_display = get_tz_offset_str(detected_tz)
        welcome_text = f"""{get_text('first_start_title', selected_lang)}

{get_text('first_start_text', selected_lang)}

{get_text('where_to_bet', selected_lang)}
{get_text('bet_partner_text', selected_lang)}

{get_text('detected_settings', selected_lang)}
• {get_text('timezone_label', selected_lang)}: {tz_display}

_{get_text('change_in_settings', selected_lang)}_{referral_msg}"""

        # Build NEW USER keyboard - focused on quick start actions
        keyboard = [
            [InlineKeyboardButton(get_text("try_prediction_btn", selected_lang), callback_data="cmd_recommend")],
            [InlineKeyboardButton(get_text("today", selected_lang), callback_data="cmd_today"),
             InlineKeyboardButton(get_text("live_alerts", selected_lang), callback_data="cmd_live")],
        ]
        # Add 1win button only if monetization enabled
        affiliate_url = get_affiliate_link(user_id)
        if affiliate_url:
            keyboard.append([InlineKeyboardButton(get_text("open_1win_btn", selected_lang), url=affiliate_url)])
        keyboard.append([InlineKeyboardButton(get_text("stats", selected_lang), callback_data="cmd_stats"),
             InlineKeyboardButton(get_text("help", selected_lang), callback_data="cmd_help")])

        await query.edit_message_text(
            welcome_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        # Schedule onboarding message after 5 minutes
        async def onboarding_callback(ctx):
            await send_new_user_onboarding(ctx, user_id, selected_lang)

        context.job_queue.run_once(
            onboarding_callback,
            when=300,  # 5 minutes
            name=f"onboarding_{user_id}"
        )

        # Schedule reminder series for inactive users (1h, 3h, 12h, 24h, 48h)
        schedule_inactive_user_reminders(context, user_id, selected_lang)

        return

    # Command callbacks
    if data == "cmd_start":
        keyboard = [
            [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend"),
             InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")],
            [InlineKeyboardButton(get_text("tomorrow", lang), callback_data="cmd_tomorrow"),
             InlineKeyboardButton(get_text("leagues", lang), callback_data="cmd_leagues")],
            [InlineKeyboardButton(get_text("live_alerts", lang), callback_data="cmd_live"),
             InlineKeyboardButton(get_text("settings", lang), callback_data="cmd_settings")],
            [InlineKeyboardButton(get_text("favorites", lang), callback_data="cmd_favorites"),
             InlineKeyboardButton(get_text("stats", lang), callback_data="cmd_stats")],
            [InlineKeyboardButton(get_text("premium_btn", lang), callback_data="cmd_premium"),
             InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")],
            [InlineKeyboardButton(get_text("help", lang), callback_data="cmd_help")]
        ]
        await query.edit_message_text(f"⚽ **AI Betting Bot v14** - {get_text('choose_action', lang)}",
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_referral":
        await referral_cmd(update, context)

    elif data == "claim_ref_bonus":
        # Claim referral predictions bonus
        success = claim_referral_bonus(user_id)
        if success:
            claimed_text = {
                "ru": "🎉 **Бонус получен!**\n\n+3 прогноза добавлено!\nИспользуй /predict чтобы получить прогнозы.",
                "en": "🎉 **Bonus claimed!**\n\n+3 predictions added!\nUse /predict to get predictions.",
                "pt": "🎉 **Bônus resgatado!**\n\n+3 previsões adicionadas!\nUse /predict para obter previsões.",
                "es": "🎉 **¡Bonus reclamado!**\n\n+3 predicciones agregadas!\nUsa /predict para obtener predicciones.",
                "id": "🎉 **Bonus diklaim!**\n\n+3 prediksi ditambahkan!\nGunakan /predict untuk mendapatkan prediksi."
            }
            keyboard = [
                [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")],
                [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
            ]
            await query.edit_message_text(claimed_text.get(lang, claimed_text["en"]), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            error_text = {
                "ru": "❌ Бонус недоступен. Пригласи 2 друзей чтобы получить.",
                "en": "❌ Bonus not available. Invite 2 friends to get it.",
                "pt": "❌ Bônus não disponível. Convide 2 amigos para obtê-lo.",
                "es": "❌ Bonus no disponible. Invita 2 amigos para obtenerlo.",
                "id": "❌ Bonus tidak tersedia. Undang 2 teman untuk mendapatkannya."
            }
            keyboard = [[InlineKeyboardButton(get_text("back", lang), callback_data="cmd_referral")]]
            await query.edit_message_text(error_text.get(lang, error_text["en"]), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "cmd_premium":
        await premium_cmd(update, context)

    # Crypto payment handlers
    elif data.startswith("pay_crypto_"):
        days = int(data.replace("pay_crypto_", ""))
        await query.edit_message_text("⏳ Создаю счёт на оплату...")

        # Show currency selection
        keyboard = [
            [InlineKeyboardButton("💵 USDT", callback_data=f"crypto_pay_{days}_USDT"),
             InlineKeyboardButton("💎 TON", callback_data=f"crypto_pay_{days}_TON")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_premium")]
        ]
        price = CRYPTO_PRICES.get(days, 15)
        text = f"""💰 **Выбери валюту**

Тариф: **{days} дней** за **${price}**

Оплата через @CryptoBot — безопасно и мгновенно!"""
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("crypto_pay_"):
        # Format: crypto_pay_{days}_{currency}
        parts = data.replace("crypto_pay_", "").split("_")
        days = int(parts[0])
        currency = parts[1]

        await query.edit_message_text("⏳ Создаю инвойс...")

        # Create invoice via CryptoBot
        result = await create_crypto_invoice(user_id, days, currency)

        if "error" in result:
            text = f"❌ Ошибка: {result['error']}\n\nПопробуй позже или напиши @{SUPPORT_USERNAME}"
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_premium")]]
        else:
            pay_url = result["pay_url"]
            amount = result["amount"]
            text = f"""✅ **Счёт создан!**

💰 Сумма: **{amount} {currency}**
📅 Тариф: **{days} дней**

Нажми кнопку ниже для оплаты через @CryptoBot.
После оплаты премиум активируется автоматически!"""
            keyboard = [
                [InlineKeyboardButton(f"💳 Оплатить {amount} {currency}", url=pay_url)],
                [InlineKeyboardButton("🔙 Назад", callback_data="cmd_premium")]
            ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_recommend":
        # Check limit
        can_use, _, use_bonus = check_daily_limit(user_id)
        if not can_use:
            text = get_limit_text(lang)
            keyboard = []
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
            return

        await query.edit_message_text(get_text("analyzing", lang))
        matches = await get_matches(days=7)
        if matches:
            user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
            recs = await get_recommendations_enhanced(matches, "", user, lang=lang, user_tz=user_tz)
            keyboard = []
            bet_btn = get_bet_button(user_id, lang)
            if bet_btn:
                keyboard.append(bet_btn)
            keyboard.append([InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")])
            increment_daily_usage(user_id)
            await query.edit_message_text(recs or get_text("no_matches", lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await query.edit_message_text(get_text("no_matches", lang))
    
    elif data == "cmd_today":
        user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
        await query.edit_message_text(get_text("analyzing", lang))
        matches = await get_matches(date_filter="today")
        if not matches:
            await query.edit_message_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        tz_info = get_tz_offset_str(user_tz)
        text = f"{get_text('matches_today', lang)} ({tz_info}):\n\n"
        for comp, ms in by_comp.items():
            text += f"🏆 **{comp}**\n"
            for m in ms[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
                text += f"  ⏰ {time_str} | {home} vs {away}\n"
            text += "\n"

        keyboard = [
            [InlineKeyboardButton(get_text("recs_today", lang), callback_data="rec_today")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_tomorrow":
        user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
        await query.edit_message_text(get_text("analyzing", lang))
        matches = await get_matches(date_filter="tomorrow")
        if not matches:
            await query.edit_message_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        tz_info = get_tz_offset_str(user_tz)
        text = f"{get_text('matches_tomorrow', lang)} ({tz_info}):\n\n"
        for comp, ms in by_comp.items():
            text += f"🏆 **{comp}**\n"
            for m in ms[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
                text += f"  ⏰ {time_str} | {home} vs {away}\n"
            text += "\n"
        
        keyboard = [
            [InlineKeyboardButton(get_text("recs_tomorrow", lang), callback_data="rec_tomorrow")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_leagues":
        keyboard = [
            [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL"),
             InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
            [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="league_BL1"),
             InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
            [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="league_FL1"),
             InlineKeyboardButton("🇳🇱 Eredivisie", callback_data="league_DED")],
            [InlineKeyboardButton("🇵🇹 Primeira Liga", callback_data="league_PPL"),
             InlineKeyboardButton("🇧🇷 Brasileirão", callback_data="league_BSA")],
            [InlineKeyboardButton("🇪🇺 Champions League", callback_data="league_CL"),
             InlineKeyboardButton("🇪🇺 Europa League", callback_data="league_EL")],
            [InlineKeyboardButton(get_text("more_leagues", lang), callback_data="cmd_leagues2")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]
        await query.edit_message_text(get_text("top_leagues", lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_leagues2":
        keyboard = [
            [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Championship", callback_data="league_ELC"),
             InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 League One", callback_data="league_EL1")],
            [InlineKeyboardButton("🇩🇪 Bundesliga 2", callback_data="league_BL2"),
             InlineKeyboardButton("🇮🇹 Serie B", callback_data="league_SB")],
            [InlineKeyboardButton("🇫🇷 Ligue 2", callback_data="league_FL2"),
             InlineKeyboardButton("🇪🇸 Segunda", callback_data="league_SD")],
            [InlineKeyboardButton("🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scotland", callback_data="league_SPL"),
             InlineKeyboardButton("🇧🇪 Belgium", callback_data="league_BJL")],
            [InlineKeyboardButton("🇦🇷 Argentina", callback_data="league_ASL"),
             InlineKeyboardButton("🇺🇸 MLS", callback_data="league_MLS")],
            [InlineKeyboardButton("🏆 FA Cup", callback_data="league_FAC"),
             InlineKeyboardButton("🏆 DFB-Pokal", callback_data="league_DFB")],
            [InlineKeyboardButton(get_text("top_leagues", lang).replace("**", "").replace(":", ""), callback_data="cmd_leagues")]
        ]
        await query.edit_message_text(get_text("other_leagues", lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_settings":
        await settings_cmd(update, context)
    
    elif data == "debug_reset_limit":
        # ADMIN ONLY: Reset daily limit for debugging
        if not is_admin(user_id):
            await query.answer(get_text("admin_only", lang), show_alert=True)
            return
        logger.info(f"DEBUG: Resetting limit for user {user_id}")
        update_user_settings(user_id, daily_requests=0, last_request_date="")
        user_after = get_user(user_id)
        logger.info(f"DEBUG: After reset - requests={user_after.get('daily_requests')}, last_date={user_after.get('last_request_date')}")
        await query.edit_message_text(
            get_text("limit_reset", lang).format(user_id=user_id, limit=FREE_DAILY_LIMIT)
        )

    elif data == "debug_remove_premium":
        # ADMIN ONLY: Remove premium status for debugging
        if not is_admin(user_id):
            await query.answer(get_text("admin_only", lang), show_alert=True)
            return
        user_before = get_user(user_id)
        logger.info(f"DEBUG: Before remove premium - is_premium={user_before.get('is_premium')}")
        update_user_settings(user_id, is_premium=0, daily_requests=0, last_request_date="")
        user_after = get_user(user_id)
        logger.info(f"DEBUG: After remove premium - is_premium={user_after.get('is_premium')}, requests={user_after.get('daily_requests')}")
        await query.edit_message_text(
            get_text("premium_removed", lang).format(
                user_id=user_id,
                premium=user_after.get('is_premium'),
                requests=user_after.get('daily_requests'),
                limit=FREE_DAILY_LIMIT
            )
        )
    
    elif data == "cmd_favorites":
        await favorites_cmd(update, context)
    
    elif data == "cmd_stats":
        await stats_cmd(update, context)

    elif data.startswith("stats_page_"):
        # Stats pagination
        page = int(data.replace("stats_page_", ""))
        await stats_cmd(update, context, page=page)

    elif data.startswith("history_"):
        # History filter callbacks
        filter_type = data.replace("history_", "")
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        if filter_type == "wins":
            c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct = 1
                         ORDER BY predicted_at DESC LIMIT 10""", (user_id,))
        elif filter_type == "losses":
            c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct = 0
                         ORDER BY predicted_at DESC LIMIT 10""", (user_id,))
        elif filter_type == "pending":
            c.execute("""SELECT * FROM predictions WHERE user_id = ? AND is_correct IS NULL
                         ORDER BY predicted_at DESC LIMIT 10""", (user_id,))
        else:
            c.execute("""SELECT * FROM predictions WHERE user_id = ?
                         ORDER BY predicted_at DESC LIMIT 10""", (user_id,))

        predictions = c.fetchall()
        conn.close()

        filter_labels = {
            "all": {"ru": "ВСЕ", "en": "ALL", "pt": "TODOS", "es": "TODOS", "id": "SEMUA"},
            "wins": {"ru": "ПОБЕДЫ", "en": "WINS", "pt": "VITÓRIAS", "es": "VICTORIAS", "id": "MENANG"},
            "losses": {"ru": "ПОРАЖЕНИЯ", "en": "LOSSES", "pt": "DERROTAS", "es": "DERROTAS", "id": "KALAH"},
            "pending": {"ru": "ОЖИДАЮТ", "en": "PENDING", "pt": "PENDENTES", "es": "PENDIENTES", "id": "MENUNGGU"}
        }
        filter_label = filter_labels.get(filter_type, filter_labels["all"]).get(lang, "ALL")

        if not predictions:
            text = f"📜 **ИСТОРИЯ** ({filter_label})\n\nНет прогнозов."
        else:
            text = f"📜 **ИСТОРИЯ ПРОГНОЗОВ** ({filter_label})\n\n"
            for p in predictions:
                date_str = p["predicted_at"][:10] if p["predicted_at"] else "?"
                home = p["home_team"] or "?"
                away = p["away_team"] or "?"
                bet = p["bet_type"] or "?"
                conf = p["confidence"] or 0
                odds = p["odds"] or 0

                if p["is_correct"] is None:
                    result_emoji = "⏳"
                elif p["is_correct"] == 1:
                    result_emoji = "✅"
                else:
                    result_emoji = "❌"

                text += f"{result_emoji} **{home}** vs **{away}**\n"
                text += f"   📅 {date_str} | {bet} @ {odds:.2f} ({conf}%)\n"
                if p["result"]:
                    text += f"   📊 Счёт: {p['result']}\n"
                text += "\n"

        keyboard = [
            [InlineKeyboardButton("🔄 Все", callback_data="history_all"),
             InlineKeyboardButton("✅ Победы", callback_data="history_wins")],
            [InlineKeyboardButton("❌ Поражения", callback_data="history_losses"),
             InlineKeyboardButton("⏳ Ожидают", callback_data="history_pending")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
        ]

        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_help":
        await help_cmd(update, context)
    
    elif data == "cmd_live":
        if user_id in live_subscribers:
            live_subscribers.remove(user_id)
            remove_live_subscriber(user_id)
            await query.edit_message_text(
                get_text("live_alerts_off", lang),
                parse_mode="Markdown"
            )
        else:
            live_subscribers.add(user_id)
            add_live_subscriber(user_id)
            keyboard = [[InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]]
            await query.edit_message_text(
                get_text("live_alerts_on", lang),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )

    elif data == "ml_train":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        await query.edit_message_text("🔄 Запускаю обучение моделей...")

        results = train_all_models()

        if results:
            text = "✅ **Обучение завершено:**\n\n"
            for cat, info in results.items():
                text += f"• {cat}: {info['accuracy']:.1%} точность\n"
        else:
            text = "❌ Недостаточно данных для обучения.\nНужно минимум 100 проверенных прогнозов на категорию."

        keyboard = [[InlineKeyboardButton("🔙 ML статус", callback_data="cmd_mlstatus")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_mlstatus":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        status = get_ml_status()
        text = f"""🤖 **ML СИСТЕМА**

🔧 **Статус:**
├ ML доступен: {'✅' if status['ml_available'] else '❌'}
└ Мин. данных для обучения: {status['min_samples']}

"""
        if status["models"]:
            text += "🎯 **Обученные модели:**\n"
            for cat, info in status["models"].items():
                text += f"├ {cat}: {info['accuracy']:.1%} точность\n"
        else:
            text += "🎯 **Модели:** ещё не обучены\n"

        keyboard = [
            [InlineKeyboardButton("🔄 Обучить", callback_data="ml_train")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_admin":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return
        # Simplified admin panel for callback
        text = "👑 **АДМИН-ПАНЕЛЬ**\n\nИспользуй /admin для полной статистики"
        keyboard = [
            [InlineKeyboardButton("🤖 ML система", callback_data="cmd_mlstatus")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_broadcast":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return
        text = """📢 **Рассылка**

Чтобы отправить сообщение всем пользователям, используй команду:

`/broadcast Ваш текст сообщения`

Пример:
`/broadcast 🎉 Новая функция! Теперь доступны live-алерты!`"""
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_users":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            conn = get_db_connection()
            c = conn.cursor()

            # Get recent users
            c.execute("""
                SELECT user_id, username, is_premium, created_at
                FROM users
                ORDER BY COALESCE(created_at, '1970-01-01') DESC
                LIMIT 20
            """)
            users = c.fetchall()

            # Stats
            c.execute("SELECT COUNT(*) FROM users")
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
            premium = c.fetchone()[0]
            conn.close()

            text = f"👥 Пользователи ({total} всего, {premium} premium)\n\nПоследние 20:\n"
            for uid, uname, is_prem, created in users:
                prem_icon = "💎 " if is_prem else ""
                name = f"@{uname}" if uname else f"ID:{uid}"
                date = (created[:10] if created and len(created) >= 10 else "?") if created else "?"
                text += f"• {prem_icon}{name} ({date})\n"

            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Admin users error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data == "admin_sources" or data.startswith("admin_sources_filter_"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            conn = get_db_connection()
            c = conn.cursor()

            # Get stats by source
            c.execute("""
                SELECT
                    COALESCE(source, 'organic') as src,
                    COUNT(*) as total,
                    SUM(CASE WHEN is_premium = 1 THEN 1 ELSE 0 END) as premium_count
                FROM users
                GROUP BY src
                ORDER BY total DESC
            """)
            sources = c.fetchall()

            # Total users
            c.execute("SELECT COUNT(*) FROM users")
            total_users = c.fetchone()[0]
            conn.close()

            text = f"📈 **Статистика по источникам**\n\nВсего юзеров: {total_users}\n\n"

            keyboard_rows = []
            for src, count, prem in sources:
                pct = round(count / total_users * 100, 1) if total_users > 0 else 0
                prem_str = f" ({prem}💎)" if prem > 0 else ""
                # Escape underscores in source name for Markdown
                src_escaped = src.replace("_", "\\_")
                text += f"• **{src_escaped}**: {count} ({pct}%){prem_str}\n"
                # Add filter button for each source
                keyboard_rows.append([InlineKeyboardButton(
                    f"👥 {src} ({count})",
                    callback_data=f"admin_users_src_{src[:20]}"
                )])

            keyboard_rows.append([InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard_rows), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Admin sources error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data.startswith("admin_users_src_"):
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            source_filter = data.replace("admin_users_src_", "")
            conn = get_db_connection()
            c = conn.cursor()

            # Get users by source
            c.execute("""
                SELECT user_id, username, is_premium, created_at
                FROM users
                WHERE COALESCE(source, 'organic') = ?
                ORDER BY COALESCE(created_at, '1970-01-01') DESC
                LIMIT 20
            """, (source_filter,))
            users = c.fetchall()

            c.execute("SELECT COUNT(*) FROM users WHERE COALESCE(source, 'organic') = ?", (source_filter,))
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE COALESCE(source, 'organic') = ? AND is_premium = 1", (source_filter,))
            premium = c.fetchone()[0]
            conn.close()

            text = f"👥 Источник: {source_filter}\n({total} всего, {premium} premium)\n\nПоследние 20:\n"
            for uid, uname, is_prem, created in users:
                prem_icon = "💎 " if is_prem else ""
                name = f"@{uname}" if uname else f"ID:{uid}"
                date = (created[:10] if created and len(created) >= 10 else "?") if created else "?"
                text += f"• {prem_icon}{name} ({date})\n"

            keyboard = [[InlineKeyboardButton("🔙 К источникам", callback_data="admin_sources")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Admin users by source error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data == "admin_stats":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        conn = get_db_connection()
        c = conn.cursor()

        # Stats by bet type
        c.execute("""
            SELECT bet_type,
                   COUNT(*) as total,
                   SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct,
                   SUM(CASE WHEN is_correct = 0 THEN 1 ELSE 0 END) as wrong
            FROM predictions
            WHERE is_correct IS NOT NULL
            GROUP BY bet_type
            ORDER BY total DESC
        """)
        by_type = c.fetchall()

        # Stats by confidence range
        c.execute("""
            SELECT
                CASE
                    WHEN confidence >= 75 THEN '75%+'
                    WHEN confidence >= 70 THEN '70-74%'
                    WHEN confidence >= 65 THEN '65-69%'
                    ELSE '<65%'
                END as conf_range,
                COUNT(*) as total,
                SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END) as correct
            FROM predictions
            WHERE is_correct IS NOT NULL
            GROUP BY conf_range
            ORDER BY conf_range DESC
        """)
        by_conf = c.fetchall()

        # ROI calculation
        c.execute("""
            SELECT
                SUM(CASE WHEN is_correct = 1 THEN (odds - 1) ELSE -1 END) as profit,
                COUNT(*) as bets
            FROM predictions
            WHERE is_correct IS NOT NULL AND odds > 0
        """)
        roi_row = c.fetchone()
        profit = roi_row[0] or 0
        total_bets = roi_row[1] or 1
        roi = round(profit / total_bets * 100, 1)

        conn.close()

        text = f"""📊 **Детальная статистика**

**По типу ставки:**
"""
        for bet_type, total, correct, wrong in by_type:
            acc = round(correct / total * 100, 1) if total > 0 else 0
            text += f"• {bet_type}: {correct}/{total} ({acc}%)\n"

        text += f"""
**По уверенности:**
"""
        for conf_range, total, correct in by_conf:
            acc = round(correct / total * 100, 1) if total > 0 else 0
            text += f"• {conf_range}: {correct}/{total} ({acc}%)\n"

        text += f"""
**ROI:** {roi}% (profit: {profit:.1f} units)
"""

        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "cmd_admin":
        # Return to admin panel (simplified)
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return
        text = "👑 **АДМИН-ПАНЕЛЬ**\n\nИспользуй /admin для полной статистики"
        keyboard = [
            [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
             InlineKeyboardButton("👥 Юзеры", callback_data="admin_users")],
            [InlineKeyboardButton("📊 Детальная статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("🧹 Очистить дубликаты", callback_data="admin_clean_dups")],
            [InlineKeyboardButton("🔙 В меню", callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_accuracy":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        await query.edit_message_text("📊 Собираю статистику точности...")

        conn = get_db_connection()
        c = conn.cursor()

        text = "📊 **АНАЛИЗ ТОЧНОСТИ**\n" + "=" * 30 + "\n\n"

        # Overall stats
        c.execute("""
            SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions WHERE is_correct IS NOT NULL
        """)
        total, wins = c.fetchone()
        wins = wins or 0
        accuracy = round(wins / total * 100, 1) if total > 0 else 0

        # Industry verdict
        if accuracy >= 57:
            verdict = "🏆 ТОП-УРОВЕНЬ"
        elif accuracy >= 53:
            verdict = "✅ В ПЛЮСЕ"
        elif accuracy >= 50:
            verdict = "⚠️ БЕЗУБЫТОК"
        else:
            verdict = "❌ НУЖНА РАБОТА"

        text += f"🎯 **Общая:** {wins}/{total} = **{accuracy}%**\n"
        text += f"📈 **Оценка:** {verdict}\n\n"

        # By confidence
        text += "**По уверенности:**\n"
        c.execute("""
            SELECT
                CASE WHEN confidence >= 80 THEN '80%+' WHEN confidence >= 70 THEN '70-79%' ELSE '<70%' END,
                COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions WHERE is_correct IS NOT NULL AND confidence IS NOT NULL
            GROUP BY 1 ORDER BY 1 DESC
        """)
        for row in c.fetchall():
            conf, cnt, w = row
            w = w or 0
            acc = round(w / cnt * 100, 1) if cnt > 0 else 0
            emoji = "✅" if acc >= 55 else "⚠️" if acc >= 50 else "❌"
            text += f"├ {emoji} {conf}: **{acc}%** ({cnt})\n"

        # By category (top 5)
        text += "\n**Топ типы ставок:**\n"
        c.execute("""
            SELECT bet_category, COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions WHERE is_correct IS NOT NULL AND bet_category IS NOT NULL
            GROUP BY bet_category HAVING COUNT(*) >= 3
            ORDER BY (SUM(CASE WHEN is_correct = 1 THEN 1.0 ELSE 0 END) / COUNT(*)) DESC LIMIT 5
        """)
        cat_names = {"totals_over": "ТБ", "totals_under": "ТМ", "outcomes_home": "П1",
                     "outcomes_away": "П2", "btts": "ОЗ", "outcomes_draw": "X"}
        for row in c.fetchall():
            cat, cnt, w = row
            w = w or 0
            acc = round(w / cnt * 100, 1) if cnt > 0 else 0
            name = cat_names.get(cat, cat[:10] if cat else "?")
            emoji = "✅" if acc >= 55 else "⚠️"
            text += f"├ {emoji} {name}: **{acc}%** ({cnt})\n"

        # ROI
        c.execute("""
            SELECT SUM(CASE WHEN is_correct = 1 THEN odds - 1 ELSE -1 END), COUNT(*)
            FROM predictions WHERE is_correct IS NOT NULL AND odds IS NOT NULL
        """)
        row = c.fetchone()
        if row and row[1] and row[1] > 0:
            roi = round((row[0] or 0) / row[1] * 100, 1)
            emoji = "✅" if roi > 0 else "❌"
            text += f"\n💰 **ROI:** {emoji} **{roi}%**\n"

        conn.close()

        keyboard = [
            [InlineKeyboardButton("📋 Полный отчёт → /accuracy", callback_data="admin_accuracy_full")],
            [InlineKeyboardButton("🔙 В админ-панель", callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_accuracy_full":
        # Just tell user to use /accuracy command for full report
        await query.answer("Используй /accuracy для полного отчёта", show_alert=True)

    elif data == "admin_ml_stats":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            conn = get_db_connection()
            c = conn.cursor()

            # Total ML training samples
            c.execute("SELECT COUNT(*) FROM ml_training_data")
            total_samples = c.fetchone()[0]

            # Samples with known results (target is not NULL)
            c.execute("SELECT COUNT(*) FROM ml_training_data WHERE target IS NOT NULL")
            labeled_samples = c.fetchone()[0]

            # MAIN vs ALT stats
            c.execute("""
                SELECT
                    bet_rank,
                    COUNT(*) as total,
                    SUM(CASE WHEN target = 1 THEN 1 ELSE 0 END) as correct
                FROM ml_training_data
                WHERE target IS NOT NULL
                GROUP BY bet_rank
                ORDER BY bet_rank
            """)
            rank_stats = c.fetchall()

            # Stats by bet category
            c.execute("""
                SELECT
                    bet_category,
                    COUNT(*) as total,
                    SUM(CASE WHEN target = 1 THEN 1 ELSE 0 END) as correct
                FROM ml_training_data
                WHERE target IS NOT NULL
                GROUP BY bet_category
                ORDER BY total DESC
            """)
            category_stats = c.fetchall()

            conn.close()

            text = f"🤖 **ML СТАТИСТИКА**\n\n"
            text += f"📊 **Данные для обучения:**\n"
            text += f"├ Всего записей: {total_samples}\n"
            text += f"└ С результатами: {labeled_samples}\n\n"

            if rank_stats:
                text += f"⚡ **MAIN vs ALT точность:**\n"
                for rank, total, correct in rank_stats:
                    acc = round(correct / total * 100, 1) if total > 0 else 0
                    rank_name = "ОСНОВНАЯ" if rank == 1 else f"АЛЬТЕРНАТИВНАЯ"
                    emoji = "⚡" if rank == 1 else "📌"
                    text += f"{emoji} {rank_name}: {acc}% ({correct}/{total})\n"
                text += "\n"

            if category_stats:
                text += f"📈 **По типам ставок:**\n"
                for cat, total, correct in category_stats:
                    acc = round(correct / total * 100, 1) if total > 0 else 0
                    # Escape underscores to prevent Markdown parsing errors
                    cat_escaped = str(cat).replace("_", "\\_") if cat else "unknown"
                    text += f"• {cat_escaped}: {acc}% ({correct}/{total})\n"

            if total_samples == 0:
                text += "\n⚠️ Данных пока нет. ML начнёт собирать после новых прогнозов."
            elif labeled_samples < 50:
                text += f"\n⚠️ Мало данных ({labeled_samples}/50 мин). Модели ещё не обучаются."
            else:
                text += f"\n✅ Достаточно данных для обучения ML моделей!"

            keyboard = [
                [InlineKeyboardButton("🔄 Обучить модели", callback_data="ml_train"),
                 InlineKeyboardButton("🤖 ML система", callback_data="cmd_mlstatus")],
                [InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Admin ML stats error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data == "admin_live_status":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            # Get live alert diagnostics
            text = "🔔 **LIVE ALERTS ДИАГНОСТИКА**\n\n"

            # Subscribers count
            text += f"👥 **Подписчики:** {len(live_subscribers)}\n"
            if live_subscribers:
                text += f"   IDs: {', '.join(str(x) for x in list(live_subscribers)[:5])}"
                if len(live_subscribers) > 5:
                    text += f"... (+{len(live_subscribers)-5})"
                text += "\n\n"
            else:
                text += "   ⚠️ Нет подписчиков на live алерты!\n\n"

            # Recent sent alerts
            text += f"📤 **Отправленные алерты:** {len(sent_alerts)}\n"
            if sent_alerts:
                for match_id, sent_time in list(sent_alerts.items())[:5]:
                    time_ago = (datetime.now() - sent_time).total_seconds() / 60
                    text += f"   • Match {match_id}: {time_ago:.0f} мин назад\n"
            else:
                text += "   ⚠️ Нет алертов за последние 4 часа\n"
            text += "\n"

            # Check current matches in window
            matches = await get_matches(days=1)
            now = datetime.utcnow()
            upcoming_count = 0
            upcoming_matches = []

            if matches:
                for m in matches:
                    try:
                        match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
                        hours_until = (match_time - now).total_seconds() / 3600
                        if 0.5 < hours_until < 3:
                            upcoming_count += 1
                            home = m.get("homeTeam", {}).get("name", "?")[:15]
                            away = m.get("awayTeam", {}).get("name", "?")[:15]
                            upcoming_matches.append(f"{home} vs {away} ({hours_until:.1f}h)")
                    except:
                        continue

            text += f"⏰ **Матчи в окне 0.5-3ч:** {upcoming_count}\n"
            if upcoming_matches:
                for m in upcoming_matches[:5]:
                    text += f"   • {m}\n"
            else:
                text += "   ⚠️ Нет матчей в окне для алертов\n"
            text += "\n"

            # Alert requirements reminder
            text += "📋 **Требования для алерта:**\n"
            text += "   • Confidence ≥ 70%\n"
            text += "   • Odds ≥ 1.60\n"
            text += "   • ML не блокирует (conf ≥ 50%)\n"
            text += "   • Матч не был уже оповещён\n\n"

            # Job status check
            text += "⚙️ **Интервал проверки:** каждые 10 мин\n"

            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Admin live status error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    elif data == "admin_clean_dups":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return
        # Clean duplicate predictions
        result = clean_duplicate_predictions()
        if result["deleted"] > 0:
            text = f"""🧹 **Дубликаты очищены!**

├ Удалено прогнозов: {result['deleted']}
├ Затронуто матчей: {result['matches_affected']}
└ ML записей очищено: {result['orphaned_ml_cleaned']}

📊 Статистика теперь точная!"""
        else:
            text = "✅ Дубликатов не найдено!"

        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "admin_learning":
        if not is_admin(user_id):
            await query.edit_message_text("⛔ Только для администраторов")
            return

        try:
            learning = get_learning_stats()

            text = "🧠 СИСТЕМА САМООБУЧЕНИЯ\n\n"

            # Show totals for diagnostics
            totals = learning.get("totals", {})
            text += f"📈 Статус обучения:\n"
            text += f"├ Калибровка: {totals.get('calibration_samples', 0)} примеров в {totals.get('calibration_records', 0)} записях\n"
            text += f"└ Паттерны: {totals.get('pattern_samples', 0)} примеров в {totals.get('pattern_records', 0)} записях\n\n"

            # Calibration stats
            if learning["calibrations"]:
                text += "📊 Калибровка уверенности:\n"
                for cat, bands in learning["calibrations"].items():
                    # Escape underscores to prevent Markdown issues
                    cat_safe = cat.replace("_", " ")
                    text += f"  {cat_safe}:\n"
                    for band, data in bands.items():
                        emoji = "✅" if 0.9 <= data["calibration"] <= 1.1 else "⚠️"
                        text += f"    {emoji} {band}%: {data['rate']}% факт (x{data['calibration']}) [{data['count']}]\n"
                text += "\n"
            else:
                text += "📊 Калибровка: данных пока нет\n\n"

            # Best patterns
            if learning["best_patterns"]:
                text += "🏆 Лучшие паттерны:\n"
                for p in learning["best_patterns"][:3]:
                    pattern_short = p["pattern"].split(">")[0][:30].replace("_", " ").replace("|", " ")
                    text += f"✅ {pattern_short} → {p['rate']}% ({p['wins']}W/{p['losses']}L)\n"
                text += "\n"

            # Worst patterns
            if learning["worst_patterns"]:
                text += "⚠️ Худшие паттерны:\n"
                for p in learning["worst_patterns"][:3]:
                    pattern_short = p["pattern"].split(">")[0][:30].replace("_", " ").replace("|", " ")
                    text += f"❌ {pattern_short} → {p['rate']}% ({p['wins']}W/{p['losses']}L)\n"
                text += "\n"

            # ROI by category (NEW!)
            roi_data = learning.get("roi_by_category", {})
            if roi_data:
                text += "💰 ROI по категориям:\n"
                for cat, data in roi_data.items():
                    cat_safe = cat.replace("_", " ")
                    roi = data["roi"]
                    win_rate = data["win_rate"]
                    total = data["total"]
                    emoji = "🚀" if roi > 10 else "📈" if roi > 0 else "📉" if roi > -10 else "💸"
                    text += f"  {emoji} {cat_safe}: ROI {roi:+.1f}% | WR {win_rate:.0f}% | n={total}\n"
                text += "\n"

            # Recent learning events
            if learning["recent_learning"]:
                text += "📚 Последние события:\n"
                for e in learning["recent_learning"][:5]:
                    desc_safe = e['desc'][:50].replace("_", " ").replace("*", "")
                    text += f"• {desc_safe}...\n"
            else:
                text += "📚 События обучения: пока нет\n"

            text += "\n💡 Система учится с каждым проверенным прогнозом!"

            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_admin")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Admin learning stats error: {e}")
            await query.edit_message_text(f"❌ Ошибка: {e}")

    # League selection
    elif data.startswith("league_"):
        code = data.replace("league_", "")
        league_name = COMPETITIONS.get(code, code)
        await query.edit_message_text(get_text("loading", lang).format(name=league_name))
        matches = await get_matches(code, days=14)

        if not matches:
            await query.edit_message_text(get_text("no_matches_league", lang).format(name=league_name))
            return

        text = f"🏆 **{league_name}**\n\n"
        for m in matches[:10]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            try:
                dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                date_str = dt.strftime("%d.%m %H:%M")
            except:
                date_str = ""
            text += f"📅 {date_str}\n   {home} vs {away}\n\n"

        keyboard = [
            [InlineKeyboardButton(get_text("recommendations", lang), callback_data=f"rec_{code}")],
            [InlineKeyboardButton(get_text("back_to_leagues", lang), callback_data="cmd_leagues")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    # Recommendations for specific context
    elif data.startswith("rec_"):
        # Check limit
        can_use, _, use_bonus = check_daily_limit(user_id)
        if not can_use:
            text = get_limit_text(lang)
            keyboard = []
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
            return

        context_type = data.replace("rec_", "")
        await query.edit_message_text(get_text("analyzing", lang))

        if context_type == "today":
            matches = await get_matches(date_filter="today")
        elif context_type == "tomorrow":
            matches = await get_matches(date_filter="tomorrow")
        else:
            matches = await get_matches(context_type, days=14)

        if matches:
            user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
            recs = await get_recommendations_enhanced(matches, "", user, lang=lang, user_tz=user_tz)
            keyboard = []
            bet_btn = get_bet_button(user_id, lang)
            if bet_btn:
                keyboard.append(bet_btn)
            keyboard.append([InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")])
            increment_daily_usage(user_id)
            await query.edit_message_text(recs or get_text("no_matches", lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await query.edit_message_text(get_text("no_matches", lang))

    # Settings changes
    elif data == "set_min_odds":
        keyboard = [
            [InlineKeyboardButton("1.1", callback_data="min_1.1"),
             InlineKeyboardButton("1.3", callback_data="min_1.3"),
             InlineKeyboardButton("1.5", callback_data="min_1.5")],
            [InlineKeyboardButton("1.7", callback_data="min_1.7"),
             InlineKeyboardButton("2.0", callback_data="min_2.0"),
             InlineKeyboardButton("2.5", callback_data="min_2.5")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_settings")]
        ]
        await query.edit_message_text(get_text("select_min_odds", lang), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("min_"):
        value = float(data.replace("min_", ""))
        update_user_settings(user_id, min_odds=value)
        await query.answer(get_text("min_odds_set", lang).format(value=value))
        await settings_cmd(update, context)

    elif data == "set_max_odds":
        keyboard = [
            [InlineKeyboardButton("2.0", callback_data="max_2.0"),
             InlineKeyboardButton("2.5", callback_data="max_2.5"),
             InlineKeyboardButton("3.0", callback_data="max_3.0")],
            [InlineKeyboardButton("4.0", callback_data="max_4.0"),
             InlineKeyboardButton("5.0", callback_data="max_5.0"),
             InlineKeyboardButton("10.0", callback_data="max_10.0")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_settings")]
        ]
        await query.edit_message_text(get_text("select_max_odds", lang), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("max_"):
        value = float(data.replace("max_", ""))
        update_user_settings(user_id, max_odds=value)
        await query.answer(get_text("max_odds_set", lang).format(value=value))
        await settings_cmd(update, context)

    elif data == "set_risk":
        keyboard = [
            [InlineKeyboardButton("🟢 Low (safe)", callback_data="risk_low")],
            [InlineKeyboardButton("🟡 Medium (balanced)", callback_data="risk_medium")],
            [InlineKeyboardButton("🔴 High (aggressive)", callback_data="risk_high")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_settings")]
        ]
        await query.edit_message_text(get_text("select_risk", lang), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("risk_"):
        value = data.replace("risk_", "")
        update_user_settings(user_id, risk_level=value)
        await query.answer(get_text("risk_set", lang).format(value=value))
        await settings_cmd(update, context)

    elif data == "toggle_exclude_cups":
        current = user.get('exclude_cups', 0)
        new_value = 0 if current else 1
        update_user_settings(user_id, exclude_cups=new_value)
        confirm = {
            "ru": "✅ Кубки исключены" if new_value else "✅ Кубки включены",
            "en": "✅ Cups excluded" if new_value else "✅ Cups included",
            "pt": "✅ Copas excluídas" if new_value else "✅ Copas incluídas",
            "es": "✅ Copas excluidas" if new_value else "✅ Copas incluidas",
            "id": "✅ Piala dikecualikan" if new_value else "✅ Piala dimasukkan"
        }
        await query.answer(confirm.get(lang, confirm["ru"]))
        await settings_cmd(update, context)

    elif data == "set_language":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
             InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇧🇷 Português", callback_data="lang_pt"),
             InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es")],
            [InlineKeyboardButton("🇮🇩 Indonesia", callback_data="lang_id")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_settings")]
        ]
        await query.edit_message_text(get_text("select_language", lang), reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("lang_"):
        new_lang = data.replace("lang_", "")
        update_user_settings(user_id, language=new_lang)
        confirm = {
            "ru": "✅ Язык изменён на русский",
            "en": "✅ Language changed to English",
            "pt": "✅ Idioma alterado para português",
            "es": "✅ Idioma cambiado a español",
            "id": "✅ Bahasa diubah ke Indonesia"
        }
        await query.answer(confirm.get(new_lang, "✅"))
        
        # Send new keyboard
        await context.bot.send_message(
            chat_id=user_id,
            text=get_text("welcome", new_lang),
            reply_markup=get_main_keyboard(new_lang)
        )
        await settings_cmd(update, context)
    
    # Timezone selection
    elif data == "set_timezone":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Moscow", callback_data="tz_msk"),
             InlineKeyboardButton("🇺🇦 Kyiv", callback_data="tz_kiev")],
            [InlineKeyboardButton("🇬🇧 London", callback_data="tz_london"),
             InlineKeyboardButton("🇫🇷 Paris", callback_data="tz_paris")],
            [InlineKeyboardButton("🇹🇷 Istanbul", callback_data="tz_istanbul"),
             InlineKeyboardButton("🇦🇪 Dubai", callback_data="tz_dubai")],
            [InlineKeyboardButton("🇮🇳 Mumbai", callback_data="tz_mumbai"),
             InlineKeyboardButton("🇮🇩 Jakarta", callback_data="tz_jakarta")],
            [InlineKeyboardButton("🇵🇭 Manila", callback_data="tz_manila"),
             InlineKeyboardButton("🇧🇷 São Paulo", callback_data="tz_sao_paulo")],
            [InlineKeyboardButton("🇳🇬 Lagos", callback_data="tz_lagos"),
             InlineKeyboardButton("🇺🇸 New York", callback_data="tz_new_york")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_settings")]
        ]
        await query.edit_message_text(get_text("select_timezone", lang), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("tz_"):
        tz_key = data.replace("tz_", "")
        if tz_key in TIMEZONES:
            tz_value, tz_name = TIMEZONES[tz_key]
            update_user_settings(user_id, timezone=tz_value)
            await query.answer(f"✅ {tz_name}")
            await settings_cmd(update, context)

    # Add favorite league
    elif data == "add_fav_league":
        keyboard = [
            [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 PL", callback_data="fav_league_PL"),
             InlineKeyboardButton("🇪🇸 La Liga", callback_data="fav_league_PD"),
             InlineKeyboardButton("🇩🇪 BL", callback_data="fav_league_BL1")],
            [InlineKeyboardButton("🇮🇹 Serie A", callback_data="fav_league_SA"),
             InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="fav_league_FL1"),
             InlineKeyboardButton("🇪🇺 CL", callback_data="fav_league_CL")],
            [InlineKeyboardButton("🇧🇷 BSA", callback_data="fav_league_BSA")],
            [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_favorites")]
        ]
        await query.edit_message_text(get_text("select_league", lang), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("fav_league_"):
        code = data.replace("fav_league_", "")
        add_favorite_league(user_id, code)
        await query.answer(get_text("league_added", lang).format(name=COMPETITIONS.get(code, code)))
        await favorites_cmd(update, context)

    elif data.startswith("fav_team_"):
        team_name = data.replace("fav_team_", "")
        add_favorite_team(user_id, team_name)
        await query.answer(get_text("team_added", lang).format(name=team_name))

    elif data.startswith("analyze_match_"):
        # Analyze specific match (from hot match alerts)
        match_id = data.replace("analyze_match_", "")

        # Check daily limit (counts as analysis)
        can_use, _, use_bonus = check_daily_limit(user_id)
        if not can_use:
            text = get_limit_text(lang)
            keyboard = []
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
            return

        await query.edit_message_text(get_text("analyzing", lang))

        # Fetch match by ID
        try:
            session = await get_http_session()
            url = f"{FOOTBALL_API_URL}/matches/{match_id}"
            headers = {"X-Auth-Token": FOOTBALL_API_KEY}
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    match_data = await resp.json()
                    matches = [match_data]  # Wrap in list for get_recommendations_enhanced
                else:
                    matches = []
        except Exception as e:
            logger.error(f"Error fetching match {match_id}: {e}")
            matches = []

        if matches:
            user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
            # Localized query for match analysis
            analysis_query = {
                "ru": "Подробный анализ этого матча",
                "en": "Detailed analysis of this match",
                "pt": "Análise detalhada deste jogo",
                "es": "Análisis detallado de este partido",
                "id": "Analisis detail pertandingan ini"
            }.get(lang, "Detailed analysis of this match")
            recs = await get_recommendations_enhanced(matches, analysis_query, user, lang=lang, user_tz=user_tz)
            keyboard = []
            bet_btn = get_bet_button(user_id, lang)
            if bet_btn:
                keyboard.append(bet_btn)
            keyboard.append([InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")])
            increment_daily_usage(user_id)  # Count as usage
            await query.edit_message_text(recs or get_text("no_matches", lang), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await query.edit_message_text(get_text("no_matches", lang))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    user_text = update.message.text.strip()
    user_id = update.effective_user.id

    if len(user_text) < 2:
        return

    # Ensure user exists
    if not get_user(user_id):
        lang = detect_language(update.effective_user)
        is_new = create_user(user_id, update.effective_user.username, lang)
        if is_new:
            await notify_admins_new_user(
                context.bot,
                user_id,
                update.effective_user.username,
                lang,
                "organic"
            )

    user = get_user(user_id)
    lang = user.get("language", "ru")

    # Update user activity and streak
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET last_active = datetime('now') WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        # Update streak (once per day)
        streak_info = update_user_streak(user_id)
    except:
        pass

    # Handle keyboard buttons
    button_map = {
        get_text("top_bets", "ru"): recommend_cmd,
        get_text("top_bets", "en"): recommend_cmd,
        get_text("top_bets", "pt"): recommend_cmd,
        get_text("top_bets", "es"): recommend_cmd,
        get_text("matches", "ru"): today_cmd,
        get_text("matches", "en"): today_cmd,
        get_text("matches", "pt"): today_cmd,
        get_text("matches", "es"): today_cmd,
        get_text("stats", "ru"): stats_cmd,
        get_text("stats", "en"): stats_cmd,
        get_text("stats", "pt"): stats_cmd,
        get_text("stats", "es"): stats_cmd,
        get_text("favorites", "ru"): favorites_cmd,
        get_text("favorites", "en"): favorites_cmd,
        get_text("favorites", "pt"): favorites_cmd,
        get_text("favorites", "es"): favorites_cmd,
        get_text("premium_btn", "ru"): premium_cmd,
        get_text("premium_btn", "en"): premium_cmd,
        get_text("premium_btn", "pt"): premium_cmd,
        get_text("premium_btn", "es"): premium_cmd,
        get_text("settings", "ru"): settings_cmd,
        get_text("settings", "en"): settings_cmd,
        get_text("settings", "pt"): settings_cmd,
        get_text("settings", "es"): settings_cmd,
        get_text("help_btn", "ru"): help_cmd,
        get_text("help_btn", "en"): help_cmd,
        get_text("help_btn", "pt"): help_cmd,
        get_text("help_btn", "es"): help_cmd,
        # Referral button
        get_text("referral_btn", "ru"): referral_cmd,
        get_text("referral_btn", "en"): referral_cmd,
        get_text("referral_btn", "pt"): referral_cmd,
        get_text("referral_btn", "es"): referral_cmd,
    }

    if user_text in button_map:
        await button_map[user_text](update, context)
        return

    # Check for premium-related keywords
    premium_keywords = [
        "купить премиум", "премиум", "premium", "buy premium",
        "comprar premium", "подписка", "subscription", "оплата", "payment"
    ]
    if any(kw in user_text.lower() for kw in premium_keywords):
        await premium_cmd(update, context)
        return
    
    status = await update.message.reply_text(get_text("analyzing", lang))
    
    # Parse query
    parsed = parse_user_query(user_text)
    intent = parsed.get("intent", "unknown")
    teams = parsed.get("teams", [])
    league = parsed.get("league")
    
    logger.info(f"Parsed: intent={intent}, teams={teams}, league={league}")
    
    # Handle intents
    if intent == "greeting":
        keyboard = [
            [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend"),
             InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")]
        ]
        await status.edit_text(get_text("greeting_response", lang),
                               reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if intent == "help":
        await status.delete()
        await help_cmd(update, context)
        return
    
    if intent == "settings":
        await status.delete()
        await settings_cmd(update, context)
        return
    
    if intent == "favorites":
        await status.delete()
        await favorites_cmd(update, context)
        return
    
    if intent == "stats":
        await status.delete()
        await stats_cmd(update, context)
        return
    
    if intent == "today":
        await status.delete()
        await today_cmd(update, context)
        return
    
    if intent == "tomorrow":
        await status.delete()
        await tomorrow_cmd(update, context)
        return
    
    if intent == "recommend":
        # Check limit
        can_use, _, use_bonus = check_daily_limit(user_id)
        if not can_use:
            text = get_limit_text(lang)
            keyboard = []
            premium_btns = get_premium_buttons(user_id, lang)
            if premium_btns:
                keyboard.append(premium_btns)
            await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
            return

        await status.edit_text(get_text("analyzing_bets", lang))
        matches = await get_matches(days=7)
        if not matches:
            await status.edit_text(get_text("no_matches", lang))
            return
        user_tz = user.get("timezone", "Europe/Moscow") if user else "Europe/Moscow"
        recs = await get_recommendations_enhanced(matches, user_text, user, league, lang=lang, user_tz=user_tz)
        if recs:
            keyboard = []
            bet_btn = get_bet_button(user_id, lang)
            if bet_btn:
                keyboard.append(bet_btn)
            keyboard.append([InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")])
            increment_daily_usage(user_id)
            await status.edit_text(recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await status.edit_text(get_text("analysis_error", lang))
        return
    
    if intent == "matches_list":
        matches = await get_matches(league, days=14) if league else await get_matches(days=14)
        if not matches:
            await status.edit_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        text = get_text("upcoming_matches", lang) + "\n\n"
        for comp, ms in list(by_comp.items())[:5]:
            text += f"🏆 **{comp}**\n"
            for m in ms[:3]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                text += f"  • {home} vs {away}\n"
            text += "\n"
        
        keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # Team search - detailed analysis
    # Check limit first
    can_use, _, use_bonus = check_daily_limit(user_id)
    if not can_use:
        text = get_limit_text(lang)
        keyboard = []
        premium_btns = get_premium_buttons(user_id, lang)
        if premium_btns:
            keyboard.append(premium_btns)
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        return

    await status.edit_text(get_text("searching_match", lang))

    # Optimization: if Claude detected a league, search there first
    match = None
    matches = []

    if league:
        # Search in specific league first (fast - single API call)
        league_matches = await get_matches(competition=league, days=14)
        if league_matches:
            if teams:
                match = find_match(teams, league_matches)
            if not match:
                match = find_match([user_text], league_matches)
            matches = league_matches

    # If not found in specific league, try cached global matches
    if not match:
        # Use days=7 to leverage cache
        all_matches = await get_matches(days=7)
        if teams:
            match = find_match(teams, all_matches)
        if not match:
            match = find_match([user_text], all_matches)
        if not matches:
            matches = all_matches

    if not match:
        query = ', '.join(teams) if teams else user_text
        text = get_text("match_not_found", lang).format(query=query) + "\n\n"
        if matches:
            text += get_text("available_matches", lang) + "\n"
            for m in matches[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                text += f"  • {home} vs {away}\n"

        keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    # Found match - do enhanced analysis
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    comp = match.get("competition", {}).get("name", "?")
    match_id = match.get("id")

    await status.edit_text(get_text("match_found", lang).format(home=home, away=away, comp=comp))

    # Enhanced analysis - returns (text, ml_features)
    analysis, ml_features = await analyze_match_enhanced(match, user, lang)

    # Extract and save prediction - parse ONLY from MAIN BET section
    try:
        confidence = 70
        bet_type = "П1"
        odds_value = 1.5
        
        # Extract main bet section only
        main_bet_section = ""
        main_bet_match = re.search(r'ОСНОВНАЯ СТАВКА.*?(?=📈|ДОПОЛНИТЕЛЬНЫЕ|$)', analysis, re.DOTALL | re.IGNORECASE)
        if main_bet_match:
            main_bet_section = main_bet_match.group(0).lower()
        else:
            # Fallback - look for first bet mention
            main_bet_section = analysis[:500].lower()
        
        logger.info(f"Main bet section: {main_bet_section[:200]}")
        
        # Get confidence from main bet section
        conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', main_bet_section)
        if conf_match:
            confidence = int(conf_match.group(1))
        else:
            # Try full text
            conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', analysis)
            if conf_match:
                confidence = int(conf_match.group(1))
        
        # Detect bet type from main bet section ONLY
        # IMPORTANT: Check double chances FIRST (before single outcomes)
        
        # Double chance 1X (home or draw)
        if "п1 или х" in main_bet_section or "1x" in main_bet_section or "п1/х" in main_bet_section or "1 или х" in main_bet_section or "home or draw" in main_bet_section:
            bet_type = "1X"
        # Double chance X2 (draw or away)
        elif "х или п2" in main_bet_section or "x2" in main_bet_section or "2x" in main_bet_section or "х/п2" in main_bet_section or "draw or away" in main_bet_section:
            bet_type = "X2"
        # Double chance 12 (home or away, no draw)
        elif "п1 или п2" in main_bet_section or " 12 " in main_bet_section or "не ничья" in main_bet_section or "no draw" in main_bet_section:
            bet_type = "12"
        # Handicaps
        elif "фора" in main_bet_section or "handicap" in main_bet_section:
            # Parse handicap value
            fora_match = re.search(r'фора\s*[12]?\s*\(?([-+]?\d+\.?\d*)\)?', main_bet_section)
            if fora_match:
                fora_value = fora_match.group(1)
                if "-1" in main_bet_section or "(-1)" in main_bet_section:
                    bet_type = "Фора1(-1)"
                elif "+1" in main_bet_section or "(+1)" in main_bet_section:
                    bet_type = "Фора2(+1)"
                elif "-1.5" in main_bet_section:
                    bet_type = "Фора1(-1.5)"
                else:
                    bet_type = f"Фора({fora_value})"
            else:
                bet_type = "Фора1(-1)"
        elif "тб 2.5" in main_bet_section or "тотал больше 2.5" in main_bet_section or "over 2.5" in main_bet_section:
            bet_type = "ТБ 2.5"
        elif "тм 2.5" in main_bet_section or "тотал меньше 2.5" in main_bet_section or "under 2.5" in main_bet_section:
            bet_type = "ТМ 2.5"
        elif "обе забьют" in main_bet_section or "btts" in main_bet_section:
            bet_type = "BTTS"
        # Single outcomes (check AFTER double chances)
        elif "п2" in main_bet_section or "победа гостей" in main_bet_section:
            bet_type = "П2"
        elif "п1" in main_bet_section or "победа хозя" in main_bet_section:
            bet_type = "П1"
        elif "ничья" in main_bet_section or " х " in main_bet_section:
            bet_type = "Х"
        
        # Get odds from main bet section
        odds_match = re.search(r'@\s*~?(\d+\.?\d*)', main_bet_section)
        if odds_match:
            odds_value = float(odds_match.group(1))
        else:
            # Try full text
            odds_match = re.search(r'@\s*~?(\d+\.?\d*)', analysis)
            if odds_match:
                odds_value = float(odds_match.group(1))

        # COUNTER-CHECK: Validate totals predictions against expected goals
        totals_warning = None
        if "тб" in bet_type.lower() or "тм" in bet_type.lower():
            home_id = match.get("homeTeam", {}).get("id")
            away_id = match.get("awayTeam", {}).get("id")
            if home_id and away_id:
                home_form = await get_team_form(home_id)
                away_form = await get_team_form(away_id)
                bet_type, confidence, totals_warning = validate_totals_prediction(
                    bet_type, confidence, home_form, away_form
                )
                if totals_warning:
                    logger.warning(f"Totals counter-check triggered: {totals_warning}")
                    # Add warning to analysis
                    analysis = analysis + f"\n\n{totals_warning}"

        # Apply ML correction to confidence
        original_confidence = confidence
        ml_status = None
        ml_conf = None

        if ml_features:
            confidence, ml_status, ml_conf = apply_ml_correction(bet_type, confidence, ml_features)

            # Add ML status to analysis (localized)
            ml_texts = {
                "confirmed": {
                    "ru": f"🤖 **ML:** Подтверждено ({ml_conf:.0f}%)",
                    "en": f"🤖 **ML:** Confirmed ({ml_conf:.0f}%)",
                    "pt": f"🤖 **ML:** Confirmado ({ml_conf:.0f}%)",
                    "es": f"🤖 **ML:** Confirmado ({ml_conf:.0f}%)",
                    "id": f"🤖 **ML:** Dikonfirmasi ({ml_conf:.0f}%)"
                },
                "warning": {
                    "ru": f"⚠️ **ML:** Риск! Модель даёт только {ml_conf:.0f}%",
                    "en": f"⚠️ **ML:** Risk! Model gives only {ml_conf:.0f}%",
                    "pt": f"⚠️ **ML:** Risco! Modelo dá apenas {ml_conf:.0f}%",
                    "es": f"⚠️ **ML:** ¡Riesgo! Modelo da solo {ml_conf:.0f}%",
                    "id": f"⚠️ **ML:** Risiko! Model hanya {ml_conf:.0f}%"
                },
                "adjusted": {
                    "ru": f"📊 **ML:** Скорректировано {original_confidence}% → {confidence}%",
                    "en": f"📊 **ML:** Adjusted {original_confidence}% → {confidence}%",
                    "pt": f"📊 **ML:** Ajustado {original_confidence}% → {confidence}%",
                    "es": f"📊 **ML:** Ajustado {original_confidence}% → {confidence}%",
                    "id": f"📊 **ML:** Disesuaikan {original_confidence}% → {confidence}%"
                }
            }

            # ML adjustments applied internally, no display to user
            # (confidence already includes ML corrections)

        # Kelly Criterion calculated internally, not displayed
        # (confidence already reflects value assessment)

        bet_category = categorize_bet(bet_type)
        # Personalized advice applied internally through Smart Learning adjustments

        # Smart Learning adjustments already applied via apply_learning_adjustments()
        # No warnings displayed - confidence already reflects all factors

        # Extract league_code from features for learning system
        league_code = ml_features.get("league_code") if ml_features else None

        # Save MAIN prediction (bet_rank=1) with ML features
        match_time = match.get("utcDate") if match else None
        save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_value,
                        ml_features=ml_features, bet_rank=1, league_code=league_code, match_time=match_time)
        increment_daily_usage(user_id)
        logger.info(f"Saved MAIN: {home} vs {away}, {bet_type}, {confidence}%, odds={odds_value}, league={league_code}")

        # Parse and save ALTERNATIVE predictions (bet_rank=2,3,4) with same ML features
        alternatives = parse_alternative_bets(analysis)
        original_alt_count = len(alternatives)

        # Filter out any alternatives that match the main bet type
        alternatives = [(t, c, o) for t, c, o in alternatives if t and t != bet_type]

        if len(alternatives) < original_alt_count:
            logger.warning(f"Filtered out {original_alt_count - len(alternatives)} alt(s) that matched main bet {bet_type}")

        if len(alternatives) < 3:
            logger.warning(f"Only {len(alternatives)}/3 unique alternatives for {home} vs {away}")

        # Save each alternative with correct sequential bet_rank
        # Apply Smart Learning adjustments to alternatives too
        for alt_idx, (alt_type, alt_conf, alt_odds) in enumerate(alternatives[:3]):
            bet_rank = alt_idx + 2  # bet_rank 2, 3, 4

            # Apply learning adjustments to alternative confidence
            if ml_features:
                adjusted_alt_conf, alt_adjustments = apply_learning_adjustments(alt_type, alt_conf, ml_features)
                if alt_adjustments:
                    logger.info(f"ALT{alt_idx+1} adjustments: {alt_conf}% → {adjusted_alt_conf}% ({', '.join(alt_adjustments[:2])})")
                alt_conf = adjusted_alt_conf

            save_prediction(user_id, match_id, home, away, alt_type, alt_conf, alt_odds,
                            ml_features=ml_features, bet_rank=bet_rank, league_code=league_code, match_time=match_time)
            logger.info(f"Saved ALT{alt_idx+1}: {home} vs {away}, {alt_type}, {alt_conf}%, odds={alt_odds}")

    except Exception as e:
        logger.error(f"Error saving prediction: {e}")

    header = f"⚽ **{home}** vs **{away}**\n🏆 {comp}\n{'─'*30}\n\n"

    keyboard = []
    bet_btn = get_bet_button(user_id, lang)
    if bet_btn:
        keyboard.append(bet_btn)
    keyboard.append([InlineKeyboardButton(f"⭐ {home}", callback_data=f"fav_team_{home}"),
         InlineKeyboardButton(f"⭐ {away}", callback_data=f"fav_team_{away}")])
    keyboard.append([InlineKeyboardButton("📊 Ещё рекомендации", callback_data="cmd_recommend")])

    # Try Markdown first, fallback to plain text if parsing fails
    try:
        await status.edit_text(header + analysis, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Markdown error in analysis, using plain text: {e}")
        # Remove ** for plain text
        plain_header = f"⚽ {home} vs {away}\n🏆 {comp}\n{'─'*30}\n\n"
        plain_analysis = analysis.replace("**", "")
        try:
            await status.edit_text(plain_header + plain_analysis, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e2:
            logger.error(f"Plain text also failed: {e2}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")


# ===== LIVE ALERTS SYSTEM =====

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle live alerts subscription (with DB persistence)"""
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    lang = user_data.get("language", "ru") if user_data else "ru"

    if user_id in live_subscribers:
        live_subscribers.remove(user_id)
        remove_live_subscriber(user_id)  # Save to DB
        await update.message.reply_text(
            get_text("live_alerts_off", lang),
            parse_mode="Markdown"
        )
    else:
        live_subscribers.add(user_id)
        add_live_subscriber(user_id)  # Save to DB
        await update.message.reply_text(
            get_text("live_alerts_on", lang),
            parse_mode="Markdown"
        )


async def testalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test alert - manually trigger check"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    await update.message.reply_text(get_text("analyzing", lang))
    
    was_subscribed = user_id in live_subscribers
    live_subscribers.add(user_id)
    
    matches = await get_matches(days=1, use_cache=False)
    
    if not matches:
        await update.message.reply_text(get_text("no_matches", lang))
        if not was_subscribed:
            live_subscribers.discard(user_id)
        return
    
    now = datetime.now()
    upcoming = []
    all_today = []
    
    for m in matches:
        try:
            match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            hours_until = (match_time - now).total_seconds() / 3600
            
            if hours_until > 0:
                all_today.append((m, hours_until))
                if 0.5 < hours_until < 3:
                    upcoming.append(m)
        except:
            continue
    
    text = f"📊 **Статус алертов:**\n\n"
    text += f"🔔 Подписчики: {len(live_subscribers)}\n"
    text += f"📅 Матчей сегодня: {len(matches)}\n"
    text += f"⏰ В окне 0.5-3ч: {len(upcoming)}\n\n"
    
    if all_today:
        text += "**Ближайшие матчи:**\n"
        for m, hours in sorted(all_today, key=lambda x: x[1])[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            in_window = "✅" if 0.5 < hours < 3 else "⏳"
            text += f"{in_window} {home} vs {away} (через {hours:.1f}ч)\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")
    
    if not was_subscribed:
        live_subscribers.discard(user_id)


async def check_results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually check prediction results"""
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔄 Проверяю результаты...")
    
    pending = get_pending_predictions()
    user_pending = [p for p in pending if p.get("user_id") == user_id]
    
    if not user_pending:
        await update.message.reply_text("✅ Нет прогнозов, ожидающих результата.")
        return
    
    text = f"📊 **Твои прогнозы ({len(user_pending)}):**\n\n"
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    checked = 0
    
    for pred in user_pending[:5]:
        match_id = pred.get("match_id")
        home = pred.get("home", "?")
        away = pred.get("away", "?")
        bet_type = pred.get("bet_type", "?")
        
        text += f"⚽ {home} vs {away}\n"
        text += f"   📊 Ставка: {bet_type}\n"
        
        if not match_id:
            text += f"   ⚠️ Нет match_id\n\n"
            continue
        
        try:
            url = f"{FOOTBALL_API_URL}/matches/{match_id}"
            session = await get_http_session()
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    text += f"   ⚠️ API error\n\n"
                    continue

                match_data = await r.json()
            status = match_data.get("status")
            
            if status == "FINISHED":
                score = match_data.get("score", {}).get("fullTime", {})
                home_score = score.get("home", 0)
                away_score = score.get("away", 0)
                
                is_correct = check_bet_result(bet_type, home_score, away_score)
                
                if is_correct is not None:
                    result_str = f"{home_score}:{away_score}"
                    update_prediction_result(pred["id"], result_str, 1 if is_correct else 0)
                    
                    emoji = "✅" if is_correct else "❌"
                    text += f"   {emoji} Результат: {result_str}\n"
                    checked += 1
            else:
                text += f"   ⏳ Матч не завершён\n"
            
            text += "\n"
            await asyncio.sleep(0.5)
            
        except Exception as e:
            text += f"   ❌ Ошибка\n\n"
    
    text += f"✅ Обновлено: {checked} прогнозов\nНапиши /stats для статистики"

    await update.message.reply_text(text, parse_mode="Markdown")


async def force_check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to force-check ALL pending predictions (regardless of age)"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    await update.message.reply_text("🔄 Запускаю полную проверку ВСЕХ pending predictions...")

    # Get ALL pending predictions (no time limit)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""SELECT id, user_id, match_id, home_team, away_team, bet_type, confidence, odds, bet_rank,
                        predicted_at
                 FROM predictions
                 WHERE is_correct IS NULL""")
    rows = c.fetchall()
    conn.close()

    pending = [{"id": r[0], "user_id": r[1], "match_id": r[2], "home": r[3],
                "away": r[4], "bet_type": r[5], "confidence": r[6], "odds": r[7],
                "bet_rank": r[8] if len(r) > 8 else 1, "predicted_at": r[9]} for r in rows]

    if not pending:
        await update.message.reply_text("✅ Нет pending predictions!")
        return

    # Group by match_id to avoid duplicate API calls
    from collections import defaultdict
    by_match = defaultdict(list)
    without_match_id = 0
    for p in pending:
        if p.get("match_id"):
            by_match[p["match_id"]].append(p)
        else:
            without_match_id += 1

    unique_matches = len(by_match)

    status_msg = f"""📊 Найдено {len(pending)} pending predictions:
├ Уникальных матчей: {unique_matches}
└ Без match_id: {without_match_id} (невозможно проверить)

🔄 Проверяю (по {unique_matches} матчам)..."""

    await update.message.reply_text(status_msg)

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    matches_checked = 0
    predictions_updated = 0
    errors = 0
    not_finished = 0

    # Process unique matches (not individual predictions)
    match_ids = list(by_match.keys())
    for i, match_id in enumerate(match_ids):
        preds = by_match[match_id]

        try:
            url = f"{FOOTBALL_API_URL}/matches/{match_id}"
            session = await get_http_session()

            # Add timeout
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as r:
                    if r.status == 429:
                        # Rate limited - wait and continue
                        logger.warning(f"Rate limited at match {match_id}")
                        await update.message.reply_text(f"⚠️ Rate limited, ждём 5 сек...")
                        await asyncio.sleep(5)
                        continue
                    elif r.status != 200:
                        errors += 1
                        logger.warning(f"API error {r.status} for match {match_id}")
                        continue

                    match_data = await r.json()

            status = match_data.get("status")

            if status == "FINISHED":
                score = match_data.get("score", {}).get("fullTime", {})
                home_score = score.get("home", 0) or 0
                away_score = score.get("away", 0) or 0
                result_str = f"{home_score}-{away_score}"

                # Update ALL predictions for this match
                for pred in preds:
                    try:
                        is_correct = check_bet_result(pred["bet_type"], home_score, away_score)

                        if is_correct is True:
                            db_value = 1
                        elif is_correct is False:
                            db_value = 0
                        else:
                            db_value = 2  # Push

                        update_prediction_result(pred["id"], result_str, db_value)
                        predictions_updated += 1
                    except Exception as e:
                        logger.error(f"Error updating pred {pred['id']}: {e}")
                        errors += 1
            else:
                not_finished += len(preds)

            matches_checked += 1
            await asyncio.sleep(0.5)  # Rate limit

            # Progress update every 10 matches
            if (i + 1) % 10 == 0:
                await update.message.reply_text(
                    f"⏳ Проверено {matches_checked}/{unique_matches} матчей, обновлено {predictions_updated} прогнозов..."
                )

        except asyncio.TimeoutError:
            errors += 1
            logger.error(f"Timeout for match {match_id}")
        except Exception as e:
            errors += 1
            logger.error(f"Force check error for {match_id}: {e}")

    result_text = f"""✅ **Проверка завершена!**

📊 **Результаты:**
├ Матчей проверено: {matches_checked}/{unique_matches}
├ Прогнозов обновлено: {predictions_updated}
├ Ещё не завершены: {not_finished}
├ Ошибок: {errors}
└ Без match_id: {without_match_id}

💡 Теперь /stats покажет обновлённую статистику"""

    try:
        await update.message.reply_text(result_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Markdown error in forcecheck: {e}")
        await update.message.reply_text(result_text)


async def jobstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to check status of background jobs and pending predictions"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    conn = get_db_connection()
    c = conn.cursor()

    # Count pending predictions
    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NULL")
    pending_total = c.fetchone()[0]

    # Pending by age
    c.execute("""SELECT COUNT(*) FROM predictions
                 WHERE is_correct IS NULL AND predicted_at > datetime('now', '-1 day')""")
    pending_1d = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM predictions
                 WHERE is_correct IS NULL AND predicted_at > datetime('now', '-7 days')""")
    pending_7d = c.fetchone()[0]

    # Count by match_id presence
    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NULL AND match_id IS NULL")
    without_match_id = c.fetchone()[0]

    # Get last checked prediction time
    c.execute("SELECT MAX(checked_at) FROM predictions WHERE checked_at IS NOT NULL")
    last_check = c.fetchone()[0] or "Never"

    # Get sample pending matches
    c.execute("""SELECT DISTINCT match_id, home_team, away_team, predicted_at
                 FROM predictions
                 WHERE is_correct IS NULL AND match_id IS NOT NULL
                 ORDER BY predicted_at DESC LIMIT 5""")
    sample_matches = c.fetchall()

    # Verified stats
    c.execute("SELECT COUNT(*) FROM predictions WHERE is_correct IS NOT NULL")
    verified = c.fetchone()[0]

    c.execute("""SELECT COUNT(*) FROM predictions
                 WHERE is_correct = 1""")
    wins = c.fetchone()[0] or 0

    conn.close()

    accuracy = round(wins / verified * 100, 1) if verified > 0 else 0

    text = f"""🔧 **СТАТУС СИСТЕМЫ**
═══════════════════════════

📊 **Pending Predictions:**
├ Всего: {pending_total}
├ За 24ч: {pending_1d}
├ За 7 дней: {pending_7d} (проверяются автоматически)
├ Без match_id: {without_match_id} ⚠️
└ Последняя проверка: {last_check}

✅ **Проверенные:**
├ Всего: {verified}
├ Правильных: {wins}
└ Точность: {accuracy}%

⏰ **Background Jobs:**
├ check_predictions_results: каждые 20 мин
├ track_upcoming_odds: каждые 30 мин
├ check_live_matches: каждые 10 мин
└ send_daily_digest: каждые 2 часа

"""

    if sample_matches:
        text += "📋 Последние pending матчи:\n"
        for m in sample_matches:
            match_id, home, away, pred_at = m
            # Escape underscores for Markdown
            home_safe = str(home).replace("_", " ") if home else "?"
            away_safe = str(away).replace("_", " ") if away else "?"
            pred_at_safe = str(pred_at)[:16] if pred_at else "?"
            text += f"• {home_safe} vs {away_safe}\n  ID: {match_id} | {pred_at_safe}\n"

    text += """
💡 Команды:
/forcecheck — проверить все pending (без уведомлений)
/forceresults — проверить И отправить уведомления
/accuracy — детальная статистика
"""

    try:
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Markdown error in jobstatus: {e}")
        # Fallback to plain text
        await update.message.reply_text(text.replace("**", "").replace("_", ""))


async def forceresults_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force check results AND send notifications to users (like auto job but manual)"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    status_msg = await update.message.reply_text(
        "🔄 Принудительная проверка результатов\n\n"
        "⏳ Собираю pending predictions..."
    )

    # Get pending predictions (same as auto job)
    pending = get_pending_predictions()

    if not pending:
        await status_msg.edit_text("✅ Нет pending predictions для проверки!")
        return

    # Group by (user_id, match_id)
    from collections import defaultdict
    grouped = defaultdict(list)
    no_match_id = 0

    for pred in pending:
        if not pred.get("match_id"):
            no_match_id += 1
            continue
        if pred.get("user_id", 0) > 0:
            key = (pred["user_id"], pred["match_id"])
            grouped[key].append(pred)

    try:
        await status_msg.edit_text(
            f"🔄 Принудительная проверка результатов\n\n"
            f"📊 Найдено: {len(pending)} predictions\n"
            f"├ Групп (user+match): {len(grouped)}\n"
            f"└ Без match_id: {no_match_id}\n\n"
            f"⏳ Проверяю матчи..."
        )
    except Exception:
        pass

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    match_results = {}

    processed = 0
    notified = 0
    not_finished = 0
    errors = 0

    for (uid, match_id), preds in list(grouped.items())[:50]:
        try:
            # Get match result (cache within this run)
            if match_id not in match_results:
                url = f"{FOOTBALL_API_URL}/matches/{match_id}"
                session = await get_http_session()
                async with session.get(url, headers=headers) as r:
                    if r.status == 200:
                        match_results[match_id] = await r.json()
                    elif r.status == 429:
                        await asyncio.sleep(3)
                        continue
                    else:
                        errors += 1
                        continue
                await asyncio.sleep(0.3)

            match = match_results.get(match_id)
            if not match:
                continue

            if match.get("status") != "FINISHED":
                not_finished += len(preds)
                continue

            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home", 0) or 0
            away_score = score.get("away", 0) or 0
            result = f"{home_score}-{away_score}"

            # Get user language
            user_data = get_user(uid)
            lang = user_data.get("language", "ru") if user_data else "ru"

            # Process predictions and build message
            preds.sort(key=lambda x: x.get("bet_rank", 1))

            main_line = ""
            alt_lines = []
            main_bet_type = None
            main_is_correct = None
            main_pred_id = None

            for pred in preds:
                is_correct = check_bet_result(pred["bet_type"], home_score, away_score)

                if is_correct is True:
                    db_value = 1
                    emoji = "✅"
                elif is_correct is False:
                    db_value = 0
                    emoji = "❌"
                else:
                    db_value = 2
                    emoji = "🔄"

                update_prediction_result(pred["id"], result, db_value)
                processed += 1

                bet_rank = pred.get("bet_rank", 1)
                if bet_rank == 1:
                    main_line = f"{emoji} **{pred['bet_type']}** ({pred.get('confidence', 0)}%)"
                    main_bet_type = pred["bet_type"]
                    main_is_correct = is_correct
                    main_pred_id = pred["id"]
                else:
                    alt_lines.append(f"{emoji} {pred['bet_type']} ({pred.get('confidence', 0)}%)")

            # Generate Claude explanation
            explanation = ""
            if main_bet_type and main_is_correct is not None and main_pred_id:
                try:
                    explanation = await generate_claude_result_explanation(
                        prediction_id=main_pred_id,
                        match_data=match,
                        bet_type=main_bet_type,
                        is_correct=main_is_correct is True,
                        home_score=home_score,
                        away_score=away_score,
                        lang=lang
                    )
                except Exception as e:
                    logger.error(f"Explanation error: {e}")

            # Send notification to user
            try:
                # Escape team names for Markdown
                home_safe = str(preds[0].get('home', '?')).replace("_", " ").replace("*", "")
                away_safe = str(preds[0].get('away', '?')).replace("_", " ").replace("*", "")

                msg = f"📊 Результат матча\n\n"
                msg += f"⚽ {home_safe} vs {away_safe}\n"
                msg += f"📈 Счёт: {result}\n\n"

                if main_line:
                    msg += f"{main_line}\n"
                if alt_lines:
                    msg += "\n".join(alt_lines) + "\n"
                if explanation:
                    msg += f"\n{explanation}"

                try:
                    await context.bot.send_message(
                        chat_id=uid,
                        text=msg,
                        parse_mode="Markdown"
                    )
                except Exception:
                    # Fallback to plain text
                    await context.bot.send_message(
                        chat_id=uid,
                        text=msg.replace("**", "").replace("_", " ")
                    )
                notified += 1
            except Exception as e:
                logger.warning(f"Failed to notify user {uid}: {e}")

        except Exception as e:
            errors += 1
            logger.error(f"Force results error: {e}")

        # Progress update
        if processed > 0 and processed % 20 == 0:
            try:
                await status_msg.edit_text(
                    f"🔄 Проверка результатов\n\n"
                    f"⏳ Обработано: {processed}\n"
                    f"📤 Уведомлений: {notified}"
                )
            except Exception:
                pass

    final_text = f"""✅ Проверка завершена!

📊 Результаты:
├ Predictions обновлено: {processed}
├ Уведомлений отправлено: {notified}
├ Матчи не завершены: {not_finished}
├ Ошибок: {errors}
└ Без match_id: {no_match_id}

💡 /accuracy для статистики"""

    try:
        await status_msg.edit_text(final_text, parse_mode="Markdown")
    except Exception:
        await status_msg.edit_text(final_text)


async def analyze_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to analyze ALL today's matches - SAVES everything to DB with ML features"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    # Get today's matches
    await update.message.reply_text("🔄 Загружаю все матчи на сегодня...")

    matches = await get_matches(date_filter="today", use_cache=False)

    if not matches:
        await update.message.reply_text("❌ Нет матчей на сегодня!")
        return

    # Sort by time
    matches.sort(key=lambda x: x.get("utcDate", ""))

    total = len(matches)
    await update.message.reply_text(f"""📊 **Массовый анализ матчей (ПОЛНЫЙ)**

├ Всего матчей: {total}
├ Сохранение: ✅ predictions + ML features + alternatives
├ Пауза между анализами: 3 сек
└ Примерное время: {total * 25 // 60} минут

🔄 Начинаю анализ...""", parse_mode="Markdown")

    # Track results
    results = {
        "analyzed": 0,
        "main_saved": 0,
        "alts_saved": 0,
        "good_bets": [],
        "errors": 0,
        "skipped": 0
    }

    progress_msg = await update.message.reply_text("⏳ Прогресс: 0%")

    for i, match in enumerate(matches):
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        match_id = match.get("id")
        comp = match.get("competition", {}).get("name", "?")
        comp_code = match.get("competition", {}).get("code", "")
        match_utc_date = match.get("utcDate", "")  # Full ISO format for DB
        match_time = match_utc_date[:16].replace("T", " ") if match_utc_date else ""  # Display format

        try:
            # Run full analysis with ML features
            analysis, ml_features = await analyze_match_enhanced(match, None, "ru")

            if analysis and "AI unavailable" not in analysis:
                results["analyzed"] += 1

                # === PARSE MAIN BET (same logic as regular flow) ===
                confidence = 65
                bet_type = "П1"
                odds_value = 1.5

                # Extract main bet section
                main_bet_match = re.search(r'ОСНОВНАЯ СТАВКА.*?(?=📈|АЛЬТЕРНАТИВ|ДОПОЛНИТЕЛЬНЫЕ|$)', analysis, re.DOTALL | re.IGNORECASE)
                if main_bet_match:
                    main_bet_section = main_bet_match.group(0).lower()
                else:
                    main_bet_section = analysis[:500].lower()

                # Get confidence
                conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', main_bet_section)
                if conf_match:
                    confidence = int(conf_match.group(1))
                else:
                    conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', analysis)
                    if conf_match:
                        confidence = int(conf_match.group(1))

                # Detect bet type (SAME ORDER as regular flow - double chances first!)
                if "п1 или х" in main_bet_section or "1x" in main_bet_section or "п1/х" in main_bet_section:
                    bet_type = "1X"
                elif "х или п2" in main_bet_section or "x2" in main_bet_section or "2x" in main_bet_section or "х/п2" in main_bet_section:
                    bet_type = "X2"
                elif "п1 или п2" in main_bet_section or " 12 " in main_bet_section or "не ничья" in main_bet_section:
                    bet_type = "12"
                elif "фора" in main_bet_section or "handicap" in main_bet_section:
                    fora_match = re.search(r'фора\s*[12]?\s*\(?([-+]?\d+\.?\d*)\)?', main_bet_section)
                    if fora_match:
                        fora_value = fora_match.group(1)
                        if "-1" in main_bet_section:
                            bet_type = "Фора1(-1)"
                        elif "+1" in main_bet_section:
                            bet_type = "Фора2(+1)"
                        else:
                            bet_type = f"Фора({fora_value})"
                    else:
                        bet_type = "Фора1(-1)"
                elif "тб 2.5" in main_bet_section or "тотал больше 2.5" in main_bet_section or "over 2.5" in main_bet_section:
                    bet_type = "ТБ 2.5"
                elif "тм 2.5" in main_bet_section or "тотал меньше 2.5" in main_bet_section or "under 2.5" in main_bet_section:
                    bet_type = "ТМ 2.5"
                elif "обе забьют" in main_bet_section or "btts" in main_bet_section:
                    bet_type = "BTTS"
                elif "п2" in main_bet_section or "победа гостей" in main_bet_section:
                    bet_type = "П2"
                elif "п1" in main_bet_section or "победа хозя" in main_bet_section:
                    bet_type = "П1"
                elif "ничья" in main_bet_section or " х " in main_bet_section:
                    bet_type = "Х"

                # Get odds
                odds_match = re.search(r'@\s*~?(\d+\.?\d*)', main_bet_section)
                if odds_match:
                    odds_value = float(odds_match.group(1))
                else:
                    odds_match = re.search(r'@\s*~?(\d+\.?\d*)', analysis)
                    if odds_match:
                        odds_value = float(odds_match.group(1))

                # Get league code from ML features
                league_code = ml_features.get("league_code") if ml_features else comp_code

                # === SAVE MAIN PREDICTION ===
                try:
                    save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_value,
                                    ml_features=ml_features, bet_rank=1, league_code=league_code, match_time=match_utc_date)
                    results["main_saved"] += 1
                    logger.info(f"[BATCH] Saved MAIN: {home} vs {away}, {bet_type}, {confidence}%")
                except Exception as e:
                    logger.error(f"[BATCH] Error saving main prediction: {e}")

                # === PARSE AND SAVE ALTERNATIVES ===
                alternatives = parse_alternative_bets(analysis)
                alternatives = [(t, c, o) for t, c, o in alternatives if t and t != bet_type][:3]

                for alt_idx, (alt_type, alt_conf, alt_odds) in enumerate(alternatives):
                    try:
                        bet_rank = alt_idx + 2
                        save_prediction(user_id, match_id, home, away, alt_type, alt_conf, alt_odds,
                                        ml_features=ml_features, bet_rank=bet_rank, league_code=league_code, match_time=match_utc_date)
                        results["alts_saved"] += 1
                    except Exception as e:
                        logger.error(f"[BATCH] Error saving alt: {e}")

                # Track good bets for summary
                if confidence >= 65:
                    results["good_bets"].append({
                        "match": f"{home} vs {away}",
                        "comp": comp,
                        "time": match_time,
                        "bet": bet_type,
                        "conf": confidence,
                        "alts": len(alternatives)
                    })
            else:
                results["skipped"] += 1

        except Exception as e:
            logger.error(f"[BATCH] Error analyzing {home} vs {away}: {e}")
            results["errors"] += 1

        # Update progress every 5 matches
        if (i + 1) % 5 == 0 or i == total - 1:
            pct = int((i + 1) / total * 100)
            # Escape team names for Telegram
            home_safe = home.replace("_", " ").replace("*", "")[:30]
            away_safe = away.replace("_", " ").replace("*", "")[:30]
            try:
                await progress_msg.edit_text(
                    f"⏳ Прогресс: {pct}% ({i+1}/{total})\n"
                    f"├ Сохранено: {results['main_saved']} main + {results['alts_saved']} alts\n"
                    f"└ Последний: {home_safe} vs {away_safe}"
                )
            except Exception as e:
                logger.warning(f"Progress update failed: {e}")

        # Delay between analyses to avoid rate limiting
        if i < total - 1:
            await asyncio.sleep(3)

    # Build summary
    summary = f"""✅ **Массовый анализ завершён!**

📊 **Статистика:**
├ Проанализировано: {results['analyzed']}/{total}
├ Сохранено MAIN: {results['main_saved']}
├ Сохранено ALT: {results['alts_saved']}
├ Хороших ставок (≥65%): {len(results['good_bets'])}
├ Пропущено: {results['skipped']}
└ Ошибок: {results['errors']}

💾 Все predictions сохранены в БД!
📬 Алерты придут автоматически по результатам матчей

"""

    if results["good_bets"]:
        results["good_bets"].sort(key=lambda x: x["conf"], reverse=True)

        summary += "🎯 **ТОП ставки дня:**\n"
        for i, bet in enumerate(results["good_bets"][:20], 1):
            alts_info = f" +{bet['alts']}alt" if bet.get('alts', 0) > 0 else ""
            summary += f"{i}. **{bet['conf']}%** {bet['bet']}{alts_info} - {bet['match']}\n"

    # Send summary (may need to split if too long)
    try:
        if len(summary) > 4000:
            # Split into parts
            parts = [summary[i:i+4000] for i in range(0, len(summary), 4000)]
            for part in parts:
                await update.message.reply_text(part, parse_mode="Markdown")
        else:
            await update.message.reply_text(summary, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Markdown error in analyzeall summary: {e}")
        plain_summary = summary.replace("**", "")
        await update.message.reply_text(plain_summary)

    # Clean up progress message
    try:
        await progress_msg.delete()
    except:
        pass


async def check_live_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check upcoming matches and send alerts"""
    global sent_alerts

    if not live_subscribers:
        return

    logger.info(f"Checking live for {len(live_subscribers)} subscribers...")

    matches = await get_matches(days=1)

    if not matches:
        return

    now = datetime.utcnow()  # Use UTC to match API times
    upcoming = []

    # Clean up old sent_alerts (matches that started more than 4 hours ago)
    expired_alerts = [mid for mid, sent_time in sent_alerts.items()
                      if (now - sent_time).total_seconds() > 14400]  # 4 hours
    for mid in expired_alerts:
        del sent_alerts[mid]

    for m in matches:
        try:
            match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            hours_until = (match_time - now).total_seconds() / 3600

            if 0.5 < hours_until < 3:
                upcoming.append(m)
        except:
            continue

    if not upcoming:
        logger.info("No matches in 0.5-3h window")
        return

    logger.info(f"Found {len(upcoming)} matches in 0.5-3h window")

    logger.info(f"Found {len(upcoming)} upcoming matches, already alerted: {len(sent_alerts)}")

    for match in upcoming[:5]:  # Check up to 5 matches
        match_id = match.get("id")  # Get match ID for tracking

        # Skip if already sent alert for this match
        if match_id and match_id in sent_alerts:
            continue

        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        comp_code = match.get("competition", {}).get("code", "PL")
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")

        # Parse match date for accurate rest days calculation
        match_date = None
        match_date_str = match.get("utcDate", "")
        if match_date_str:
            try:
                match_date = datetime.fromisoformat(match_date_str.replace("Z", "+00:00"))
            except:
                pass

        # Use enhanced form for ML features with match date for rest days
        home_form_enhanced = await get_team_form_enhanced(home_id, upcoming_match_date=match_date) if home_id else None
        away_form_enhanced = await get_team_form_enhanced(away_id, upcoming_match_date=match_date) if away_id else None
        odds = await get_odds(home, away)
        h2h = await get_h2h(match_id) if match_id else None
        standings = await get_standings(comp_code)

        # Calculate congestion and motivation for ML features
        congestion = get_congestion_analysis(home_form_enhanced, away_form_enhanced)

        home_pos = 10
        away_pos = 10
        total_teams = 20
        if standings:
            for team in standings.get("standings", []):
                team_name = team.get("team", {}).get("name", "").lower()
                if home.lower() in team_name or team_name in home.lower():
                    home_pos = team.get("position", 10)
                if away.lower() in team_name or team_name in away.lower():
                    away_pos = team.get("position", 10)
            total_teams = len(standings.get("standings", [])) or 20

        is_cup = "cup" in comp.lower() or "copa" in comp.lower() or "coupe" in comp.lower()
        motivation = get_motivation_analysis(home, away, home_pos, away_pos, is_cup, total_teams)
        team_class = get_team_class_analysis(home, away, home_pos, away_pos, total_teams)

        # Extract ML features for training
        ml_features = extract_features(
            home_form=home_form_enhanced,
            away_form=away_form_enhanced,
            standings=standings,
            odds=odds,
            h2h=h2h.get("matches", []) if h2h else [],
            home_team=home,
            away_team=away,
            congestion=congestion,
            motivation=motivation,
            team_class=team_class
        )

        # Convert enhanced form to simple form for text generation
        home_form = None
        away_form = None
        if home_form_enhanced:
            home_form = {
                "form": home_form_enhanced.get("overall", {}).get("form", ""),
                "wins": home_form_enhanced.get("overall", {}).get("wins", 0),
                "draws": home_form_enhanced.get("overall", {}).get("draws", 0),
                "losses": home_form_enhanced.get("overall", {}).get("losses", 0),
                "goals_scored": home_form_enhanced.get("overall", {}).get("avg_goals_scored", 1.5) * 5,
                "goals_conceded": home_form_enhanced.get("overall", {}).get("avg_goals_conceded", 1.0) * 5,
            }
        if away_form_enhanced:
            away_form = {
                "form": away_form_enhanced.get("overall", {}).get("form", ""),
                "wins": away_form_enhanced.get("overall", {}).get("wins", 0),
                "draws": away_form_enhanced.get("overall", {}).get("draws", 0),
                "losses": away_form_enhanced.get("overall", {}).get("losses", 0),
                "goals_scored": away_form_enhanced.get("overall", {}).get("avg_goals_scored", 1.0) * 5,
                "goals_conceded": away_form_enhanced.get("overall", {}).get("avg_goals_conceded", 1.5) * 5,
            }

        # Build detailed form text
        form_text = ""
        if home_form:
            avg_scored = home_form['goals_scored'] / 5 if home_form.get('goals_scored') else 0
            avg_conceded = home_form['goals_conceded'] / 5 if home_form.get('goals_conceded') else 0
            form_text += f"{home}: {home_form['form']} ({home_form['wins']}W-{home_form['draws']}D-{home_form['losses']}L), avg goals: {avg_scored:.1f} scored, {avg_conceded:.1f} conceded\n"
        if away_form:
            avg_scored = away_form['goals_scored'] / 5 if away_form.get('goals_scored') else 0
            avg_conceded = away_form['goals_conceded'] / 5 if away_form.get('goals_conceded') else 0
            form_text += f"{away}: {away_form['form']} ({away_form['wins']}W-{away_form['draws']}D-{away_form['losses']}L), avg goals: {avg_scored:.1f} scored, {avg_conceded:.1f} conceded"

        # Calculate expected goals
        expected_text = ""
        if home_form and away_form:
            home_avg_scored = home_form['goals_scored'] / 5 if home_form.get('goals_scored') else 1.2
            home_avg_conceded = home_form['goals_conceded'] / 5 if home_form.get('goals_conceded') else 1.2
            away_avg_scored = away_form['goals_scored'] / 5 if away_form.get('goals_scored') else 1.0
            away_avg_conceded = away_form['goals_conceded'] / 5 if away_form.get('goals_conceded') else 1.0
            expected_home = (home_avg_scored + away_avg_conceded) / 2
            expected_away = (away_avg_scored + home_avg_conceded) / 2
            expected_total = expected_home + expected_away
            expected_text = f"Expected goals: {home} ~{expected_home:.1f}, {away} ~{expected_away:.1f}, Total ~{expected_total:.1f}"

        # H2H info with reliability check
        h2h_text = ""
        h2h_warning = ""
        if h2h:
            h2h_matches_count = len(h2h.get('matches', []))
            h2h_text = f"H2H ({h2h['home_wins']}-{h2h['draws']}-{h2h['away_wins']}): avg {h2h['avg_goals']:.1f} goals, BTTS {h2h['btts_percent']:.0f}%, Over2.5 {h2h['over25_percent']:.0f}% ({h2h_matches_count} matches)"
            if h2h_matches_count < 5:
                h2h_warning = f"⚠️ WARNING: H2H only {h2h_matches_count} matches - UNRELIABLE! Prioritize current form over H2H."

        odds_text = ""
        if odds:
            for k, v in odds.items():
                odds_text += f"{k}: {v}, "

        # Analyze match and send alerts in user's language
        analysis_prompt = f"""Analyze this match for betting. Check ALL bet types systematically:

Match: {home} vs {away}
Competition: {comp}
Form: {form_text if form_text else "Limited data"}
{expected_text}
{h2h_text if h2h_text else "No H2H data"}
{h2h_warning}
Odds: {odds_text if odds_text else "Not available"}

VALUE BETTING RULES (for high ROI):
1. VALUE = confidence × odds - must be > 1.2 (20% edge minimum)
   Example: 70% × 1.85 = 1.30 ✓ GOOD VALUE
   Example: 85% × 1.25 = 1.06 ✗ LOW VALUE (don't waste high confidence on low odds)

2. TARGET ODDS: 1.7 - 2.5 range optimal for ROI
   - Low odds (<1.5) = low ROI potential even if wins
   - High odds (>3.0) = need higher confidence

3. ANALYZE ALL BET TYPES fairly:
   - П1/П2: recommend when clear favorite + good odds (1.8+)
   - Double Chance: good for safety but check odds aren't too low
   - Over 2.5: check both teams avg goals (need 1.3+ each)
   - BTTS: both teams must score in >60% of their matches
   - Under/Draw: rare but valid when data supports

4. If H2H < 5 matches → rely on current form, not H2H
5. DON'T recommend weak bets - it's OK to say "no value found"

If you find a VALUE bet (confidence × odds > 1.2 AND odds >= 1.60), respond with JSON:
{{"alert": true, "bet_type": "...", "confidence": 75, "odds": 1.85, "reason_en": "...", "reason_ru": "...", "reason_es": "...", "reason_pt": "..."}}

If no good bet exists (low confidence OR odds too low), respond: {{"alert": false}}"""

        try:
            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                messages=[{"role": "user", "content": analysis_prompt}]
            )

            response_text = message.content[0].text

            # Try to parse JSON from response
            try:
                # Extract JSON from response
                import json
                json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
                if json_match:
                    alert_data = json.loads(json_match.group())
                else:
                    alert_data = {"alert": False}
            except:
                alert_data = {"alert": False}

            if alert_data.get("alert"):
                bet_type = alert_data.get("bet_type", "?")
                confidence = alert_data.get("confidence", 70)
                odds_val = alert_data.get("odds", 1.5)

                # Apply ML correction
                original_conf = confidence
                ml_status = None
                if ml_features:
                    confidence, ml_status, ml_conf = apply_ml_correction(bet_type, confidence, ml_features)

                    # If ML strongly disagrees, skip this alert
                    if ml_status == "warning" and ml_conf and ml_conf < 50:
                        logger.info(f"⚠️ Alert skipped due to ML warning: {home} vs {away}, ML only {ml_conf:.0f}%")
                        continue

                # Mark this match as alerted to prevent duplicates
                if match_id:
                    sent_alerts[match_id] = datetime.now()
                    logger.info(f"✅ Alert triggered for match {match_id}: {home} vs {away}, {bet_type} ({confidence}%), ml_status={ml_status}")

                # Send to each subscriber in their language
                for user_id in live_subscribers:
                    try:
                        user_data = get_user(user_id)
                        lang = user_data.get("language", "ru") if user_data else "ru"
                        user_tz = user_data.get("timezone", "Europe/Moscow") if user_data else "Europe/Moscow"

                        # Get localized reason
                        reason_key = f"reason_{lang}"
                        reason = alert_data.get(reason_key, alert_data.get("reason_en", "Good value bet"))

                        # ML status indicator
                        ml_indicator = ""
                        if ml_status == "confirmed":
                            ml_indicator = "\n🤖 ML: ✅ Подтверждено"
                        elif ml_status == "adjusted":
                            ml_indicator = f"\n📊 ML: {original_conf}% → {confidence}%"

                        # Format match datetime for user's timezone
                        match_dt_str = format_match_datetime(match_date_str, user_tz, lang) if match_date_str else ""

                        # Build localized alert message
                        alert_msg = f"""{get_text("live_alert_title", lang)}

⚽ **{home}** vs **{away}**
🏆 {comp}
{match_dt_str}

{get_text("bet", lang)} {bet_type}
{get_text("confidence", lang)} {confidence}%{ml_indicator}
{get_text("odds", lang)} ~{odds_val}
{get_text("reason", lang)} {reason}"""

                        keyboard = []
                        bet_btn = get_bet_button(user_id, lang)
                        if bet_btn:
                            keyboard.append(bet_btn)

                        await context.bot.send_message(
                            chat_id=user_id,
                            text=alert_msg,
                            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
                            parse_mode="Markdown"
                        )

                        # Save prediction to BOT stats (user_id=0 for alerts - not personal stats)
                        # Live alerts are bot's recommendations, not user's personal requests
                        if match_id:
                            league_code = ml_features.get("league_code") if ml_features else None
                            save_prediction(0, match_id, home, away, bet_type, confidence, odds_val,
                                            ml_features=ml_features, bet_rank=1, league_code=league_code, match_time=match_date_str)
                            logger.info(f"Live alert saved to BOT stats: {home} vs {away}, {bet_type}, league={league_code}")
                    except Exception as e:
                        logger.error(f"Failed to send to {user_id}: {e}")
            else:
                # Log why no alert was sent
                logger.info(f"⚠️ No alert for {home} vs {away}: Claude said no good bet")

        except Exception as e:
            logger.error(f"Claude error: {e}")
        
        await asyncio.sleep(1)


def generate_result_explanation(bet_type: str, home_score: int, away_score: int,
                                 is_correct: bool, confidence: int = None,
                                 home_team: str = "", away_team: str = "",
                                 lang: str = "ru") -> str:
    """Generate a SPECIFIC explanation for why prediction worked or didn't.

    Fully multilingual (ru, en, pt, es, id) with actual score and concrete reasons.
    """
    total_goals = home_score + away_score
    bet_lower = bet_type.lower()
    score_str = f"{home_score}:{away_score}"

    # Short team names with fallbacks per language
    home_fallback = {"ru": "Хозяева", "en": "Home", "pt": "Mandante", "es": "Local", "id": "Tuan rumah"}
    away_fallback = {"ru": "Гости", "en": "Away", "pt": "Visitante", "es": "Visitante", "id": "Tamu"}
    home_short = home_team.split()[-1] if home_team else home_fallback.get(lang, "Home")
    away_short = away_team.split()[-1] if away_team else away_fallback.get(lang, "Away")

    # ===== FULL MULTILINGUAL PHRASES =====
    phrases = {
        "ru": {
            "worked": "💡 **Почему сработало:**",
            "failed": "💡 **Почему не сработало:**",
            "variance": "Беттинг — это про вероятности, не гарантии",
            "keep_going": "Продолжай следить за нашими прогнозами!",
            "good_call": "Отличный анализ!",
            # Totals
            "over_ok": "Матч завершился {score} — всего {goals} голов (>2.5 ✓)",
            "productive": "Команды показали результативный футбол",
            "over_fail": "Матч завершился {score} — всего {goals} гол(а) (<2.5)",
            "no_goals": "Ни одна команда не смогла забить",
            "closed": "Очень закрытый матч, мало моментов",
            "one_short": "Не хватило всего 1 гола для захода",
            "under_ok": "Матч завершился {score} — всего {goals} гол(а) (<2.5 ✓)",
            "careful": "Команды сыграли аккуратно",
            "under_fail": "Матч завершился {score} — {goals} голов (>2.5)",
            "too_many": "Матч получился слишком результативным",
            # Home win
            "home_won": "{team} победили {score}",
            "home_crush": "Разгромная победа хозяев!",
            "home_solid": "Уверенная победа дома",
            "home_factor": "Домашний фактор сыграл свою роль",
            "home_lost": "{team} проиграли дома {score}",
            "away_better": "{team} оказались сильнее на выезде",
            "draw_home": "Ничья {score} — {team} не смогли дожать",
            "lack_attack": "Хозяевам не хватило остроты в атаке",
            # Away win
            "away_won": "{team} победили на выезде {score}",
            "away_dominate": "Гости полностью переиграли хозяев",
            "away_solid": "Крепкая гостевая победа",
            "away_lost": "{team} проиграли {score}",
            "home_held": "{team} не отдали победу дома",
            "draw_away": "Ничья {score} — {team} не реализовали моменты",
            "lack_cold": "Гостям не хватило хладнокровия",
            # Draw
            "draw_ok": "Ничья {score} — команды не выявили сильнейшего",
            "draw_00": "Нулевая ничья — обоюдная осторожность",
            "draw_fair": "Равная игра, справедливый результат",
            "team_won": "{team} выиграли {score}",
            "one_stronger": "Одна из команд оказалась сильнее",
            # Double chance
            "dc_ok": "Двойной шанс отработал!",
            "dc_no_lose": "Ничья {score} — {team} не проиграли",
            "away_surprise": "Гости удивили — хозяева провалились",
            "home_stronger": "Хозяева были сильнее",
            # BTTS
            "btts_ok": "Обе забили! Счёт {score}",
            "btts_attack": "Атакующий футбол с обеих сторон",
            "btts_00": "Счёт 0:0 — никто не забил",
            "gk_fire": "Вратари были в ударе",
            "no_score_home": "{team} не забили (счёт {score})",
            "no_score_away": "{team} не забили (счёт {score})",
            "attack_fail": "Атака хозяев не сработала",
            "away_wasted": "Гости не реализовали моменты",
            # Handicap
            "hcap_ok": "Фора зашла! Счёт {score}",
            "hcap_math": "Расчёт на форе оправдался",
            "hcap_fail": "Фора не зашла. Счёт {score}",
            "draw_kill": "Ничья убила фору",
            "diff_not_cover": "Разница голов ({diff}) не покрыла фору",
            # Generic
            "bet_ok": "Ставка зашла! Счёт {score}",
            "bet_fail": "Ставка не зашла. Счёт {score}",
            "unpredictable": "Футбол непредсказуем",
        },
        "en": {
            "worked": "💡 **Why it worked:**",
            "failed": "💡 **Why it didn't work:**",
            "variance": "Betting is about probabilities, not guarantees",
            "keep_going": "Keep following our predictions!",
            "good_call": "Great analysis!",
            "over_ok": "Match ended {score} — {goals} total goals (>2.5 ✓)",
            "productive": "Both teams showed attacking football",
            "over_fail": "Match ended {score} — only {goals} goal(s) (<2.5)",
            "no_goals": "Neither team managed to score",
            "closed": "Very defensive match, few chances",
            "one_short": "Just 1 goal short of hitting",
            "under_ok": "Match ended {score} — only {goals} goal(s) (<2.5 ✓)",
            "careful": "Both teams played carefully",
            "under_fail": "Match ended {score} — {goals} goals (>2.5)",
            "too_many": "Too many goals in this match",
            "home_won": "{team} won {score}",
            "home_crush": "Dominant home victory!",
            "home_solid": "Solid home win",
            "home_factor": "Home advantage made the difference",
            "home_lost": "{team} lost at home {score}",
            "away_better": "{team} were stronger away",
            "draw_home": "Draw {score} — {team} couldn't finish it",
            "lack_attack": "Home side lacked cutting edge",
            "away_won": "{team} won away {score}",
            "away_dominate": "Away side completely outplayed hosts",
            "away_solid": "Strong away victory",
            "away_lost": "{team} lost {score}",
            "home_held": "{team} held on at home",
            "draw_away": "Draw {score} — {team} wasted chances",
            "lack_cold": "Away side lacked composure",
            "draw_ok": "Draw {score} — neither team could win",
            "draw_00": "Goalless draw — mutual caution",
            "draw_fair": "Fair result, even match",
            "team_won": "{team} won {score}",
            "one_stronger": "One team proved stronger",
            "dc_ok": "Double chance hit!",
            "dc_no_lose": "Draw {score} — {team} didn't lose",
            "away_surprise": "Away upset — hosts collapsed",
            "home_stronger": "Home side was too strong",
            "btts_ok": "Both scored! Final {score}",
            "btts_attack": "Attacking football from both sides",
            "btts_00": "0:0 — no one scored",
            "gk_fire": "Goalkeepers were on fire",
            "no_score_home": "{team} failed to score ({score})",
            "no_score_away": "{team} failed to score ({score})",
            "attack_fail": "Home attack didn't click",
            "away_wasted": "Away side wasted chances",
            "hcap_ok": "Handicap hit! Score {score}",
            "hcap_math": "Handicap calculation paid off",
            "hcap_fail": "Handicap missed. Score {score}",
            "draw_kill": "Draw killed the handicap",
            "diff_not_cover": "Goal difference ({diff}) didn't cover",
            "bet_ok": "Bet won! Score {score}",
            "bet_fail": "Bet lost. Score {score}",
            "unpredictable": "Football is unpredictable",
        },
        "pt": {
            "worked": "💡 **Por que funcionou:**",
            "failed": "💡 **Por que não funcionou:**",
            "variance": "Apostas são sobre probabilidades, não garantias",
            "keep_going": "Continue acompanhando nossas previsões!",
            "good_call": "Excelente análise!",
            "over_ok": "Jogo terminou {score} — {goals} gols no total (>2.5 ✓)",
            "productive": "Ambas equipes mostraram futebol ofensivo",
            "over_fail": "Jogo terminou {score} — apenas {goals} gol(s) (<2.5)",
            "no_goals": "Nenhum time conseguiu marcar",
            "closed": "Jogo muito fechado, poucas chances",
            "one_short": "Faltou apenas 1 gol para acertar",
            "under_ok": "Jogo terminou {score} — apenas {goals} gol(s) (<2.5 ✓)",
            "careful": "Times jogaram com cautela",
            "under_fail": "Jogo terminou {score} — {goals} gols (>2.5)",
            "too_many": "Muitos gols neste jogo",
            "home_won": "{team} venceu {score}",
            "home_crush": "Vitória avassaladora em casa!",
            "home_solid": "Vitória sólida em casa",
            "home_factor": "O fator casa fez a diferença",
            "home_lost": "{team} perdeu em casa {score}",
            "away_better": "{team} foram melhores fora",
            "draw_home": "Empate {score} — {team} não conseguiu vencer",
            "lack_attack": "Mandante sem criatividade no ataque",
            "away_won": "{team} venceu fora {score}",
            "away_dominate": "Visitantes dominaram completamente",
            "away_solid": "Forte vitória fora de casa",
            "away_lost": "{team} perdeu {score}",
            "home_held": "{team} segurou em casa",
            "draw_away": "Empate {score} — {team} desperdiçou chances",
            "lack_cold": "Visitantes sem frieza",
            "draw_ok": "Empate {score} — ninguém venceu",
            "draw_00": "Empate sem gols — cautela mútua",
            "draw_fair": "Resultado justo, jogo equilibrado",
            "team_won": "{team} venceu {score}",
            "one_stronger": "Um time foi mais forte",
            "dc_ok": "Dupla chance acertou!",
            "dc_no_lose": "Empate {score} — {team} não perdeu",
            "away_surprise": "Surpresa visitante — mandante falhou",
            "home_stronger": "Mandante foi mais forte",
            "btts_ok": "Ambos marcaram! Placar {score}",
            "btts_attack": "Futebol ofensivo de ambos",
            "btts_00": "0:0 — ninguém marcou",
            "gk_fire": "Goleiros em grande dia",
            "no_score_home": "{team} não marcou ({score})",
            "no_score_away": "{team} não marcou ({score})",
            "attack_fail": "Ataque da casa não funcionou",
            "away_wasted": "Visitantes desperdiçaram chances",
            "hcap_ok": "Handicap acertou! Placar {score}",
            "hcap_math": "Cálculo do handicap compensou",
            "hcap_fail": "Handicap errou. Placar {score}",
            "draw_kill": "Empate matou o handicap",
            "diff_not_cover": "Diferença de gols ({diff}) não cobriu",
            "bet_ok": "Aposta ganhou! Placar {score}",
            "bet_fail": "Aposta perdeu. Placar {score}",
            "unpredictable": "Futebol é imprevisível",
        },
        "es": {
            "worked": "💡 **Por qué funcionó:**",
            "failed": "💡 **Por qué no funcionó:**",
            "variance": "Las apuestas son probabilidades, no garantías",
            "keep_going": "¡Sigue nuestras predicciones!",
            "good_call": "¡Excelente análisis!",
            "over_ok": "Partido terminó {score} — {goals} goles en total (>2.5 ✓)",
            "productive": "Ambos equipos mostraron fútbol ofensivo",
            "over_fail": "Partido terminó {score} — solo {goals} gol(es) (<2.5)",
            "no_goals": "Ningún equipo pudo marcar",
            "closed": "Partido muy cerrado, pocas ocasiones",
            "one_short": "Faltó solo 1 gol para acertar",
            "under_ok": "Partido terminó {score} — solo {goals} gol(es) (<2.5 ✓)",
            "careful": "Equipos jugaron con cautela",
            "under_fail": "Partido terminó {score} — {goals} goles (>2.5)",
            "too_many": "Demasiados goles en este partido",
            "home_won": "{team} ganó {score}",
            "home_crush": "¡Victoria aplastante en casa!",
            "home_solid": "Victoria sólida en casa",
            "home_factor": "El factor local hizo la diferencia",
            "home_lost": "{team} perdió en casa {score}",
            "away_better": "{team} fueron mejores de visita",
            "draw_home": "Empate {score} — {team} no pudo ganar",
            "lack_attack": "Local sin ideas en ataque",
            "away_won": "{team} ganó de visita {score}",
            "away_dominate": "Visitante dominó completamente",
            "away_solid": "Fuerte victoria fuera de casa",
            "away_lost": "{team} perdió {score}",
            "home_held": "{team} aguantó en casa",
            "draw_away": "Empate {score} — {team} desperdició ocasiones",
            "lack_cold": "Visitante sin frialdad",
            "draw_ok": "Empate {score} — nadie ganó",
            "draw_00": "Empate sin goles — cautela mutua",
            "draw_fair": "Resultado justo, partido parejo",
            "team_won": "{team} ganó {score}",
            "one_stronger": "Un equipo fue más fuerte",
            "dc_ok": "¡Doble oportunidad acertó!",
            "dc_no_lose": "Empate {score} — {team} no perdió",
            "away_surprise": "Sorpresa visitante — local falló",
            "home_stronger": "Local fue más fuerte",
            "btts_ok": "¡Ambos marcaron! Marcador {score}",
            "btts_attack": "Fútbol ofensivo de ambos",
            "btts_00": "0:0 — nadie marcó",
            "gk_fire": "Porteros en gran día",
            "no_score_home": "{team} no marcó ({score})",
            "no_score_away": "{team} no marcó ({score})",
            "attack_fail": "Ataque local no funcionó",
            "away_wasted": "Visitante desperdició ocasiones",
            "hcap_ok": "¡Hándicap acertó! Marcador {score}",
            "hcap_math": "Cálculo del hándicap funcionó",
            "hcap_fail": "Hándicap falló. Marcador {score}",
            "draw_kill": "Empate mató el hándicap",
            "diff_not_cover": "Diferencia de goles ({diff}) no cubrió",
            "bet_ok": "¡Apuesta ganada! Marcador {score}",
            "bet_fail": "Apuesta perdida. Marcador {score}",
            "unpredictable": "El fútbol es impredecible",
        },
        "id": {
            "worked": "💡 **Mengapa berhasil:**",
            "failed": "💡 **Mengapa tidak berhasil:**",
            "variance": "Taruhan adalah tentang probabilitas, bukan jaminan",
            "keep_going": "Terus ikuti prediksi kami!",
            "good_call": "Analisis hebat!",
            "over_ok": "Pertandingan berakhir {score} — {goals} gol total (>2.5 ✓)",
            "productive": "Kedua tim menunjukkan sepak bola menyerang",
            "over_fail": "Pertandingan berakhir {score} — hanya {goals} gol (<2.5)",
            "no_goals": "Tidak ada tim yang bisa mencetak gol",
            "closed": "Pertandingan sangat tertutup, sedikit peluang",
            "one_short": "Kurang 1 gol untuk menang",
            "under_ok": "Pertandingan berakhir {score} — hanya {goals} gol (<2.5 ✓)",
            "careful": "Kedua tim bermain hati-hati",
            "under_fail": "Pertandingan berakhir {score} — {goals} gol (>2.5)",
            "too_many": "Terlalu banyak gol di pertandingan ini",
            "home_won": "{team} menang {score}",
            "home_crush": "Kemenangan telak di kandang!",
            "home_solid": "Kemenangan solid di kandang",
            "home_factor": "Keuntungan kandang membuat perbedaan",
            "home_lost": "{team} kalah di kandang {score}",
            "away_better": "{team} lebih kuat tandang",
            "draw_home": "Seri {score} — {team} tidak bisa menyelesaikan",
            "lack_attack": "Tuan rumah kurang tajam di depan",
            "away_won": "{team} menang tandang {score}",
            "away_dominate": "Tamu sepenuhnya menguasai pertandingan",
            "away_solid": "Kemenangan tandang yang kuat",
            "away_lost": "{team} kalah {score}",
            "home_held": "{team} bertahan di kandang",
            "draw_away": "Seri {score} — {team} menyia-nyiakan peluang",
            "lack_cold": "Tamu kurang tenang",
            "draw_ok": "Seri {score} — tidak ada pemenang",
            "draw_00": "Seri tanpa gol — saling berhati-hati",
            "draw_fair": "Hasil adil, pertandingan seimbang",
            "team_won": "{team} menang {score}",
            "one_stronger": "Satu tim lebih kuat",
            "dc_ok": "Peluang ganda berhasil!",
            "dc_no_lose": "Seri {score} — {team} tidak kalah",
            "away_surprise": "Kejutan tamu — tuan rumah gagal",
            "home_stronger": "Tuan rumah lebih kuat",
            "btts_ok": "Keduanya mencetak gol! Skor {score}",
            "btts_attack": "Sepak bola menyerang dari kedua tim",
            "btts_00": "0:0 — tidak ada yang mencetak gol",
            "gk_fire": "Kiper bermain cemerlang",
            "no_score_home": "{team} tidak mencetak gol ({score})",
            "no_score_away": "{team} tidak mencetak gol ({score})",
            "attack_fail": "Serangan tuan rumah tidak berfungsi",
            "away_wasted": "Tamu menyia-nyiakan peluang",
            "hcap_ok": "Handicap berhasil! Skor {score}",
            "hcap_math": "Perhitungan handicap terbayar",
            "hcap_fail": "Handicap gagal. Skor {score}",
            "draw_kill": "Seri membunuh handicap",
            "diff_not_cover": "Selisih gol ({diff}) tidak menutupi",
            "bet_ok": "Taruhan menang! Skor {score}",
            "bet_fail": "Taruhan kalah. Skor {score}",
            "unpredictable": "Sepak bola tidak dapat diprediksi",
        },
    }

    # Get phrases for current language, fallback to English then Russian
    p = phrases.get(lang, phrases.get("en", phrases["ru"]))
    header = p["worked"] if is_correct else p["failed"]
    explanations = []

    # ===== TOTALS =====
    if "тб 2.5" in bet_lower or "over 2.5" in bet_lower or ("тб" in bet_lower and "2.5" in bet_type):
        if is_correct:
            explanations.append(p["over_ok"].format(score=score_str, goals=total_goals))
            explanations.append(p["productive"])
        else:
            explanations.append(p["over_fail"].format(score=score_str, goals=total_goals))
            if total_goals == 0:
                explanations.append(p["no_goals"])
            elif total_goals == 1:
                explanations.append(p["closed"])
            else:
                explanations.append(p["one_short"])

    elif "тм 2.5" in bet_lower or "under 2.5" in bet_lower or ("тм" in bet_lower and "2.5" in bet_type):
        if is_correct:
            explanations.append(p["under_ok"].format(score=score_str, goals=total_goals))
            explanations.append(p["careful"])
        else:
            explanations.append(p["under_fail"].format(score=score_str, goals=total_goals))
            explanations.append(p["too_many"])

    # ===== HOME WIN П1 =====
    elif "п1" in bet_lower or bet_type == "1":
        if is_correct:
            diff = home_score - away_score
            explanations.append(p["home_won"].format(team=home_short, score=score_str))
            if diff >= 3:
                explanations.append(p["home_crush"])
            elif diff == 2:
                explanations.append(p["home_solid"])
            else:
                explanations.append(p["home_factor"])
        else:
            if home_score < away_score:
                explanations.append(p["home_lost"].format(team=home_short, score=score_str))
                explanations.append(p["away_better"].format(team=away_short))
            else:
                explanations.append(p["draw_home"].format(score=score_str, team=home_short))
                explanations.append(p["lack_attack"])

    # ===== AWAY WIN П2 =====
    elif "п2" in bet_lower or bet_type == "2":
        if is_correct:
            explanations.append(p["away_won"].format(team=away_short, score=score_str))
            diff = away_score - home_score
            if diff >= 2:
                explanations.append(p["away_dominate"])
            else:
                explanations.append(p["away_solid"])
        else:
            if away_score < home_score:
                explanations.append(p["away_lost"].format(team=away_short, score=score_str))
                explanations.append(p["home_held"].format(team=home_short))
            else:
                explanations.append(p["draw_away"].format(score=score_str, team=away_short))
                explanations.append(p["lack_cold"])

    # ===== DRAW Х =====
    elif "ничья" in bet_lower or bet_lower == "х" or bet_lower == "x":
        if is_correct:
            explanations.append(p["draw_ok"].format(score=score_str))
            if total_goals == 0:
                explanations.append(p["draw_00"])
            else:
                explanations.append(p["draw_fair"])
        else:
            if home_score > away_score:
                explanations.append(p["team_won"].format(team=home_short, score=score_str))
            else:
                explanations.append(p["team_won"].format(team=away_short, score=score_str))
            explanations.append(p["one_stronger"])

    # ===== DOUBLE CHANCE 1X =====
    elif "1x" in bet_lower or "п1 или х" in bet_lower:
        if is_correct:
            if home_score > away_score:
                explanations.append(p["home_won"].format(team=home_short, score=score_str))
            else:
                explanations.append(p["dc_no_lose"].format(score=score_str, team=home_short))
            explanations.append(p["dc_ok"])
        else:
            explanations.append(p["away_won"].format(team=away_short, score=score_str))
            explanations.append(p["away_surprise"])

    # ===== DOUBLE CHANCE X2 =====
    elif "x2" in bet_lower or "2x" in bet_lower or "х или п2" in bet_lower:
        if is_correct:
            if away_score > home_score:
                explanations.append(p["away_won"].format(team=away_short, score=score_str))
            else:
                explanations.append(p["dc_no_lose"].format(score=score_str, team=away_short))
            explanations.append(p["dc_ok"])
        else:
            explanations.append(p["home_won"].format(team=home_short, score=score_str))
            explanations.append(p["home_stronger"])

    # ===== BTTS =====
    elif "btts" in bet_lower or "обе забьют" in bet_lower:
        if is_correct:
            explanations.append(p["btts_ok"].format(score=score_str))
            explanations.append(p["btts_attack"])
        else:
            if home_score == 0 and away_score == 0:
                explanations.append(p["btts_00"])
                explanations.append(p["gk_fire"])
            elif home_score == 0:
                explanations.append(p["no_score_home"].format(team=home_short, score=score_str))
                explanations.append(p["attack_fail"])
            else:
                explanations.append(p["no_score_away"].format(team=away_short, score=score_str))
                explanations.append(p["away_wasted"])

    # ===== HANDICAP ФОРА =====
    elif "фора" in bet_lower or "handicap" in bet_lower:
        if is_correct:
            explanations.append(p["hcap_ok"].format(score=score_str))
            explanations.append(p["hcap_math"])
        else:
            explanations.append(p["hcap_fail"].format(score=score_str))
            diff = abs(home_score - away_score)
            if diff == 0:
                explanations.append(p["draw_kill"])
            else:
                explanations.append(p["diff_not_cover"].format(diff=diff))

    # ===== GENERIC =====
    else:
        if is_correct:
            explanations.append(p["bet_ok"].format(score=score_str))
        else:
            explanations.append(p["bet_fail"].format(score=score_str))
            explanations.append(p["unpredictable"])

    # Add encouragement
    if is_correct:
        explanations.append(p["good_call"])
    else:
        explanations.append(p["variance"])
        explanations.append(p["keep_going"])

    # Format output
    result = f"{header}\n"
    for exp in explanations:
        result += f"• {exp}\n"

    return result.strip()


# Result explanation translations for user-facing messages
RESULT_TRANSLATIONS = {
    "ru": {
        # Headers
        "smart_analysis": "💡 **Умный анализ результата:**",
        "why_hit": "✅ Почему зашло:",
        "why_missed": "❌ Почему не зашло:",
        # Totals
        "expected_goals": "📊 Ожидаемые голы: {expected} | Реально: {actual}",
        "high_scoring": "🔥 Голевой матч! {home} и {away} устроили перестрелку",
        "both_good_form": "📈 Обе команды в хорошей форме — атакующий футбол",
        "xg_confirmed": "🎯 xG подтвердил: высокий потенциал голов",
        "one_goal_short": "😤 Не хватило всего 1 гола! Близко, но не зашло",
        "injuries_attack": "🏥 Травмы ключевых игроков повлияли на атаку",
        "xg_misled": "📉 xG обманул: ожидали {expected}, получили {actual}",
        "closed_match": "🔒 Закрытый матч, команды играли на результат",
        "top_teams_careful": "🏆 Топ-команды часто играют аккуратно друг с другом",
        "unexpected_goals": "💥 Неожиданная перестрелка! {total} голов",
        "stats_missed": "⚠️ Статистика не учла мотивацию/форму в этом матче",
        # Match result
        "positions": "📊 Позиции: {home} #{pos1} vs {away} #{pos2}",
        "form": "📈 Форма: {home} {form1}% | {away} {form2}%",
        "home_form_won": "✅ Форма хозяев была выше — логичная победа",
        "class_difference": "✅ Разница в классе команд сработала",
        "h2h_confirmed": "✅ H2H в пользу хозяев подтвердился",
        "away_form_risk": "⚠️ Форма гостей ({form}%) была выше — это был риск",
        "away_surprised": "😮 {team} удивили — выиграли на выезде вопреки статистике",
        "draw_could_not": "🤝 {team} не смогли дожать — ничья {score}",
        "draw_vs_top": "⚠️ Против топ-команды ничья — нормальный результат",
        "away_form_won": "✅ Форма гостей была выше — заслуженная победа",
        "away_class_won": "✅ Класс гостей оказался решающим",
        "home_factor": "🏠 Домашний фактор сработал для {team}",
        "away_no_realize": "🤝 {team} не реализовали преимущество",
        "equal_teams_draw": "✅ Равные по силе команды — логичная ничья",
        "close_positions_draw": "✅ Близкие позиции в таблице = ничейный результат",
        "winner_stronger": "🏆 {team} оказались сильнее в этот день",
        # BTTS
        "btts_both_scored": "⚽ {home}: {goals1} гол(а) | {away}: {goals2} гол(а)",
        "btts_stats_confirmed": "✅ Обе команды результативны — статистика подтвердилась",
        "btts_no_goal": "🚫 {team} не забили — атака не сработала",
        "btts_injuries": "🏥 Травмы атакующих игроков повлияли",
        "btts_away_no_goal": "🚫 {team} не забили на выезде",
        # Double chance
        "dc_safety_worked": "✅ Страховка сработала — безопасная ставка зашла",
        "dc_even_failed": "😱 Даже двойной шанс не спас — редкий случай",
        # Scorers/Injuries
        "goals_team": "⚽ Голы {team}: {scorers}",
        "injury_impact": "🏥 У {team} серьёзные потери из-за травм ({impact}% влияние)",
        # Claude analysis
        "claude_analysis": "💡 **Анализ результата:**",
        "goals_label": "⚽ Голы",
        # Defaults
        "home_default": "Хозяева",
        "away_default": "Гости",
    },
    "en": {
        "smart_analysis": "💡 **Smart Result Analysis:**",
        "why_hit": "✅ Why it hit:",
        "why_missed": "❌ Why it missed:",
        "expected_goals": "📊 Expected goals: {expected} | Actual: {actual}",
        "high_scoring": "🔥 High-scoring match! {home} and {away} traded blows",
        "both_good_form": "📈 Both teams in good form — attacking football",
        "xg_confirmed": "🎯 xG confirmed: high goal potential",
        "one_goal_short": "😤 Just 1 goal short! Close but no cigar",
        "injuries_attack": "🏥 Key player injuries affected attack",
        "xg_misled": "📉 xG misleading: expected {expected}, got {actual}",
        "closed_match": "🔒 Closed match, teams played for result",
        "top_teams_careful": "🏆 Top teams often play cautiously against each other",
        "unexpected_goals": "💥 Unexpected goal fest! {total} goals",
        "stats_missed": "⚠️ Stats missed motivation/form factor in this match",
        "positions": "📊 Positions: {home} #{pos1} vs {away} #{pos2}",
        "form": "📈 Form: {home} {form1}% | {away} {form2}%",
        "home_form_won": "✅ Home team's form was higher — logical win",
        "class_difference": "✅ Class difference between teams paid off",
        "h2h_confirmed": "✅ H2H advantage for home team confirmed",
        "away_form_risk": "⚠️ Away form ({form}%) was higher — this was risky",
        "away_surprised": "😮 {team} surprised — won away against the odds",
        "draw_could_not": "🤝 {team} couldn't close it out — draw {score}",
        "draw_vs_top": "⚠️ Draw vs top team — normal result",
        "away_form_won": "✅ Away form was higher — deserved win",
        "away_class_won": "✅ Away team's class was decisive",
        "home_factor": "🏠 Home advantage worked for {team}",
        "away_no_realize": "🤝 {team} failed to capitalize",
        "equal_teams_draw": "✅ Equal teams — logical draw",
        "close_positions_draw": "✅ Close table positions = draw result",
        "winner_stronger": "🏆 {team} were stronger on the day",
        "btts_both_scored": "⚽ {home}: {goals1} goal(s) | {away}: {goals2} goal(s)",
        "btts_stats_confirmed": "✅ Both teams prolific — stats confirmed",
        "btts_no_goal": "🚫 {team} failed to score — attack misfired",
        "btts_injuries": "🏥 Attacking player injuries had impact",
        "btts_away_no_goal": "🚫 {team} blanked away from home",
        "dc_safety_worked": "✅ Safety bet paid off",
        "dc_even_failed": "😱 Even double chance couldn't save it — rare case",
        "goals_team": "⚽ {team} goals: {scorers}",
        "injury_impact": "🏥 {team} had significant injury losses ({impact}% impact)",
        "claude_analysis": "💡 **Result Analysis:**",
        "goals_label": "⚽ Goals",
        "home_default": "Home",
        "away_default": "Away",
    },
    "pt": {
        "smart_analysis": "💡 **Análise Inteligente:**",
        "why_hit": "✅ Por que acertou:",
        "why_missed": "❌ Por que errou:",
        "expected_goals": "📊 Gols esperados: {expected} | Real: {actual}",
        "high_scoring": "🔥 Jogo de muitos gols! {home} e {away} fizeram uma batalha",
        "both_good_form": "📈 Ambas equipes em boa forma — futebol ofensivo",
        "xg_confirmed": "🎯 xG confirmou: alto potencial de gols",
        "one_goal_short": "😤 Faltou só 1 gol! Perto mas não deu",
        "injuries_attack": "🏥 Lesões de jogadores-chave afetaram o ataque",
        "xg_misled": "📉 xG enganou: esperava {expected}, teve {actual}",
        "closed_match": "🔒 Jogo fechado, equipes jogaram pelo resultado",
        "top_teams_careful": "🏆 Times de topo costumam jogar com cuidado entre si",
        "unexpected_goals": "💥 Festival de gols inesperado! {total} gols",
        "stats_missed": "⚠️ Estatísticas não capturaram motivação/forma neste jogo",
        "positions": "📊 Posições: {home} #{pos1} vs {away} #{pos2}",
        "form": "📈 Forma: {home} {form1}% | {away} {form2}%",
        "home_form_won": "✅ Forma do mandante era melhor — vitória lógica",
        "class_difference": "✅ Diferença de qualidade entre equipes funcionou",
        "h2h_confirmed": "✅ H2H favorável ao mandante confirmado",
        "away_form_risk": "⚠️ Forma visitante ({form}%) era melhor — foi arriscado",
        "away_surprised": "😮 {team} surpreendeu — venceu fora contra as probabilidades",
        "draw_could_not": "🤝 {team} não conseguiu fechar — empate {score}",
        "draw_vs_top": "⚠️ Empate contra time de topo — resultado normal",
        "away_form_won": "✅ Forma visitante era melhor — vitória merecida",
        "away_class_won": "✅ Qualidade do visitante foi decisiva",
        "home_factor": "🏠 Fator casa funcionou para {team}",
        "away_no_realize": "🤝 {team} não aproveitou a vantagem",
        "equal_teams_draw": "✅ Times iguais — empate lógico",
        "close_positions_draw": "✅ Posições próximas na tabela = resultado de empate",
        "winner_stronger": "🏆 {team} foi mais forte no dia",
        "btts_both_scored": "⚽ {home}: {goals1} gol(s) | {away}: {goals2} gol(s)",
        "btts_stats_confirmed": "✅ Ambos times produtivos — estatísticas confirmadas",
        "btts_no_goal": "🚫 {team} não marcou — ataque falhou",
        "btts_injuries": "🏥 Lesões de atacantes tiveram impacto",
        "btts_away_no_goal": "🚫 {team} não marcou fora de casa",
        "dc_safety_worked": "✅ Aposta segura funcionou",
        "dc_even_failed": "😱 Nem chance dupla salvou — caso raro",
        "goals_team": "⚽ Gols {team}: {scorers}",
        "injury_impact": "🏥 {team} teve perdas significativas por lesões ({impact}% impacto)",
        "claude_analysis": "💡 **Análise do Resultado:**",
        "goals_label": "⚽ Gols",
        "home_default": "Mandante",
        "away_default": "Visitante",
    },
    "es": {
        "smart_analysis": "💡 **Análisis Inteligente:**",
        "why_hit": "✅ Por qué acertó:",
        "why_missed": "❌ Por qué falló:",
        "expected_goals": "📊 Goles esperados: {expected} | Real: {actual}",
        "high_scoring": "🔥 ¡Partido de muchos goles! {home} y {away} se enfrentaron",
        "both_good_form": "📈 Ambos equipos en buena forma — fútbol ofensivo",
        "xg_confirmed": "🎯 xG confirmó: alto potencial de goles",
        "one_goal_short": "😤 ¡Faltó solo 1 gol! Cerca pero no alcanzó",
        "injuries_attack": "🏥 Lesiones de jugadores clave afectaron el ataque",
        "xg_misled": "📉 xG engañó: esperaba {expected}, obtuvo {actual}",
        "closed_match": "🔒 Partido cerrado, equipos jugaron por el resultado",
        "top_teams_careful": "🏆 Los equipos top suelen jugar con cuidado entre sí",
        "unexpected_goals": "💥 ¡Festival de goles inesperado! {total} goles",
        "stats_missed": "⚠️ Las estadísticas no capturaron la motivación/forma",
        "positions": "📊 Posiciones: {home} #{pos1} vs {away} #{pos2}",
        "form": "📈 Forma: {home} {form1}% | {away} {form2}%",
        "home_form_won": "✅ La forma del local era mejor — victoria lógica",
        "class_difference": "✅ La diferencia de clase entre equipos funcionó",
        "h2h_confirmed": "✅ H2H favorable al local confirmado",
        "away_form_risk": "⚠️ Forma visitante ({form}%) era mejor — fue arriesgado",
        "away_surprised": "😮 {team} sorprendió — ganó fuera contra todo pronóstico",
        "draw_could_not": "🤝 {team} no pudo cerrar — empate {score}",
        "draw_vs_top": "⚠️ Empate contra equipo top — resultado normal",
        "away_form_won": "✅ Forma visitante era mejor — victoria merecida",
        "away_class_won": "✅ La clase del visitante fue decisiva",
        "home_factor": "🏠 El factor local funcionó para {team}",
        "away_no_realize": "🤝 {team} no aprovechó la ventaja",
        "equal_teams_draw": "✅ Equipos iguales — empate lógico",
        "close_positions_draw": "✅ Posiciones cercanas en la tabla = empate",
        "winner_stronger": "🏆 {team} fue más fuerte en el día",
        "btts_both_scored": "⚽ {home}: {goals1} gol(es) | {away}: {goals2} gol(es)",
        "btts_stats_confirmed": "✅ Ambos equipos productivos — estadísticas confirmadas",
        "btts_no_goal": "🚫 {team} no marcó — el ataque falló",
        "btts_injuries": "🏥 Las lesiones de atacantes tuvieron impacto",
        "btts_away_no_goal": "🚫 {team} no marcó de visitante",
        "dc_safety_worked": "✅ La apuesta segura funcionó",
        "dc_even_failed": "😱 Ni la doble oportunidad salvó — caso raro",
        "goals_team": "⚽ Goles {team}: {scorers}",
        "injury_impact": "🏥 {team} tuvo pérdidas significativas por lesiones ({impact}% impacto)",
        "claude_analysis": "💡 **Análisis del Resultado:**",
        "goals_label": "⚽ Goles",
        "home_default": "Local",
        "away_default": "Visitante",
    },
    "id": {
        "smart_analysis": "💡 **Analisis Cerdas:**",
        "why_hit": "✅ Mengapa tepat:",
        "why_missed": "❌ Mengapa meleset:",
        "expected_goals": "📊 Gol diharapkan: {expected} | Aktual: {actual}",
        "high_scoring": "🔥 Pertandingan banyak gol! {home} dan {away} saling serang",
        "both_good_form": "📈 Kedua tim dalam form bagus — sepakbola menyerang",
        "xg_confirmed": "🎯 xG mengkonfirmasi: potensi gol tinggi",
        "one_goal_short": "😤 Kurang 1 gol saja! Dekat tapi tidak masuk",
        "injuries_attack": "🏥 Cedera pemain kunci mempengaruhi serangan",
        "xg_misled": "📉 xG menyesatkan: diharapkan {expected}, dapat {actual}",
        "closed_match": "🔒 Pertandingan tertutup, tim bermain untuk hasil",
        "top_teams_careful": "🏆 Tim top sering bermain hati-hati satu sama lain",
        "unexpected_goals": "💥 Festival gol tak terduga! {total} gol",
        "stats_missed": "⚠️ Statistik tidak menangkap motivasi/form di pertandingan ini",
        "positions": "📊 Posisi: {home} #{pos1} vs {away} #{pos2}",
        "form": "📈 Form: {home} {form1}% | {away} {form2}%",
        "home_form_won": "✅ Form tuan rumah lebih tinggi — kemenangan logis",
        "class_difference": "✅ Perbedaan kelas antar tim berhasil",
        "h2h_confirmed": "✅ H2H menguntungkan tuan rumah terkonfirmasi",
        "away_form_risk": "⚠️ Form tamu ({form}%) lebih tinggi — ini berisiko",
        "away_surprised": "😮 {team} mengejutkan — menang tandang melawan odds",
        "draw_could_not": "🤝 {team} tidak bisa menutup — seri {score}",
        "draw_vs_top": "⚠️ Seri lawan tim top — hasil normal",
        "away_form_won": "✅ Form tamu lebih tinggi — kemenangan layak",
        "away_class_won": "✅ Kelas tim tamu yang menentukan",
        "home_factor": "🏠 Keuntungan kandang bekerja untuk {team}",
        "away_no_realize": "🤝 {team} gagal memanfaatkan",
        "equal_teams_draw": "✅ Tim setara — seri logis",
        "close_positions_draw": "✅ Posisi klasemen dekat = hasil seri",
        "winner_stronger": "🏆 {team} lebih kuat hari ini",
        "btts_both_scored": "⚽ {home}: {goals1} gol | {away}: {goals2} gol",
        "btts_stats_confirmed": "✅ Kedua tim produktif — statistik terkonfirmasi",
        "btts_no_goal": "🚫 {team} gagal mencetak gol — serangan meleset",
        "btts_injuries": "🏥 Cedera penyerang berdampak",
        "btts_away_no_goal": "🚫 {team} tidak mencetak gol di kandang lawan",
        "dc_safety_worked": "✅ Taruhan aman berhasil",
        "dc_even_failed": "😱 Bahkan peluang ganda tidak bisa menyelamatkan — kasus langka",
        "goals_team": "⚽ Gol {team}: {scorers}",
        "injury_impact": "🏥 {team} mengalami kerugian signifikan karena cedera ({impact}% dampak)",
        "claude_analysis": "💡 **Analisis Hasil:**",
        "goals_label": "⚽ Gol",
        "home_default": "Tuan Rumah",
        "away_default": "Tamu",
    }
}


def get_result_text(key: str, lang: str = "ru", **kwargs) -> str:
    """Get translated result explanation text with formatting"""
    translations = RESULT_TRANSLATIONS.get(lang, RESULT_TRANSLATIONS.get("en", RESULT_TRANSLATIONS["ru"]))
    text = translations.get(key, RESULT_TRANSLATIONS["ru"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text
    return text


async def generate_smart_result_explanation(
    prediction_id: int,
    match_data: dict,
    bet_type: str,
    is_correct: bool,
    home_score: int,
    away_score: int,
    lang: str = "ru"
) -> str:
    """
    Generate INTELLIGENT explanation for why a bet won or lost.

    Uses:
    - Original prediction features (form, standings, H2H, xG, etc.)
    - Match statistics (shots, possession if available)
    - Goalscorer data
    - Comparison of expected vs actual outcome

    Returns a smart, contextual explanation that helps user understand the result.
    """
    try:
        home_team = match_data.get("homeTeam", {}).get("name", "")
        away_team = match_data.get("awayTeam", {}).get("name", "")
        home_short = home_team.split()[-1] if home_team else get_result_text("home_default", lang)
        away_short = away_team.split()[-1] if away_team else get_result_text("away_default", lang)
        total_goals = home_score + away_score
        score_str = f"{home_score}:{away_score}"

        # === 1. GET ORIGINAL PREDICTION DATA ===
        conn = get_db_connection()
        c = conn.cursor()

        # Get prediction features
        c.execute("""
            SELECT p.confidence, p.ml_features_json, m.features_json, p.league_code
            FROM predictions p
            LEFT JOIN ml_training_data m ON m.prediction_id = p.id
            WHERE p.id = ?
        """, (prediction_id,))
        row = c.fetchone()
        conn.close()

        features = {}
        confidence = 70
        league_code = ""

        if row:
            confidence = row[0] or 70
            features_json = row[1] or row[2]
            league_code = row[3] or ""
            if features_json:
                try:
                    features = json.loads(features_json)
                except:
                    pass

        # === 2. EXTRACT KEY PRE-MATCH DATA ===
        home_form = features.get("home_form", 50)
        away_form = features.get("away_form", 50)
        home_pos = features.get("home_position", 10)
        away_pos = features.get("away_position", 10)
        home_xg = features.get("home_xg", 0)
        away_xg = features.get("away_xg", 0)
        h2h_home_wins = features.get("h2h_home_wins", 0)
        h2h_away_wins = features.get("h2h_away_wins", 0)
        home_goals_avg = features.get("home_goals_scored_avg", 1.5)
        away_goals_avg = features.get("away_goals_scored_avg", 1.5)
        home_injured_impact = features.get("home_injured_impact", 0)
        away_injured_impact = features.get("away_injured_impact", 0)

        # === 3. ANALYZE GOALSCORERS ===
        goals_data = match_data.get("goals", [])
        scorers_home = []
        scorers_away = []
        home_id = match_data.get("homeTeam", {}).get("id")

        for goal in goals_data:
            scorer_name = goal.get("scorer", {}).get("name", "")
            minute = goal.get("minute", "")
            team_id = goal.get("team", {}).get("id")

            if scorer_name:
                goal_info = f"{scorer_name} ({minute}')" if minute else scorer_name
                if team_id == home_id:
                    scorers_home.append(goal_info)
                else:
                    scorers_away.append(goal_info)

        # === 4. BUILD SMART ANALYSIS ===
        insights = []

        # Use translated headers
        main_header = get_result_text("smart_analysis", lang)
        why_header = get_result_text("why_hit" if is_correct else "why_missed", lang)

        bet_lower = bet_type.lower()

        # --- TOTALS ANALYSIS ---
        if "тб" in bet_lower or "тм" in bet_lower or "over" in bet_lower or "under" in bet_lower:
            is_over = "тб" in bet_lower or "over" in bet_lower
            expected_goals = home_xg + away_xg if (home_xg > 0 or away_xg > 0) else home_goals_avg + away_goals_avg

            insights.append(get_result_text("expected_goals", lang, expected=f"{expected_goals:.1f}", actual=total_goals))

            if is_over and is_correct:
                if total_goals >= 4:
                    insights.append(get_result_text("high_scoring", lang, home=home_short, away=away_short))
                elif home_form > 60 and away_form > 60:
                    insights.append(get_result_text("both_good_form", lang))
                if home_xg > 1.5 or away_xg > 1.5:
                    insights.append(get_result_text("xg_confirmed", lang))

            elif is_over and not is_correct:
                if total_goals == 2:
                    insights.append(get_result_text("one_goal_short", lang))
                if home_injured_impact > 20 or away_injured_impact > 20:
                    insights.append(get_result_text("injuries_attack", lang))
                if expected_goals > 2.5 and total_goals <= 2:
                    insights.append(get_result_text("xg_misled", lang, expected=f"{expected_goals:.1f}", actual=total_goals))

            elif not is_over and is_correct:
                if total_goals <= 1:
                    insights.append(get_result_text("closed_match", lang))
                if home_pos <= 5 or away_pos <= 5:
                    insights.append(get_result_text("top_teams_careful", lang))

            elif not is_over and not is_correct:
                if total_goals >= 4:
                    insights.append(get_result_text("unexpected_goals", lang, total=total_goals))
                insights.append(get_result_text("stats_missed", lang))

        # --- MATCH RESULT ANALYSIS (П1, П2, X) ---
        elif "п1" in bet_lower or bet_type == "1" or "п2" in bet_lower or bet_type == "2" or "ничья" in bet_lower or bet_lower in ["х", "x"]:
            home_won = home_score > away_score
            away_won = away_score > home_score
            draw = home_score == away_score

            # Only show positions/form if we have real data (not defaults)
            has_real_data = (home_pos != 10 or away_pos != 10) and (home_form != 50 or away_form != 50)
            if has_real_data:
                if home_pos != 10 or away_pos != 10:
                    insights.append(get_result_text("positions", lang, home=home_short, away=away_short, pos1=home_pos, pos2=away_pos))
                if home_form != 50 or away_form != 50:
                    insights.append(get_result_text("form", lang, home=home_short, away=away_short, form1=home_form, form2=away_form))

            # П1 analysis
            if "п1" in bet_lower or bet_type == "1":
                if is_correct:
                    if home_form > away_form:
                        insights.append(get_result_text("home_form_won", lang))
                    if home_pos < away_pos:
                        insights.append(get_result_text("class_difference", lang))
                    if h2h_home_wins > h2h_away_wins:
                        insights.append(get_result_text("h2h_confirmed", lang))
                else:
                    if away_won:
                        if away_form > home_form:
                            insights.append(get_result_text("away_form_risk", lang, form=away_form))
                        else:
                            insights.append(get_result_text("away_surprised", lang, team=away_short))
                    elif draw:
                        insights.append(get_result_text("draw_could_not", lang, team=home_short, score=score_str))
                        if away_pos <= 6:
                            insights.append(get_result_text("draw_vs_top", lang))

            # П2 analysis
            elif "п2" in bet_lower or bet_type == "2":
                if is_correct:
                    if away_form > home_form:
                        insights.append(get_result_text("away_form_won", lang))
                    if away_pos < home_pos:
                        insights.append(get_result_text("away_class_won", lang))
                else:
                    if home_won:
                        insights.append(get_result_text("home_factor", lang, team=home_short))
                    elif draw:
                        insights.append(get_result_text("away_no_realize", lang, team=away_short))

            # Draw analysis
            elif "ничья" in bet_lower or bet_lower in ["х", "x"]:
                if is_correct:
                    # Only show insights if we have real data
                    if (home_form != 50 or away_form != 50) and abs(home_form - away_form) < 15:
                        insights.append(get_result_text("equal_teams_draw", lang))
                    if (home_pos != 10 or away_pos != 10) and abs(home_pos - away_pos) <= 3:
                        insights.append(get_result_text("close_positions_draw", lang))
                else:
                    winner = home_short if home_won else away_short
                    insights.append(get_result_text("winner_stronger", lang, team=winner))

        # --- BTTS ANALYSIS ---
        elif "обе забьют" in bet_lower or "btts" in bet_lower:
            both_scored = home_score > 0 and away_score > 0

            if is_correct and both_scored:
                insights.append(get_result_text("btts_both_scored", lang, home=home_short, away=away_short, goals1=home_score, goals2=away_score))
                if home_goals_avg > 1.3 and away_goals_avg > 1.3:
                    insights.append(get_result_text("btts_stats_confirmed", lang))
            elif not is_correct:
                if home_score == 0:
                    insights.append(get_result_text("btts_no_goal", lang, team=home_short))
                    if home_injured_impact > 15:
                        insights.append(get_result_text("btts_injuries", lang))
                if away_score == 0:
                    insights.append(get_result_text("btts_away_no_goal", lang, team=away_short))

        # --- DOUBLE CHANCE ANALYSIS ---
        elif "1x" in bet_lower or "x2" in bet_lower or "12" in bet_lower:
            if is_correct:
                insights.append(get_result_text("dc_safety_worked", lang))
            else:
                insights.append(get_result_text("dc_even_failed", lang))

        # === 5. ADD GOALSCORER INFO ===
        if scorers_home or scorers_away:
            if scorers_home:
                insights.append(get_result_text("goals_team", lang, team=home_short, scorers=', '.join(scorers_home)))
            if scorers_away:
                insights.append(get_result_text("goals_team", lang, team=away_short, scorers=', '.join(scorers_away)))

        # === 6. ADD INJURY CONTEXT (if significant) ===
        if home_injured_impact > 15:
            insights.append(get_result_text("injury_impact", lang, team=home_short, impact=home_injured_impact))
        if away_injured_impact > 15:
            insights.append(get_result_text("injury_impact", lang, team=away_short, impact=away_injured_impact))

        # === 7. FORMAT OUTPUT - USE CLAUDE IF AVAILABLE ===
        if not insights:
            # Fallback to simple explanation
            return generate_result_explanation(
                bet_type, home_score, away_score, is_correct,
                confidence, home_team, away_team, lang
            )

        # Try to use Claude for natural language explanation
        if claude_client:
            try:
                # Build context for Claude
                context_data = f"""
МАТЧ: {home_team} vs {away_team}
СЧЁТ: {score_str} (голов: {total_goals})
СТАВКА: {bet_type}
РЕЗУЛЬТАТ: {"✅ ЗАШЛА" if is_correct else "❌ НЕ ЗАШЛА"}

ДАННЫЕ ДО МАТЧА:
- Форма: {home_team} {home_form}%, {away_team} {away_form}%
- Позиции: {home_team} #{home_pos}, {away_team} #{away_pos}
- xG: {home_team} {home_xg:.2f}, {away_team} {away_xg:.2f}
- Средние голы: {home_team} {home_goals_avg:.1f}, {away_team} {away_goals_avg:.1f}
- H2H: {home_team} {h2h_home_wins} побед, {away_team} {h2h_away_wins} побед

{"ГОЛЫ: " + ', '.join(scorers_home + scorers_away) if scorers_home or scorers_away else ""}
"""
                # Language-specific instructions
                lang_instructions = {
                    "ru": "Отвечай на русском языке.",
                    "en": "Respond in English.",
                    "pt": "Responda em português.",
                    "es": "Responde en español.",
                    "id": "Jawab dalam Bahasa Indonesia."
                }
                lang_instruction = lang_instructions.get(lang, lang_instructions["ru"])

                bet_worked = {
                    "ru": ("зашла", "не зашла"),
                    "en": ("hit", "missed"),
                    "pt": ("acertou", "errou"),
                    "es": ("acertó", "falló"),
                    "id": ("berhasil", "gagal")
                }
                worked, failed = bet_worked.get(lang, bet_worked["ru"])
                bet_status = worked if is_correct else failed

                prompt = f"""{lang_instruction}

Explain briefly (2-3 sentences) why the bet {bet_status}.

{context_data}

IMPORTANT:
- DON'T write obvious things like "one goal short" or "xG was wrong"
- Explain REAL reasons: tactics, team form, key moments
- If bet failed - what did we underestimate?
- If bet won - what was the decisive factor?
- Write naturally, not template phrases"""

                message = claude_client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                claude_text = message.content[0].text.strip()

                result = f"{main_header}\n{why_header}\n\n{claude_text}"

                # Add goalscorers with translated label
                if scorers_home or scorers_away:
                    all_scorers = scorers_home + scorers_away
                    goals_label = {"ru": "Голы", "en": "Goals", "pt": "Gols", "es": "Goles", "id": "Gol"}.get(lang, "Goals")
                    result += f"\n\n⚽ {goals_label}: {', '.join(all_scorers[:4])}"

                return result.strip()

            except Exception as e:
                logger.warning(f"Claude explanation fallback: {e}")
                # Fall through to template-based output below

        # Template-based fallback (if Claude unavailable or failed)
        result = f"{main_header}\n{why_header}\n\n"
        for insight in insights[:6]:  # Max 6 insights
            result += f"• {insight}\n"

        return result.strip()

    except Exception as e:
        logger.error(f"Smart explanation error: {e}")
        # Fallback to simple explanation
        return generate_result_explanation(
            bet_type, home_score, away_score, is_correct,
            confidence if 'confidence' in dir() else 70,
            home_team if 'home_team' in dir() else "",
            away_team if 'away_team' in dir() else "",
            lang
        )


async def generate_claude_result_explanation(
    prediction_id: int,
    match_data: dict,
    bet_type: str,
    is_correct: bool,
    home_score: int,
    away_score: int,
    lang: str = "ru"
) -> str:
    """
    Generate UNIQUE, AI-powered explanation for match result using Claude.

    Unlike template-based explanations, this creates contextual analysis that:
    - Explains the REAL reasons for the outcome (not just "1 goal short")
    - Uses pre-match analytics (form, xG, H2H, injuries, motivation)
    - Considers tactical and psychological factors
    - Generates unique text for every match

    Falls back to template-based explanation if Claude unavailable.
    """
    if not claude_client:
        # No Claude API - use template-based explanation
        return await generate_smart_result_explanation(
            prediction_id, match_data, bet_type, is_correct,
            home_score, away_score, lang
        )

    try:
        home_team = match_data.get("homeTeam", {}).get("name", "Unknown")
        away_team = match_data.get("awayTeam", {}).get("name", "Unknown")
        total_goals = home_score + away_score
        score_str = f"{home_score}:{away_score}"

        # === 1. GET PREDICTION FEATURES FROM DB ===
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT p.confidence, p.ml_features_json, m.features_json, p.league_code,
                   p.reasoning, p.raw_analysis
            FROM predictions p
            LEFT JOIN ml_training_data m ON m.prediction_id = p.id
            WHERE p.id = ?
        """, (prediction_id,))
        row = c.fetchone()
        conn.close()

        features = {}
        confidence = 70
        league_code = ""
        original_reasoning = ""
        raw_analysis = ""

        if row:
            confidence = row[0] or 70
            features_json = row[1] or row[2]
            league_code = row[3] or ""
            original_reasoning = row[4] or ""
            raw_analysis = row[5] or ""
            if features_json:
                try:
                    features = json.loads(features_json)
                except:
                    pass

        # === 2. BUILD ANALYTICS CONTEXT ===
        analytics_context = []

        # Form analysis
        home_form = features.get("home_form", 0)
        away_form = features.get("away_form", 0)
        if home_form or away_form:
            analytics_context.append(f"Форма до матча: {home_team} {home_form}%, {away_team} {away_form}%")

        # Positions
        home_pos = features.get("home_position", 0)
        away_pos = features.get("away_position", 0)
        if home_pos or away_pos:
            analytics_context.append(f"Позиции в таблице: {home_team} #{home_pos}, {away_team} #{away_pos}")

        # xG (expected goals)
        home_xg = features.get("home_xg", 0)
        away_xg = features.get("away_xg", 0)
        if home_xg or away_xg:
            analytics_context.append(f"xG (ожидаемые голы): {home_team} {home_xg:.2f}, {away_team} {away_xg:.2f}")

        # Goals averages
        home_goals_avg = features.get("home_goals_scored_avg", 0)
        away_goals_avg = features.get("away_goals_scored_avg", 0)
        home_conceded_avg = features.get("home_goals_conceded_avg", 0)
        away_conceded_avg = features.get("away_goals_conceded_avg", 0)
        if home_goals_avg or away_goals_avg:
            analytics_context.append(
                f"Средние голы: {home_team} забивает {home_goals_avg:.1f}, пропускает {home_conceded_avg:.1f}; "
                f"{away_team} забивает {away_goals_avg:.1f}, пропускает {away_conceded_avg:.1f}"
            )

        # H2H
        h2h_home = features.get("h2h_home_wins", 0)
        h2h_away = features.get("h2h_away_wins", 0)
        h2h_draws = features.get("h2h_draws", 0)
        if h2h_home or h2h_away or h2h_draws:
            analytics_context.append(f"H2H последние встречи: {home_team} {h2h_home} побед, {away_team} {h2h_away} побед, ничьи {h2h_draws}")

        # Injuries
        home_injured = features.get("home_injured_impact", 0)
        away_injured = features.get("away_injured_impact", 0)
        if home_injured > 10 or away_injured > 10:
            analytics_context.append(f"Травмы: влияние на {home_team} {home_injured}%, на {away_team} {away_injured}%")

        # Home advantage
        home_adv = features.get("home_advantage", 0)
        if home_adv:
            analytics_context.append(f"Фактор домашнего поля: {home_adv}%")

        # Motivation
        home_motivation = features.get("home_motivation_score", 0)
        away_motivation = features.get("away_motivation_score", 0)
        if home_motivation or away_motivation:
            analytics_context.append(f"Мотивация: {home_team} {home_motivation}/10, {away_team} {away_motivation}/10")

        # === 3. GET GOALSCORERS ===
        goals_data = match_data.get("goals", [])
        scorers_info = []
        home_id = match_data.get("homeTeam", {}).get("id")

        for goal in goals_data:
            scorer = goal.get("scorer", {}).get("name", "")
            minute = goal.get("minute", "")
            team_id = goal.get("team", {}).get("id")
            team_name = home_team if team_id == home_id else away_team
            if scorer:
                scorers_info.append(f"{scorer} ({team_name}, {minute}')")

        # === 4. BUILD CLAUDE PROMPT ===
        result_status = "✅ ЗАШЛА" if is_correct else "❌ НЕ ЗАШЛА"
        lang_names = {"ru": "русский", "en": "English", "pt": "português", "es": "español", "id": "Bahasa Indonesia"}
        target_lang = lang_names.get(lang, "русский")

        prompt = f"""Ты - эксперт по футбольной аналитике. Объясни результат матча и почему ставка {'зашла' if is_correct else 'не зашла'}.

МАТЧ: {home_team} vs {away_team}
СЧЁТ: {score_str} (всего голов: {total_goals})

СТАВКА: {bet_type}
УВЕРЕННОСТЬ: {confidence}%
РЕЗУЛЬТАТ: {result_status}

АНАЛИТИКА ДО МАТЧА:
{chr(10).join(analytics_context) if analytics_context else "Нет данных"}

{"ГОЛЫ: " + ", ".join(scorers_info) if scorers_info else ""}

{"НАШЕ ОБОСНОВАНИЕ ДО МАТЧА: " + original_reasoning[:500] if original_reasoning else ""}

ЗАДАЧА:
Напиши короткое (3-5 предложений) объяснение почему ставка {'сработала' if is_correct else 'не сработала'}.

ВАЖНО:
- НЕ пиши "не хватило одного гола" или "было близко" - это очевидно из счёта
- Объясни РЕАЛЬНЫЕ причины: тактика, форма, ключевые игроки, мотивация
- Если ставка не зашла - объясни что мы недооценили/переоценили
- Если зашла - объясни какой фактор был решающим
- Пиши кратко и по делу, без воды
- Язык: {target_lang}

Формат ответа - только текст объяснения, без заголовков."""

        # === 5. CALL CLAUDE API ===
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        claude_explanation = message.content[0].text.strip()

        # === 6. FORMAT OUTPUT ===
        header = get_result_text("claude_analysis", lang)
        status_emoji = "✅" if is_correct else "❌"

        result = f"{header}\n{status_emoji} {bet_type} | {score_str}\n\n{claude_explanation}"

        # Add goalscorers if available
        if scorers_info:
            goals_label = get_result_text("goals_label", lang)
            result += f"\n\n{goals_label}: {', '.join(scorers_info[:4])}"

        return result

    except Exception as e:
        logger.error(f"Claude explanation error: {e}")
        # Fallback to template-based explanation
        return await generate_smart_result_explanation(
            prediction_id, match_data, bet_type, is_correct,
            home_score, away_score, lang
        )


async def track_upcoming_odds(context: ContextTypes.DEFAULT_TYPE):
    """Background task to track odds for upcoming matches.

    This runs every 30 min to build historical odds data for line movement detection.
    Prioritizes matches starting soon (within 6 hours) for more frequent tracking.
    """
    logger.info("=== TRACK ODDS JOB STARTED ===")

    try:
        # Get matches for next 48 hours across all leagues
        matches = await get_matches(days=2)

        if not matches:
            logger.info("No upcoming matches to track odds")
            return

        # Sort matches by start time - prioritize soon-starting matches
        now = datetime.now(timezone.utc)
        matches_with_time = []
        for m in matches:
            try:
                match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                hours_until = (match_time - now).total_seconds() / 3600
                matches_with_time.append((m, hours_until))
            except:
                matches_with_time.append((m, 999))

        # Sort by hours until match (closest first)
        matches_with_time.sort(key=lambda x: x[1])

        tracked = 0
        soon_matches = 0

        for match, hours_until in matches_with_time[:40]:  # Track up to 40 matches
            home = match.get("homeTeam", {}).get("name", "")
            away = match.get("awayTeam", {}).get("name", "")

            if not home or not away:
                continue

            # Get odds for this match
            odds = await get_odds(home, away)

            if odds:
                tracked += 1
                if hours_until <= 6:
                    soon_matches += 1
                    logger.info(f"⚡ Tracked SOON match ({hours_until:.1f}h): {home} vs {away}")
                else:
                    logger.debug(f"Tracked odds: {home} vs {away}")

            # Small delay to not overwhelm API
            await asyncio.sleep(0.3)

        logger.info(f"=== TRACK ODDS COMPLETED: {tracked} total, {soon_matches} within 6h ===")

    except Exception as e:
        logger.error(f"Track odds job error: {e}")


async def update_key_players_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Background job to update key players database from API.
    Runs every 12 hours to keep player impact data fresh.
    """
    logger.info("=== UPDATE KEY PLAYERS JOB STARTED ===")

    # Leagues supported by football-data.org
    leagues = ["PL", "PD", "SA", "BL1", "FL1", "DED", "PPL", "ELC", "CL", "EL"]

    total_updated = 0

    for league_code in leagues:
        try:
            updated = await update_key_players_from_api(league_code)
            total_updated += updated

            # Small delay to respect API rate limits
            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Error updating key players for {league_code}: {e}")

    logger.info(f"=== UPDATE KEY PLAYERS COMPLETED: {total_updated} players updated ===")


async def train_ensemble_models_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Background job to train ensemble ML models.
    Runs every 24 hours to keep models updated with latest data.
    """
    logger.info("=== TRAIN ENSEMBLE MODELS JOB STARTED ===")

    # Train for different bet categories
    categories = ["match_result", "totals", "btts"]

    for category in categories:
        try:
            result = train_ensemble_models(category)

            if "error" not in result:
                logger.info(f"✅ Ensemble trained for {category}: {result}")
            else:
                logger.warning(f"⚠️ Ensemble training for {category}: {result['error']}")

        except Exception as e:
            logger.error(f"Error training ensemble for {category}: {e}")

    logger.info("=== TRAIN ENSEMBLE MODELS COMPLETED ===")


async def check_predictions_results(context: ContextTypes.DEFAULT_TYPE):
    """Check results of past predictions - grouped by match for combined notifications"""
    logger.info("=== CHECK RESULTS JOB STARTED ===")

    pending = get_pending_predictions()

    if not pending:
        logger.info("No pending predictions to check")
        return

    logger.info(f"Found {len(pending)} pending predictions to check")

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}

    # Group predictions by (user_id, match_id) for combined notifications
    from collections import defaultdict
    grouped = defaultdict(list)
    no_match_id = 0
    for pred in pending:
        if not pred.get("match_id"):
            no_match_id += 1
            continue
        if pred.get("user_id", 0) > 0:  # Skip bot alerts (user_id=0)
            key = (pred["user_id"], pred["match_id"])
            grouped[key].append(pred)

    if no_match_id > 0:
        logger.warning(f"{no_match_id} predictions have no match_id - cannot check results")

    # Also process bot alerts (user_id=0) separately - no notification needed
    bot_alerts = [p for p in pending if p.get("user_id", 0) == 0 and p.get("match_id")]

    logger.info(f"Grouped into {len(grouped)} user matches + {len(bot_alerts)} bot alerts")

    # Track checked matches to avoid duplicate API calls
    match_results = {}

    # Process grouped user predictions (max 40 matches per check)
    processed = 0
    for (user_id, match_id), preds in list(grouped.items())[:40]:
        try:
            # Get match result (use cache if already fetched)
            if match_id not in match_results:
                url = f"{FOOTBALL_API_URL}/matches/{match_id}"
                session = await get_http_session()
                async with session.get(url, headers=headers) as r:
                    if r.status == 200:
                        match_results[match_id] = await r.json()
                    elif r.status == 429:
                        logger.warning(f"Rate limited fetching match {match_id}, will retry later")
                        await asyncio.sleep(2)
                        continue
                    else:
                        logger.warning(f"API error {r.status} for match {match_id}")
                await asyncio.sleep(0.3)

            match = match_results.get(match_id)
            if not match:
                logger.debug(f"No data for match {match_id}")
                continue

            match_status = match.get("status", "UNKNOWN")
            if match_status != "FINISHED":
                logger.debug(f"Match {match_id} status: {match_status} (waiting)")
                continue

            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home", 0) or 0
            away_score = score.get("away", 0) or 0
            result = f"{home_score}-{away_score}"

            # 📊 Update player stats from goalscorers (for Flat Track Bully analysis)
            league_code_match = match.get("competition", {}).get("code", "")
            if league_code_match:
                await update_player_stats_from_finished_match(match, league_code_match)

            # Sort predictions: main first (rank=1), then alternatives
            preds.sort(key=lambda x: x.get("bet_rank", 1))

            # Update all predictions and build combined message
            user_data = get_user(user_id)
            lang = user_data.get("language", "ru") if user_data else "ru"

            main_line = ""
            alt_lines = []
            main_bet_type = None
            main_is_correct = None

            for pred in preds:
                is_correct = check_bet_result(pred["bet_type"], home_score, away_score)

                if is_correct is True:
                    db_value = 1
                    emoji = "✅"
                elif is_correct is False:
                    db_value = 0
                    emoji = "❌"
                else:
                    db_value = 2
                    emoji = "🔄"

                update_prediction_result(pred["id"], result, db_value)
                logger.info(f"Updated prediction {pred['id']}: {result} -> {emoji}")

                bet_rank = pred.get("bet_rank", 1)
                if bet_rank == 1:
                    main_line = f"⚡ {get_text('bet_main', lang)}: {pred['bet_type']} {emoji}"
                    main_bet_type = pred['bet_type']
                    main_is_correct = is_correct
                else:
                    alt_lines.append(f"📌 {get_text('bet_alt', lang)}: {pred['bet_type']} {emoji}")

            # Generate SMART explanation for the main bet
            explanation = ""
            main_pred_id = None
            for p in preds:
                if p.get("bet_rank", 1) == 1:
                    main_pred_id = p.get("id")
                    break

            if main_bet_type and main_is_correct is not None and main_pred_id:
                # Use Claude-powered explanation for unique, contextual analysis
                explanation = await generate_claude_result_explanation(
                    prediction_id=main_pred_id,
                    match_data=match,
                    bet_type=main_bet_type,
                    is_correct=main_is_correct is True,
                    home_score=home_score,
                    away_score=away_score,
                    lang=lang
                )

            # Send ONE combined notification with explanation
            try:
                msg = f"{get_text('pred_result_title', lang)}\n\n"
                msg += f"⚽ **{preds[0]['home']}** vs **{preds[0]['away']}**\n"
                msg += f"📈 {result}\n\n"

                if main_line:
                    msg += f"{main_line}\n"
                if alt_lines:
                    msg += "\n".join(alt_lines) + "\n"

                # Add explanation
                if explanation:
                    msg += f"\n{explanation}"

                await context.bot.send_message(
                    chat_id=user_id,
                    text=msg,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify user {user_id}: {e}")

            processed += 1

        except Exception as e:
            import traceback
            logger.error(f"Error checking match {match_id}: {e}\n{traceback.format_exc()}")

    # Process bot alerts (user_id=0) - update DB only, no notification
    for pred in bot_alerts[:40]:
        match_id = pred.get("match_id")
        try:
            if match_id not in match_results:
                url = f"{FOOTBALL_API_URL}/matches/{match_id}"
                session = await get_http_session()
                async with session.get(url, headers=headers) as r:
                    if r.status == 200:
                        match_results[match_id] = await r.json()
                    elif r.status != 200:
                        logger.warning(f"API error {r.status} for bot alert match {match_id}")
                await asyncio.sleep(0.3)

            match = match_results.get(match_id)
            if not match or match.get("status") != "FINISHED":
                continue

            score = match.get("score", {}).get("fullTime", {})
            home_score = score.get("home", 0) or 0
            away_score = score.get("away", 0) or 0
            result = f"{home_score}-{away_score}"

            # 📊 Update player stats from goalscorers (for Flat Track Bully analysis)
            league_code_alert = match.get("competition", {}).get("code", "")
            if league_code_alert:
                await update_player_stats_from_finished_match(match, league_code_alert)

            is_correct = check_bet_result(pred["bet_type"], home_score, away_score)
            db_value = 1 if is_correct is True else (0 if is_correct is False else 2)

            update_prediction_result(pred["id"], result, db_value)
            logger.info(f"Updated BOT alert {pred['id']}: {result} -> {'✅' if db_value == 1 else '❌' if db_value == 0 else '🔄'}")

        except Exception as e:
            logger.error(f"Error checking bot alert {pred['id']}: {e}")

    logger.info(f"=== CHECK RESULTS JOB DONE: {processed} user matches, {len(bot_alerts)} bot alerts ===")


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Send daily digest at 10:00 UTC"""

    if not live_subscribers:
        return

    current_hour = datetime.utcnow().hour
    if current_hour != 10:  # 10:00 UTC = 13:00 Moscow
        return

    logger.info("Sending daily digest...")

    matches = await get_matches(date_filter="today")

    if not matches:
        return

    # Use Moscow timezone for digest (most common user timezone)
    recs = await get_recommendations_enhanced(matches, "daily digest", user_tz="Europe/Moscow")

    if not recs:
        return

    for user_id in live_subscribers:
        try:
            user_data = get_user(user_id)
            lang = user_data.get("language", "ru") if user_data else "ru"

            text = f"{get_text('daily_digest_title', lang)}\n\n{recs}"
            keyboard = []
            bet_btn = get_bet_button(user_id, lang, "place_bet_btn")
            if bet_btn:
                keyboard.append(bet_btn)
            keyboard.append([InlineKeyboardButton(get_text("all_matches_btn", lang), callback_data="cmd_today")])
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send digest to {user_id}: {e}")


# ===== ENHANCED NOTIFICATION SYSTEM =====

def get_marketing_stats(days: int = 1) -> dict:
    """Get marketing-friendly stats (show only good results, ~70%+)

    This function returns curated statistics for marketing purposes,
    emphasizing positive results to engage users.
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get stats for the period
        c.execute("""
            SELECT bet_type, is_correct, home, away, match_id
            FROM predictions
            WHERE is_correct IS NOT NULL
            AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
        """, (f'-{days} days',))
        predictions = c.fetchall()
        conn.close()

        if not predictions:
            return {"wins": 0, "total": 0, "percent": 70, "best_type": None, "best_match": None}

        # Group by bet type
        by_type = {}
        for bet_type, is_correct, home, away, match_id in predictions:
            category = categorize_bet(bet_type) if bet_type else "other"
            if category not in by_type:
                by_type[category] = {"wins": 0, "total": 0, "matches": []}
            by_type[category]["total"] += 1
            if is_correct == 1:
                by_type[category]["wins"] += 1
                by_type[category]["matches"].append(f"{home} vs {away}")

        # Find best type (only show if 65%+)
        best_type = None
        best_percent = 0
        for cat, stats in by_type.items():
            if stats["total"] >= 3:  # Minimum 3 bets to count
                pct = (stats["wins"] / stats["total"]) * 100
                if pct >= 65 and pct > best_percent:
                    best_percent = pct
                    best_type = {
                        "category": cat,
                        "wins": stats["wins"],
                        "total": stats["total"],
                        "percent": int(pct),
                        "match": stats["matches"][0] if stats["matches"] else None
                    }

        # Calculate overall - but inflate slightly for marketing
        total_wins = sum(s["wins"] for s in by_type.values())
        total_bets = sum(s["total"] for s in by_type.values())

        # Only show if at least 60% real accuracy
        real_percent = (total_wins / total_bets * 100) if total_bets > 0 else 0
        if real_percent < 55:
            # If too low, show only best type stats or fallback
            if best_type:
                return {
                    "wins": best_type["wins"],
                    "total": best_type["total"],
                    "percent": best_type["percent"],
                    "best_type": best_type,
                    "best_match": best_type["match"]
                }
            return {"wins": 7, "total": 10, "percent": 70, "best_type": None, "best_match": None}

        # Slightly round up for marketing
        shown_percent = min(int(real_percent) + 3, 85)  # Cap at 85%

        return {
            "wins": total_wins,
            "total": total_bets,
            "percent": shown_percent,
            "best_type": best_type,
            "best_match": predictions[0][2] + " vs " + predictions[0][3] if predictions else None
        }
    except Exception as e:
        logger.error(f"Error getting marketing stats: {e}")
        return {"wins": 7, "total": 10, "percent": 70, "best_type": None, "best_match": None}


def get_day_name(day_num: int, lang: str) -> str:
    """Get localized day name"""
    day_keys = ["day_monday", "day_tuesday", "day_wednesday", "day_thursday",
                "day_friday", "day_saturday", "day_sunday"]
    return get_text(day_keys[day_num], lang)


async def send_evening_digest(context: ContextTypes.DEFAULT_TYPE):
    """Send evening digest at 22:00 MSK (19:00 UTC)"""
    current_hour = datetime.utcnow().hour
    if current_hour != 19:  # 19:00 UTC = 22:00 Moscow
        return

    logger.info("Sending evening digest...")

    # Get all users
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, language FROM users WHERE last_active >= datetime('now', '-30 days')")
        all_users = c.fetchall()

        # Count tomorrow's matches
        c.execute("""SELECT COUNT(*) FROM predictions
                     WHERE created_at >= datetime('now', '+1 day', 'start of day')
                     AND created_at < datetime('now', '+2 days', 'start of day')""")
        tomorrow_count = c.fetchone()[0] or 12  # Default to 12 if no data
        conn.close()
    except Exception as e:
        logger.error(f"Error getting users for evening digest: {e}")
        return

    # Get today's marketing stats
    stats = get_marketing_stats(days=1)

    sent_count = 0
    for user_id, lang in all_users:
        lang = lang or "ru"

        if not should_send_notification(user_id, "evening_digest", cooldown_hours=20):
            continue

        try:
            # Build message
            text = f"{get_text('evening_digest_title', lang)}\n\n"

            if stats["best_type"]:
                bt = stats["best_type"]
                category_names = {
                    "totals_over": "ТБ 2.5" if lang == "ru" else "Over 2.5",
                    "totals_under": "ТМ 2.5" if lang == "ru" else "Under 2.5",
                    "outcomes_home": "П1" if lang == "ru" else "Home Win",
                    "outcomes_away": "П2" if lang == "ru" else "Away Win",
                    "btts": "BTTS",
                    "double_chance": "1X/X2",
                    "handicap": "Фора" if lang == "ru" else "Handicap",
                }
                cat_name = category_names.get(bt["category"], bt["category"])
                text += f"{get_text('evening_best_bet', lang)}\n"
                text += f"**{cat_name}** — {bt['wins']}/{bt['total']} ({bt['percent']}%) ✅\n\n"

            text += f"{get_text('evening_overall', lang)} {stats['wins']}/{stats['total']} ({stats['percent']}%)\n\n"

            if stats["best_match"]:
                text += f"{get_text('evening_top_match', lang)} {stats['best_match']} ✅\n\n"

            text += f"{get_text('evening_tomorrow_count', lang).format(count=tomorrow_count)}\n"
            text += f"{get_text('evening_cta', lang)}"

            keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]
            bet_btn = get_bet_button(user_id, lang, "place_bet_btn")
            if bet_btn:
                keyboard.append(bet_btn)

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            mark_notification_sent(user_id, "evening_digest")
            sent_count += 1

            # Rate limiting
            if sent_count % 30 == 0:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to send evening digest to {user_id}: {e}")

    logger.info(f"Evening digest sent to {sent_count} users")


async def send_morning_alert(context: ContextTypes.DEFAULT_TYPE):
    """Send morning alert at 10:00 MSK (07:00 UTC)"""
    current_hour = datetime.utcnow().hour
    if current_hour != 7:  # 07:00 UTC = 10:00 Moscow
        return

    logger.info("Sending morning alerts...")

    # Get today's matches
    matches = await get_matches(date_filter="today")
    if not matches:
        return

    match_count = len(matches)

    # Find main match (biggest teams or earliest)
    main_match = None
    for m in matches:
        home = m.get("homeTeam", {}).get("name", "Team A")
        away = m.get("awayTeam", {}).get("name", "Team B")
        utc_date = m.get("utcDate", "")

        # Simple heuristic: prefer matches with well-known teams
        big_teams = ["Real Madrid", "Barcelona", "Bayern", "Manchester", "Liverpool",
                     "Chelsea", "Arsenal", "Juventus", "PSG", "Inter", "Milan"]

        is_big = any(t in home or t in away for t in big_teams)
        if is_big or main_match is None:
            main_match = {
                "home": home,
                "away": away,
                "utc_date": utc_date  # Store for datetime formatting
            }
            if is_big:
                break

    if not main_match:
        main_match = {"home": "Top Team", "away": "Top Team", "utc_date": ""}

    # Get all users
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, language FROM users WHERE last_active >= datetime('now', '-14 days')")
        all_users = c.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error getting users: {e}")
        return

    sent_count = 0
    for user_id, lang in all_users:
        lang = lang or "ru"

        if not should_send_notification(user_id, "morning_alert", cooldown_hours=20):
            continue

        try:
            # Get user's timezone
            user_data = get_user(user_id)
            user_tz = user_data.get("timezone", "Europe/Moscow") if user_data else "Europe/Moscow"

            # Format match datetime for user's timezone
            match_dt_str = format_match_datetime(main_match.get("utc_date", ""), user_tz, lang)

            text = f"{get_text('morning_alert_title', lang).format(count=match_count)}\n\n"
            text += f"{get_text('morning_main_match', lang)}\n"
            text += f"**{main_match['home']}** vs **{main_match['away']}**\n"
            text += f"{match_dt_str}\n\n" if match_dt_str else "\n"
            text += f"{get_text('morning_cta', lang)}"

            keyboard = [
                [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")],
                [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")]
            ]

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            mark_notification_sent(user_id, "morning_alert")
            sent_count += 1

            if sent_count % 30 == 0:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to send morning alert to {user_id}: {e}")

    logger.info(f"Morning alert sent to {sent_count} users")


async def send_inactive_user_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Send alerts to users inactive for 3+ days"""
    logger.info("Checking inactive users...")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Users who were active 3-14 days ago (not too old, not too recent)
        c.execute("""SELECT user_id, language FROM users
                     WHERE last_active BETWEEN datetime('now', '-14 days')
                     AND datetime('now', '-3 days')""")
        inactive_users = c.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error getting inactive users: {e}")
        return

    if not inactive_users:
        return

    # Get marketing stats for the period
    stats = get_marketing_stats(days=7)

    sent_count = 0
    for user_id, lang in inactive_users:
        lang = lang or "ru"

        if not should_send_notification(user_id, "inactive_alert", cooldown_hours=72):
            continue

        try:
            text = f"{get_text('inactive_title', lang)}\n\n"
            text += f"{get_text('inactive_stats', lang)}\n"
            text += f"{get_text('inactive_wins', lang).format(wins=stats['wins'], total=stats['total'], percent=stats['percent'])}\n\n"

            # Show a streak (always show good number)
            streak = max(4, stats["wins"] // 3)
            text += f"{get_text('inactive_streak', lang).format(streak=streak)}\n\n"
            text += f"{get_text('inactive_cta', lang)}"

            keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]
            bet_btn = get_bet_button(user_id, lang, "place_bet_btn")
            if bet_btn:
                keyboard.append(bet_btn)

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            mark_notification_sent(user_id, "inactive_alert")
            sent_count += 1

            if sent_count % 30 == 0:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to send inactive alert to {user_id}: {e}")

    logger.info(f"Inactive alerts sent to {sent_count} users")


async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    """Send weekly report on Sunday at 20:00 MSK (17:00 UTC)"""
    now = datetime.utcnow()
    if now.weekday() != 6 or now.hour != 17:  # Sunday, 17:00 UTC = 20:00 Moscow
        return

    logger.info("Sending weekly reports...")

    # Get weekly stats
    stats = get_marketing_stats(days=7)

    # Get best day of the week (fake good data for marketing)
    best_day_num = (now.weekday() + 4) % 7  # Usually Friday

    # Get all users
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, language FROM users WHERE last_active >= datetime('now', '-30 days')")
        all_users = c.fetchall()

        # Count next week matches (estimate)
        next_week_count = 45  # Default estimate
        conn.close()
    except Exception as e:
        logger.error(f"Error getting users: {e}")
        return

    sent_count = 0
    for user_id, lang in all_users:
        lang = lang or "ru"

        if not should_send_notification(user_id, "weekly_report", cooldown_hours=160):
            continue

        try:
            text = f"{get_text('weekly_title', lang)}\n\n"
            text += f"{get_text('weekly_accuracy', lang).format(wins=stats['wins'], total=stats['total'], percent=stats['percent'])}\n"

            best_day_name = get_day_name(best_day_num, lang)
            # Show good stats for best day
            best_day_wins = max(8, stats["wins"] // 4)
            best_day_total = best_day_wins + 2
            text += f"{get_text('weekly_best_day', lang).format(day=best_day_name, wins=best_day_wins, total=best_day_total)}\n\n"

            if stats["best_type"]:
                bt = stats["best_type"]
                category_names = {
                    "totals_over": "ТБ 2.5" if lang == "ru" else "Over 2.5",
                    "totals_under": "ТМ 2.5" if lang == "ru" else "Under 2.5",
                    "outcomes_home": "П1" if lang == "ru" else "Home Win",
                    "outcomes_away": "П2" if lang == "ru" else "Away Win",
                    "btts": "BTTS",
                    "double_chance": "1X/X2" if lang == "ru" else "Double Chance",
                    "handicap": "Фора" if lang == "ru" else "Handicap",
                }
                cat_name = category_names.get(bt["category"], bt["category"])
                text += f"{get_text('weekly_best_bet_type', lang)}\n"
                text += f"**{cat_name}** — {bt['wins']}/{bt['total']} ({bt['percent']}%)\n\n"

            text += f"{get_text('weekly_next_week', lang).format(count=next_week_count)}"

            keyboard = [
                [InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")],
                [InlineKeyboardButton(get_text("referral_invite_btn", lang), callback_data="cmd_referral")]
            ]

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            mark_notification_sent(user_id, "weekly_report")
            sent_count += 1

            if sent_count % 30 == 0:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to send weekly report to {user_id}: {e}")

    logger.info(f"Weekly report sent to {sent_count} users")


async def send_hot_match_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Send hot match alerts for matches starting in 2-3 hours with high confidence"""
    logger.info("Checking for hot matches...")

    # Get upcoming matches
    matches = await get_matches(days=0)  # Today only
    if not matches:
        return

    now = datetime.utcnow()
    hot_matches = []

    for m in matches:
        try:
            utc_date = m.get("utcDate", "")
            match_time = datetime.fromisoformat(utc_date.replace("Z", "+00:00")).replace(tzinfo=None)
            hours_until = (match_time - now).total_seconds() / 3600

            # Match starting in 2-3 hours
            if 2 <= hours_until <= 3:
                home = m.get("homeTeam", {}).get("name", "Team A")
                away = m.get("awayTeam", {}).get("name", "Team B")
                hot_matches.append({
                    "home": home,
                    "away": away,
                    "hours": int(hours_until),
                    "match_id": m.get("id"),
                    "utc_date": utc_date  # Store for datetime formatting
                })
        except:
            continue

    if not hot_matches:
        return

    # Get live subscribers
    if not live_subscribers:
        return

    sent_count = 0
    for user_id in live_subscribers:
        try:
            user_data = get_user(user_id)
            lang = user_data.get("language", "ru") if user_data else "ru"
            user_tz = user_data.get("timezone", "Europe/Moscow") if user_data else "Europe/Moscow"

            for match in hot_matches[:1]:  # Only one match per cycle
                if not should_send_notification(user_id, f"hot_match_{match['match_id']}", cooldown_hours=6):
                    continue

                # Format match datetime for user's timezone
                match_dt_str = format_match_datetime(match.get("utc_date", ""), user_tz, lang)

                text = f"{get_text('hot_match_title', lang)}\n\n"
                text += f"**{match['home']}** vs **{match['away']}**\n"
                text += f"{match_dt_str}\n" if match_dt_str else ""
                text += f"{get_text('hot_match_confidence', lang).format(percent=75)}\n\n"
                text += f"{get_text('hot_match_cta', lang)}"

                keyboard = []
                bet_btn = get_bet_button(user_id, lang, "place_bet_btn")
                if bet_btn:
                    keyboard.append(bet_btn)
                # Button to analyze THIS specific match (counts as daily usage)
                keyboard.append([InlineKeyboardButton(
                    get_text("analyze_match_btn", lang),
                    callback_data=f"analyze_match_{match['match_id']}"
                )])

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, f"hot_match_{match['match_id']}")
                sent_count += 1

        except Exception as e:
            logger.error(f"Failed to send hot match alert to {user_id}: {e}")

    logger.info(f"Hot match alerts sent to {sent_count} users")


async def send_new_user_onboarding(context: ContextTypes.DEFAULT_TYPE, user_id: int, lang: str):
    """Send onboarding sequence for new users - shows ONLY strong stats (>70%) for marketing"""
    try:
        # Get real bot stats
        bot_stats = get_bot_accuracy_stats()

        # Build stats text showing ONLY strong points (>70%)
        strong_points = []

        # Check overall accuracy
        overall_acc = bot_stats.get("overall_accuracy", 0)
        if overall_acc >= 70:
            strong_points.append(("overall", overall_acc))

        # Check by confidence range - high confidence is usually better
        by_conf = bot_stats.get("by_confidence", {})
        for conf_range in ["80-100%", "70-79%"]:
            conf_data = by_conf.get(conf_range, {})
            if conf_data.get("accuracy", 0) >= 70 and conf_data.get("total", 0) >= 5:
                strong_points.append((f"conf_{conf_range}", conf_data["accuracy"]))
                break  # Only show one

        # Check best bet types
        by_type = bot_stats.get("by_bet_type", {})
        best_types = []
        for bet_type, data in by_type.items():
            if data.get("accuracy", 0) >= 70 and data.get("total", 0) >= 5:
                best_types.append((bet_type, data["accuracy"]))
        best_types.sort(key=lambda x: x[1], reverse=True)

        # Format multilingual stats - only show strong points
        def format_strong_stats(lang_code: str) -> str:
            labels = {
                "ru": {"title": "📊 **Сильные стороны:**", "overall": "Общая точность",
                       "conf": "Точность топ-ставок", "type": "Лучшие типы ставок"},
                "en": {"title": "📊 **Our strengths:**", "overall": "Overall accuracy",
                       "conf": "High confidence accuracy", "type": "Best bet types"},
                "pt": {"title": "📊 **Nossos pontos fortes:**", "overall": "Precisão geral",
                       "conf": "Precisão alta confiança", "type": "Melhores tipos"},
                "es": {"title": "📊 **Nuestros puntos fuertes:**", "overall": "Precisión general",
                       "conf": "Precisión alta confianza", "type": "Mejores tipos"},
                "id": {"title": "📊 **Keunggulan kami:**", "overall": "Akurasi keseluruhan",
                       "conf": "Akurasi prediksi top", "type": "Jenis taruhan terbaik"}
            }
            lbl = labels.get(lang_code, labels["en"])

            lines = [lbl["title"]]
            for point_type, acc in strong_points:
                if point_type == "overall":
                    lines.append(f"• {lbl['overall']}: **{acc}%**")
                elif point_type.startswith("conf_"):
                    lines.append(f"• {lbl['conf']}: **{acc}%**")

            if best_types[:2]:
                type_names = [f"{t[0]} ({t[1]}%)" for t in best_types[:2]]
                lines.append(f"• {lbl['type']}: {', '.join(type_names)}")

            return "\n".join(lines) if len(lines) > 1 else ""

        stats_text_formatted = format_strong_stats(lang)

        reminder_text = {
            "ru": "⏰ **Ещё не пробовал?**\n\nНажми кнопку — получи первый прогноз бесплатно!",
            "en": "⏰ **Haven't tried yet?**\n\nTap a button — get your first prediction free!",
            "pt": "⏰ **Ainda não testou?**\n\nToque no botão — obtenha sua primeira previsão grátis!",
            "es": "⏰ **¿Aún no lo probaste?**\n\n¡Toca el botón — obtén tu primer pronóstico gratis!",
            "id": "⏰ **Belum mencoba?**\n\nKetuk tombol — dapatkan prediksi pertama gratis!"
        }

        text = reminder_text.get(lang, reminder_text["en"])
        # Only add stats if we have strong points to show
        if stats_text_formatted:
            text += "\n\n" + stats_text_formatted
        text += f"\n\n{get_text('onboard_try_now', lang)}"

        keyboard = [
            [InlineKeyboardButton(get_text("try_prediction_btn", lang), callback_data="cmd_recommend")],
            [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today"),
             InlineKeyboardButton(get_text("live_alerts", lang), callback_data="cmd_live")],
        ]
        # Add 1win button only if monetization enabled
        affiliate_url = get_affiliate_link(user_id)
        if affiliate_url:
            keyboard.append([InlineKeyboardButton(get_text("open_1win_btn", lang), url=affiliate_url)])

        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        # Schedule follow-up message in 4 hours
        mark_notification_sent(user_id, "onboarding_sent")

    except Exception as e:
        logger.error(f"Failed to send onboarding to {user_id}: {e}")


def user_has_made_prediction(user_id: int) -> bool:
    """Check if user has made at least one prediction request"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT daily_requests FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return row[0] > 0  # Has made at least 1 request
        return False
    except Exception as e:
        logger.error(f"Error checking user activity: {e}")
        return True  # Assume active to avoid spamming


# Reminder messages for inactive users (multilingual)
# More aggressive schedule for first hours to activate new users
INACTIVE_USER_REMINDERS = {
    "10m": {
        "ru": "👋 **Добро пожаловать!**\n\nГотов к первому прогнозу?\nНажми кнопку — AI уже анализирует матчи!",
        "en": "👋 **Welcome!**\n\nReady for your first prediction?\nTap the button — AI is analyzing matches!",
        "pt": "👋 **Bem-vindo!**\n\nPronto para sua primeira previsão?\nToque no botão — IA está analisando!",
        "es": "👋 **¡Bienvenido!**\n\n¿Listo para tu primer pronóstico?\n¡Toca el botón — IA está analizando!",
        "id": "👋 **Selamat datang!**\n\nSiap untuk prediksi pertama?\nKetuk tombol — AI sedang menganalisis!",
    },
    "30m": {
        "ru": "⚽ **Горячий матч!**\n\nПрямо сейчас идёт интересная игра.\nПолучи прогноз за 10 секунд — бесплатно!",
        "en": "⚽ **Hot match!**\n\nExciting game happening now.\nGet prediction in 10 seconds — free!",
        "pt": "⚽ **Jogo quente!**\n\nJogo emocionante agora.\nObtenha previsão em 10 segundos — grátis!",
        "es": "⚽ **¡Partido caliente!**\n\nJuego emocionante ahora.\n¡Pronóstico en 10 segundos — gratis!",
        "id": "⚽ **Pertandingan panas!**\n\nPertandingan seru sedang berlangsung.\nPrediksi dalam 10 detik — gratis!",
    },
    "1h": {
        "ru": "⏰ **Прошёл час!**\n\nТы ещё не попробовал AI-прогнозы.\nЭто бесплатно — просто нажми кнопку!",
        "en": "⏰ **One hour passed!**\n\nYou haven't tried AI predictions yet.\nIt's free — just tap a button!",
        "pt": "⏰ **Uma hora se passou!**\n\nVocê ainda não testou as previsões AI.\nÉ grátis — toque no botão!",
        "es": "⏰ **¡Pasó una hora!**\n\nAún no probaste los pronósticos AI.\n¡Es gratis — toca el botón!",
        "id": "⏰ **Satu jam berlalu!**\n\nAnda belum mencoba prediksi AI.\nGratis — ketuk tombol!",
    },
    "2h": {
        "ru": "🎰 **Уже 2 часа!**\n\nДругие пользователи получают прогнозы.\nПопробуй — это реально работает!",
        "en": "🎰 **2 hours already!**\n\nOther users are getting predictions.\nTry it — it really works!",
        "pt": "🎰 **Já 2 horas!**\n\nOutros usuários estão recebendo previsões.\nTeste — realmente funciona!",
        "es": "🎰 **¡Ya 2 horas!**\n\nOtros usuarios reciben pronósticos.\n¡Pruébalo — realmente funciona!",
        "id": "🎰 **Sudah 2 jam!**\n\nPengguna lain sudah dapat prediksi.\nCoba — ini benar-benar bekerja!",
    },
    "4h": {
        "ru": "🔥 **4 часа прошло!**\n\nСегодня уже было много удачных прогнозов.\nНе упусти следующий — нажми кнопку!",
        "en": "🔥 **4 hours passed!**\n\nMany successful predictions today.\nDon't miss the next one — tap now!",
        "pt": "🔥 **4 horas passaram!**\n\nMuitas previsões certeiras hoje.\nNão perca a próxima — toque agora!",
        "es": "🔥 **¡4 horas pasaron!**\n\nMuchos pronósticos exitosos hoy.\n¡No pierdas el próximo — toca ahora!",
        "id": "🔥 **4 jam berlalu!**\n\nBanyak prediksi sukses hari ini.\nJangan lewatkan — ketuk sekarang!",
    },
    "8h": {
        "ru": "📊 **Наш AI работает 24/7**\n\nУже проанализировано 100+ матчей.\nПопробуй — это займёт 10 секунд!",
        "en": "📊 **Our AI works 24/7**\n\n100+ matches analyzed.\nTry it — takes 10 seconds!",
        "pt": "📊 **Nossa IA trabalha 24/7**\n\n100+ jogos analisados.\nTeste — leva 10 segundos!",
        "es": "📊 **Nuestra IA trabaja 24/7**\n\n100+ partidos analizados.\n¡Pruébalo — toma 10 segundos!",
        "id": "📊 **AI kami bekerja 24/7**\n\n100+ pertandingan dianalisis.\nCoba — hanya 10 detik!",
    },
    "24h": {
        "ru": "⚡ **Прошли сутки!**\n\nДругие пользователи уже получили прогнозы.\nНе упусти свой шанс — это бесплатно!",
        "en": "⚡ **24 hours passed!**\n\nOther users already got predictions.\nDon't miss your chance — it's free!",
        "pt": "⚡ **24 horas se passaram!**\n\nOutros usuários já receberam previsões.\nNão perca sua chance — é grátis!",
        "es": "⚡ **¡Pasaron 24 horas!**\n\nOtros usuarios ya recibieron pronósticos.\n¡No pierdas tu oportunidad — es gratis!",
        "id": "⚡ **24 jam berlalu!**\n\nPengguna lain sudah mendapat prediksi.\nJangan lewatkan — gratis!",
    },
    "48h": {
        "ru": "💎 **Последнее напоминание!**\n\nМы анализируем матчи каждый день.\nПопробуй хотя бы раз — тебе понравится!",
        "en": "💎 **Last reminder!**\n\nWe analyze matches daily.\nTry at least once — you'll love it!",
        "pt": "💎 **Último lembrete!**\n\nAnalisamos jogos diariamente.\nTeste pelo menos uma vez — você vai gostar!",
        "es": "💎 **¡Último recordatorio!**\n\nAnalizamos partidos diariamente.\n¡Prueba al menos una vez — te gustará!",
        "id": "💎 **Pengingat terakhir!**\n\nKami menganalisis pertandingan setiap hari.\nCoba sekali — Anda akan suka!",
    },
}


async def send_inactive_user_reminder(context: ContextTypes.DEFAULT_TYPE, user_id: int, lang: str, reminder_key: str):
    """Send reminder to inactive user if they haven't made any predictions"""
    try:
        # Check if user has made any predictions
        if user_has_made_prediction(user_id):
            logger.info(f"User {user_id} already active, skipping {reminder_key} reminder")
            return

        # Check if user hasn't blocked the bot
        user = get_user(user_id)
        if not user:
            return

        # Get reminder text
        reminder_texts = INACTIVE_USER_REMINDERS.get(reminder_key, INACTIVE_USER_REMINDERS["1h"])
        text = reminder_texts.get(lang, reminder_texts["en"])

        # Add stats for credibility - only if >70%
        bot_stats = get_bot_accuracy_stats()
        accuracy = bot_stats.get("overall_accuracy", 0)

        if accuracy >= 70:
            stats_line = {
                "ru": f"\n\n📈 Точность наших прогнозов: {accuracy}%",
                "en": f"\n\n📈 Our prediction accuracy: {accuracy}%",
                "pt": f"\n\n📈 Nossa precisão: {accuracy}%",
                "es": f"\n\n📈 Nuestra precisión: {accuracy}%",
                "id": f"\n\n📈 Akurasi prediksi: {accuracy}%",
            }
            text += stats_line.get(lang, stats_line["en"])

        keyboard = [
            [InlineKeyboardButton(get_text("try_prediction_btn", lang), callback_data="cmd_recommend")],
            [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")],
        ]
        # Add 1win button only if monetization enabled
        affiliate_url = get_affiliate_link(user_id)
        if affiliate_url:
            keyboard.append([InlineKeyboardButton(get_text("open_1win_btn", lang), url=affiliate_url)])

        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

        logger.info(f"Sent {reminder_key} reminder to inactive user {user_id}")
        mark_notification_sent(user_id, f"reminder_{reminder_key}")

    except Exception as e:
        logger.error(f"Failed to send {reminder_key} reminder to {user_id}: {e}")


def schedule_inactive_user_reminders(context, user_id: int, lang: str):
    """Schedule all reminder messages for a new user.

    More aggressive schedule for first hours to activate users:
    - 10min, 30min, 1h, 2h, 4h - intensive first-day activation
    - 8h, 24h, 48h - follow-up reminders
    """
    # Aggressive reminder schedule for first hours
    reminder_schedule = [
        ("10m", 600),       # 10 minutes - welcome nudge
        ("30m", 1800),      # 30 minutes - hot match alert
        ("1h", 3600),       # 1 hour
        ("2h", 7200),       # 2 hours - social proof
        ("4h", 14400),      # 4 hours - success stories
        ("8h", 28800),      # 8 hours - AI working 24/7
        ("24h", 86400),     # 24 hours
        ("48h", 172800),    # 48 hours - last reminder
    ]

    for reminder_key, delay_seconds in reminder_schedule:
        async def reminder_callback(ctx, uid=user_id, lg=lang, rk=reminder_key):
            await send_inactive_user_reminder(ctx, uid, lg, rk)

        context.job_queue.run_once(
            reminder_callback,
            when=delay_seconds,
            name=f"reminder_{reminder_key}_{user_id}"
        )

    logger.info(f"Scheduled 8 activation reminders for new user {user_id}")


# Re-engagement alerts for users inactive 12+ hours (multilingual)
REENGAGEMENT_MESSAGES = {
    "12h": {
        "ru": "👋 **Давно не виделись!**\n\nЗа последние 12 часов было много интересных матчей.\nПроверь, что сегодня на прогнозе!",
        "en": "👋 **Long time no see!**\n\nLots of interesting matches in the last 12 hours.\nCheck today's predictions!",
        "pt": "👋 **Há quanto tempo!**\n\nMuitos jogos interessantes nas últimas 12 horas.\nConfira as previsões de hoje!",
        "es": "👋 **¡Cuánto tiempo!**\n\nMuchos partidos interesantes en las últimas 12 horas.\n¡Mira los pronósticos de hoy!",
        "id": "👋 **Lama tidak bertemu!**\n\nBanyak pertandingan menarik 12 jam terakhir.\nCek prediksi hari ini!",
    },
    "24h": {
        "ru": "⚽ **Пропустил целый день!**\n\nВчера было несколько отличных прогнозов.\nСегодня тоже есть горячие матчи — не пропусти!",
        "en": "⚽ **Missed a whole day!**\n\nYesterday had some great predictions.\nToday has hot matches too — don't miss out!",
        "pt": "⚽ **Perdeu um dia inteiro!**\n\nOntem teve ótimas previsões.\nHoje também tem jogos quentes — não perca!",
        "es": "⚽ **¡Te perdiste un día entero!**\n\nAyer hubo excelentes pronósticos.\n¡Hoy también hay partidos calientes — no te lo pierdas!",
        "id": "⚽ **Melewatkan sehari penuh!**\n\nKemarin ada prediksi bagus.\nHari ini juga ada pertandingan panas — jangan lewatkan!",
    },
    "48h": {
        "ru": "🔥 **2 дня без прогнозов?**\n\nМы работали — анализировали матчи, искали value ставки.\nВозвращайся — бесплатный прогноз ждёт!",
        "en": "🔥 **2 days without predictions?**\n\nWe were working — analyzing matches, finding value bets.\nCome back — free prediction awaits!",
        "pt": "🔥 **2 dias sem previsões?**\n\nEstávamos trabalhando — analisando jogos, achando apostas de valor.\nVolte — previsão grátis te espera!",
        "es": "🔥 **¿2 días sin pronósticos?**\n\nEstuvimos trabajando — analizando partidos, buscando value.\n¡Vuelve — pronóstico gratis te espera!",
        "id": "🔥 **2 hari tanpa prediksi?**\n\nKami bekerja — menganalisis pertandingan, mencari value bet.\nKembali — prediksi gratis menunggu!",
    },
}


async def send_reengagement_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Send re-engagement alerts to users inactive for 12+, 24+, 48+ hours"""
    logger.info("Running re-engagement alerts...")

    # Define time windows (hours_min, hours_max, alert_type)
    time_windows = [
        (12, 24, "12h"),    # 12-24 hours inactive
        (24, 48, "24h"),    # 24-48 hours inactive
        (48, 96, "48h"),    # 48-96 hours inactive
    ]

    total_sent = 0

    for hours_min, hours_max, alert_type in time_windows:
        try:
            conn = get_db_connection()
            c = conn.cursor()
            # Get users who were active but became inactive in this window
            c.execute("""SELECT user_id, language FROM users
                         WHERE last_active BETWEEN datetime('now', ? || ' hours')
                         AND datetime('now', ? || ' hours')
                         AND daily_requests > 0""",
                      (f"-{hours_max}", f"-{hours_min}"))
            users = c.fetchall()
            conn.close()
        except Exception as e:
            logger.error(f"Error getting {alert_type} inactive users: {e}")
            continue

        if not users:
            continue

        for user_id, lang in users:
            lang = lang or "ru"

            # Check cooldown - don't spam
            if not should_send_notification(user_id, f"reengagement_{alert_type}", cooldown_hours=24):
                continue

            try:
                messages = REENGAGEMENT_MESSAGES.get(alert_type, REENGAGEMENT_MESSAGES["12h"])
                text = messages.get(lang, messages["en"])

                # Add strong stats if available
                bot_stats = get_bot_accuracy_stats()
                accuracy = bot_stats.get("overall_accuracy", 0)
                if accuracy >= 70:
                    stats_line = {
                        "ru": f"\n\n📈 Текущая точность: **{accuracy}%**",
                        "en": f"\n\n📈 Current accuracy: **{accuracy}%**",
                        "pt": f"\n\n📈 Precisão atual: **{accuracy}%**",
                        "es": f"\n\n📈 Precisión actual: **{accuracy}%**",
                        "id": f"\n\n📈 Akurasi saat ini: **{accuracy}%**",
                    }
                    text += stats_line.get(lang, stats_line["en"])

                keyboard = [
                    [InlineKeyboardButton(get_text("try_prediction_btn", lang), callback_data="cmd_recommend")],
                    [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")],
                ]
                # Add 1win button only if monetization enabled
                affiliate_url = get_affiliate_link(user_id)
                if affiliate_url:
                    keyboard.append([InlineKeyboardButton(get_text("open_1win_btn", lang), url=affiliate_url)])

                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, f"reengagement_{alert_type}")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Failed to send {alert_type} re-engagement to {user_id}: {e}")

    logger.info(f"Re-engagement alerts sent: {total_sent}")


# =============================================================================
# SUCCESS TRIGGER SYSTEM - Smart reactivation based on bot performance
# =============================================================================

SUCCESS_TRIGGER_MESSAGES = {
    "win_streak": {
        "ru": "🔥 **СЕРИЯ ПОБЕД!**\n\nМы угадали **{streak} прогнозов подряд!**\nПрисоединяйся — горячая серия продолжается!",
        "en": "🔥 **WIN STREAK!**\n\nWe hit **{streak} predictions in a row!**\nJoin now — hot streak continues!",
        "pt": "🔥 **SEQUÊNCIA DE VITÓRIAS!**\n\nAcertamos **{streak} previsões seguidas!**\nEntre agora — sequência continua!",
        "es": "🔥 **¡RACHA GANADORA!**\n\n¡Acertamos **{streak} pronósticos seguidos!**\n¡Únete — la racha continúa!",
        "id": "🔥 **STREAK KEMENANGAN!**\n\nKami menang **{streak} prediksi berturut-turut!**\nGabung sekarang — streak berlanjut!",
    },
    "hot_day": {
        "ru": "⚡ **ГОРЯЧИЙ ДЕНЬ!**\n\nСегодня уже **{wins} из {total} прогнозов** сыграли!\nТочность дня: **{accuracy}%** 🎯",
        "en": "⚡ **HOT DAY!**\n\nToday **{wins} of {total} predictions** hit!\nDaily accuracy: **{accuracy}%** 🎯",
        "pt": "⚡ **DIA QUENTE!**\n\nHoje **{wins} de {total} previsões** acertaram!\nPrecisão do dia: **{accuracy}%** 🎯",
        "es": "⚡ **¡DÍA CALIENTE!**\n\n¡Hoy **{wins} de {total} pronósticos** acertaron!\nPrecisión del día: **{accuracy}%** 🎯",
        "id": "⚡ **HARI PANAS!**\n\nHari ini **{wins} dari {total} prediksi** menang!\nAkurasi hari ini: **{accuracy}%** 🎯",
    },
    "missed_wins": {
        "ru": "😱 **Ты пропустил!**\n\nЗа последние дни **{missed} наших прогнозов сыграли**.\nНе пропусти следующий — зайди прямо сейчас!",
        "en": "😱 **You missed out!**\n\nLast few days **{missed} of our predictions won**.\nDon't miss the next one — check now!",
        "pt": "😱 **Você perdeu!**\n\nÚltimos dias **{missed} previsões nossas ganharam**.\nNão perca a próxima — confira agora!",
        "es": "😱 **¡Te lo perdiste!**\n\nÚltimos días **{missed} pronósticos nuestros ganaron**.\n¡No pierdas el próximo — mira ahora!",
        "id": "😱 **Anda melewatkan!**\n\nBeberapa hari terakhir **{missed} prediksi kami menang**.\nJangan lewatkan lagi — cek sekarang!",
    },
    "big_odds_win": {
        "ru": "💰 **КРУПНЫЙ ВЫИГРЫШ!**\n\nТолько что сыграл прогноз с коэффициентом **{odds}**!\nТакие возможности нельзя упускать!",
        "en": "💰 **BIG WIN!**\n\nJust hit a prediction with odds **{odds}**!\nDon't miss opportunities like this!",
        "pt": "💰 **GRANDE VITÓRIA!**\n\nAcabamos de acertar odds de **{odds}**!\nNão perca oportunidades assim!",
        "es": "💰 **¡GRAN VICTORIA!**\n\n¡Acertamos pronóstico con cuota **{odds}**!\n¡No pierdas oportunidades así!",
        "id": "💰 **KEMENANGAN BESAR!**\n\nBaru saja menang dengan odds **{odds}**!\nJangan lewatkan kesempatan ini!",
    },
    "comeback": {
        "ru": "🚀 **Мы вернулись в форму!**\n\nПосле небольшой паузы снова побеждаем!\nПоследние **{wins} из {total}** прогнозов сыграли.",
        "en": "🚀 **We're back on track!**\n\nAfter a short pause, winning again!\nLast **{wins} of {total}** predictions hit.",
        "pt": "🚀 **Voltamos à forma!**\n\nApós pausa curta, ganhando novamente!\nÚltimas **{wins} de {total}** previsões acertaram.",
        "es": "🚀 **¡Volvimos a la forma!**\n\nDespués de una pausa, ¡ganando de nuevo!\nÚltimos **{wins} de {total}** pronósticos acertaron.",
        "id": "🚀 **Kami kembali!**\n\nSetelah jeda singkat, menang lagi!\n**{wins} dari {total}** prediksi terakhir menang.",
    },
}


def get_recent_prediction_stats() -> dict:
    """Get recent prediction statistics for trigger calculations."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Today's stats
        c.execute("""
            SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions
            WHERE is_correct IS NOT NULL
            AND date(predicted_at) = date('now')
        """)
        row = c.fetchone()
        today_total = row[0] or 0
        today_wins = row[1] or 0

        # Last 3 days stats (for missed wins)
        c.execute("""
            SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
            FROM predictions
            WHERE is_correct IS NOT NULL
            AND predicted_at >= datetime('now', '-3 days')
        """)
        row = c.fetchone()
        recent_total = row[0] or 0
        recent_wins = row[1] or 0

        # Current win streak
        c.execute("""
            SELECT is_correct FROM predictions
            WHERE is_correct IS NOT NULL
            ORDER BY predicted_at DESC
            LIMIT 20
        """)
        results = c.fetchall()
        win_streak = 0
        for r in results:
            if r[0] == 1:
                win_streak += 1
            else:
                break

        # Last big odds win (>2.5)
        c.execute("""
            SELECT odds FROM predictions
            WHERE is_correct = 1 AND odds >= 2.5
            AND predicted_at >= datetime('now', '-24 hours')
            ORDER BY odds DESC LIMIT 1
        """)
        big_odds_row = c.fetchone()
        big_odds_win = big_odds_row[0] if big_odds_row else None

        # Recent form (last 10)
        c.execute("""
            SELECT SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END), COUNT(*)
            FROM (
                SELECT is_correct FROM predictions
                WHERE is_correct IS NOT NULL
                ORDER BY predicted_at DESC
                LIMIT 10
            )
        """)
        row = c.fetchone()
        last10_wins = row[0] or 0
        last10_total = row[1] or 0

        conn.close()

        return {
            "today_wins": today_wins,
            "today_total": today_total,
            "today_accuracy": round(today_wins / today_total * 100) if today_total > 0 else 0,
            "recent_wins": recent_wins,
            "recent_total": recent_total,
            "win_streak": win_streak,
            "big_odds_win": big_odds_win,
            "last10_wins": last10_wins,
            "last10_total": last10_total,
        }
    except Exception as e:
        logger.error(f"Error getting prediction stats: {e}")
        return {
            "today_wins": 0, "today_total": 0, "today_accuracy": 0,
            "recent_wins": 0, "recent_total": 0, "win_streak": 0,
            "big_odds_win": None, "last10_wins": 0, "last10_total": 0
        }


def get_inactive_users_for_triggers(min_hours: int = 6, max_hours: int = 168) -> list:
    """Get users who are inactive for trigger notifications.

    Args:
        min_hours: Minimum hours of inactivity (default 6 hours)
        max_hours: Maximum hours of inactivity (default 7 days)
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT user_id, language FROM users
            WHERE last_active BETWEEN datetime('now', ? || ' hours')
                                  AND datetime('now', ? || ' hours')
            AND daily_requests > 0
        """, (f"-{max_hours}", f"-{min_hours}"))
        users = c.fetchall()
        conn.close()
        return users
    except Exception as e:
        logger.error(f"Error getting inactive users for triggers: {e}")
        return []


async def send_success_trigger_alerts(context: ContextTypes.DEFAULT_TYPE):
    """Send success-based trigger alerts to inactive users.

    Triggers:
    1. Win Streak (3+ wins in a row) → Send to 6-48h inactive users
    2. Hot Day (>60% accuracy today with 5+ predictions) → Send to 12-72h inactive
    3. Big Odds Win (>2.5 odds won) → Send to 6-24h inactive users
    4. Missed Wins (user inactive, we had wins) → Send to 24-168h inactive
    5. Comeback (back to form after losses) → Send to 48-168h inactive
    """
    logger.info("Running success trigger alerts...")

    stats = get_recent_prediction_stats()
    total_sent = 0

    # 1. WIN STREAK TRIGGER (3+ consecutive wins)
    if stats["win_streak"] >= 3:
        users = get_inactive_users_for_triggers(min_hours=6, max_hours=48)
        for user_id, lang in users:
            lang = lang or "ru"
            if not should_send_notification(user_id, "trigger_win_streak", cooldown_hours=12):
                continue

            try:
                text = SUCCESS_TRIGGER_MESSAGES["win_streak"].get(lang, SUCCESS_TRIGGER_MESSAGES["win_streak"]["en"])
                text = text.format(streak=stats["win_streak"])

                keyboard = [
                    [InlineKeyboardButton("🎯 Получить прогноз" if lang == "ru" else "🎯 Get Prediction",
                                          callback_data="cmd_recommend")],
                    [InlineKeyboardButton("📊 Матчи сегодня" if lang == "ru" else "📊 Today's Matches",
                                          callback_data="cmd_today")]
                ]

                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, "trigger_win_streak")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Failed to send win streak trigger to {user_id}: {e}")

    # 2. HOT DAY TRIGGER (>60% accuracy today with 5+ predictions)
    if stats["today_total"] >= 5 and stats["today_accuracy"] >= 60:
        users = get_inactive_users_for_triggers(min_hours=12, max_hours=72)
        for user_id, lang in users:
            lang = lang or "ru"
            if not should_send_notification(user_id, "trigger_hot_day", cooldown_hours=24):
                continue

            try:
                text = SUCCESS_TRIGGER_MESSAGES["hot_day"].get(lang, SUCCESS_TRIGGER_MESSAGES["hot_day"]["en"])
                text = text.format(
                    wins=stats["today_wins"],
                    total=stats["today_total"],
                    accuracy=stats["today_accuracy"]
                )

                keyboard = [
                    [InlineKeyboardButton("🔥 Не пропусти!" if lang == "ru" else "🔥 Don't Miss!",
                                          callback_data="cmd_recommend")]
                ]

                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, "trigger_hot_day")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Failed to send hot day trigger to {user_id}: {e}")

    # 3. BIG ODDS WIN TRIGGER (odds >= 2.5 won in last 24h)
    if stats["big_odds_win"] and stats["big_odds_win"] >= 2.5:
        users = get_inactive_users_for_triggers(min_hours=6, max_hours=24)
        for user_id, lang in users:
            lang = lang or "ru"
            if not should_send_notification(user_id, "trigger_big_odds", cooldown_hours=24):
                continue

            try:
                text = SUCCESS_TRIGGER_MESSAGES["big_odds_win"].get(lang, SUCCESS_TRIGGER_MESSAGES["big_odds_win"]["en"])
                text = text.format(odds=round(stats["big_odds_win"], 2))

                keyboard = [
                    [InlineKeyboardButton("💰 Хочу такой!" if lang == "ru" else "💰 I Want That!",
                                          callback_data="cmd_recommend")]
                ]

                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, "trigger_big_odds")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Failed to send big odds trigger to {user_id}: {e}")

    # 4. MISSED WINS TRIGGER (for longer inactive users)
    if stats["recent_wins"] >= 3:
        users = get_inactive_users_for_triggers(min_hours=24, max_hours=168)
        for user_id, lang in users:
            lang = lang or "ru"
            if not should_send_notification(user_id, "trigger_missed_wins", cooldown_hours=48):
                continue

            try:
                text = SUCCESS_TRIGGER_MESSAGES["missed_wins"].get(lang, SUCCESS_TRIGGER_MESSAGES["missed_wins"]["en"])
                text = text.format(missed=stats["recent_wins"])

                keyboard = [
                    [InlineKeyboardButton("😤 Больше не пропущу!" if lang == "ru" else "😤 Won't Miss Again!",
                                          callback_data="cmd_recommend")],
                    [InlineKeyboardButton("📅 Матчи сегодня" if lang == "ru" else "📅 Today's Matches",
                                          callback_data="cmd_today")]
                ]

                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, "trigger_missed_wins")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Failed to send missed wins trigger to {user_id}: {e}")

    # 5. COMEBACK TRIGGER (good recent form after slump)
    if stats["last10_total"] >= 8 and stats["last10_wins"] >= 6:
        users = get_inactive_users_for_triggers(min_hours=48, max_hours=168)
        for user_id, lang in users:
            lang = lang or "ru"
            if not should_send_notification(user_id, "trigger_comeback", cooldown_hours=72):
                continue

            try:
                text = SUCCESS_TRIGGER_MESSAGES["comeback"].get(lang, SUCCESS_TRIGGER_MESSAGES["comeback"]["en"])
                text = text.format(wins=stats["last10_wins"], total=stats["last10_total"])

                keyboard = [
                    [InlineKeyboardButton("🚀 Вернуться!" if lang == "ru" else "🚀 Come Back!",
                                          callback_data="cmd_recommend")]
                ]

                await context.bot.send_message(
                    chat_id=user_id, text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, "trigger_comeback")
                total_sent += 1

                if total_sent % 30 == 0:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Failed to send comeback trigger to {user_id}: {e}")

    logger.info(f"Success trigger alerts sent: {total_sent}")
    return total_sent


async def send_marketing_notifications(context: ContextTypes.DEFAULT_TYPE):
    """Send periodic marketing notifications (referral reminders, social proof, friend wins)."""
    import random

    logger.info("Running marketing notifications job...")

    # Get all active users
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""SELECT user_id, language FROM users
                     WHERE last_active >= datetime('now', '-7 days')""")
        active_users = c.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"Error getting active users: {e}")
        return

    # Get social stats once
    social_stats = get_social_stats()

    for user_id, lang in active_users:
        lang = lang or "ru"

        try:
            # Random chance to send each type of notification
            notification_type = random.choice([
                "referral_reminder",
                "social_proof",
                "friend_wins",
                None, None, None  # 50% chance of no notification
            ])

            if notification_type is None:
                continue

            if not should_send_notification(user_id, notification_type, cooldown_hours=48):
                continue

            if notification_type == "referral_reminder":
                # Send referral reminder
                ref_link = get_referral_link(user_id)
                text = get_text("referral_reminder", lang).format(link=ref_link)
                keyboard = [[InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")]]

            elif notification_type == "social_proof":
                # Send social proof
                if social_stats["wins_today"] > 0:
                    text = get_text("social_wins_today", lang).format(count=social_stats["wins_today"])
                    if social_stats["best_win"]:
                        text += f"\n\n{get_text('social_top_win', lang).format(odds=social_stats['best_win']['odds'], match=social_stats['best_win']['match'])}"
                    text += f"\n\n{get_text('social_accuracy', lang).format(accuracy=social_stats['accuracy'])}"
                    keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]
                else:
                    continue

            elif notification_type == "friend_wins":
                # Notify about friend wins
                friend_wins = get_friend_wins(user_id, lang)
                if friend_wins:
                    win = friend_wins[0]
                    text = get_text("social_friend_won", lang).format(
                        name=win["name"],
                        match=win["match"],
                        bet=win["bet"],
                        odds=win["odds"]
                    )
                    keyboard = [[InlineKeyboardButton(get_text("referral_btn", lang), callback_data="cmd_referral")]]
                else:
                    continue
            else:
                continue

            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            mark_notification_sent(user_id, notification_type)
            logger.info(f"Sent {notification_type} to user {user_id}")

        except Exception as e:
            logger.error(f"Error sending marketing notification to {user_id}: {e}")


async def check_streak_milestones(context: ContextTypes.DEFAULT_TYPE):
    """Check and notify users about streak milestones."""
    logger.info("Checking streak milestones...")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Users with notable streaks who haven't been notified today
        c.execute("""SELECT user_id, language, streak_days FROM users
                     WHERE streak_days IN (3, 7, 14, 30, 50, 100)
                     AND last_streak_date = date('now')""")
        users = c.fetchall()
        conn.close()

        for user_id, lang, streak in users:
            lang = lang or "ru"

            if not should_send_notification(user_id, f"streak_{streak}", cooldown_hours=24):
                continue

            text = get_text("streak_milestone", lang).format(days=streak)
            keyboard = [[InlineKeyboardButton(get_text("recommendations", lang), callback_data="cmd_recommend")]]

            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                mark_notification_sent(user_id, f"streak_{streak}")
            except Exception as e:
                logger.error(f"Error sending streak notification to {user_id}: {e}")

    except Exception as e:
        logger.error(f"Error in check_streak_milestones: {e}")


# ===== WEB SERVER FOR POSTBACK =====

from aiohttp import web


def verify_webhook_signature(payload: str, signature: str, secret: str) -> bool:
    """Verify webhook signature using HMAC-SHA256."""
    if not secret:
        # If no secret configured, skip verification (but log warning)
        logger.warning("Webhook secret not configured - skipping signature verification")
        return True

    if not signature:
        logger.warning("No signature provided in webhook request")
        return False

    # Calculate expected signature
    expected = hmac.new(
        secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # Compare signatures (constant-time comparison to prevent timing attacks)
    return hmac.compare_digest(expected, signature)


async def handle_postback(request):
    """Handle 1win postback webhook."""
    try:
        # Verify signature if secret is configured
        if WEBHOOK_SECRET_1WIN:
            raw_body = await request.text()
            signature = request.headers.get("X-Signature", "") or request.query.get("signature", "")

            if not verify_webhook_signature(raw_body, signature, WEBHOOK_SECRET_1WIN):
                logger.warning(f"Invalid signature for 1win postback from {request.remote}")
                return web.json_response({"status": "error", "reason": "invalid signature"}, status=401)

        # Get data from query params or POST body
        if request.method == "POST":
            try:
                data = await request.json()
            except:
                data = dict(await request.post())
        else:
            data = dict(request.query)

        logger.info(f"Received postback: {data}")

        result = process_1win_postback(data)

        return web.json_response(result)
    except Exception as e:
        logger.error(f"Postback error: {e}")
        return web.json_response({"status": "error", "reason": str(e)}, status=500)


async def handle_health(request):
    """Health check endpoint."""
    return web.json_response({"status": "ok", "bot": "running"})


async def handle_crypto_webhook(request):
    """Handle CryptoBot payment webhook."""
    try:
        # Verify signature if secret is configured
        if WEBHOOK_SECRET_CRYPTO:
            raw_body = await request.text()
            signature = request.headers.get("X-Signature", "") or request.headers.get("Crypto-Pay-Api-Signature", "")

            if not verify_webhook_signature(raw_body, signature, WEBHOOK_SECRET_CRYPTO):
                logger.warning(f"Invalid signature for crypto webhook from {request.remote}")
                return web.json_response({"status": "error", "reason": "invalid signature"}, status=401)

            # Re-parse the body since we read it
            data = json.loads(raw_body)
        else:
            data = await request.json()

        logger.info(f"Received crypto webhook: {data}")

        result = process_crypto_webhook(data)

        # If payment successful, notify user via bot
        if result.get("status") == "success":
            user_id = result.get("user_id")
            days = result.get("days")
            if user_id:
                # We'll need to send notification via bot - store for later
                logger.info(f"Premium granted via crypto: user={user_id}, days={days}")

        return web.json_response(result)
    except Exception as e:
        logger.error(f"Crypto webhook error: {e}")
        return web.json_response({"status": "error", "reason": str(e)}, status=500)


async def start_web_server():
    """Start aiohttp web server for postbacks."""
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/1win/postback", handle_postback)
    app.router.add_post("/api/1win/postback", handle_postback)
    app.router.add_post("/api/crypto/webhook", handle_crypto_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_SERVER_PORT)
    await site.start()
    logger.info(f"Web server started on port {WEB_SERVER_PORT}")
    print(f"   🌐 1win postback: http://0.0.0.0:{WEB_SERVER_PORT}/api/1win/postback")
    print(f"   🌐 Crypto webhook: http://0.0.0.0:{WEB_SERVER_PORT}/api/crypto/webhook")


# ===== MAIN =====

def main():
    global live_subscribers

    # Validate configuration
    config_errors = validate_config()
    if config_errors:
        print("⚠️ Configuration warnings:")
        for error in config_errors:
            print(f"   - {error}")

    init_db()

    # Load persistent subscribers from DB
    live_subscribers = load_live_subscribers()

    print("🚀 Starting AI Betting Bot v14 (Refactored)...")
    print(f"   💾 Database: {DB_PATH}")
    print(f"   👥 Live subscribers: {len(live_subscribers)}")

    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    
    print("   ✅ Telegram")
    print(f"   ✅ Football Data ({len(COMPETITIONS)} leagues)" if FOOTBALL_API_KEY else "   ⚠️ No Football API")
    print("   ✅ Odds API (20K credits)" if ODDS_API_KEY else "   ⚠️ No Odds API")
    print("   ✅ Claude AI" if CLAUDE_API_KEY else "   ⚠️ No Claude API")
    print(f"   👑 Admins: {len(ADMIN_IDS)}" if ADMIN_IDS else "   ⚠️ No admins configured")
    print(f"   🔗 Affiliate: 1win")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("myid", myid_cmd))  # Get Telegram user ID
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("recommend", recommend_cmd))
    app.add_handler(CommandHandler("sure", sure_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("favorites", favorites_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("testalert", testalert_cmd))
    app.add_handler(CommandHandler("checkresults", check_results_cmd))
    app.add_handler(CommandHandler("forcecheck", force_check_cmd))  # Admin: force-check ALL pending
    app.add_handler(CommandHandler("analyzeall", analyze_all_cmd))  # Admin: analyze all today's matches
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("ref", referral_cmd))
    app.add_handler(CommandHandler("referral", referral_cmd))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("addpremium", addpremium_cmd))
    app.add_handler(CommandHandler("removepremium", removepremium_cmd))
    app.add_handler(CommandHandler("cleanfavs", cleanfavs_cmd))  # Clean duplicate favorites
    app.add_handler(CommandHandler("userinfo", userinfo_cmd))
    app.add_handler(CommandHandler("mlstatus", mlstatus_cmd))
    app.add_handler(CommandHandler("mltrain", mltrain_cmd))
    app.add_handler(CommandHandler("train", mltrain_cmd))  # Alias for /mltrain
    app.add_handler(CommandHandler("learnhistory", learnhistory_cmd))  # Learn from historical data
    app.add_handler(CommandHandler("accuracy", accuracy_cmd))  # Detailed accuracy analysis
    app.add_handler(CommandHandler("jobstatus", jobstatus_cmd))  # Admin: check job status and pending
    app.add_handler(CommandHandler("forceresults", forceresults_cmd))  # Admin: check results + send notifications
    app.add_handler(CommandHandler("roi", roi_cmd))  # ROI statistics

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Job Queue
    job_queue = app.job_queue
    job_queue.run_repeating(check_live_matches, interval=600, first=120)
    job_queue.run_repeating(send_daily_digest, interval=7200, first=300)
    job_queue.run_repeating(check_predictions_results, interval=600, first=180)  # Every 10 min (was 20)
    job_queue.run_repeating(track_upcoming_odds, interval=1800, first=300)  # Every 30 min - track odds for line movements
    job_queue.run_repeating(update_key_players_job, interval=43200, first=600)  # Every 12 hours - update player impact DB
    job_queue.run_repeating(train_ensemble_models_job, interval=86400, first=900)  # Every 24 hours - train ensemble ML
    # Marketing jobs
    job_queue.run_repeating(send_marketing_notifications, interval=14400, first=1800)  # Every 4 hours
    job_queue.run_repeating(check_streak_milestones, interval=3600, first=900)  # Every hour

    # Notification system jobs
    job_queue.run_repeating(send_evening_digest, interval=3600, first=300)  # Check hourly (22:00 MSK)
    job_queue.run_repeating(send_morning_alert, interval=3600, first=300)   # Check hourly (10:00 MSK)
    job_queue.run_repeating(send_inactive_user_alerts, interval=21600, first=3600)  # Every 6 hours
    job_queue.run_repeating(send_reengagement_alerts, interval=14400, first=2700)  # Every 4 hours (12h+ inactive)
    job_queue.run_repeating(send_success_trigger_alerts, interval=7200, first=3600)  # Every 2 hours (success triggers)
    job_queue.run_repeating(send_weekly_report, interval=3600, first=300)   # Check hourly (Sunday 20:00)
    job_queue.run_repeating(send_hot_match_alerts, interval=1800, first=600)  # Every 30 min

    print("\n✅ Bot v14 (Refactored) running!")
    print("   🔥 Features:")
    print("   • Reply keyboard menu (always visible)")
    print("   • Multi-language (RU/EN/PT/ES)")
    print("   • Daily limit (3 free predictions)")
    print("   • Stats by bet category")
    print("   • 1win affiliate integration + postback")
    print("   • Cup/Top club warnings")
    print(f"   • {len(COMPETITIONS)} leagues (Standard plan)")
    print("   • Live alerts system (persistent)")
    print("   • Prediction tracking")
    print("   • Daily digest")
    print("   • Admin-only debug commands")
    print("   • Async API calls (aiohttp)")

    # Run both telegram bot and web server
    async def run_all():
        # Start web server
        await start_web_server()
        # Start telegram bot
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Keep running until stopped
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()

    asyncio.run(run_all())


if __name__ == "__main__":
    main()
