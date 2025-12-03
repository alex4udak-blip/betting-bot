import os
import logging
import requests
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, JobQueue
import anthropic

# ===== CONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

FOOTBALL_API_URL = "https://api.football-data.org/v4"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

claude_client = None
if CLAUDE_API_KEY:
    claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# Live mode subscribers
live_subscribers = set()
inplay_subscribers = set()

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
}

# ===== DATABASE =====

DB_PATH = "/home/claude/betting_bot.db" if os.path.exists("/home/claude") else "betting_bot.db"

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Users table
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        min_odds REAL DEFAULT 1.3,
        max_odds REAL DEFAULT 3.0,
        risk_level TEXT DEFAULT 'medium',
        language TEXT DEFAULT 'ru'
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
    
    # Predictions tracking
    c.execute('''CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        match_id INTEGER,
        home_team TEXT,
        away_team TEXT,
        bet_type TEXT,
        confidence INTEGER,
        odds REAL,
        predicted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        result TEXT,
        is_correct INTEGER,
        checked_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )''')
    
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
            "min_odds": row[3],
            "max_odds": row[4],
            "risk_level": row[5],
            "language": row[6]
        }
    return None

def create_user(user_id, username=None):
    """Create new user"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()

def update_user_settings(user_id, **kwargs):
    """Update user settings"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    for key, value in kwargs.items():
        if key in ['min_odds', 'max_odds', 'risk_level', 'language']:
            c.execute(f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id))
    
    conn.commit()
    conn.close()

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

