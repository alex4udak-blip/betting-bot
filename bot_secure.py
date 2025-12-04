import os
import logging
import requests
import json
import sqlite3
import asyncio
import time
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, JobQueue
import anthropic

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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

claude_client = None
if CLAUDE_API_KEY:
    claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

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
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "ELC": "Championship",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "BSA": "Brasileirão",
    "EL": "Europa League",
    "CLI": "Conference League",
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
        "daily_limit": "⚠️ Достигнут лимит ({limit} прогнозов/день).\n\n💎 Для безлимита сделай депозит:",
        "place_bet": "🎰 Поставить",
        "no_matches": "Матчей не найдено",
        "analyzing": "🔍 Анализирую...",
        "cup_warning": "⚠️ Кубковый матч — выше риск сенсации!",
        "rotation_warning": "⚠️ Возможна ротация состава",
        "top_club_warning": "⚠️ Топ-клуб — не ставь против",
        "unlimited": "🎰 Безлимитный доступ",
    },
    "en": {
        "welcome": "👋 Hello! I'm an AI betting bot for football.\n\nUse the menu below or type a team name.",
        "top_bets": "🔥 Top Bets",
        "matches": "⚽ Matches",
        "stats": "📊 Stats",
        "favorites": "⭐ Favorites",
        "settings": "⚙️ Settings",
        "help_btn": "❓ Help",
        "daily_limit": "⚠️ Daily limit reached ({limit} predictions).\n\n💎 For unlimited access, make a deposit:",
        "place_bet": "🎰 Place bet",
        "no_matches": "No matches found",
        "analyzing": "🔍 Analyzing...",
        "cup_warning": "⚠️ Cup match — higher upset risk!",
        "rotation_warning": "⚠️ Possible squad rotation",
        "top_club_warning": "⚠️ Top club — don't bet against",
        "unlimited": "🎰 Get unlimited access",
    },
    "pt": {
        "welcome": "👋 Olá! Sou um bot de apostas com IA para futebol.\n\nUse o menu ou digite o nome de um time.",
        "top_bets": "🔥 Top Apostas",
        "matches": "⚽ Jogos",
        "stats": "📊 Estatísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Config",
        "help_btn": "❓ Ajuda",
        "daily_limit": "⚠️ Limite diário atingido ({limit} previsões).\n\n💎 Para acesso ilimitado, faça um depósito:",
        "place_bet": "🎰 Apostar",
        "no_matches": "Nenhum jogo encontrado",
        "analyzing": "🔍 Analisando...",
        "cup_warning": "⚠️ Jogo de copa — maior risco!",
        "rotation_warning": "⚠️ Possível rotação",
        "top_club_warning": "⚠️ Clube top — não aposte contra",
        "unlimited": "🎰 Acesso ilimitado",
    },
    "es": {
        "welcome": "👋 ¡Hola! Soy un bot de apuestas con IA para fútbol.\n\nUsa el menú o escribe el nombre de un equipo.",
        "top_bets": "🔥 Top Apuestas",
        "matches": "⚽ Partidos",
        "stats": "📊 Estadísticas",
        "favorites": "⭐ Favoritos",
        "settings": "⚙️ Ajustes",
        "help_btn": "❓ Ayuda",
        "daily_limit": "⚠️ Límite diario alcanzado ({limit} pronósticos).\n\n💎 Para acceso ilimitado, haz un depósito:",
        "place_bet": "🎰 Apostar",
        "no_matches": "No se encontraron partidos",
        "analyzing": "🔍 Analizando...",
        "cup_warning": "⚠️ Partido de copa — ¡mayor riesgo!",
        "rotation_warning": "⚠️ Posible rotación",
        "top_club_warning": "⚠️ Club top — no apuestes en contra",
        "unlimited": "🎰 Acceso ilimitado",
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
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, persistent=True)

def detect_language(user):
    """Detect user language from Telegram settings"""
    lang_code = user.language_code or "ru"
    if lang_code.startswith("pt"):
        return "pt"
    elif lang_code.startswith("es"):
        return "es"
    elif lang_code.startswith("en"):
        return "en"
    return "ru"


# ===== DATABASE =====

DB_PATH = "/data/betting_bot.db" if os.path.exists("/data") else "betting_bot.db"

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
        last_request_date TEXT
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
    
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def get_user(user_id):
    """Get user settings"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "min_odds": row[3] if len(row) > 3 else 1.3,
            "max_odds": row[4] if len(row) > 4 else 3.0,
            "risk_level": row[5] if len(row) > 5 else "medium",
            "language": row[6] if len(row) > 6 else "ru",
            "is_premium": row[7] if len(row) > 7 else 0,
            "daily_requests": row[8] if len(row) > 8 else 0,
            "last_request_date": row[9] if len(row) > 9 else None
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

def update_user_settings(user_id, **kwargs):
    """Update user settings"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    for key, value in kwargs.items():
        if key in ['min_odds', 'max_odds', 'risk_level', 'language', 'is_premium', 'daily_requests', 'last_request_date']:
            c.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
    
    conn.commit()
    conn.close()

def check_daily_limit(user_id):
    """Check if user has reached daily limit. Returns (can_use, remaining)"""
    user = get_user(user_id)
    if not user:
        return True, FREE_DAILY_LIMIT
    
    # Premium users have no limit
    if user.get("is_premium", 0):
        return True, 999
    
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = user.get("last_request_date")
    daily_requests = user.get("daily_requests", 0)
    
    # Reset counter if new day
    if last_date != today:
        update_user_settings(user_id, daily_requests=0, last_request_date=today)
        return True, FREE_DAILY_LIMIT
    
    if daily_requests >= FREE_DAILY_LIMIT:
        return False, 0
    
    return True, FREE_DAILY_LIMIT - daily_requests

