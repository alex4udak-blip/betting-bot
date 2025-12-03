import os
import logging
import requests
import json
import sqlite3
import asyncio
import time
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

# Matches cache to reduce API calls
matches_cache = {
    "data": [],
    "updated_at": None,
    "ttl_seconds": 120  # Cache for 2 minutes
}

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
}

# ===== DATABASE =====

DB_PATH = "/data/betting_bot.db" if os.path.exists("/data") else "betting_bot.db"

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
    
    # If we can't determine bet type but it's something like "match_analysis"
    # Try to guess based on the result - home team usually favored
    elif "analysis" in bet_lower or bet_type == "":
        return home_score > away_score  # Default to home win check
    
    # Default - can't determine
    return None

def get_user_stats(user_id):
    """Get user's prediction statistics with recent predictions"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ?", (user_id,))
    total = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct = 1", (user_id,))
    correct = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM predictions WHERE user_id = ? AND is_correct IS NOT NULL", (user_id,))
    checked = c.fetchone()[0]
    
    # Get recent predictions
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
- "Лига чемпионов" / "Champions League" = "CL"
- If no league = null

TEAM TRANSLATIONS:
Бавария=Bayern Munich, Арсенал=Arsenal, Ливерпуль=Liverpool, Реал=Real Madrid, Барселона=Barcelona, Дортмунд=Borussia Dortmund, ПСЖ=PSG, МЮ=Manchester United, Челси=Chelsea, Ман Сити=Manchester City, Тоттенхэм=Tottenham, Брайтон=Brighton, Астон Вилла=Aston Villa, Брентфорд=Brentford, Вест Хэм=West Ham

EXAMPLES:
- "Liverpool" → {{"intent": "team_search", "teams": ["Liverpool"], "league": null}}
- "Арсенал против Брентфорда" → {{"intent": "team_search", "teams": ["Arsenal", "Brentford"], "league": null}}
- "что думаешь про Баварию" → {{"intent": "team_search", "teams": ["Bayern Munich"], "league": null}}
- "Brighton vs Aston Villa analysis" → {{"intent": "team_search", "teams": ["Brighton", "Aston Villa"], "league": null}}
- "лучшие ставки" → {{"intent": "recommend", "teams": [], "league": null}}
- "топ ставки на сегодня" → {{"intent": "recommend", "teams": [], "league": null}}

Return ONLY JSON."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        
        result = json.loads(text)
        
        # SAFETY CHECK: If teams are mentioned, force team_search
        if result.get("teams") and len(result.get("teams", [])) > 0:
            result["intent"] = "team_search"
        
        return result
        
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return {"intent": "team_search", "teams": [user_message]}


# ===== API FUNCTIONS =====

def get_matches(competition=None, days=7, date_filter=None, use_cache=True):
    """Get matches from all leagues with rate limit handling and caching"""
    global matches_cache
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    # Check cache first (only for default params)
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
                time.sleep(6)  # Wait and retry
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    return r.json().get("matches", [])
            else:
                logger.error(f"API error {r.status_code} for {competition}")
        except Exception as e:
            logger.error(f"Error getting matches for {competition}: {e}")
        return []
    
    # Get from all leagues with rate limit awareness
    all_matches = []
    for code in ["PL", "PD", "BL1", "SA", "FL1"]:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{code}/matches"
            r = requests.get(url, headers=headers, params=params, timeout=10)
            
            if r.status_code == 200:
                matches = r.json().get("matches", [])
                all_matches.extend(matches)
                logger.info(f"Got {len(matches)} from {code}")
            elif r.status_code == 429:
                logger.warning(f"Rate limit hit at {code}, waiting 6s...")
                time.sleep(6)  # Wait 6 seconds before retry
                r = requests.get(url, headers=headers, params=params, timeout=10)
                if r.status_code == 200:
                    matches = r.json().get("matches", [])
                    all_matches.extend(matches)
                    logger.info(f"Retry got {len(matches)} from {code}")
            else:
                logger.error(f"API error {r.status_code} for {code}: {r.text[:100]}")
            
            # Small delay between requests to avoid rate limit
            time.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Error: {e}")
    
    logger.info(f"Total: {len(all_matches)} matches")
    
    # Update cache (only for default params)
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
    """Find match by team names - flexible matching"""
    if not matches or not team_names:
        return None
    
    for team in team_names:
        if not team:
            continue
            
        team_lower = team.lower().strip()
        
        # Skip very short queries
        if len(team_lower) < 3:
            continue
        
        for m in matches:
            home = m.get("homeTeam", {}).get("name", "").lower()
            away = m.get("awayTeam", {}).get("name", "").lower()
            home_short = m.get("homeTeam", {}).get("shortName", "").lower()
            away_short = m.get("awayTeam", {}).get("shortName", "").lower()
            home_tla = m.get("homeTeam", {}).get("tla", "").lower()
            away_tla = m.get("awayTeam", {}).get("tla", "").lower()
            
            # Check all name variants
            if (team_lower in home or team_lower in away or
                team_lower in home_short or team_lower in away_short or
                team_lower == home_tla or team_lower == away_tla or
                home in team_lower or away in team_lower):
                logger.info(f"Found match: {home} vs {away} for query '{team}'")
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
    
    prompt = f"""You are an expert betting analyst. Analyze this match with available data:

