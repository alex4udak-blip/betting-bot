import os
import logging
import json
import sqlite3
import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import aiohttp
import requests  # Keep for sync fallback in non-async contexts
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

# ===== CONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
# Standard plan key: 2e13bc51a3474c29b6a513feee9dd805
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
# 20K plan key: 84f7ca372aaea9b824f191d11462e393
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

FOOTBALL_API_URL = "https://api.football-data.org/v4"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

# 1WIN Affiliate Link (Universal Router - auto GEO redirect)
AFFILIATE_LINK = "https://1wfafs.life/?open=register&p=ex2m"

# Daily free limit for predictions
FREE_DAILY_LIMIT = 3

# Admin user IDs (add your Telegram user ID here)
# Get your ID by messaging @userinfobot on Telegram
ADMIN_IDS: set[int] = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip().isdigit()
}

def is_admin(user_id: int) -> bool:
    """Check if user is an admin"""
    return user_id in ADMIN_IDS

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
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
        timeout = aiohttp.ClientTimeout(total=15)
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
        "daily_limit": "⚠️ Достигнут лимит ({limit} прогнозов/день).\n\n💎 **Премиум доступ:**\n• R$200+ → 7 дней\n• R$500+ → 30 дней\n• R$1000+ → Навсегда\n\n👇 Сделай депозит по ссылке:",
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
        "first_start_text": "Я помогу тебе делать умные ставки на футбол с помощью AI-анализа.",
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
        "searching_match": "🔍 Ищу матч...",
        "match_not_found": "😕 Не нашёл матч: {query}",
        "available_matches": "📋 **Доступные матчи:**",
        "match_found": "✅ Нашёл: {home} vs {away}\n🏆 {comp}\n\n⏳ Собираю статистику...",
    },
    "en": {
        "welcome": "👋 Hello! I'm an AI betting bot for football.\n\nUse the menu below or type a team name.",
        "top_bets": "🔥 Top Bets",
        "matches": "⚽ Matches",
        "stats": "📊 Stats",
        "favorites": "⭐ Favorites",
        "settings": "⚙️ Settings",
        "help_btn": "❓ Help",
        "daily_limit": "⚠️ Daily limit reached ({limit} predictions).\n\n💎 **Premium access:**\n• R$200+ → 7 days\n• R$500+ → 30 days\n• R$1000+ → Lifetime\n\n👇 Make a deposit via link:",
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
        "first_start_text": "I'll help you make smart football bets using AI analysis.",
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
        "searching_match": "🔍 Searching match...",
        "match_not_found": "😕 Match not found: {query}",
        "available_matches": "📋 **Available matches:**",
        "match_found": "✅ Found: {home} vs {away}\n🏆 {comp}\n\n⏳ Gathering stats...",
    },
    "pt": {
        "welcome": "👋 Olá! Sou um bot de apostas com IA para futebol.\n\nUse o menu ou digite o nome de um time.",
        "top_bets": "🔥 Top Apostas",
        "matches": "⚽ Jogos",
        "stats": "📊 Estatísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Config",
        "help_btn": "❓ Ajuda",
        "daily_limit": "⚠️ Limite diário atingido ({limit} previsões).\n\n💎 **Acesso premium:**\n• R$200+ → 7 dias\n• R$500+ → 30 dias\n• R$1000+ → Vitalício\n\n👇 Faça um depósito pelo link:",
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
        "first_start_text": "Vou ajudá-lo a fazer apostas inteligentes no futebol usando análise de IA.",
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
        "searching_match": "🔍 Procurando jogo...",
        "match_not_found": "😕 Jogo não encontrado: {query}",
        "available_matches": "📋 **Jogos disponíveis:**",
        "match_found": "✅ Encontrado: {home} vs {away}\n🏆 {comp}\n\n⏳ Coletando estatísticas...",
    },
    "es": {
        "welcome": "👋 ¡Hola! Soy un bot de apuestas con IA para fútbol.\n\nUsa el menú o escribe el nombre de un equipo.",
        "top_bets": "🔥 Top Apuestas",
        "matches": "⚽ Partidos",
        "stats": "📊 Estadísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Ajustes",
        "help_btn": "❓ Ayuda",
        "daily_limit": "⚠️ Límite diario alcanzado ({limit} pronósticos).\n\n💎 **Acceso premium:**\n• R$200+ → 7 días\n• R$500+ → 30 días\n• R$1000+ → De por vida\n\n👇 Haz un depósito por el enlace:",
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
        "first_start_text": "Te ayudaré a hacer apuestas inteligentes en fútbol usando análisis de IA.",
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
        "searching_match": "🔍 Buscando partido...",
        "match_not_found": "😕 Partido no encontrado: {query}",
        "available_matches": "📋 **Partidos disponibles:**",
        "match_found": "✅ Encontrado: {home} vs {away}\n🏆 {comp}\n\n⏳ Recopilando estadísticas...",
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
        [KeyboardButton(get_text("settings", lang)), KeyboardButton(get_text("help_btn", lang))]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

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


# ===== DATABASE =====

DB_PATH = "/data/betting_bot.db" if os.path.exists("/data") else "betting_bot.db"

# ML Models directory
ML_MODELS_DIR = "/data/ml_models" if os.path.exists("/data") else "ml_models"
ML_MIN_SAMPLES = 50  # Minimum predictions to train model (lowered for faster start)

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
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
        c.execute("ALTER TABLE users ADD COLUMN premium_expires TEXT")
    except:
        pass

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

    conn.commit()
    conn.close()
    logger.info("Database initialized")

def get_user(user_id):
    """Get user settings"""
    conn = sqlite3.connect(DB_PATH)
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

def create_user(user_id, username=None, language="ru"):
    """Create new user"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, language) VALUES (?, ?, ?)", 
              (user_id, username, language))
    conn.commit()
    conn.close()

# Whitelist of allowed settings fields (prevents SQL injection)
ALLOWED_USER_SETTINGS = frozenset({
    'min_odds', 'max_odds', 'risk_level', 'language',
    'is_premium', 'daily_requests', 'last_request_date', 'timezone',
    'exclude_cups'
})

def update_user_settings(user_id: int, **kwargs) -> None:
    """Update user settings (SQL injection safe)"""
    conn = sqlite3.connect(DB_PATH)
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
    """Check if user has reached daily limit. Returns (can_use, remaining)"""
    logger.info(f"check_daily_limit called for user {user_id}")
    
    user = get_user(user_id)
    if not user:
        logger.info(f"User {user_id} not found in DB, allowing request")
        return True, FREE_DAILY_LIMIT
    
    # Premium users have no limit
    if user.get("is_premium", 0):
        logger.info(f"User {user_id} is PREMIUM, no limit")
        return True, 999
    
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = user.get("last_request_date") or ""  # Handle None
    daily_requests = user.get("daily_requests") or 0  # Handle None
    
    logger.info(f"User {user_id}: requests={daily_requests}, last_date='{last_date}', today={today}, limit={FREE_DAILY_LIMIT}")
    
    # Reset counter if new day or empty date
    if last_date != today:
        update_user_settings(user_id, daily_requests=0, last_request_date=today)
        logger.info(f"User {user_id}: New day, reset to 0")
        return True, FREE_DAILY_LIMIT
    
    if daily_requests >= FREE_DAILY_LIMIT:
        logger.info(f"User {user_id}: ⛔ LIMIT REACHED ({daily_requests} >= {FREE_DAILY_LIMIT})")
        return False, 0
    
    remaining = FREE_DAILY_LIMIT - daily_requests
    logger.info(f"User {user_id}: ✅ OK, remaining={remaining}")
    return True, remaining

def increment_daily_usage(user_id):
    """Increment daily usage counter"""
    logger.info(f"increment_daily_usage called for user {user_id}")
    
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
    
    if last_date != today:
        update_user_settings(user_id, daily_requests=1, last_request_date=today)
        logger.info(f"User {user_id}: First request today → 1")
    else:
        new_count = current + 1
        update_user_settings(user_id, daily_requests=new_count)
        logger.info(f"User {user_id}: {current} → {new_count}")

def add_favorite_team(user_id, team_name):
    """Add favorite team"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO favorite_teams (user_id, team_name) VALUES (?, ?)", (user_id, team_name))
    conn.commit()
    conn.close()