def save_prediction(user_id, match_id, home, away, bet_type, confidence, odds):
    """Save prediction to database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""INSERT INTO predictions 
                 (user_id, match_id, home_team, away_team, bet_type, confidence, odds)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (user_id, match_id, home, away, bet_type, confidence, odds))
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    """Get user's prediction statistics"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 1", (user_id,))
    correct = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct IS NOT NULL", (user_id,))
    checked = c.fetchone()[0]
    
    conn.close()
    
    return {
        "total": total,
        "correct": correct,
        "checked": checked,
        "pending": total - checked,
        "win_rate": (correct / checked * 100) if checked > 0 else 0
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

INTENT RULES:
- "team_search" = asks about specific team OR "who wins X" OR mentions any team name
- "recommend" = wants betting tips/recommendations
- "matches_list" = wants to see matches
- "next_match" = asks for closest/next/nearest match
- "today" = asks about today's matches
- "tomorrow" = asks about tomorrow's matches
- "settings" = wants to change settings/preferences
- "favorites" = asks about favorite teams/leagues
- "stats" = asks about statistics/results
- "greeting" = just hello/hi
- "help" = asks how to use

LEAGUE DETECTION (put in "league" field):
- "немецкая лига" / "Bundesliga" / "бундеслига" = "BL1"
- "английская лига" / "Premier League" / "АПЛ" = "PL"  
- "испанская лига" / "La Liga" / "Ла Лига" = "PD"
- "итальянская лига" / "Serie A" / "Серия А" = "SA"
- "французская лига" / "Ligue 1" / "Лига 1" = "FL1"
- "Лига чемпионов" / "Champions League" = "CL"
- If no specific league mentioned = null

TEAM TRANSLATIONS:
Бавария=Bayern Munich, Арсенал=Arsenal, Ливерпуль=Liverpool, Реал=Real Madrid, Барселона=Barcelona, Дортмунд=Borussia Dortmund, ПСЖ=PSG, МЮ=Manchester United, Челси=Chelsea, Ман Сити=Manchester City

Return ONLY JSON, no other text."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        
        return json.loads(text)
        
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return {"intent": "team_search", "teams": [user_message]}


# ===== API FUNCTIONS =====

def get_matches(competition=None, days=7, date_filter=None):
    """Get matches from all leagues"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
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
        except Exception as e:
            logger.error(f"Error getting matches for {competition}: {e}")
        return []
    
    # Get from all leagues
    all_matches = []
    for code in ["PL", "PD", "BL1", "SA", "FL1"]:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{code}/matches"
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                matches = r.json().get("matches", [])
                all_matches.extend(matches)
                logger.info(f"Got {len(matches)} from {code}")
        except Exception as e:
            logger.error(f"Error: {e}")
    
    logger.info(f"Total: {len(all_matches)} matches")
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
            
            # Analyze H2H patterns
            home_wins = 0
            away_wins = 0
            draws = 0
            total_goals = 0
            btts_count = 0  # Both teams to score
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
    """Find match by team names"""
    if not matches:
        return None
    
    for team in team_names:
        team_lower = team.lower()
        for m in matches:
            home = m.get("homeTeam", {}).get("name", "").lower()
            away = m.get("awayTeam", {}).get("name", "").lower()
            
            if team_lower in home or team_lower in away:
                return m
            
            # Check short names
            home_short = m.get("homeTeam", {}).get("shortName", "").lower()
            away_short = m.get("awayTeam", {}).get("shortName", "").lower()
            
            if team_lower in home_short or team_lower in away_short:
                return m
    
    return None


# ===== ENHANCED ANALYSIS =====

def analyze_match_enhanced(match, user_settings=None):
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
    
    # Build analysis context
    analysis_data = f"Match: {home} vs {away}\nCompetition: {comp}\n\n"
    
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
        
        # Find teams in home standings
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
        
        # Find away team in away standings
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
    
    prompt = f"""You are an expert betting analyst. Analyze this match with ALL available data:

{analysis_data}

{filter_info}

IMPORTANT:
- Respond in the SAME LANGUAGE as user's query (detect from team names/competition)
- Use ALL data provided (form, H2H, home/away stats, odds)
- Be confident but realistic
- Consider the user's risk preferences if provided

PROVIDE ANALYSIS IN THIS FORMAT:

📊 **СТАТИСТИКА:**
• Форма хозяев: [краткий анализ]
• Форма гостей: [краткий анализ]
• H2H тренд: [что показывает история]
• Дома/В гостях: [как команды играют дома/в гостях]

🎯 **ОСНОВНАЯ СТАВКА** (Уверенность: X%):
[Тип ставки] @ [коэфф]
💰 Банк: X%
📝 Почему: [2-3 предложения с конкретными фактами из данных]

📈 **ДОПОЛНИТЕЛЬНЫЕ СТАВКИ:**
1. [Тотал больше/меньше X.5] - X% - коэфф X.XX
   Причина: [факт из H2H или формы]
2. [Обе забьют / Не забьют] - X% - коэфф X.XX
   Причина: [факт]
3. [Точный счёт X:X] - X% - коэфф X.XX
   Причина: [почему этот счёт вероятен]
4. [Голы в 1-м тайме / Гол до X мин] - X% - коэфф X.XX
   Причина: [факт]

⚠️ **РИСКИ:**
[Конкретные риски на основе данных]

✅ **ВЕРДИКТ:** [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ВЫСОКИЙ РИСК / ПРОПУСТИТЬ]

RULES:
- Use actual data from the analysis, not generic statements
- Bank %: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%
- Be specific about WHY you recommend each bet
- Include exact score prediction based on goal-scoring patterns"""

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


def get_recommendations_enhanced(matches, user_query="", user_settings=None, league_filter=None):
    """Enhanced recommendations with user preferences"""
    
    logger.info(f"Getting recommendations for {len(matches) if matches else 0} matches")
    
    if not claude_client:
        return None
    
    if not matches:
        return "❌ Нет доступных матчей."
    
    # Filter by league
    if league_filter:
        league_names = {
            "PL": "Premier League",
            "PD": "Primera Division",
            "BL1": "Bundesliga",
            "SA": "Serie A",
            "FL1": "Ligue 1",
            "CL": "UEFA Champions League"
        }
        target_league = league_names.get(league_filter, league_filter)
        matches = [m for m in matches if target_league.lower() in m.get("competition", {}).get("name", "").lower()]
    
    if not matches:
        return "❌ Нет матчей для выбранной лиги."
    
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
        
        match_info = f"{home} vs {away} ({comp})"
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
    
    prompt = f"""User asked: "{user_query}"

Analyze these matches with form data and give TOP 3-4 picks:

{matches_text}

{filter_info}

IMPORTANT:
- Respond in the SAME LANGUAGE as the user's query
- Use form data to support recommendations
- Filter by user's odds and risk preferences
- Be confident and specific

FORMAT:
🔥 **ТОП СТАВКИ:**

1️⃣ **[Команда] vs [Команда]**
   ✅ Ставка: [тип] @ коэфф X.XX
   📊 Уверенность: X%
   💰 Банк: X%
   📈 Форма: [краткий анализ формы обеих команд]
   💡 Почему: [1-2 предложения]

2️⃣ ...

3️⃣ ...

❌ **ИЗБЕГАТЬ:**
• [Матч] - [почему рискованно, ссылка на форму]

Bank %: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%"""

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
    """Start command with inline buttons"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    # Create user if not exists
    if not get_user(user_id):
        create_user(user_id, username)
    
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
    
    text = f"""⚽ **BetAnalyzer AI** - Умные прогнозы

Привет! Я анализирую матчи используя:
• Форму команд (последние 5 матчей)
• H2H статистику
• Домашние/гостевые показатели
• Актуальные коэффициенты

**Быстрые команды:**
📊 /recommend - Лучшие ставки
📅 /today - Матчи сегодня
📆 /tomorrow - Матчи завтра
🔔 /live - Включить алерты за 1-3ч до матча
⚙️ /settings - Настройки

Или просто напиши **название команды**!"""
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show today's matches"""
    status = await update.message.reply_text("🔍 Загружаю матчи на сегодня...")
    
    matches = get_matches(date_filter="today")
    
    if not matches:
        await status.edit_text("❌ Сегодня нет матчей в топ-лигах.")
        return
    
    # Group by competition
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
    
    # Add quick action buttons
    keyboard = [
        [InlineKeyboardButton("📊 Рекомендации на сегодня", callback_data="rec_today")],
        [InlineKeyboardButton("📆 Завтра", callback_data="cmd_tomorrow")]
    ]
    
    await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tomorrow's matches"""
    status = await update.message.reply_text("🔍 Загружаю матчи на завтра...")
    
    matches = get_matches(date_filter="tomorrow")
    
    if not matches:
        await status.edit_text("❌ Завтра нет матчей в топ-лигах.")
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
    
    keyboard = [
        [InlineKeyboardButton(f"📉 Мин. коэфф: {user['min_odds']}", callback_data="set_min_odds")],
        [InlineKeyboardButton(f"📈 Макс. коэфф: {user['max_odds']}", callback_data="set_max_odds")],
        [InlineKeyboardButton(f"⚠️ Риск: {user['risk_level']}", callback_data="set_risk")],
        [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
    ]
    
    text = f"""⚙️ **НАСТРОЙКИ**

Текущие параметры фильтрации:

📉 **Минимальный коэфф:** {user['min_odds']}
_(ставки с коэффом ниже не показываются)_

📈 **Максимальный коэфф:** {user['max_odds']}
_(ставки с коэффом выше не показываются)_

⚠️ **Уровень риска:** {user['risk_level']}
• low — только 75%+ уверенность
• medium — 65-80% уверенность
• high — включая рискованные ставки

Нажми на параметр чтобы изменить:"""
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def favorites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show favorites menu"""
    user_id = update.effective_user.id
    
    teams = get_favorite_teams(user_id)
    leagues = get_favorite_leagues(user_id)
    
    text = "⭐ **ИЗБРАННОЕ**\n\n"
    
    if teams:
        text += "**Команды:**\n"
        for t in teams:
            text += f"  • {t}\n"
    else:
        text += "_Нет избранных команд_\n"
    
    text += "\n"
    
    if leagues:
        text += "**Лиги:**\n"
        for l in leagues:
            text += f"  • {COMPETITIONS.get(l, l)}\n"
    else:
        text += "_Нет избранных лиг_\n"
    
    text += "\n💡 Напиши название команды и нажми ⭐ чтобы добавить в избранное"
    
    keyboard = [
        [InlineKeyboardButton("➕ Добавить лигу", callback_data="add_fav_league")],
        [InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user statistics"""
    user_id = update.effective_user.id
    stats = get_user_stats(user_id)
    
    if stats["total"] == 0:
        text = """📈 **МОЯ СТАТИСТИКА**

Пока нет данных. Статистика начнёт собираться когда ты будешь получать прогнозы и матчи завершатся."""
    else:
        win_emoji = "🔥" if stats["win_rate"] >= 70 else "✅" if stats["win_rate"] >= 50 else "📉"
        
        text = f"""📈 **МОЯ СТАТИСТИКА**

{win_emoji} **Точность:** {stats['correct']}/{stats['checked']} ({stats['win_rate']:.1f}%)

📊 **Всего прогнозов:** {stats['total']}
✅ **Проверено:** {stats['checked']}
⏳ **Ожидают результата:** {stats['pending']}
"""
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get recommendations with user preferences"""
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    status = await update.message.reply_text("🔍 Анализирую лучшие ставки с учётом твоих настроек...")
    
    matches = get_matches(days=7)
    
    if not matches:
        await status.edit_text("❌ Нет доступных матчей.")
        return
    
    user_query = update.message.text or ""
    recs = get_recommendations_enhanced(matches, user_query, user)
    
    if recs:
        await status.edit_text(recs, parse_mode="Markdown")
    else:
        await status.edit_text("❌ Ошибка анализа.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
    text = """❓ **ПОМОЩЬ**

**Основные команды:**
• /start - Главное меню
• /recommend - Лучшие ставки
• /today - Матчи сегодня
• /tomorrow - Матчи завтра
• /live - 🔔 Включить алерты за 1-3ч до матча
• /settings - Настройки фильтров
• /favorites - Избранные команды/лиги
• /stats - Моя статистика

**Как пользоваться:**
1. Напиши название команды (напр. "Ливерпуль")
2. Получи детальный анализ с формой, H2H и рекомендациями
3. Настрой фильтры под свой стиль игры
4. Включи /live для автоматических алертов

**Live-алерты:**
Каждые 5 минут бот проверяет матчи на ближайшие 1-3 часа.
Если находит ставку с 75%+ уверенностью — присылает алерт!

**Типы ставок:**
• П1/Х/П2 - Исход матча
• ТБ/ТМ 2.5 - Тоталы
• Обе забьют - BTTS
• Точный счёт
• Голы по таймам

**Уровни риска:**
• 🟢 low - Безопасные ставки (75%+)
• 🟡 medium - Баланс риска и прибыли
• 🔴 high - Рискованные с высоким потенциалом"""
    
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
        await query.edit_message_text("⚽ **BetAnalyzer AI** - Выбери действие:", 
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    
    elif data == "cmd_recommend":
        await query.edit_message_text("🔍 Анализирую лучшие ставки...")
        user = get_user(user_id)
        matches = get_matches(days=7)
        if matches:
            recs = get_recommendations_enhanced(matches, "", user)
            await query.edit_message_text(recs or "❌ Ошибка", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Нет матчей")
    
    elif data == "cmd_today":
        await query.edit_message_text("🔍 Загружаю матчи на сегодня...")
        matches = get_matches(date_filter="today")
        if not matches:
            await query.edit_message_text("❌ Сегодня нет матчей в топ-лигах.")
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
        await query.edit_message_text("🔍 Загружаю матчи на завтра...")
        matches = get_matches(date_filter="tomorrow")
        if not matches:
            await query.edit_message_text("❌ Завтра нет матчей в топ-лигах.")
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
        user_id = query.from_user.id
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
                "Я буду проверять матчи каждые 5 минут.\n"
                "Когда найду хорошую ставку (75%+) за 1-3 часа до матча — пришлю алерт!\n\n"
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
        context_type = data.replace("rec_", "")
        await query.edit_message_text("🔍 Анализирую...")
        
        user = get_user(user_id)
        
        if context_type == "today":
            matches = get_matches(date_filter="today")
        elif context_type == "tomorrow":
            matches = get_matches(date_filter="tomorrow")
        else:
            matches = get_matches(context_type, days=14)
        
        if matches:
            recs = get_recommendations_enhanced(matches, "", user)
            keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="cmd_start")]]
            await query.edit_message_text(recs or "❌ Ошибка", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Нет матчей")
    
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
        await query.answer(f"✅ Минимальный коэфф установлен: {value}")
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
        await query.answer(f"✅ Максимальный коэфф установлен: {value}")
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
        await query.answer(f"✅ Уровень риска установлен: {value}")
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
            [InlineKeyboardButton("🔙 Назад", callback_data="cmd_favorites")]
        ]
        await query.edit_message_text("➕ Выбери лигу для добавления:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("fav_league_"):
        code = data.replace("fav_league_", "")
        add_favorite_league(user_id, code)
        await query.answer(f"✅ {COMPETITIONS.get(code, code)} добавлена в избранное!")
        await favorites_cmd(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    user_text = update.message.text.strip()
    user_id = update.effective_user.id
    
    if len(user_text) < 2:
        return
    
    # Ensure user exists
    if not get_user(user_id):
        create_user(user_id, update.effective_user.username)
    
    user = get_user(user_id)
    
    status = await update.message.reply_text("🔍 Анализирую запрос...")
    
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
        await status.edit_text("🔍 Анализирую лучшие ставки...")
        matches = get_matches(days=7)
        if not matches:
            await status.edit_text("❌ Нет доступных матчей.")
            return
        recs = get_recommendations_enhanced(matches, user_text, user, league)
        if recs:
            keyboard = [[InlineKeyboardButton("📅 Сегодня", callback_data="cmd_today"),
                        InlineKeyboardButton("📆 Завтра", callback_data="cmd_tomorrow")]]
            await status.edit_text(recs, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await status.edit_text("❌ Ошибка анализа.")
        return
    
    if intent == "matches_list":
        matches = get_matches(league, days=14) if league else get_matches(days=14)
        if not matches:
            await status.edit_text("❌ Нет матчей.")
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
    
    if intent == "next_match":
        matches = get_matches(league, days=3) if league else get_matches(days=3)
        if not matches:
            await status.edit_text("❌ Нет ближайших матчей.")
            return
        
        matches.sort(key=lambda m: m.get("utcDate", ""))
        next_match = matches[0]
        home = next_match.get("homeTeam", {}).get("name", "?")
        away = next_match.get("awayTeam", {}).get("name", "?")
        comp = next_match.get("competition", {}).get("name", "?")
        
        try:
            dt = datetime.fromisoformat(next_match.get("utcDate", "").replace("Z", "+00:00"))
            date_str = dt.strftime("%d.%m.%Y %H:%M")
        except:
            date_str = "?"
        
        text = f"⏰ **Ближайший матч:**\n\n⚽ {home} vs {away}\n🏆 {comp}\n📅 {date_str}"
        
        keyboard = [[InlineKeyboardButton(f"📊 Анализ", callback_data=f"analyze_{next_match.get('id', 0)}")]]
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # Team search - detailed analysis
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
    
    await status.edit_text(f"✅ Нашёл: {home} vs {away}\n🏆 {comp}\n\n⏳ Собираю статистику...")
    
    # Enhanced analysis
    analysis = analyze_match_enhanced(match, user)
    
    header = f"⚽ **{home}** vs **{away}**\n🏆 {comp}\n{'─'*30}\n\n"
    
    # Add to favorites button
    keyboard = [
        [InlineKeyboardButton(f"⭐ В избранное: {home}", callback_data=f"fav_team_{home}"),
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
            "Ты больше не будешь получать уведомления о матчах.\n"
            "Напиши /live чтобы включить снова.",
            parse_mode="Markdown"
        )
    else:
        live_subscribers.add(user_id)
        await update.message.reply_text(
            "🔔 **Live-алерты включены!**\n\n"
            "Я буду проверять матчи каждые 5 минут.\n"
            "Когда найду хорошую ставку (75%+) за 1-3 часа до матча — пришлю алерт!\n\n"
            "📊 Типы алертов:\n"
            "• Победа фаворита\n"
            "• Тоталы с высокой вероятностью\n"
            "• Обе забьют\n\n"
            "Напиши /live чтобы выключить.",
            parse_mode="Markdown"
        )


async def check_live_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check upcoming matches and send alerts - runs every 5 minutes"""
    
    if not live_subscribers:
        return
    
    logger.info(f"Checking live matches for {len(live_subscribers)} subscribers...")
    
    # Get matches in next 3 hours
    matches = get_matches(days=1)
    
    if not matches:
        return
    
    now = datetime.now()
    upcoming = []
    
    for m in matches:
        try:
            match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            hours_until = (match_time - now).total_seconds() / 3600
            
            if 0.5 < hours_until < 3:  # Between 30 min and 3 hours
                upcoming.append(m)
        except:
            continue
    
    if not upcoming:
        logger.info("No upcoming matches in 0.5-3h window")
        return
    
    logger.info(f"Found {len(upcoming)} upcoming matches")
    
    # Analyze each match
    for match in upcoming[:3]:  # Limit to 3 matches per check
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")
        
        # Get form data
        home_form = get_team_form(home_id) if home_id else None
        away_form = get_team_form(away_id) if away_id else None
        odds = get_odds(home, away)
        
        # Build context for Claude
        form_text = ""
        if home_form:
            form_text += f"{home}: {home_form['form']} ({home_form['wins']}W-{home_form['draws']}D-{home_form['losses']}L)\n"
        if away_form:
            form_text += f"{away}: {away_form['form']} ({away_form['wins']}W-{away_form['draws']}D-{away_form['losses']}L)"
        
        odds_text = ""
        if odds:
            for k, v in odds.items():
                if not k.startswith("Over") and not k.startswith("Under"):
                    odds_text += f"{k}: {v}, "
        
        prompt = f"""You are a betting expert. Quick analysis for live alert:

Match: {home} vs {away}
Competition: {comp}
Form: {form_text}
Odds: {odds_text}

If you find a bet with 75%+ confidence, respond with:

🚨 LIVE ALERT!

⚽ {home} vs {away}
🏆 {comp}
⏰ Через 1-3 часа

⚡ СТАВКА: [bet type]
📊 Уверенность: X%
💰 Коэфф: X.XX
🎯 Банк: X%
📝 Почему: [1 sentence based on form]

If NO good bet (all <75%), respond exactly: NO_ALERT

Be selective - only alert for really good opportunities!"""

        try:
            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response = message.content[0].text
            
            if "NO_ALERT" not in response and "LIVE ALERT" in response:
                logger.info(f"Sending alert for {home} vs {away}")
                
                for user_id in live_subscribers:
                    try:
                        keyboard = [[InlineKeyboardButton("📊 Подробный анализ", callback_data=f"analyze_{match.get('id', 0)}")]]
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=response,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.error(f"Failed to send alert to {user_id}: {e}")
                        
        except Exception as e:
            logger.error(f"Alert analysis error: {e}")


async def send_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Send daily digest at 10:00 - runs every 2 hours"""
    
    if not live_subscribers:
        return
    
    # Only send once a day (around 10:00)
    current_hour = datetime.now().hour
    if current_hour != 10:
        return
    
    logger.info("Sending daily digest...")
    
    matches = get_matches(date_filter="today")
    
    if not matches:
        return
    
    # Get top recommendations
    recs = get_recommendations_enhanced(matches, "daily digest")
    
    if not recs:
        return
    
    text = f"☀️ **ДАЙДЖЕСТ НА СЕГОДНЯ**\n\n{recs}"
    
    for user_id in live_subscribers:
        try:
            keyboard = [[InlineKeyboardButton("📅 Все матчи", callback_data="cmd_today")]]
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
    # Initialize database
    init_db()
    
    print("🚀 Starting AI Betting Bot v9 Enhanced...")
    print("   ✅ Database initialized")
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    
    print("   ✅ Telegram")
    print("   ✅ Football Data" if FOOTBALL_API_KEY else "   ⚠️ No Football API")
    print("   ✅ Odds API" if ODDS_API_KEY else "   ⚠️ No Odds API")
    print("   ✅ Claude AI" if CLAUDE_API_KEY else "   ⚠️ No Claude API")
    
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
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Job Queue - Live Alerts
    job_queue = app.job_queue
    job_queue.run_repeating(check_live_matches, interval=300, first=60)  # Every 5 min
    job_queue.run_repeating(send_daily_digest, interval=7200, first=120)  # Every 2 hours
    
    print("\n✅ Bot v9 Enhanced running!")
    print("   📊 Enhanced analysis with form + H2H + home/away")
    print("   💾 SQLite database for user settings")
    print("   ⚙️ Personalization (odds, risk level)")
    print("   🎛️ Inline buttons for better UX")
    print("   🔔 Live alerts every 5 min (use /live)")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