{analysis_data}

{filter_info}

CRITICAL RULES:
1. ALWAYS give a prediction even if some data is missing
2. If opponent data is missing - still analyze based on what you have
3. If it's a cup match or lower division team - acknowledge it but still predict
4. Respond in the SAME LANGUAGE as team names (Russian for Russian teams, etc.)
5. NEVER say "cannot analyze" or "need more data" - work with what's available
6. Use common football knowledge if specific stats are missing (e.g., Liverpool is historically strong at home)

PROVIDE ANALYSIS IN THIS FORMAT:

📊 **СТАТИСТИКА:**
• Форма хозяев: [анализ или "данные недоступны"]
• Форма гостей: [анализ или "данные недоступны - команда из низшего дивизиона"]
• H2H тренд: [если есть] 
• Контекст: [кубковый матч / лига / дерби и т.д.]

🎯 **ОСНОВНАЯ СТАВКА** (Уверенность: X%):
[Тип ставки] @ [примерный коэфф если нет точного]
💰 Банк: X%
📝 Почему: [2-3 предложения - используй общие знания о командах если нет статистики]

📈 **ДОПОЛНИТЕЛЬНЫЕ СТАВКИ:**
1. [Тотал] - X% - коэфф ~X.XX
2. [BTTS] - X% - коэфф ~X.XX  
3. [Точный счёт] - X% - коэфф ~X.XX
4. [Голы в тайме] - X% - коэфф ~X.XX

⚠️ **РИСКИ:**
[Риски включая неполные данные если применимо]

✅ **ВЕРДИКТ:** [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ВЫСОКИЙ РИСК]