def increment_daily_usage(user_id):
    """Increment daily usage counter"""
    user = get_user(user_id)
    if not user:
        return
    
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = user.get("last_request_date")
    
    if last_date != today:
        update_user_settings(user_id, daily_requests=1, last_request_date=today)
    else:
        current = user.get("daily_requests", 0)
        update_user_settings(user_id, daily_requests=current + 1)

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

def save_prediction(user_id, match_id, home, away, bet_type, confidence, odds):
    """Save prediction to database with category"""
    category = categorize_bet(bet_type)
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO predictions 
                 (user_id, match_id, home_team, away_team, bet_type, bet_category, confidence, odds)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (user_id, match_id, home, away, bet_type, category, confidence, odds))
    conn.commit()
    conn.close()

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
    """Update prediction with result"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""UPDATE predictions 
                 SET result = ?, is_correct = ?, checked_at = CURRENT_TIMESTAMP 
                 WHERE id = ?""", (result, is_correct, pred_id))
    conn.commit()
    conn.close()

def check_bet_result(bet_type, home_score, away_score):
    """Check if bet was correct based on score"""
    total_goals = home_score + away_score
    bet_lower = bet_type.lower() if bet_type else ""
    
    # Home win
    if bet_type == "П1" or "победа хозя" in bet_lower or "home win" in bet_lower or bet_type == "1":
        return home_score > away_score
    
    # Away win
    elif bet_type == "П2" or "победа гост" in bet_lower or "away win" in bet_lower or bet_type == "2":
        return away_score > home_score
    
    # Draw
    elif bet_type == "Х" or "ничья" in bet_lower or "draw" in bet_lower:
        return home_score == away_score
    
    # Over 2.5
    elif "ТБ" in bet_type or "тотал больше" in bet_lower or "over" in bet_lower or "больше 2" in bet_lower:
        return total_goals > 2.5
    
    # Under 2.5
    elif "ТМ" in bet_type or "тотал меньше" in bet_lower or "under" in bet_lower or "меньше 2" in bet_lower:
        return total_goals < 2.5
    
    # BTTS
    elif "BTTS" in bet_type.upper() or "обе забьют" in bet_lower or "both teams" in bet_lower:
        return home_score > 0 and away_score > 0
    
    # Double chance 1X
    elif "1X" in bet_type or "двойной шанс 1" in bet_lower:
        return home_score >= away_score
    
    # Double chance X2
    elif "X2" in bet_type or "двойной шанс 2" in bet_lower:
        return away_score >= home_score
    
    # If we can't determine bet type
    elif "analysis" in bet_lower or bet_type == "":
        return home_score > away_score
    
    return None

def get_user_stats(user_id):
    """Get user's prediction statistics with categories"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 1", (user_id,))
    correct = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct IS NOT NULL", (user_id,))
    checked = c.fetchone()[0]
    
    # Stats by category
    categories = {}
    for cat in ["totals_over", "totals_under", "outcomes_home", "outcomes_away", "outcomes_draw", 
                "btts", "double_chance", "handicap", "other"]:
        c.execute("""SELECT COUNT(*), SUM(CASE WHEN is_correct = 1 THEN 1 ELSE 0 END)
                     FROM predictions 
                     WHERE user_id = ? AND bet_category = ? AND is_correct IS NOT NULL""", 
                  (user_id, cat))
        row = c.fetchone()
        cat_total = row[0] or 0
        cat_correct = row[1] or 0
        if cat_total > 0:
            categories[cat] = {
                "total": cat_total,
                "correct": cat_correct,
                "rate": round(cat_correct / cat_total * 100, 1)
            }
    
    # Recent predictions
    c.execute("""SELECT home_team, away_team, bet_type, confidence, result, is_correct, predicted_at 
                 FROM predictions 
                 WHERE user_id = ? 
                 ORDER BY predicted_at DESC 
                 LIMIT 10""", (user_id,))
    recent = c.fetchall()
    
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
            "date": r[6]
        })
    
    return {
        "total": total,
        "correct": correct,
        "checked": checked,
        "pending": total - checked,
        "win_rate": (correct / checked * 100) if checked > 0 else 0,
        "categories": categories,
        "predictions": predictions
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

def get_matches(competition=None, date_filter=None, days=7, use_cache=True):
    """Get matches from Football Data API"""
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
    
    params = {"dateFrom": date_from, "dateTo": date_to}
    
    if competition:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                matches = r.json().get("matches", [])
                logger.info(f"Got {len(matches)} from {competition}")
                return matches
            elif r.status_code == 429:
                logger.warning(f"Rate limit hit for {competition}, waiting...")
                time.sleep(6)
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    return r.json().get("matches", [])
            else:
                logger.error(f"API error {r.status_code} for {competition}")
        except Exception as e:
            logger.error(f"Error getting matches for {competition}: {e}")
        return []
    
    # Get from all leagues with rate limit awareness (Standard plan = 25 leagues, 60 req/min)
    all_matches = []
    leagues = ["PL", "PD", "BL1", "SA", "FL1", "ELC", "DED", "PPL", "BSA", "CL", "EL"]
    
    for code in leagues:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{code}/matches"
            r = requests.get(url, headers=headers, params=params, timeout=10)
            
            if r.status_code == 200:
                matches = r.json().get("matches", [])
                all_matches.extend(matches)
                logger.info(f"Got {len(matches)} from {code}")
            elif r.status_code == 429:
                logger.warning(f"Rate limit hit at {code}, waiting 6s...")
                time.sleep(6)
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    matches = r.json().get("matches", [])
                    all_matches.extend(matches)
                    logger.info(f"Retry got {len(matches)} from {code}")
            else:
                logger.error(f"API error {r.status_code} for {code}: {r.text[:100]}")
            
            time.sleep(0.3)
            
        except Exception as e:
            logger.error(f"Error: {e}")
    
    logger.info(f"Total: {len(all_matches)} matches")
    
    # Update cache
    if not competition and not date_filter:
        matches_cache["data"] = all_matches
        matches_cache["updated_at"] = datetime.now()
        logger.info("Matches cache updated")
    
    return all_matches


def get_standings(competition="PL"):
    """Get league standings with home/away stats"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        url = f"{FOOTBALL_API_URL}/competitions/{competition}/standings"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
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