def remove_favorite_team(user_id, team_name):
    """Remove favorite team"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM favorite_teams WHERE user_id = ? AND team_name = ?", (user_id, team_name))
    conn.commit()
    conn.close()

def get_favorite_teams(user_id):
    """Get user's favorite teams"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT team_name FROM favorite_teams WHERE user_id = ?", (user_id,))
    teams = [row[0] for row in c.fetchall()]
    conn.close()
    return teams

def add_favorite_league(user_id, league_code):
    """Add favorite league"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO favorite_leagues (user_id, league_code) VALUES (?, ?)", (user_id, league_code))
    conn.commit()
    conn.close()

def get_favorite_leagues(user_id):
    """Get user's favorite leagues"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT league_code FROM favorite_leagues WHERE user_id = ?", (user_id,))
    leagues = [row[0] for row in c.fetchall()]
    conn.close()
    return leagues


# ===== 1WIN POSTBACK & PREMIUM SYSTEM =====

# Deposit thresholds for premium (in BRL)
PREMIUM_TIERS = {
    200: 7,      # R$200+ = 7 days
    500: 30,     # R$500+ = 30 days
    1000: 36500  # R$1000+ = Lifetime (100 years)
}

def calculate_premium_days(amount: float, currency: str = "BRL") -> int:
    """Calculate premium days based on deposit amount."""
    # Convert to BRL if needed (rough estimates)
    if currency == "USD":
        amount = amount * 5
    elif currency == "EUR":
        amount = amount * 5.5

    for threshold, days in sorted(PREMIUM_TIERS.items(), reverse=True):
        if amount >= threshold:
            return days
    return 0


def grant_premium(user_id: int, days: int) -> bool:
    """Grant premium to user for specified days."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Get current premium expiry
        c.execute("SELECT premium_expires FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()

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


def check_premium_expired(user_id: int) -> bool:
    """Check if user's premium has expired and update status if needed."""
    try:
        conn = sqlite3.connect(DB_PATH)
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
            conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id FROM deposits_1win WHERE transaction_id = ?", (transaction_id,))
        if c.fetchone():
            conn.close()
            return {"status": "duplicate", "reason": "transaction already processed"}

        # Calculate premium days
        premium_days = calculate_premium_days(amount, currency)

        if premium_days == 0:
            conn.close()
            return {"status": "ignored", "reason": f"deposit {amount} {currency} below minimum threshold"}

        # Save deposit record
        c.execute("""INSERT INTO deposits_1win
                     (user_id, onewin_user_id, amount, currency, event, transaction_id, country, premium_days)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                  (telegram_user_id, onewin_user_id, amount, currency, event, transaction_id, country, premium_days))
        conn.commit()
        conn.close()

        # Grant premium
        grant_premium(telegram_user_id, premium_days)

        return {
            "status": "success",
            "user_id": telegram_user_id,
            "amount": amount,
            "premium_days": premium_days
        }

    except Exception as e:
        logger.error(f"Error processing 1win postback: {e}")
        return {"status": "error", "reason": str(e)}


def get_affiliate_link(user_id: int) -> str:
    """Generate affiliate link with user tracking."""
    # Base 1win affiliate link with sub1 parameter for tracking
    base_link = AFFILIATE_LINK.rstrip("/")
    if "?" in base_link:
        return f"{base_link}&sub1={user_id}"
    else:
        return f"{base_link}?sub1={user_id}"


# ===== LIVE SUBSCRIBERS PERSISTENCE =====

def load_live_subscribers() -> set[int]:
    """Load live subscribers from database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM live_subscribers")
    subscribers = {row[0] for row in c.fetchall()}
    conn.close()
    logger.info(f"Loaded {len(subscribers)} live subscribers from DB")
    return subscribers


def add_live_subscriber(user_id: int) -> None:
    """Add user to live subscribers in DB"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO live_subscribers (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def remove_live_subscriber(user_id: int) -> None:
    """Remove user from live subscribers in DB"""
    conn = sqlite3.connect(DB_PATH)
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

    # Look for [ALT1], [ALT2], [ALT3] format
    for i in range(1, 4):
        alt_match = re.search(rf'\[ALT{i}\]\s*(.+?)(?=\[ALT|\n⚠️|\n✅|$)', analysis, re.IGNORECASE | re.DOTALL)
        if alt_match:
            alt_text = alt_match.group(1).strip()
            bet_type, confidence, odds = parse_bet_from_text(alt_text)
            if bet_type:
                alternatives.append((bet_type, confidence, odds))
                logger.info(f"Parsed ALT{i}: {bet_type} @ {odds} ({confidence}%)")

    # Fallback: try numbered list format (1. 2. 3.)
    if not alternatives:
        for line in analysis.split('\n'):
            if re.match(r'^\s*[123]\.\s', line):
                bet_type, confidence, odds = parse_bet_from_text(line)
                if bet_type:
                    alternatives.append((bet_type, confidence, odds))

    return alternatives[:3]  # Max 3 alternatives


def save_prediction(user_id, match_id, home, away, bet_type, confidence, odds, ml_features=None, bet_rank=1):
    """Save prediction to database with category and ML features.

    Args:
        bet_rank: 1 = main bet, 2+ = alternatives

    Prevents duplicates - checks for same match + bet_type + bet_rank combination.
    """
    category = categorize_bet(bet_type)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check for existing prediction with same match, bet_type and rank
    c.execute("""SELECT id, bet_type FROM predictions
                 WHERE user_id = ? AND match_id = ? AND bet_type = ? AND bet_rank = ?
                 LIMIT 1""", (user_id, match_id, bet_type, bet_rank))
    existing = c.fetchone()

    if existing:
        # Already have this exact prediction - skip duplicate
        conn.close()
        logger.info(f"Skipping duplicate: match {match_id}, {bet_type}, rank {bet_rank}")
        return existing[0]  # Return existing prediction ID

    c.execute("""INSERT INTO predictions
                 (user_id, match_id, home_team, away_team, bet_type, bet_category, confidence, odds, bet_rank)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
              (user_id, match_id, home, away, bet_type, category, confidence, odds, bet_rank))
    prediction_id = c.lastrowid
    conn.commit()
    conn.close()

    # Save ML training data if features provided
    if ml_features and category:
        save_ml_training_data(prediction_id, category, ml_features, target=None)

    rank_label = "MAIN" if bet_rank == 1 else f"ALT{bet_rank-1}"
    logger.info(f"Saved prediction [{rank_label}]: {home} vs {away}, {bet_type} ({confidence}%)")

    return prediction_id