NOTE: If data is limited, use lower confidence (55-65%) but STILL make a prediction.
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
    """Show user statistics with prediction history"""
    user_id = update.effective_user.id
    stats = get_user_stats(user_id)
    
    if stats["total"] == 0:
        text = """📈 **МОЯ СТАТИСТИКА**

Пока нет данных. Статистика начнёт собираться когда ты будешь получать прогнозы.

💡 Напиши название команды (например "Liverpool") чтобы получить прогноз."""
    else:
        win_emoji = "🔥" if stats["win_rate"] >= 70 else "✅" if stats["win_rate"] >= 50 else "📉"
        
        text = f"""📈 **МОЯ СТАТИСТИКА**

{win_emoji} **Точность:** {stats['correct']}/{stats['checked']} ({stats['win_rate']:.1f}%)

📊 **Всего прогнозов:** {stats['total']}
✅ **Проверено:** {stats['checked']}
⏳ **Ожидают результата:** {stats['pending']}

{'─'*25}
📋 **Последние прогнозы:**
"""
        # Add recent predictions
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
            
            # Shorten team names
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
                "Каждые 10 минут проверяю матчи.\n"
                "Если найду ставку с 70%+ за 1-3 часа — пришлю алерт!\n\n"
                "📨 Пример алерта:\n"
                "```\n"
                "🚨 LIVE ALERT!\n"
                "⚽ Arsenal vs Chelsea\n"
                "⚡ СТАВКА: Победа хозяев\n"
                "📊 Уверенность: 72%\n"
                "```\n\n"
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
    
    elif data.startswith("fav_team_"):
        team_name = data.replace("fav_team_", "")
        add_favorite_team(user_id, team_name)
        await query.answer(f"✅ {team_name} добавлена в избранное!")
        # Don't edit message, just show notification


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
    
    logger.info(f"Team search for: {teams}")
    
    matches = get_matches(days=14)
    logger.info(f"Got {len(matches) if matches else 0} matches to search")
    
    match = None
    
    if teams:
        match = find_match(teams, matches)
        logger.info(f"find_match with teams result: {match is not None}")
    
    if not match:
        match = find_match([user_text], matches)
        logger.info(f"find_match with user_text result: {match is not None}")
    
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
    analysis = analyze_match_enhanced(match, user)
    
    # Extract and save prediction from analysis
    try:
        # Try to extract confidence from response
        confidence = 70  # default
        bet_type = "П1"  # default to home win (most common)
        odds_value = 1.5
        
        # Look for confidence percentage
        import re
        conf_match = re.search(r'[Уу]веренность[:\s]*(\d+)%', analysis)
        if conf_match:
            confidence = int(conf_match.group(1))
        
        # Look for bet type - check from most specific to least
        analysis_lower = analysis.lower()
        
        if "тб 2.5" in analysis_lower or "тотал больше 2.5" in analysis_lower or "over 2.5" in analysis_lower:
            bet_type = "ТБ 2.5"
        elif "тм 2.5" in analysis_lower or "тотал меньше 2.5" in analysis_lower or "under 2.5" in analysis_lower:
            bet_type = "ТМ 2.5"
        elif "обе забьют" in analysis_lower or "btts" in analysis_lower:
            bet_type = "BTTS"
        elif "п2" in analysis_lower or "победа гостей" in analysis_lower:
            bet_type = "П2"
        elif "п1" in analysis_lower or "победа хозя" in analysis_lower or "победа " + home.lower()[:5] in analysis_lower:
            bet_type = "П1"
        elif "ничья" in analysis_lower or " х " in analysis_lower:
            bet_type = "Х"
        elif "1x" in analysis_lower or "двойной шанс" in analysis_lower:
            bet_type = "1X"
        
        # Look for odds
        odds_match = re.search(r'@\s*~?(\d+\.?\d*)', analysis)
        if odds_match:
            odds_value = float(odds_match.group(1))
        
        # Save prediction
        save_prediction(user_id, match_id, home, away, bet_type, confidence, odds_value)
        logger.info(f"Saved prediction: {home} vs {away}, {bet_type}, {confidence}%")
        
    except Exception as e:
        logger.error(f"Error saving prediction: {e}")
    
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
            "Каждые 10 минут проверяю матчи.\n"
            "Если найду ставку с 70%+ уверенностью за 1-3 часа до матча — пришлю алерт!\n\n"
            "📊 Типы алертов:\n"
            "• Победа фаворита\n"
            "• Тоталы с высокой вероятностью\n"
            "• Обе забьют\n\n"
            "⚠️ Алерт придёт только если:\n"
            "1. Есть матч в окне 0.5-3 часа\n"
            "2. Claude найдёт ставку 70%+\n\n"
            "Напиши /live чтобы выключить.",
            parse_mode="Markdown"
        )


async def testalert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test alert - manually trigger check"""
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔍 Принудительно проверяю матчи для алертов...")
    
    # Temporarily add user to subscribers
    was_subscribed = user_id in live_subscribers
    live_subscribers.add(user_id)
    
    # Get matches
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
            
            if hours_until > 0:  # Future matches
                all_today.append((m, hours_until))
                if 0.5 < hours_until < 3:
                    upcoming.append(m)
        except:
            continue
    
    # Show status
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
    
    if not upcoming:
        text += "\n⚠️ Нет матчей в окне 0.5-3 часа — алерты не сработают"
        await update.message.reply_text(text, parse_mode="Markdown")
        if not was_subscribed:
            live_subscribers.discard(user_id)
        return
    
    text += "\n🔄 Анализирую матчи в окне..."
    await update.message.reply_text(text, parse_mode="Markdown")
    
    # Analyze one match
    match = upcoming[0]
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
    
    odds_text = str(odds) if odds else "нет данных"
    
    prompt = f"""You are a betting expert. Quick analysis for live alert:

Match: {home} vs {away}
Competition: {comp}
Form (if available): {form_text if form_text else "Limited data"}
Odds (if available): {odds_text if odds_text else "Not available"}