def get_team_form(team_id, limit=5):
    """Get team's recent form (last N matches)"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        url = f"{FOOTBALL_API_URL}/teams/{team_id}/matches"
        params = {"status": "FINISHED", "limit": limit}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        
        if r.status_code == 200:
            matches = r.json().get("matches", [])
            
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


def get_h2h(match_id):
    """Get head-to-head history"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        url = f"{FOOTBALL_API_URL}/matches/{match_id}/head2head"
        params = {"limit": 10}
        r = requests.get(url, headers=headers, params=params, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
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


def get_odds(home_team, away_team):
    """Get betting odds"""
    if not ODDS_API_KEY:
        return None
    
    try:
        url = f"{ODDS_API_URL}/sports/soccer/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",
            "markets": "h2h,totals",
            "oddsFormat": "decimal"
        }
        r = requests.get(url, params=params, timeout=10)
        
        if r.status_code == 200:
            events = r.json()
            
            for event in events:
                if (home_team.lower() in event.get("home_team", "").lower() or
                    away_team.lower() in event.get("away_team", "").lower()):
                    
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
            home = m.get("homeTeam", {}).get("name", "").lower()
            away = m.get("awayTeam", {}).get("name", "").lower()
            home_short = m.get("homeTeam", {}).get("shortName", "").lower()
            away_short = m.get("awayTeam", {}).get("shortName", "").lower()
            home_tla = m.get("homeTeam", {}).get("tla", "").lower()
            away_tla = m.get("awayTeam", {}).get("tla", "").lower()
            
            if (team_lower in home or team_lower in away or
                team_lower in home_short or team_lower in away_short or
                team_lower == home_tla or team_lower == away_tla or
                home in team_lower or away in team_lower):
                logger.info(f"Found match: {home} vs {away} for query '{team}'")
                return m
    
    return None


# ===== MATCH WARNINGS =====

def get_match_warnings(match, home_form, away_form, lang="ru"):
    """Get warnings for a match (cup, top club, rotation)"""
    warnings = []
    
    home_team = match.get("homeTeam", {}).get("name", "")
    away_team = match.get("awayTeam", {}).get("name", "")
    competition = match.get("competition", {}).get("name", "")
    
    # Check if cup match
    is_cup = any(kw in competition for kw in CUP_KEYWORDS)
    if is_cup:
        warnings.append(get_text("cup_warning", lang))
    
    # Check if playing against top club
    home_is_top = any(club.lower() in home_team.lower() for club in TOP_CLUBS)
    away_is_top = any(club.lower() in away_team.lower() for club in TOP_CLUBS)
    
    if home_is_top or away_is_top:
        top_club = home_team if home_is_top else away_team
        warnings.append(f"{get_text('top_club_warning', lang)} ({top_club})")
    
    # Check form for rotation risk (3+ losses)
    if home_form and home_form.get("losses", 0) >= 3:
        warnings.append(f"{get_text('rotation_warning', lang)} ({home_team})")
    if away_form and away_form.get("losses", 0) >= 3:
        warnings.append(f"{get_text('rotation_warning', lang)} ({away_team})")
    
    return warnings


# ===== ENHANCED ANALYSIS =====

def analyze_match_enhanced(match, user_settings=None, lang="ru"):
    """Enhanced match analysis with form, H2H, and home/away stats"""
    
    if not claude_client:
        return "AI unavailable"
    
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "?")
    comp_code = match.get("competition", {}).get("code", "PL")
    
    # Get all data
    home_form = get_team_form(home_id) if home_id else None
    away_form = get_team_form(away_id) if away_id else None
    h2h = get_h2h(match_id) if match_id else None
    odds = get_odds(home, away)
    standings = get_standings(comp_code)
    
    # Get warnings
    warnings = get_match_warnings(match, home_form, away_form, lang)
    
    # Build analysis context
    analysis_data = f"Match: {home} vs {away}\nCompetition: {comp}\n\n"
    
    # Add warnings to context
    if warnings:
        analysis_data += "⚠️ WARNINGS:\n"
        for w in warnings:
            analysis_data += f"  {w}\n"
        analysis_data += "\n"
    
    # Form analysis
    if home_form:
        analysis_data += f"📊 {home} форма (последние 5):\n"
        analysis_data += f"  Результат: {home_form['form']} ({home_form['wins']}W-{home_form['draws']}D-{home_form['losses']}L)\n"
        analysis_data += f"  Голы: забито {home_form['goals_scored']}, пропущено {home_form['goals_conceded']}\n\n"
    
    if away_form:
        analysis_data += f"📊 {away} форма (последние 5):\n"
        analysis_data += f"  Результат: {away_form['form']} ({away_form['wins']}W-{away_form['draws']}D-{away_form['losses']}L)\n"
        analysis_data += f"  Голы: забито {away_form['goals_scored']}, пропущено {away_form['goals_conceded']}\n\n"
    
    # H2H analysis
    if h2h:
        analysis_data += f"⚔️ H2H (последние {len(h2h.get('matches', []))} матчей):\n"
        analysis_data += f"  {home}: {h2h['home_wins']} побед | Ничьи: {h2h['draws']} | {away}: {h2h['away_wins']} побед\n"
        analysis_data += f"  Средние голы: {h2h['avg_goals']:.1f} за матч\n"
        analysis_data += f"  Обе забьют: {h2h['btts_percent']:.0f}%\n"
        analysis_data += f"  Тотал больше 2.5: {h2h['over25_percent']:.0f}%\n\n"
    
    # Home/Away standings
    if standings:
        home_stats = None
        away_stats = None
        
        for team in standings.get("home", []):
            if home.lower() in team.get("team", {}).get("name", "").lower():
                home_stats = team
            if away.lower() in team.get("team", {}).get("name", "").lower():
                away_stats = team
        
        if home_stats:
            analysis_data += f"🏠 {home} дома:\n"
            analysis_data += f"  Позиция: {home_stats.get('position', '?')}\n"
            analysis_data += f"  Очки: {home_stats.get('points', '?')} ({home_stats.get('won', 0)}W-{home_stats.get('draw', 0)}D-{home_stats.get('lost', 0)}L)\n"
            analysis_data += f"  Голы: {home_stats.get('goalsFor', 0)}-{home_stats.get('goalsAgainst', 0)}\n\n"
        
        for team in standings.get("away", []):
            if away.lower() in team.get("team", {}).get("name", "").lower():
                away_stats = team
                break
        
        if away_stats:
            analysis_data += f"✈️ {away} в гостях:\n"
            analysis_data += f"  Позиция: {away_stats.get('position', '?')}\n"
            analysis_data += f"  Очки: {away_stats.get('points', '?')} ({away_stats.get('won', 0)}W-{away_stats.get('draw', 0)}D-{away_stats.get('lost', 0)}L)\n"
            analysis_data += f"  Голы: {away_stats.get('goalsFor', 0)}-{away_stats.get('goalsAgainst', 0)}\n\n"
    
    # Odds
    if odds:
        analysis_data += "💰 Коэффициенты:\n"
        for k, v in odds.items():
            analysis_data += f"  {k}: {v}\n"
        analysis_data += "\n"
    
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