def get_pending_predictions():
    """Get predictions that haven't been checked yet"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT id, user_id, match_id, home_team, away_team, bet_type, confidence, odds 
                 FROM predictions 
                 WHERE is_correct IS NULL 
                 AND predicted_at > datetime('now', '-7 days')""")
    rows = c.fetchall()
    conn.close()
    
    return [{"id": r[0], "user_id": r[1], "match_id": r[2], "home": r[3], 
             "away": r[4], "bet_type": r[5], "confidence": r[6], "odds": r[7]} for r in rows]

def update_prediction_result(pred_id, result, is_correct):
    """Update prediction with result and ML training data"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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


def clean_duplicate_predictions() -> dict:
    """Remove duplicate predictions, keeping only the first one per match per user.
    Returns stats about what was cleaned."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Find duplicates (same user_id + match_id, keep oldest)
    c.execute("""
        SELECT user_id, match_id, COUNT(*) as cnt, MIN(id) as keep_id
        FROM predictions
        GROUP BY user_id, match_id
        HAVING cnt > 1
    """)
    duplicates = c.fetchall()

    deleted_count = 0
    affected_matches = 0

    for user_id, match_id, count, keep_id in duplicates:
        # Delete all except the first one
        c.execute("""DELETE FROM predictions
                     WHERE user_id = ? AND match_id = ? AND id != ?""",
                  (user_id, match_id, keep_id))
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


def get_clean_stats() -> dict:
    """Get accuracy stats counting only FIRST prediction per match (no duplicates)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get unique predictions (first per user+match)
    c.execute("""
        WITH unique_preds AS (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id, match_id ORDER BY predicted_at ASC) as rn
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


def get_roi_stats(user_id: int = None) -> dict:
    """Calculate ROI (Return on Investment) for predictions.
    Assumes flat betting (1 unit per bet)."""
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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


def validate_totals_prediction(bet_type: str, confidence: int, home_form: dict, away_form: dict) -> tuple:
    """Validate totals prediction against expected goals.
    Returns (validated_bet_type, validated_confidence, warning_message)"""

    if not bet_type or not home_form or not away_form:
        return bet_type, confidence, None

    bet_lower = bet_type.lower()

    # Only validate totals bets
    if "тб" not in bet_lower and "тм" not in bet_lower and "over" not in bet_lower and "under" not in bet_lower:
        return bet_type, confidence, None

    # Calculate expected goals from form
    try:
        home_scored = home_form.get('goals_scored', 7.5) / 5  # 5 matches
        home_conceded = home_form.get('goals_conceded', 5) / 5
        away_scored = away_form.get('goals_scored', 5) / 5
        away_conceded = away_form.get('goals_conceded', 7.5) / 5

        expected_home = (home_scored + away_conceded) / 2
        expected_away = (away_scored + home_conceded) / 2
        expected_total = expected_home + expected_away

        logger.info(f"Totals validation: expected_total={expected_total:.2f}, bet_type={bet_type}")

        is_over = "тб" in bet_lower or "over" in bet_lower or "больше" in bet_lower
        is_under = "тм" in bet_lower or "under" in bet_lower or "меньше" in bet_lower

        # STRICT VALIDATION
        if is_over and expected_total < 2.3:
            # Over recommended but expected goals too low!
            warning = f"⚠️ КОНТР-ПРОВЕРКА: expected_total={expected_total:.1f} < 2.5, ТБ рискован!"
            logger.warning(f"Totals mismatch: Over recommended but expected={expected_total:.2f}")
            # Reduce confidence significantly
            new_confidence = min(confidence, 60)
            return bet_type, new_confidence, warning

        if is_under and expected_total > 2.7:
            # Under recommended but expected goals too high!
            warning = f"⚠️ КОНТР-ПРОВЕРКА: expected_total={expected_total:.1f} > 2.5, ТМ рискован!"
            logger.warning(f"Totals mismatch: Under recommended but expected={expected_total:.2f}")
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

def extract_features(home_form: dict, away_form: dict, standings: dict,
                     odds: dict, h2h: list, home_team: str, away_team: str) -> dict:
    """Extract numerical features for ML model"""
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

    # Calculated features
    features["expected_goals"] = (features["home_goals_scored"] + features["away_goals_conceded"]) / 2 + \
                                  (features["away_goals_scored"] + features["home_goals_conceded"]) / 2
    features["avg_btts_pct"] = (features["home_btts_pct"] + features["away_btts_pct"]) / 2
    features["avg_over25_pct"] = (features["home_over25_pct"] + features["away_over25_pct"]) / 2

    return features


def save_ml_training_data(prediction_id: int, bet_category: str, features: dict, target: int = None):
    """Save features for ML training"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO ml_training_data (prediction_id, bet_category, features_json, target)
                 VALUES (?, ?, ?, ?)""",
              (prediction_id, bet_category, json.dumps(features), target))
    conn.commit()
    conn.close()