RULES:
1. ALWAYS try to find a betting opportunity
2. If one team's data is missing - analyze with what you have  
3. Use general football knowledge about teams
4. For cup matches against lower league teams - favorites usually win
5. If data is limited, give 65-70% confidence for obvious favorites

If you find a reasonable bet (65%+ confidence), respond with:

🚨 LIVE ALERT!

⚽ {home} vs {away}
🏆 {comp}
⏰ Через 1-3 часа

⚡ СТАВКА: [bet type]
📊 Уверенность: X%
💰 Коэфф: ~X.XX
🎯 Банк: X%
📝 Почему: [1 sentence - can use general knowledge]

ONLY respond "NO_ALERT" if both teams are unknown AND no clear favorite exists."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response = message.content[0].text
        
        result = f"**Claude ответил:**\n\n{response}"
        await update.message.reply_text(result, parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    
    if not was_subscribed:
        live_subscribers.discard(user_id)


async def check_results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually check prediction results"""
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔄 Проверяю результаты матчей...")
    
    pending = get_pending_predictions()
    user_pending = [p for p in pending if p.get("user_id") == user_id]
    
    if not user_pending:
        await update.message.reply_text(
            "✅ Нет прогнозов, ожидающих результата.\n\n"
            "Напиши название команды чтобы получить прогноз, например: `Liverpool`",
            parse_mode="Markdown"
        )
        return
    
    text = f"📊 **Твои прогнозы ({len(user_pending)}):**\n\n"
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    checked = 0
    
    for pred in user_pending[:5]:  # Check max 5
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
                text += f"   ⚠️ API error: {r.status_code}\n\n"
                continue
            
            match_data = r.json()
            status = match_data.get("status")
            
            text += f"   📍 Статус: {status}\n"
            
            if status == "FINISHED":
                score = match_data.get("score", {}).get("fullTime", {})
                home_score = score.get("home", 0)
                away_score = score.get("away", 0)
                
                is_correct = check_bet_result(bet_type, home_score, away_score)
                
                if is_correct is not None:
                    result_str = f"{home_score}:{away_score}"
                    update_prediction_result(pred["id"], result_str, 1 if is_correct else 0)
                    
                    emoji = "✅" if is_correct else "❌"
                    text += f"   {emoji} Результат: {result_str} → {'верно!' if is_correct else 'неверно'}\n"
                    checked += 1
                else:
                    text += f"   ⚠️ Не могу определить результат для ставки '{bet_type}'\n"
            else:
                text += f"   ⏳ Матч ещё не завершён\n"
            
            text += "\n"
            time.sleep(0.5)  # Rate limit
            
        except Exception as e:
            text += f"   ❌ Ошибка: {e}\n\n"
    
    text += f"{'─'*25}\n"
    text += f"✅ Обновлено: {checked} прогнозов\n"
    text += f"Напиши /stats для статистики"
    
    await update.message.reply_text(text, parse_mode="Markdown")


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
Form (if available): {form_text if form_text else "Limited data"}
Odds (if available): {odds_text if odds_text else "Not available"}

RULES:
1. ALWAYS try to find a betting opportunity
2. If one team's data is missing - that's OK, analyze with what you have
3. Use general football knowledge (team strength, historical performance)
4. For cup matches against lower league teams - favorites usually win
5. If data is limited, you can still give 65-70% confidence for obvious favorites

If you find a reasonable bet (65%+ confidence), respond with:

🚨 LIVE ALERT!

⚽ {home} vs {away}
🏆 {comp}
⏰ Через 1-3 часа

⚡ СТАВКА: [bet type]
📊 Уверенность: X%
💰 Коэфф: ~X.XX
🎯 Банк: X%
📝 Почему: [1 sentence - can use general knowledge]

ONLY respond "NO_ALERT" if both teams are unknown AND no clear favorite exists.

For matches like Liverpool vs lower league team - this IS a clear opportunity!"""

        try:
            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response = message.content[0].text
            
            # Log Claude's response for debugging
            if "NO_ALERT" in response:
                logger.info(f"Alert check for {home} vs {away}: NO_ALERT (confidence < 75%)")
            elif "LIVE ALERT" in response:
                logger.info(f"🚨 Alert triggered for {home} vs {away}!")
                
                for user_id in live_subscribers:
                    try:
                        keyboard = [[InlineKeyboardButton("📊 Подробный анализ", callback_data=f"analyze_{match.get('id', 0)}")]]
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=response,
                            reply_markup=InlineKeyboardMarkup(keyboard),
                            parse_mode="Markdown"
                        )
                        logger.info(f"Alert sent to user {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to send alert to {user_id}: {e}")
            else:
                logger.warning(f"Unexpected response for {home} vs {away}: {response[:100]}...")
                        
        except Exception as e:
            logger.error(f"Alert analysis error: {e}")