You are an expert betting analyst. Analyze this match with available data:

{analysis_data}

{filter_info}

CRITICAL RULES:
1. ALWAYS give a prediction even if some data is missing
2. If opponent data is missing - still analyze based on what you have
3. If it's a cup match or lower division team - acknowledge it but still predict
4. NEVER say "cannot analyze" or "need more data" - work with what's available
5. Use common football knowledge if specific stats are missing
6. DIVERSIFY bet types - not only totals! Include outcomes, BTTS, double chance
7. For TOP CLUBS (Real, Barca, Bayern, Liverpool, City) - never bet against them even if bad form
8. Consider VALUE BETTING: confidence × odds > 1.0 means value exists

PROVIDE ANALYSIS IN THIS FORMAT:

📊 **СТАТИСТИКА:**
• Форма хозяев: [анализ или "данные недоступны"]
• Форма гостей: [анализ или "данные недоступны"]
• H2H тренд: [если есть] 
• Контекст: [кубковый матч / лига / дерби и т.д.]

🎯 **ОСНОВНАЯ СТАВКА** (Уверенность: X%):
[Тип ставки] @ [коэфф]
💰 Банк: X%
📝 Почему: [2-3 предложения]

📈 **ДОПОЛНИТЕЛЬНЫЕ СТАВКИ:**
1. [Исход/Тотал/BTTS] - X% - коэфф ~X.XX
2. [Другой тип] - X% - коэфф ~X.XX  
3. [Точный счёт] - X% - коэфф ~X.XX

⚠️ **РИСКИ:**
[Риски включая предупреждения выше]

✅ **ВЕРДИКТ:** [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ВЫСОКИЙ РИСК]

Bank %: 75%+=4-5%, 70-75%=3%, 65-70%=2%, 60-65%=1%, <60%=0.5%"""

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


def get_recommendations_enhanced(matches, user_query="", user_settings=None, league_filter=None, lang="ru"):
    """Enhanced recommendations with user preferences"""
    
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
        target_league = league_names.get(league_filter, league_filter)
        matches = [m for m in matches if target_league.lower() in m.get("competition", {}).get("name", "").lower()]
    
    if not matches:
        return "❌ Нет матчей для выбранной лиги." if lang == "ru" else "❌ No matches for selected league."
    
    # Get form data for top matches
    matches_data = []
    for m in matches[:8]:
        home = m.get("homeTeam", {}).get("name", "?")
        away = m.get("awayTeam", {}).get("name", "?")
        comp = m.get("competition", {}).get("name", "?")
        home_id = m.get("homeTeam", {}).get("id")
        away_id = m.get("awayTeam", {}).get("id")
        
        home_form = get_team_form(home_id) if home_id else None
        away_form = get_team_form(away_id) if away_id else None
        
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
    """Start command with reply keyboard"""
    user = update.effective_user
    lang = detect_language(user)
    
    if not get_user(user.id):
        create_user(user.id, user.username, lang)
    
    keyboard = [
        [InlineKeyboardButton("📊 Рекомендации", callback_data="cmd_recommend"),
         InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")],
        [InlineKeyboardButton("📆 Завтра", callback_data="cmd_tomorrow"),
         InlineKeyboardButton("🏆 Лиги", callback_data="cmd_leagues")],
        [InlineKeyboardButton("🔔 Live-алерты", callback_data="cmd_live"),
         InlineKeyboardButton("⚙️ Настройки", callback_data="cmd_settings")],
        [InlineKeyboardButton("⭐ Избранное", callback_data="cmd_favorites"),
         InlineKeyboardButton("📈 Статистика", callback_data="cmd_stats")],
        [InlineKeyboardButton("❓ Помощь", callback_data="cmd_help")]
    ]
    
    text = f"""⚽ **AI Betting Bot v13**

{get_text('welcome', lang)}