def update_ml_training_target(prediction_id: int, target: int):
    """Update target (result) for ML training data"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE ml_training_data SET target = ? WHERE prediction_id = ?", (target, prediction_id))
    conn.commit()
    conn.close()


def get_ml_training_data(bet_category: str) -> tuple:
    """Get training data for specific bet category"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT features_json, target FROM ml_training_data
                 WHERE bet_category = ? AND target IS NOT NULL""", (bet_category,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None, None

    X = []
    y = []
    for features_json, target in rows:
        try:
            features = json.loads(features_json)
            # Convert to list in consistent order
            feature_values = [
                features.get("home_wins", 0),
                features.get("home_draws", 0),
                features.get("home_losses", 0),
                features.get("home_goals_scored", 1.5),
                features.get("home_goals_conceded", 1.0),
                features.get("home_home_win_rate", 50),
                features.get("away_wins", 0),
                features.get("away_draws", 0),
                features.get("away_losses", 0),
                features.get("away_goals_scored", 1.0),
                features.get("away_goals_conceded", 1.5),
                features.get("away_away_win_rate", 30),
                features.get("home_position", 10),
                features.get("away_position", 10),
                features.get("position_diff", 0),
                features.get("odds_home", 2.5),
                features.get("odds_draw", 3.5),
                features.get("odds_away", 3.0),
                features.get("implied_home", 0.4),
                features.get("implied_draw", 0.25),
                features.get("implied_away", 0.35),
                features.get("h2h_home_wins", 0),
                features.get("h2h_draws", 0),
                features.get("h2h_away_wins", 0),
                features.get("expected_goals", 2.5),
                features.get("avg_btts_pct", 50),
                features.get("avg_over25_pct", 50),
            ]
            X.append(feature_values)
            y.append(target)
        except:
            continue

    return X, y


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
    conn = sqlite3.connect(DB_PATH)
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


def ml_predict(features: dict, bet_category: str) -> Optional[dict]:
    """Get ML prediction for a bet category"""
    if not ML_AVAILABLE:
        return None

    model_path = os.path.join(ML_MODELS_DIR, f"model_{bet_category}.pkl")

    if not os.path.exists(model_path):
        return None

    try:
        model = joblib.load(model_path)

        # Convert features to array
        feature_values = [
            features.get("home_wins", 0),
            features.get("home_draws", 0),
            features.get("home_losses", 0),
            features.get("home_goals_scored", 1.5),
            features.get("home_goals_conceded", 1.0),
            features.get("home_home_win_rate", 50),
            features.get("away_wins", 0),
            features.get("away_draws", 0),
            features.get("away_losses", 0),
            features.get("away_goals_scored", 1.0),
            features.get("away_goals_conceded", 1.5),
            features.get("away_away_win_rate", 30),
            features.get("home_position", 10),
            features.get("away_position", 10),
            features.get("position_diff", 0),
            features.get("odds_home", 2.5),
            features.get("odds_draw", 3.5),
            features.get("odds_away", 3.0),
            features.get("implied_home", 0.4),
            features.get("implied_draw", 0.25),
            features.get("implied_away", 0.35),
            features.get("h2h_home_wins", 0),
            features.get("h2h_draws", 0),
            features.get("h2h_away_wins", 0),
            features.get("expected_goals", 2.5),
            features.get("avg_btts_pct", 50),
            features.get("avg_over25_pct", 50),
        ]

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


def check_and_train_models():
    """Check if we have enough data and train models"""
    conn = sqlite3.connect(DB_PATH)
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
    conn = sqlite3.connect(DB_PATH)
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


def get_user_stats(user_id):
    """Get user's prediction statistics with categories"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
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
    
    # Recent predictions (main bets only for display)
    c.execute("""SELECT home_team, away_team, bet_type, confidence, result, is_correct, predicted_at, bet_rank
                 FROM predictions
                 WHERE user_id = ?
                 ORDER BY predicted_at DESC
                 LIMIT 10""", (user_id,))
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
                      "decided": alt_stats["decided"], "rate": alt_rate}
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