async def check_predictions_results(context: ContextTypes.DEFAULT_TYPE):
    """Check finished matches and update prediction results - runs every hour"""
    
    pending = get_pending_predictions()
    
    if not pending:
        logger.info("No pending predictions to check")
        return
    
    logger.info(f"Checking {len(pending)} pending predictions...")
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    for pred in pending:
        match_id = pred.get("match_id")
        if not match_id:
            continue
        
        try:
            # Get match result from API
            url = f"{FOOTBALL_API_URL}/matches/{match_id}"
            r = requests.get(url, headers=headers, timeout=10)
            
            if r.status_code != 200:
                continue
            
            match_data = r.json()
            status = match_data.get("status")
            
            # Only process finished matches
            if status != "FINISHED":
                continue
            
            score = match_data.get("score", {}).get("fullTime", {})
            home_score = score.get("home")
            away_score = score.get("away")
            
            if home_score is None or away_score is None:
                continue
            
            # Check if bet was correct
            bet_type = pred.get("bet_type", "")
            is_correct = check_bet_result(bet_type, home_score, away_score)
            
            if is_correct is None:
                # Can't determine, skip
                continue
            
            result_str = f"{home_score}:{away_score}"
            update_prediction_result(pred["id"], result_str, 1 if is_correct else 0)
            
            logger.info(f"Updated prediction {pred['id']}: {pred['home']} vs {pred['away']} = {result_str}, correct={is_correct}")
            
            # Notify user about result
            user_id = pred.get("user_id")
            if user_id:
                emoji = "✅" if is_correct else "❌"
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"{emoji} **Результат прогноза:**\n\n"
                             f"⚽ {pred['home']} {home_score}:{away_score} {pred['away']}\n"
                             f"📊 Ставка: {bet_type}\n"
                             f"{'✅ Прогноз верный!' if is_correct else '❌ Прогноз не сработал'}\n\n"
                             f"Напиши /stats для полной статистики",
                        parse_mode="Markdown"
                    )
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Error checking prediction {pred['id']}: {e}")


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


def get_match_result(match_id):
    """Get match result by ID"""
    if not FOOTBALL_API_KEY:
        return None
    
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        url = f"{FOOTBALL_API_URL}/matches/{match_id}"
        r = requests.get(url, headers=headers, timeout=10)
        
        if r.status_code == 200:
            match = r.json()
            status = match.get("status")
            
            if status == "FINISHED":
                score = match.get("score", {}).get("fullTime", {})
                home_score = score.get("home", 0) or 0
                away_score = score.get("away", 0) or 0
                return {
                    "status": "FINISHED",
                    "home_score": home_score,
                    "away_score": away_score,
                    "result": f"{home_score}-{away_score}"
                }
            else:
                return {"status": status}
    except Exception as e:
        logger.error(f"Get match result error: {e}")
    
    return None


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
    app.add_handler(CommandHandler("testalert", testalert_cmd))
    app.add_handler(CommandHandler("checkresults", check_results_cmd))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Job Queue - Live Alerts (increased intervals to avoid API rate limit)
    job_queue = app.job_queue
    job_queue.run_repeating(check_live_matches, interval=600, first=120)  # Every 10 min
    job_queue.run_repeating(send_daily_digest, interval=7200, first=300)  # Every 2 hours
    job_queue.run_repeating(check_predictions_results, interval=3600, first=600)  # Every hour
    
    print("\n✅ Bot v12 running!")
    print(f"   💾 Database: {DB_PATH}")
    print("   📊 Enhanced analysis with form + H2H + home/away")
    print("   ⚙️ Personalization (odds, risk level)")
    print("   🎛️ Inline buttons for better UX")
    print("   🔔 Live alerts every 10 min (use /live)")
    print("   📈 Prediction tracking + auto-results check")
    print("   💾 Matches cache (2 min TTL)")
    print("   🔧 Commands: /testalert, /checkresults")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