💎 Бесплатно: {FREE_DAILY_LIMIT} прогноза/день
🔓 Безлимит: сделай депозит по ссылке"""
    
    await update.message.reply_text(
        text, 
        reply_markup=get_main_keyboard(lang),
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        "Выбери действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's matches"""
    user = get_user(update.effective_user.id)
    lang = user.get("language", "ru") if user else "ru"
    
    status = await update.message.reply_text(get_text("analyzing", lang))
    
    matches = get_matches(date_filter="today")
    
    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    by_comp = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(m)
    
    text = "📅 **МАТЧИ СЕГОДНЯ:**\n\n" if lang == "ru" else "📅 **TODAY'S MATCHES:**\n\n"
    
    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            try:
                dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except:
                time_str = "?"
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
    
    status = await update.message.reply_text(get_text("analyzing", lang))
    
    matches = get_matches(date_filter="tomorrow")
    
    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    by_comp = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(m)
    
    text = "📆 **МАТЧИ ЗАВТРА:**\n\n" if lang == "ru" else "📆 **TOMORROW'S MATCHES:**\n\n"
    
    for comp, ms in by_comp.items():
        text += f"🏆 **{comp}**\n"
        for m in ms[:5]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            try:
                dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                time_str = dt.strftime("%H:%M")
            except:
                time_str = "?"
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
    
    keyboard = [
        [InlineKeyboardButton(f"📉 Мин. коэфф: {user['min_odds']}", callback_data="set_min_odds")],
        [InlineKeyboardButton(f"📈 Макс. коэфф: {user['max_odds']}", callback_data="set_max_odds")],
        [InlineKeyboardButton(f"⚠️ Риск: {user['risk_level']}", callback_data="set_risk")],
        [InlineKeyboardButton("🌍 Язык / Language", callback_data="set_language")],
        [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
    ]
    
    text = f"""⚙️ **НАСТРОЙКИ**

📉 **Минимальный коэфф:** {user['min_odds']}
📈 **Максимальный коэфф:** {user['max_odds']}
⚠️ **Уровень риска:** {user['risk_level']}
🌍 **Язык:** {lang.upper()}
💎 **Премиум:** {'✅ Да' if user.get('is_premium') else '❌ Нет'}

Нажми на параметр чтобы изменить:"""
    
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
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить лигу", callback_data="add_fav_league")],
        [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
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
    
    text = f"""📈 **СТАТИСТИКА**

{win_emoji} **Точность:** {stats['correct']}/{stats['checked']} ({stats['win_rate']:.1f}%)

📊 **Всего прогнозов:** {stats['total']}
✅ **Проверено:** {stats['checked']}
⏳ **Ожидают результата:** {stats['pending']}

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
            text += f"  • {cat_name}: {data['correct']}/{data['total']} ({data['rate']}%)\n"
        text += "\n"
    
    # Recent predictions
    text += f"{'─'*25}\n📝 **Последние прогнозы:**\n"
    for p in stats.get("predictions", [])[:7]:
        if p["is_correct"] is None:
            emoji = "⏳"
            result_text = "ожидаем"
        elif p["is_correct"]:
            emoji = "✅"
            result_text = p["result"] or "выиграл"
        else:
            emoji = "❌"
            result_text = p["result"] or "проиграл"
        
        home_short = p["home"][:10] + ".." if len(p["home"]) > 12 else p["home"]
        away_short = p["away"][:10] + ".." if len(p["away"]) > 12 else p["away"]
        
        text += f"{emoji} {home_short} - {away_short}\n"
        text += f"    📊 {p['bet_type']} ({p['confidence']}%) → {result_text}\n"
    
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="cmd_stats")],
        [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get recommendations with user preferences"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("language", "ru") if user else "ru"
    
    # Check daily limit
    can_use, remaining = check_daily_limit(user_id)
    if not can_use:
        text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
        keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=AFFILIATE_LINK)]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    status = await update.message.reply_text(get_text("analyzing", lang))
    
    matches = get_matches(days=7)
    
    if not matches:
        await status.edit_text(get_text("no_matches", lang))
        return
    
    user_query = update.message.text or ""
    recs = get_recommendations_enhanced(matches, user_query, user, lang=lang)
    
    if recs:
        # Add affiliate button
        keyboard = [
            [InlineKeyboardButton(get_text("place_bet", lang), url=AFFILIATE_LINK)],
            [InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")]
        ]
        increment_daily_usage(user_id)
        await status.edit_text(recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await status.edit_text("❌ Ошибка анализа.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    user = get_user(update.effective_user.id)
    lang = user.get("language", "ru") if user else "ru"
    
    text = f"""❓ **ПОМОЩЬ**

**Основные команды:**
• /start - Главное меню
• /recommend - Лучшие ставки
• /today - Матчи сегодня
• /tomorrow - Матчи завтра
• /live - 🔔 Включить алерты
• /settings - Настройки
• /favorites - Избранное
• /stats - Статистика

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
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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
            [InlineKeyboardButton("📊 Рекомендации", callback_data="cmd_recommend"),
             InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")],
            [InlineKeyboardButton("📆 Завтра", callback_data="cmd_tomorrow"),
             InlineKeyboardButton("🏆 Лиги", callback_data="cmd_leagues")],
            [InlineKeyboardButton("🔔 Live-алерты", callback_data="cmd_live"),
             InlineKeyboardButton("⚙️ Настройки", callback_data="cmd_settings")],
            [InlineKeyboardButton("⭐ Избранное", callback_data="cmd_favorites"),
             InlineKeyboardButton("📈 Статистика", callback_data="cmd_stats")],
            [InlineKeyboardButton("❓ Помощь", callback_data="cmd_help")]
        ]
        await query.edit_message_text("⚽ **AI Betting Bot v13** - Выбери действие:", 
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_recommend":
        # Check limit
        can_use, _ = check_daily_limit(user_id)
        if not can_use:
            text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=AFFILIATE_LINK)]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        await query.edit_message_text(get_text("analyzing", lang))
        matches = get_matches(days=7)
        if matches:
            recs = get_recommendations_enhanced(matches, "", user, lang=lang)
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=AFFILIATE_LINK)],
                [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
            ]
            increment_daily_usage(user_id)
            await query.edit_message_text(recs or "❌ Ошибка", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await query.edit_message_text(get_text("no_matches", lang))
    
    elif data == "cmd_today":
        await query.edit_message_text(get_text("analyzing", lang))
        matches = get_matches(date_filter="today")
        if not matches:
            await query.edit_message_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        text = "📅 **МАТЧИ СЕГОДНЯ:**\n\n"
        for comp, ms in by_comp.items():
            text += f"🏆 **{comp}**\n"
            for m in ms[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                try:
                    dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M")
                except:
                    time_str = "?"
                text += f"  ⏰ {time_str} | {home} vs {away}\n"
            text += "\n"
        
        keyboard = [
            [InlineKeyboardButton("📊 Рекомендации", callback_data="rec_today")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_tomorrow":
        await query.edit_message_text(get_text("analyzing", lang))
        matches = get_matches(date_filter="tomorrow")
        if not matches:
            await query.edit_message_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        text = "📆 **МАТЧИ ЗАВТРА:**\n\n"
        for comp, ms in by_comp.items():
            text += f"🏆 **{comp}**\n"
            for m in ms[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                try:
                    dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                    time_str = dt.strftime("%H:%M")
                except:
                    time_str = "?"
                text += f"  ⏰ {time_str} | {home} vs {away}\n"
            text += "\n"
        
        keyboard = [
            [InlineKeyboardButton("📊 Рекомендации", callback_data="rec_tomorrow")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_leagues":
        keyboard = [
            [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL"),
             InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
            [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="league_BL1"),
             InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
            [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="league_FL1"),
             InlineKeyboardButton("🇪🇺 Champions League", callback_data="league_CL")],
            [InlineKeyboardButton("🇧🇷 Brasileirão", callback_data="league_BSA")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
        ]
        await query.edit_message_text("🏆 **Выбери лигу:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_settings":
        await settings_cmd(update, context)
    
    elif data == "cmd_favorites":
        await favorites_cmd(update, context)
    
    elif data == "cmd_stats":
        await stats_cmd(update, context)
    
    elif data == "cmd_help":
        await help_cmd(update, context)
    
    elif data == "cmd_live":
        if user_id in live_subscribers:
            live_subscribers.remove(user_id)
            await query.edit_message_text(
                "🔕 **Live-алерты выключены**\n\n"
                "Напиши /live чтобы включить снова.",
                parse_mode="Markdown"
            )
        else:
            live_subscribers.add(user_id)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]]
            await query.edit_message_text(
                "🔔 **Live-алерты включены!**\n\n"
                "Каждые 10 минут проверяю матчи.\n"
                "Если найду ставку с 70%+ за 1-3 часа — пришлю алерт!\n\n"
                "Напиши /live чтобы выключить.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
    
    # League selection
    elif data.startswith("league_"):
        code = data.replace("league_", "")
        await query.edit_message_text(f"🔍 Загружаю {COMPETITIONS.get(code, code)}...")
        matches = get_matches(code, days=14)
        
        if not matches:
            await query.edit_message_text(f"❌ Нет матчей {COMPETITIONS.get(code, code)}")
            return
        
        text = f"🏆 **{COMPETITIONS.get(code, code)}**\n\n"
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
            [InlineKeyboardButton("📊 Рекомендации", callback_data=f"rec_{code}")],
            [InlineKeyboardButton("🔙 К лигам", callback_data="cmd_leagues")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    # Recommendations for specific context
    elif data.startswith("rec_"):
        # Check limit
        can_use, _ = check_daily_limit(user_id)
        if not can_use:
            text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=AFFILIATE_LINK)]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        context_type = data.replace("rec_", "")
        await query.edit_message_text(get_text("analyzing", lang))
        
        if context_type == "today":
            matches = get_matches(date_filter="today")
        elif context_type == "tomorrow":
            matches = get_matches(date_filter="tomorrow")
        else:
            matches = get_matches(context_type, days=14)
        
        if matches:
            recs = get_recommendations_enhanced(matches, "", user, lang=lang)
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=AFFILIATE_LINK)],
                [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
            ]
            increment_daily_usage(user_id)
            await query.edit_message_text(recs or "❌ Ошибка", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_settings")]
        ]
        await query.edit_message_text("📉 Выбери минимальный коэффициент:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("min_"):
        value = float(data.replace("min_", ""))
        update_user_settings(user_id, min_odds=value)
        await query.answer(f"✅ Минимальный коэфф: {value}")
        await settings_cmd(update, context)
    
    elif data == "set_max_odds":
        keyboard = [
            [InlineKeyboardButton("2.0", callback_data="max_2.0"),
             InlineKeyboardButton("2.5", callback_data="max_2.5"),
             InlineKeyboardButton("3.0", callback_data="max_3.0")],
            [InlineKeyboardButton("4.0", callback_data="max_4.0"),
             InlineKeyboardButton("5.0", callback_data="max_5.0"),
             InlineKeyboardButton("10.0", callback_data="max_10.0")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_settings")]
        ]
        await query.edit_message_text("📈 Выбери максимальный коэффициент:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("max_"):
        value = float(data.replace("max_", ""))
        update_user_settings(user_id, max_odds=value)
        await query.answer(f"✅ Максимальный коэфф: {value}")
        await settings_cmd(update, context)
    
    elif data == "set_risk":
        keyboard = [
            [InlineKeyboardButton("🟢 Низкий (safe)", callback_data="risk_low")],
            [InlineKeyboardButton("🟡 Средний (balanced)", callback_data="risk_medium")],
            [InlineKeyboardButton("🔴 Высокий (aggressive)", callback_data="risk_high")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_settings")]
        ]
        await query.edit_message_text("⚠️ Выбери уровень риска:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("risk_"):
        value = data.replace("risk_", "")
        update_user_settings(user_id, risk_level=value)
        await query.answer(f"✅ Риск: {value}")
        await settings_cmd(update, context)
    
    elif data == "set_language":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
             InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇧🇷 Português", callback_data="lang_pt"),
             InlineKeyboardButton("🇪🇸 Español", callback_data="lang_es")],
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_settings")]
        ]
        await query.edit_message_text("🌍 Выбери язык / Choose language:", reply_markup=InlineKeyboardMarkup(keyboard))
    
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
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_favorites")]
        ]
        await query.edit_message_text("➕ Выбери лигу:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("fav_league_"):
        code = data.replace("fav_league_", "")
        add_favorite_league(user_id, code)
        await query.answer(f"✅ {COMPETITIONS.get(code, code)} добавлена!")
        await favorites_cmd(update, context)
    
    elif data.startswith("fav_team_"):
        team_name = data.replace("fav_team_", "")
        add_favorite_team(user_id, team_name)
        await query.answer(f"✅ {team_name} добавлена в избранное!")


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
            [InlineKeyboardButton("📊 Рекомендации", callback_data="cmd_recommend"),
             InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")]
        ]
        await status.edit_text("👋 Привет! Выбери действие или напиши название команды:", 
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
            keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=AFFILIATE_LINK)]]
            await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        await status.edit_text(get_text("analyzing", lang))
        matches = get_matches(days=7)
        if not matches:
            await status.edit_text(get_text("no_matches", lang))
            return
        recs = get_recommendations_enhanced(matches, user_text, user, league, lang=lang)
        if recs:
            keyboard = [
                [InlineKeyboardButton(get_text("place_bet", lang), url=AFFILIATE_LINK)],
                [InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today")]
            ]
            increment_daily_usage(user_id)
            await status.edit_text(recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await status.edit_text("❌ Ошибка анализа.")
        return
    
    if intent == "matches_list":
        matches = get_matches(league, days=14) if league else get_matches(days=14)
        if not matches:
            await status.edit_text(get_text("no_matches", lang))
            return
        
        by_comp = {}
        for m in matches:
            comp = m.get("competition", {}).get("name", "Other")
            if comp not in by_comp:
                by_comp[comp] = []
            by_comp[comp].append(m)
        
        text = "⚽ **Ближайшие матчи:**\n\n"
        for comp, ms in list(by_comp.items())[:5]:
            text += f"🏆 **{comp}**\n"
            for m in ms[:3]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                text += f"  • {home} vs {away}\n"
            text += "\n"
        
        keyboard = [[InlineKeyboardButton("📊 Рекомендации", callback_data="cmd_recommend")]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # Team search - detailed analysis
    # Check limit first
    can_use, _ = check_daily_limit(user_id)
    if not can_use:
        text = get_text("daily_limit", lang).format(limit=FREE_DAILY_LIMIT)
        keyboard = [[InlineKeyboardButton(get_text("unlimited", lang), url=AFFILIATE_LINK)]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    await status.edit_text("🔍 Ищу матч...")
    
    matches = get_matches(days=14)
    match = None
    
    if teams:
        match = find_match(teams, matches)
    
    if not match:
        match = find_match([user_text], matches)
    
    if not match:
        text = f"😕 Не нашёл матч: {', '.join(teams) if teams else user_text}\n\n"
        if matches:
            text += "📋 **Доступные матчи:**\n"
            for m in matches[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                text += f"  • {home} vs {away}\n"
        
        keyboard = [[InlineKeyboardButton("📊 Рекомендации", callback_data="cmd_recommend")]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # Found match - do enhanced analysis
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    comp = match.get("competition", {}).get("name", "?")
    match_id = match.get("id")
    
    await status.edit_text(f"✅ Нашёл: {home} vs {away}\n🏆 {comp}\n\n⏳ Собираю статистику...")
    
    # Enhanced analysis
    analysis = analyze_match_enhanced(match, user, lang)
    
    # Extract and save prediction
    try:
        confidence = 70
        bet_type = "П1"
        odds_value = 1.5
        
        conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', analysis)
        if conf_match:
            confidence = int(conf_match.group(1))
        
        analysis_lower = analysis.lower()
        
        if "тб 2.5" in analysis_lower or "over 2.5" in analysis_lower:
            bet_type = "ТБ 2.5"
        elif "тм 2.5" in analysis_lower or "under 2.5" in analysis_lower:
            bet_type = "ТМ 2.5"
        elif "обе забьют" in analysis_lower or "btts" in analysis_lower:
            bet_type = "BTTS"
        elif "п2" in analysis_lower or "победа гостей" in analysis_lower:
            bet_type = "П2"
        elif "п1" in analysis_lower or "победа хозя" in analysis_lower:
            bet_type = "П1"
        elif "ничья" in analysis_lower:
            bet_type = "Х"
        elif "1x" in analysis_lower or "x2" in analysis_lower:
            bet_type = "1X" if "1x" in analysis_lower else "X2"
        
        odds_match = re.search(r'@\s*~?(\d+\.?\d*)', analysis)
        if odds_match:
            odds_value = float(odds_match.group(1))
        
        save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_value)
        increment_daily_usage(user_id)
        logger.info(f"Saved prediction: {home} vs {away}, {bet_type}, {confidence}%")
        
    except Exception as e:
        logger.error(f"Error saving prediction: {e}")
    
    header = f"⚽ **{home}** vs **{away}**\n🏆 {comp}\n{'─'*30}\n\n"
    
    keyboard = [
        [InlineKeyboardButton(get_text("place_bet", lang), url=AFFILIATE_LINK)],
        [InlineKeyboardButton(f"⭐ {home}", callback_data=f"fav_team_{home}"),
         InlineKeyboardButton(f"⭐ {away}", callback_data=f"fav_team_{away}")],
        [InlineKeyboardButton("📊 Ещё рекомендации", callback_data="cmd_recommend")]
    ]
    
    await status.edit_text(header + analysis, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")


# ===== LIVE ALERTS SYSTEM =====

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle live alerts subscription"""
    user_id = update.effective_user.id
    
    if user_id in live_subscribers:
        live_subscribers.remove(user_id)
        await update.message.reply_text(
            "🔕 **Live-алерты выключены**\n\n"
            "Напиши /live чтобы включить снова.",
            parse_mode="Markdown"
        )
    else:
        live_subscribers.add(user_id)
        await update.message.reply_text(
            "🔔 **Live-алерты включены!**\n\n"
            "Каждые 10 минут проверяю матчи.\n"
            "Если найду ставку 70%+ за 1-3 часа — пришлю алерт!\n\n"
            "Напиши /live чтобы выключить.",
            parse_mode="Markdown"
        )


async def testalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test alert - manually trigger check"""
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔍 Проверяю матчи для алертов...")
    
    was_subscribed = user_id in live_subscribers
    live_subscribers.add(user_id)
    
    matches = get_matches(days=1, use_cache=False)
    
    if not matches:
        await update.message.reply_text("❌ Нет матчей сегодня")
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
            time.sleep(0.5)
            
        except Exception as e:
            text += f"   ❌ Ошибка\n\n"
    
    text += f"✅ Обновлено: {checked} прогнозов\nНапиши /stats для статистики"
    
    await update.message.reply_text(text, parse_mode="Markdown")


async def check_live_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check upcoming matches and send alerts"""
    
    if not live_subscribers:
        return
    
    logger.info(f"Checking live for {len(live_subscribers)} subscribers...")
    
    matches = get_matches(days=1)
    
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
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")
        
        home_form = get_team_form(home_id) if home_id else None
        away_form = get_team_form(away_id) if away_id else None
        odds = get_odds(home, away)
        
        form_text = ""
        if home_form:
            form_text += f"{home}: {home_form['form']}\n"
        if away_form:
            form_text += f"{away}: {away_form['form']}"
        
        odds_text = ""
        if odds:
            for k, v in odds.items():
                if not k.startswith("Over") and not k.startswith("Under"):
                    odds_text += f"{k}: {v}, "
        
        prompt = f"""Quick analysis for live alert:

Match: {home} vs {away}
Competition: {comp}
Form: {form_text if form_text else "Limited data"}
Odds: {odds_text if odds_text else "Not available"}

If you find a reasonable bet (70%+ confidence), respond with:

🚨 LIVE ALERT!

⚽ {home} vs {away}
🏆 {comp}
⏰ Через 1-3 часа

⚡ СТАВКА: [bet type]
📊 Уверенность: X%
💰 Коэфф: ~X.XX
📝 Почему: [1 sentence]

ONLY respond "NO_ALERT" if no good bet exists."""

        try:
            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response = message.content[0].text
            
            if "NO_ALERT" not in response and "LIVE ALERT" in response:
                keyboard = [[InlineKeyboardButton("🎰 Поставить", url=AFFILIATE_LINK)]]
                
                for user_id in live_subscribers:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=response,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to send to {user_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Claude error: {e}")
        
        time.sleep(1)


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
                    
                    if is_correct is not None:
                        update_prediction_result(pred["id"], result, 1 if is_correct else 0)
                        logger.info(f"Updated prediction {pred['id']}: {result} -> {'✅' if is_correct else '❌'}")
                        
                        # Notify user
                        try:
                            await context.bot.send_message(
                                chat_id=pred["user_id"],
                                text=f"📊 **Результат прогноза**\n\n"
                                     f"⚽ {pred['home']} vs {pred['away']}\n"
                                     f"🎯 Ставка: {pred['bet_type']}\n"
                                     f"📈 Счёт: {result}\n"
                                     f"{'✅ Прогноз верный!' if is_correct else '❌ Прогноз не сработал'}",
                                parse_mode="Markdown"
                            )
                        except:
                            pass
            
            time.sleep(0.5)
            
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
    
    matches = get_matches(date_filter="today")
    
    if not matches:
        return
    
    recs = get_recommendations_enhanced(matches, "daily digest")
    
    if not recs:
        return
    
    text = f"☀️ **ДАЙДЖЕСТ НА СЕГОДНЯ**\n\n{recs}"
    
    keyboard = [
        [InlineKeyboardButton("🎰 Ставить", url=AFFILIATE_LINK)],
        [InlineKeyboardButton("📅 Все матчи", callback_data="cmd_today")]
    ]
    
    for user_id in live_subscribers:
        try:
            await context.bot.send_message(
                chat_id=user_id, 
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to send digest to {user_id}: {e}")


# ===== MAIN =====

def main():
    init_db()
    
    print("🚀 Starting AI Betting Bot v13 Complete...")
    print(f"   💾 Database: {DB_PATH}")
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    
    print("   ✅ Telegram")
    print("   ✅ Football Data (Standard 25 leagues)" if FOOTBALL_API_KEY else "   ⚠️ No Football API")
    print("   ✅ Odds API (20K credits)" if ODDS_API_KEY else "   ⚠️ No Odds API")
    print("   ✅ Claude AI" if CLAUDE_API_KEY else "   ⚠️ No Claude API")
    print(f"   🔗 Affiliate: 1win")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("recommend", recommend_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("favorites", favorites_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("testalert", testalert_cmd))
    app.add_handler(CommandHandler("checkresults", check_results_cmd))
    
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
    
    print("\n✅ Bot v13 Complete running!")
    print("   🔥 Features:")
    print("   • Reply keyboard menu (always visible)")
    print("   • Multi-language (RU/EN/PT/ES)")
    print("   • Daily limit (3 free predictions)")
    print("   • Stats by bet category")
    print("   • 1win affiliate integration")
    print("   • Cup/Top club warnings")
    print("   • 25 leagues (Standard plan)")
    print("   • Live alerts system")
    print("   • Prediction tracking")
    print("   • Daily digest")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