async def get_team_form_enhanced(team_id: int, limit: int = 10) -> Optional[dict]:
    """Get enhanced team form with home/away split and average goals"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    session = await get_http_session()

    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}/matches"
        params = {"status": "FINISHED", "limit": limit}
        async with session.get(url, headers=headers, params=params) as r:
            if r.status == 200:
                data = await r.json()
                matches = data.get("matches", [])

                # Overall stats
                overall = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "form": []}
                # Home stats
                home = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "matches": 0}
                # Away stats
                away = {"w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "matches": 0}
                # BTTS tracking
                btts_count = 0
                over25_count = 0

                for m in matches[:limit]:
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
                }
    except Exception as e:
        logger.error(f"Enhanced form error: {e}")
    return None


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
    conn = sqlite3.connect(DB_PATH)
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

                return {
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_lineup": home_lineup,
                    "away_lineup": away_lineup,
                    "home_injuries": home_injuries,
                    "away_injuries": away_injuries,
                    "status": data.get("status", "SCHEDULED"),
                "venue": data.get("venue", "Unknown")
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


async def get_odds(home_team: str, away_team: str) -> Optional[dict]:
    """Get betting odds (ASYNC)"""
    if not ODDS_API_KEY:
        return None

    session = await get_http_session()

    try:
        url = f"{ODDS_API_URL}/sports/soccer/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h,totals",
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

                    if (home_lower in event_home or away_lower in event_away):

                        odds = {}
                        for bookmaker in event.get("bookmakers", [])[:1]:
                            for market in bookmaker.get("markets", []):
                                if market.get("key") == "h2h":
                                    for outcome in market.get("outcomes", []):
                                        odds[outcome.get("name")] = outcome.get("price")
                                elif market.get("key") == "totals":
                                    for outcome in market.get("outcomes", []):
                                        name = outcome.get("name")
                                        point = outcome.get("point", 2.5)
                                        odds[f"{name}_{point}"] = outcome.get("price")
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
                                 lang: str = "ru") -> str:
    """Enhanced match analysis with form, H2H, home/away stats, top scorers, and value betting (ASYNC)"""

    if not claude_client:
        return "AI unavailable"

    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "?")
    comp_code = match.get("competition", {}).get("code", "PL")

    # Get all data (async) - using ENHANCED form function
    home_form = await get_team_form_enhanced(home_id) if home_id else None
    away_form = await get_team_form_enhanced(away_id) if away_id else None
    h2h = await get_h2h(match_id) if match_id else None
    odds = await get_odds(home, away)
    standings = await get_standings(comp_code)
    lineups = await get_lineups(match_id) if match_id else None
    top_scorers = await get_top_scorers(comp_code, 15)

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
        analysis_data += f"  📈 BTTS: {hf['btts_percent']}% | Тотал >2.5: {hf['over25_percent']}%\n\n"

    if away_form:
        af = away_form
        analysis_data += f"📊 {away} ФОРМА (последние 10 матчей):\n"
        analysis_data += f"  Общая: {af['overall']['form']} ({af['overall']['wins']}W-{af['overall']['draws']}D-{af['overall']['losses']}L)\n"
        analysis_data += f"  🏠 Дома: {af['home']['wins']}W-{af['home']['draws']}D-{af['home']['losses']}L (винрейт {af['home']['win_rate']}%)\n"
        analysis_data += f"  ✈️ В ГОСТЯХ: {af['away']['wins']}W-{af['away']['draws']}D-{af['away']['losses']}L (винрейт {af['away']['win_rate']}%)\n"
        analysis_data += f"      Средние голы: забито {af['away']['avg_goals_scored']}, пропущено {af['away']['avg_goals_conceded']}\n"
        analysis_data += f"  📈 BTTS: {af['btts_percent']}% | Тотал >2.5: {af['over25_percent']}%\n\n"

    # EXPECTED GOALS calculation
    if home_form and away_form:
        expected_home = (home_form['home']['avg_goals_scored'] + away_form['away']['avg_goals_conceded']) / 2
        expected_away = (away_form['away']['avg_goals_scored'] + home_form['home']['avg_goals_conceded']) / 2
        expected_total = expected_home + expected_away
        analysis_data += f"🎯 ОЖИДАЕМЫЕ ГОЛЫ (расчёт):\n"
        analysis_data += f"  {home}: ~{expected_home:.1f} голов\n"
        analysis_data += f"  {away}: ~{expected_away:.1f} голов\n"
        analysis_data += f"  Ожидаемый тотал: ~{expected_total:.1f}\n\n"

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

    # Odds with VALUE calculation
    if odds:
        analysis_data += "💰 КОЭФФИЦИЕНТЫ И VALUE:\n"
        for k, v in odds.items():
            if isinstance(v, (int, float)) and v > 1:
                implied = round(1 / v * 100, 1)
                analysis_data += f"  {k}: {v} (implied prob: {implied}%)\n"
            else:
                analysis_data += f"  {k}: {v}\n"
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
    # Extract features for ML
    ml_features = extract_features(
        home_form=home_form,
        away_form=away_form,
        standings=standings,
        odds=odds,
        h2h=h2h.get("matches", []) if h2h else [],
        home_team=home,
        away_team=away
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

    # Store features for future ML training (will be linked to prediction later)
    # Features are stored in match context for saving after Claude response

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
        "es": "Responde en español."
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

6. CONFIDENCE CALCULATION:
   - Base on statistical data, not feelings
   - 80%+: Strong statistical edge + good value
   - 70-79%: Clear favorite + decent value
   - 60-69%: Slight edge, moderate risk
   - <60%: High risk, only if excellent value

7. DIVERSIFY BET TYPES based on data:
   - High home win rate → П1 or 1X
   - High expected goals → Totals
   - Both teams score often → BTTS
   - Close match → X2 or 1X (double chance)

RESPONSE FORMAT:

📊 **АНАЛИЗ ДАННЫХ:**
• Форма {home} ДОМА: [конкретные цифры]
• Форма {away} В ГОСТЯХ: [конкретные цифры]
• Ожидаемые голы: [расчёт]
• H2H тренд: [если есть]

🎯 **ОСНОВНАЯ СТАВКА** (Уверенность: X%):
[Тип ставки] @ [коэфф]
📊 Value: [ваша вероятность]% - [implied]% = [+X% VALUE или NO VALUE]
💰 Банк: X%
📝 Почему: [основано на конкретных данных выше]

📈 **ДОПОЛНИТЕЛЬНЫЕ СТАВКИ:**
[ALT1] [Тип ставки] @ [коэфф] | [X]% уверенность
[ALT2] [Тип ставки] @ [коэфф] | [X]% уверенность
[ALT3] [Тип ставки] @ [коэфф] | [X]% уверенность

⚠️ **РИСКИ:**
[Конкретные риски на основе данных]

✅ **ВЕРДИКТ:** [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ВЫСОКИЙ РИСК / ПРОПУСТИТЬ]

Bank allocation: 80%+=5%, 75-79%=4%, 70-74%=3%, 65-69%=2%, 60-64%=1%, <60%=skip"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        return f"Error: {e}"


async def get_recommendations_enhanced(matches: list, user_query: str = "",
                                       user_settings: Optional[dict] = None,
                                       league_filter: Optional[str] = None,
                                       lang: str = "ru",
                                       min_confidence: int = 0) -> Optional[str]:
    """Enhanced recommendations with user preferences (ASYNC)

    Args:
        min_confidence: Minimum confidence threshold (0 = no filter, 75 = only high confidence)
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

        home_form = await get_team_form(home_id) if home_id else None
        away_form = await get_team_form(away_id) if away_id else None

        # Get warnings
        warnings = get_match_warnings(m, home_form, away_form, lang)

        match_info = f"{home} vs {away} ({comp})"
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
        "es": "Responde en español."
    }
    lang_instruction = lang_map.get(lang, lang_map["ru"])
    
    prompt = f"""{lang_instruction}

User asked: "{user_query}"

Analyze these matches with form data and give TOP 3-4 picks:

{matches_text}

{filter_info}

RULES:
1. DIVERSIFY bet types - include outcomes (1/X/2), totals, BTTS, double chance
2. For TOP CLUBS - never recommend betting against them
3. Cup matches = higher upset risk, lower confidence
4. Consider VALUE: confidence × odds > 1.0
5. If warnings present - adjust confidence accordingly
{f'6. ONLY recommend bets with {min_confidence}%+ confidence! Skip all bets below this threshold.' if min_confidence > 0 else ''}

FORMAT:
🔥 **ТОП СТАВКИ:**

1️⃣ **[Home] vs [Away]** ({comp})
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

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - first launch with onboarding or regular menu"""
    user = update.effective_user
    lang = detect_language(user)
    detected_tz = detect_timezone(user)
    is_new_user = not get_user(user.id)

    if is_new_user:
        # Create user with auto-detected settings
        create_user(user.id, user.username, lang)
        update_user_settings(user.id, timezone=detected_tz)

        # Show beautiful welcome message for new users
        tz_display = get_tz_offset_str(detected_tz)
        welcome_text = f"""{get_text('first_start_title', lang)}

{get_text('first_start_text', lang)}

{get_text('detected_settings', lang)}
• {get_text('language_label', lang)}: {LANGUAGE_NAMES.get(lang, lang)}
• {get_text('timezone_label', lang)}: {tz_display}

_{get_text('change_in_settings', lang)}_"""

        await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_keyboard(lang),
            parse_mode="Markdown"
        )

    # Show main menu
    await show_main_menu(update, context, lang)


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show main menu (can be called anytime)"""
    user_data = get_user(update.effective_user.id)
    if not user_data:
        lang = detect_language(update.effective_user)
        create_user(update.effective_user.id, update.effective_user.username, lang)
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
    text = f"📅 **МАТЧИ СЕГОДНЯ** ({tz_info}):\n\n" if lang == "ru" else f"📅 **TODAY'S MATCHES** ({tz_info}):\n\n"
    
    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
            text += f"  ⏰ {time_str} | {home} vs {away}\n"
        text += "\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 Рекомендации на сегодня", callback_data="rec_today")],
        [InlineKeyboardButton("📆 Завтра", callback_data="cmd_tomorrow")]
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
    text = f"📆 **МАТЧИ ЗАВТРА** ({tz_info}):\n\n" if lang == "ru" else f"📆 **TOMORROW'S MATCHES** ({tz_info}):\n\n"
    
    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            time_str = convert_utc_to_user_tz(m.get("utcDate", ""), user_tz)
            text += f"  ⏰ {time_str} | {home} vs {away}\n"
        text += "\n"
    
    keyboard = [
        [InlineKeyboardButton("📊 Рекомендации на завтра", callback_data="rec_tomorrow")],
        [InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")]
    ]
    
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user:
        create_user(user_id)
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
    
    add_league_label = {"ru": "➕ Добавить лигу", "en": "➕ Add league", "pt": "➕ Adicionar liga", "es": "➕ Añadir liga"}
    keyboard = [
        [InlineKeyboardButton(add_league_label.get(lang, add_league_label["en"]), callback_data="add_fav_league")],
        [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics with categories"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"

    stats = get_user_stats(user_id)

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
    roi_text = f"{roi_emoji} **ROI:** {roi['roi']:+.1f}% (профит: {roi['profit']:+.1f} ед.)"

    # Build stats string with push
    decided = stats['correct'] + stats.get('incorrect', 0)
    push_str = f"\n🔄 **Возвраты:** {stats['push']}" if stats.get('push', 0) > 0 else ""

    # Main vs Alt stats display
    main_s = stats.get("main_stats", {})
    alt_s = stats.get("alt_stats", {})

    main_display = ""
    alt_display = ""
    if main_s.get("decided", 0) > 0:
        main_emoji = "🎯" if main_s["rate"] >= 50 else "📊"
        main_display = f"{main_emoji} **Основные:** {main_s['correct']}/{main_s['decided']} ({main_s['rate']:.1f}%)"
    if alt_s.get("decided", 0) > 0:
        alt_emoji = "📈" if alt_s["rate"] >= 50 else "📉"
        alt_display = f"{alt_emoji} **Альтернативы:** {alt_s['correct']}/{alt_s['decided']} ({alt_s['rate']:.1f}%)"

    stats_by_rank = ""
    if main_display or alt_display:
        stats_by_rank = f"\n{main_display}\n{alt_display}" if alt_display else f"\n{main_display}"

    text = f"""📈 **СТАТИСТИКА**

{win_emoji} **Точность:** {stats['correct']}/{decided} ({stats['win_rate']:.1f}%)
{roi_text}
{streak_text}
{stats_by_rank}

📊 **Всего прогнозов:** {stats['total']}
✅ **Верных:** {stats['correct']}
❌ **Неверных:** {stats.get('incorrect', 0)}{push_str}
⏳ **Ожидают:** {stats['pending']}

🏆 **Рекорды:** лучшая серия {streak['best_win_streak']}W | худшая {streak['worst_lose_streak']}L

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
        
        text += "📋 **По типам ставок:**\n"
        for cat, data in stats["categories"].items():
            cat_name = cat_names.get(cat, cat)
            push_info = f" (+{data['push']}🔄)" if data.get('push', 0) > 0 else ""
            text += f"  • {cat_name}: {data['correct']}/{data['total'] - data.get('push', 0)} ({data['rate']}%){push_info}\n"
        text += "\n"
    
    # Recent predictions
    text += f"{'─'*25}\n📝 **Последние прогнозы:**\n"
    for p in stats.get("predictions", [])[:7]:
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

        # Show bet rank marker
        rank = p.get("bet_rank", 1)
        rank_marker = "" if rank == 1 or rank is None else f" [ALT{rank-1}]"

        text += f"{emoji} {home_short} - {away_short}\n"
        text += f"    📊 {p['bet_type']}{rank_marker} ({p['confidence']}%) → {result_text}\n"
    
    refresh_label = {"ru": "🔄 Обновить", "en": "🔄 Refresh", "pt": "🔄 Atualizar", "es": "🔄 Actualizar"}
    keyboard = [
        [InlineKeyboardButton(refresh_label.get(lang, refresh_label["en"]), callback_data="cmd_stats")],
        [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
    ]
    
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
    
    can_use, remaining = check_daily_limit(user_id)
    
    text = f"""🔧 DEBUG INFO

👤 User ID: {user_id}
📛 Username: {user.get('username', 'N/A')}

📊 Limits:
- Daily requests: {user.get('daily_requests', 0)}/{FREE_DAILY_LIMIT}
- Last request date: {user.get('last_request_date', 'Never')}
- Can use: {'Yes' if can_use else 'No'}
- Remaining: {remaining}

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
    can_use, remaining = check_daily_limit(user_id)
    if not can_use:
        text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
        keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    status = await update.message.reply_text(get_text("analyzing", lang))

    matches = await get_matches(days=7)
    matches = filter_cup_matches(matches, exclude=bool(exclude_cups))

    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    user_query = update.message.text or ""
    recs = await get_recommendations_enhanced(matches, user_query, user, lang=lang)
    
    if recs:
        # Add affiliate button
        keyboard = [
            [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
            [InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")]
        ]
        increment_daily_usage(user_id)
        await status.edit_text(recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status.edit_text("❌ Ошибка анализа.")


async def sure_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get only HIGH CONFIDENCE (75%+) recommendations"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    exclude_cups = user.get("exclude_cups", 0) if user else 0

    # Check daily limit
    can_use, remaining = check_daily_limit(user_id)
    if not can_use:
        text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
        keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    status = await update.message.reply_text("🎯 Ищу уверенные ставки (75%+)...")

    matches = await get_matches(days=7)
    matches = filter_cup_matches(matches, exclude=bool(exclude_cups))

    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return

    recs = await get_recommendations_enhanced(matches, "", user, lang=lang, min_confidence=75)

    if recs:
        header = "🎯 **УВЕРЕННЫЕ СТАВКИ (75%+)**\n\n"
        keyboard = [
            [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
            [InlineKeyboardButton("📊 Все ставки", callback_data="cmd_recommend")]
        ]
        increment_daily_usage(user_id)
        await status.edit_text(header + recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status.edit_text("❌ Нет уверенных ставок 75%+ на ближайшие дни.")


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
• /settings - Настройки
• /favorites - Избранное
• /stats - Статистика
• /history - 📜 История прогнозов

**Как пользоваться:**
1. Напиши название команды (напр. "Ливерпуль")
2. Получи анализ с формой, H2H и рекомендациями
3. Настрой фильтры под свой стиль

**Лимиты:**
• Бесплатно: {FREE_DAILY_LIMIT} прогноза/день
• Безлимит: сделай депозит по ссылке

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

    conn = sqlite3.connect(DB_PATH)
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
            "es": "📜 Sin historial. ¡Haz una predicción!"
        }
        await update.message.reply_text(no_history.get(lang, no_history["ru"]))
        return

    # Build history text
    filter_labels = {
        "all": {"ru": "ВСЕ", "en": "ALL"},
        "wins": {"ru": "ПОБЕДЫ", "en": "WINS"},
        "losses": {"ru": "ПОРАЖЕНИЯ", "en": "LOSSES"},
        "pending": {"ru": "ОЖИДАЮТ", "en": "PENDING"}
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
    conn = sqlite3.connect(DB_PATH)
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
└ Точность (сырая): {accuracy}%

📈 **Чистая статистика (без дубликатов):**
├ Уникальных: {clean['clean_total']}
├ Верных: {clean['clean_correct']}
└ **Реальная точность: {clean['clean_accuracy']}%**{duplicates_info}

⚙️ **Админ-команды:**
• /broadcast текст - Рассылка всем
• /addpremium ID - Дать премиум
• /checkresults - Проверить результаты

🔧 **Система:**
├ Админов: {len(ADMIN_IDS)}
└ Твой ID: {user_id}"""

    keyboard = [
        [InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast"),
         InlineKeyboardButton("👥 Юзеры", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Детальная статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🧹 Очистить дубликаты", callback_data="admin_clean_dups")],
        [InlineKeyboardButton("🔙 В меню", callback_data="cmd_start")]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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

    conn = sqlite3.connect(DB_PATH)
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
    """Add premium to user - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Использование: /addpremium <user_id>")
        return

    target_id = int(context.args[0])

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (target_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
        await update.message.reply_text(f"✅ Премиум выдан юзеру {target_id}")
        try:
            await context.bot.send_message(target_id, "🎉 Вам выдан Premium-статус! Безлимитные прогнозы активированы.")
        except Exception:
            pass
    else:
        await update.message.reply_text(f"❌ Юзер {target_id} не найден")


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

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET is_premium = 0 WHERE user_id = ?", (target_id,))
    affected = c.rowcount
    conn.commit()
    conn.close()

    if affected > 0:
        await update.message.reply_text(f"✅ Премиум убран у юзера {target_id}")
    else:
        await update.message.reply_text(f"❌ Юзер {target_id} не найден")


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

    conn = sqlite3.connect(DB_PATH)
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
    """Force train ML models - admin only"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов")
        return

    await update.message.reply_text("🔄 Запускаю обучение моделей...")

    results = train_all_models()

    if results:
        text = "✅ **Обучение завершено:**\n\n"
        for cat, info in results.items():
            text += f"• {cat}: {info['accuracy']:.1%} точность\n"
    else:
        text = "❌ Недостаточно данных для обучения.\nНужно минимум 100 проверенных прогнозов на категорию."

    await update.message.reply_text(text, parse_mode="Markdown")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    
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
            [InlineKeyboardButton(get_text("help", lang), callback_data="cmd_help")]
        ]
        await query.edit_message_text(f"⚽ **AI Betting Bot v14** - {get_text('choose_action', lang)}",
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_recommend":
        # Check limit
        can_use, _ = check_daily_limit(user_id)
        if not can_use:
            text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return

        await query.edit_message_text(get_text("analyzing", lang))
        matches = await get_matches(days=7)
        if matches:
            recs = await get_recommendations_enhanced(matches, "", user, lang=lang)
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
                [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
            ]
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

    elif data.startswith("history_"):
        # History filter callbacks
        filter_type = data.replace("history_", "")
        conn = sqlite3.connect(DB_PATH)
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
            "all": {"ru": "ВСЕ", "en": "ALL"},
            "wins": {"ru": "ПОБЕДЫ", "en": "WINS"},
            "losses": {"ru": "ПОРАЖЕНИЯ", "en": "LOSSES"},
            "pending": {"ru": "ОЖИДАЮТ", "en": "PENDING"}
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
        can_use, _ = check_daily_limit(user_id)
        if not can_use:
            text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
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
            recs = await get_recommendations_enhanced(matches, "", user, lang=lang)
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
                [InlineKeyboardButton(get_text("back", lang), callback_data="cmd_start")]
            ]
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
            "es": "✅ Copas excluidas" if new_value else "✅ Copas incluidas"
        }
        await query.answer(confirm.get(lang, confirm["ru"]))
        await settings_cmd(update, context)

    elif data == "set_language":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
             InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇧🇷 Português", callback_data="lang_pt"),
             InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es")],
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
            "es": "✅ Idioma cambiado a español"
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    user_text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if len(user_text) < 2:
        return
    
    # Ensure user exists
    if not get_user(user_id):
        lang = detect_language(update.effective_user)
        create_user(user_id, update.effective_user.username, lang)
    
    user = get_user(user_id)
    lang = user.get("language", "ru")
    
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
        get_text("settings", "ru"): settings_cmd,
        get_text("settings", "en"): settings_cmd,
        get_text("settings", "pt"): settings_cmd,
        get_text("settings", "es"): settings_cmd,
        get_text("help_btn", "ru"): help_cmd,
        get_text("help_btn", "en"): help_cmd,
        get_text("help_btn", "pt"): help_cmd,
        get_text("help_btn", "es"): help_cmd,
    }
    
    if user_text in button_map:
        await button_map[user_text](update, context)
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
        can_use, _ = check_daily_limit(user_id)
        if not can_use:
            text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
            await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return

        await status.edit_text(get_text("analyzing_bets", lang))
        matches = await get_matches(days=7)
        if not matches:
            await status.edit_text(get_text("no_matches", lang))
            return
        recs = await get_recommendations_enhanced(matches, user_text, user, league, lang=lang)
        if recs:
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
                [InlineKeyboardButton(get_text("today", lang), callback_data="cmd_today")]
            ]
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
    can_use, _ = check_daily_limit(user_id)
    if not can_use:
        text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
        keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=get_affiliate_link(user_id))]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await status.edit_text(get_text("searching_match", lang))
    
    matches = await get_matches(days=14)
    match = None
    
    if teams:
        match = find_match(teams, matches)
    
    if not match:
        match = find_match([user_text], matches)
    
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
    
    # Enhanced analysis
    analysis = await analyze_match_enhanced(match, user, lang)
    
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

        # Add Kelly Criterion recommendation
        if confidence > 0 and odds_value > 1:
            kelly_stake = calculate_kelly(confidence / 100, odds_value)
            if kelly_stake > 0:
                kelly_percent = kelly_stake * 100
                if kelly_percent >= 5:
                    stake_emoji = "🔥"
                    stake_text = "АГРЕССИВНО"
                elif kelly_percent >= 2:
                    stake_emoji = "✅"
                    stake_text = "УМЕРЕННО"
                else:
                    stake_emoji = "⚠️"
                    stake_text = "ОСТОРОЖНО"
                analysis = analysis + f"\n\n{stake_emoji} **KELLY CRITERION:** {kelly_percent:.1f}% банкролла ({stake_text})"
            else:
                analysis = analysis + f"\n\n⛔ **KELLY:** Нет ценности (VALUE отрицательный)"

        # Save MAIN prediction (bet_rank=1)
        save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_value, bet_rank=1)
        increment_daily_usage(user_id)
        logger.info(f"Saved MAIN: {home} vs {away}, {bet_type}, {confidence}%, odds={odds_value}")

        # Parse and save ALTERNATIVE predictions (bet_rank=2,3,4)
        alternatives = parse_alternative_bets(analysis)
        for idx, (alt_type, alt_conf, alt_odds) in enumerate(alternatives, start=2):
            if alt_type and alt_type != bet_type:  # Don't duplicate main bet
                save_prediction(user_id, match_id, home, away, alt_type, alt_conf, alt_odds, bet_rank=idx)
                logger.info(f"Saved ALT{idx-1}: {home} vs {away}, {alt_type}, {alt_conf}%, odds={alt_odds}")

    except Exception as e:
        logger.error(f"Error saving prediction: {e}")

    header = f"⚽ **{home}** vs **{away}**\n🏆 {comp}\n{'─'*30}\n\n"

    keyboard = [
        [InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))],
        [InlineKeyboardButton(f"⭐ {home}", callback_data=f"fav_team_{home}"),
         InlineKeyboardButton(f"⭐ {away}", callback_data=f"fav_team_{away}")],
        [InlineKeyboardButton("📊 Ещё рекомендации", callback_data="cmd_recommend")]
    ]

    await status.edit_text(header + analysis, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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
            r = requests.get(url, headers=headers, timeout=10)
            
            if r.status_code != 200:
                text += f"   ⚠️ API error\n\n"
                continue
            
            match_data = r.json()
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


async def check_live_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check upcoming matches and send alerts"""
    
    if not live_subscribers:
        return
    
    logger.info(f"Checking live for {len(live_subscribers)} subscribers...")
    
    matches = await get_matches(days=1)
    
    if not matches:
        return
    
    now = datetime.now()
    upcoming = []
    
    for m in matches:
        try:
            match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            hours_until = (match_time - now).total_seconds() / 3600
            
            if 0.5 < hours_until < 3:
                upcoming.append(m)
        except:
            continue
    
    if not upcoming:
        return
    
    logger.info(f"Found {len(upcoming)} upcoming matches")
    
    for match in upcoming[:3]:
        match_id = match.get("id")  # Get match ID for tracking
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")
        
        home_form = await get_team_form(home_id) if home_id else None
        away_form = await get_team_form(away_id) if away_id else None
        odds = await get_odds(home, away)
        h2h = await get_h2h(match_id) if match_id else None

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
                if not k.startswith("Over") and not k.startswith("Under"):
                    odds_text += f"{k}: {v}, "

        # Analyze match and send alerts in user's language
        analysis_prompt = f"""Analyze this match for betting:

Match: {home} vs {away}
Competition: {comp}
Form: {form_text if form_text else "Limited data"}
{expected_text}
{h2h_text if h2h_text else "No H2H data"}
{h2h_warning}
Odds: {odds_text if odds_text else "Not available"}

IMPORTANT RULES:
- If H2H has < 5 matches, IGNORE H2H for totals! Use current form instead.
- If H2H avg goals > 2.8 AND H2H has 5+ matches → favor Over 2.5
- If H2H avg goals < 2.2 AND H2H has 5+ matches → favor Under 2.5
- Expected goals from current form is MORE RELIABLE than small H2H sample

If you find a good bet (70%+ confidence), respond with JSON:
{{"alert": true, "bet_type": "...", "confidence": 75, "odds": 1.85, "reason_en": "...", "reason_ru": "...", "reason_es": "...", "reason_pt": "..."}}

If no good bet exists, respond: {{"alert": false}}"""

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

                # Send to each subscriber in their language
                for user_id in live_subscribers:
                    try:
                        user_data = get_user(user_id)
                        lang = user_data.get("language", "ru") if user_data else "ru"

                        # Get localized reason
                        reason_key = f"reason_{lang}"
                        reason = alert_data.get(reason_key, alert_data.get("reason_en", "Good value bet"))

                        # Build localized alert message
                        alert_msg = f"""{get_text("live_alert_title", lang)}

⚽ **{home}** vs **{away}**
🏆 {comp}
⏰ {get_text("in_hours", lang).format(hours="1-3")}

{get_text("bet", lang)} {bet_type}
{get_text("confidence", lang)} {confidence}%
{get_text("odds", lang)} ~{odds_val}
{get_text("reason", lang)} {reason}"""

                        keyboard = [[InlineKeyboardButton(get_text("place_bet", lang), url=get_affiliate_link(user_id))]]

                        await context.bot.send_message(
                            chat_id=user_id,
                            text=alert_msg,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )

                        # Save prediction to database for statistics tracking
                        if match_id:
                            save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_val)
                            logger.info(f"Live alert prediction saved: {home} vs {away}, {bet_type} for user {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to send to {user_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Claude error: {e}")
        
        await asyncio.sleep(1)


async def check_predictions_results(context: ContextTypes.DEFAULT_TYPE):
    """Check results of past predictions"""
    logger.info("Checking prediction results...")
    
    pending = get_pending_predictions()
    
    if not pending:
        return
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    for pred in pending[:20]:
        match_id = pred.get("match_id")
        
        if not match_id:
            continue
        
        try:
            url = f"{FOOTBALL_API_URL}/matches/{match_id}"
            r = requests.get(url, headers=headers, timeout=10)
            
            if r.status_code == 200:
                match = r.json()
                if match.get("status") == "FINISHED":
                    score = match.get("score", {}).get("fullTime", {})
                    home_score = score.get("home", 0) or 0
                    away_score = score.get("away", 0) or 0
                    
                    is_correct = check_bet_result(pred["bet_type"], home_score, away_score)
                    result = f"{home_score}-{away_score}"
                    
                    # Handle three outcomes: win (1), lose (0), push/void (2)
                    if is_correct is True:
                        db_value = 1
                        emoji = "✅"
                        status_text = "Прогноз верный!"
                    elif is_correct is False:
                        db_value = 0
                        emoji = "❌"
                        status_text = "Прогноз не сработал"
                    else:  # is_correct is None = push/void
                        db_value = 2
                        emoji = "🔄"
                        status_text = "Возврат (push)"
                    
                    update_prediction_result(pred["id"], result, db_value)
                    logger.info(f"Updated prediction {pred['id']}: {result} -> {emoji}")
                    
                    # Notify user
                    try:
                        await context.bot.send_message(
                            chat_id=pred["user_id"],
                            text=f"📊 **Результат прогноза**\n\n"
                                 f"⚽ {pred['home']} vs {pred['away']}\n"
                                 f"🎯 Ставка: {pred['bet_type']}\n"
                                 f"📈 Счёт: {result}\n"
                                 f"{emoji} {status_text}",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error checking {pred['id']}: {e}")


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Send daily digest at 10:00"""
    
    if not live_subscribers:
        return
    
    current_hour = datetime.now().hour
    if current_hour != 10:
        return
    
    logger.info("Sending daily digest...")
    
    matches = await get_matches(date_filter="today")
    
    if not matches:
        return
    
    recs = await get_recommendations_enhanced(matches, "daily digest")
    
    if not recs:
        return
    
    text = f"☀️ **ДАЙДЖЕСТ НА СЕГОДНЯ**\n\n{recs}"

    for user_id in live_subscribers:
        try:
            keyboard = [
                [InlineKeyboardButton("🎰 Ставить", url=get_affiliate_link(user_id))],
                [InlineKeyboardButton("📅 Все матчи", callback_data="cmd_today")]
            ]
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send digest to {user_id}: {e}")


# ===== WEB SERVER FOR POSTBACK =====

from aiohttp import web

async def handle_postback(request):
    """Handle 1win postback webhook."""
    try:
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


async def start_web_server():
    """Start aiohttp web server for postbacks."""
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/1win/postback", handle_postback)
    app.router.add_post("/api/1win/postback", handle_postback)

    port = int(os.getenv("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web server started on port {port}")
    print(f"   🌐 Postback endpoint: http://0.0.0.0:{port}/api/1win/postback")


# ===== MAIN =====

def main():
    global live_subscribers
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
    app.add_handler(CommandHandler("debug", debug_cmd))

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("addpremium", addpremium_cmd))
    app.add_handler(CommandHandler("removepremium", removepremium_cmd))
    app.add_handler(CommandHandler("userinfo", userinfo_cmd))
    app.add_handler(CommandHandler("mlstatus", mlstatus_cmd))
    app.add_handler(CommandHandler("mltrain", mltrain_cmd))

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
    job_queue.run_repeating(check_predictions_results, interval=3600, first=600)
    
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
