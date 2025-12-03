import os
import logging
import requests
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import anthropic

# ===== CONFIGURATION (from environment variables) =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# API URLs
FOOTBALL_API_URL = "https://api.football-data.org/v4"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Claude client
claude_client = None
if CLAUDE_API_KEY:
    claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)


# ===== TEAM NAME TRANSLATIONS =====
TEAM_TRANSLATIONS = {
    # Russian to English - Premier League
    "арсенал": "arsenal",
    "ливерпуль": "liverpool",
    "манчестер юнайтед": "manchester united",
    "манчестер сити": "manchester city",
    "ман юнайтед": "manchester united",
    "ман сити": "manchester city",
    "челси": "chelsea",
    "тоттенхэм": "tottenham",
    "тоттенхем": "tottenham",
    "шпоры": "tottenham",
    "вест хэм": "west ham",
    "ньюкасл": "newcastle",
    "астон вилла": "aston villa",
    "эвертон": "everton",
    "брайтон": "brighton",
    "фулхэм": "fulham",
    "кристал пэлас": "crystal palace",
    "вулверхэмптон": "wolverhampton",
    "вулвз": "wolverhampton",
    "борнмут": "bournemouth",
    "ноттингем": "nottingham",
    "брентфорд": "brentford",
    "лестер": "leicester",
    "саутгемптон": "southampton",
    "ипсвич": "ipswich",
    
    # Spanish teams
    "барселона": "barcelona",
    "барса": "barcelona",
    "реал мадрид": "real madrid",
    "реал": "real madrid",
    "атлетико": "atletico madrid",
    "севилья": "sevilla",
    "валенсия": "valencia",
    "вильярреал": "villarreal",
    "бетис": "betis",
    "сосьедад": "real sociedad",
    "атлетик бильбао": "athletic bilbao",
    
    # German teams
    "бавария": "bayern",
    "байерн": "bayern",
    "боруссия дортмунд": "borussia dortmund",
    "дортмунд": "dortmund",
    "лейпциг": "leipzig",
    "байер": "bayer leverkusen",
    "леверкузен": "bayer leverkusen",
    "вольфсбург": "wolfsburg",
    "айнтрахт": "eintracht frankfurt",
    "фрайбург": "freiburg",
    "штутгарт": "stuttgart",
    "гладбах": "gladbach",
    "менхенгладбах": "monchengladbach",
    
    # Italian teams
    "ювентус": "juventus",
    "юве": "juventus",
    "милан": "milan",
    "интер": "inter",
    "наполи": "napoli",
    "рома": "roma",
    "лацио": "lazio",
    "аталанта": "atalanta",
    "фиорентина": "fiorentina",
    
    # French teams
    "пари сен жермен": "paris saint-germain",
    "псж": "paris",
    "марсель": "marseille",
    "лион": "lyon",
    "монако": "monaco",
    "лилль": "lille",
    
    # Other
    "аякс": "ajax",
    "псв": "psv",
    "порту": "porto",
    "бенфика": "benfica",
    "спортинг": "sporting",
    "селтик": "celtic",
    "рейнджерс": "rangers",
}

# ===== INTENT PATTERNS =====
RECOMMEND_PATTERNS = [
    r"посоветуй",
    r"рекоменд",
    r"на что поставить",
    r"что поставить",
    r"лучшие? ставк",
    r"интересные? матч",
    r"топ матч",
    r"suggest",
    r"recommend",
    r"best bet",
    r"good bet",
]

MATCHES_PATTERNS = [
    r"какие матчи",
    r"все матчи",
    r"список матч",
    r"матчи сегодня",
    r"матчи на выходн",
    r"what matches",
    r"show matches",
    r"list matches",
]

ANALYSIS_PATTERNS = [
    r"кто выиграет",
    r"кто победит",
    r"что думаешь про",
    r"анализ матча",
    r"прогноз на",
    r"шансы на",
    r"who will win",
    r"who wins",
    r"analyze",
    r"prediction for",
]


# ===== COMPETITION CODES =====
COMPETITIONS = {
    "PL": "Premier League 🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "PD": "La Liga 🇪🇸",
    "BL1": "Bundesliga 🇩🇪",
    "SA": "Serie A 🇮🇹",
    "FL1": "Ligue 1 🇫🇷",
    "CL": "Champions League 🇪🇺",
    "EL": "Europa League 🇪🇺",
}


# ===== HELPER FUNCTIONS =====

def translate_team_name(query):
    """Translate Russian team names to English"""
    query_lower = query.lower().strip()
    
    for ru, en in TEAM_TRANSLATIONS.items():
        if ru in query_lower:
            query_lower = query_lower.replace(ru, en)
    
    return query_lower


def detect_intent(message):
    """Detect user intent from message"""
    message_lower = message.lower()
    
    for pattern in RECOMMEND_PATTERNS:
        if re.search(pattern, message_lower):
            return "recommend"
    
    for pattern in MATCHES_PATTERNS:
        if re.search(pattern, message_lower):
            return "matches"
    
    for pattern in ANALYSIS_PATTERNS:
        if re.search(pattern, message_lower):
            return "analysis"
    
    return "team_search"


def extract_team_from_query(query):
    """Extract team name from natural language query"""
    query_lower = translate_team_name(query.lower())
    
    remove_words = [
        "кто", "выиграет", "победит", "что", "думаешь", "про", "матч", 
        "анализ", "прогноз", "на", "шансы", "или", "vs", "против",
        "who", "will", "win", "wins", "analyze", "prediction", "for",
        "match", "game", "vs", "versus", "against", "the", "a", "an"
    ]
    
    words = query_lower.split()
    filtered_words = [w for w in words if w not in remove_words and len(w) > 2]
    
    return " ".join(filtered_words)


# ===== API FUNCTIONS =====

def get_upcoming_matches(competition=None):
    """Get upcoming matches from football-data.org"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    
    params = {"dateFrom": date_from, "dateTo": date_to}
    
    try:
        if competition:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
        else:
            url = f"{FOOTBALL_API_URL}/matches"
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            return response.json().get("matches", [])
        else:
            logger.error(f"Football API error: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        logger.error(f"Error fetching matches: {e}")
        return []


def get_head_to_head(match_id):
    """Get head-to-head stats for a match"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/matches/{match_id}/head2head",
            headers=headers,
            params={"limit": 10},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        logger.error(f"Error fetching H2H: {e}")
        return None


def get_team_recent_matches(team_id, limit=5):
    """Get team's recent matches"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/teams/{team_id}/matches",
            headers=headers,
            params={"status": "FINISHED", "limit": limit},
            timeout=10
        )
        if response.status_code == 200:
            return response.json().get("matches", [])
        return []
    except Exception as e:
        logger.error(f"Error fetching team matches: {e}")
        return []


def search_match(query):
    """Search for a specific match by team names"""
    matches = get_upcoming_matches()
    query_lower = translate_team_name(query.lower().strip())
    query_clean = extract_team_from_query(query_lower)
    
    best_match = None
    best_score = 0
    
    for match in matches:
        home_team = match.get("homeTeam", {}).get("name", "").lower()
        away_team = match.get("awayTeam", {}).get("name", "").lower()
        
        score = 0
        for word in query_clean.split():
            if len(word) >= 3:
                if word in home_team:
                    score += 2
                if word in away_team:
                    score += 2
                if any(word in part for part in home_team.split()):
                    score += 1
                if any(word in part for part in away_team.split()):
                    score += 1
        
        if score > best_score:
            best_score = score
            best_match = match
    
    return best_match if best_score >= 2 else None


def get_best_matches_for_recommendation():
    """Get best matches for recommendation based on league importance"""
    matches = get_upcoming_matches()
    
    priority_leagues = ["Premier League", "La Liga", "Bundesliga", "Serie A", "UEFA Champions League"]
    
    priority_matches = []
    other_matches = []
    
    for match in matches:
        competition = match.get("competition", {}).get("name", "")
        if any(league in competition for league in priority_leagues):
            priority_matches.append(match)
        else:
            other_matches.append(match)
    
    return (priority_matches + other_matches)[:5]


def get_odds_for_match(home_team, away_team, sport="soccer_epl"):
    """Get betting odds from The Odds API"""
    sports_to_try = [
        "soccer_epl",
        "soccer_spain_la_liga", 
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
    ]
    
    for sport_key in sports_to_try:
        try:
            response = requests.get(
                f"{ODDS_API_URL}/sports/{sport_key}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal"
                },
                timeout=10
            )
            
            if response.status_code == 200:
                events = response.json()
                
                for event in events:
                    event_home = event.get("home_team", "").lower()
                    event_away = event.get("away_team", "").lower()
                    
                    home_match = any(word in event_home for word in home_team.lower().split()[:2])
                    away_match = any(word in event_away for word in away_team.lower().split()[:2])
                    
                    if home_match or away_match:
                        result = {"home_team": event.get("home_team"), "away_team": event.get("away_team")}
                        
                        bookmakers = event.get("bookmakers", [])
                        if bookmakers:
                            for market in bookmakers[0].get("markets", []):
                                if market.get("key") == "h2h":
                                    for outcome in market.get("outcomes", []):
                                        result[outcome["name"]] = outcome["price"]
                                elif market.get("key") == "totals":
                                    for outcome in market.get("outcomes", []):
                                        result[f"total_{outcome['name']}_{outcome.get('point', 2.5)}"] = outcome["price"]
                        
                        return result
        except Exception as e:
            logger.error(f"Error fetching odds for {sport_key}: {e}")
            continue
    
    return None


def format_recent_form(matches, team_id):
    """Format team's recent form as W/D/L string"""
    form = []
    for match in matches[:5]:
        home_id = match.get("homeTeam", {}).get("id")
        home_score = match.get("score", {}).get("fullTime", {}).get("home", 0)
        away_score = match.get("score", {}).get("fullTime", {}).get("away", 0)
        
        if home_score is None or away_score is None:
            continue
            
        if home_id == team_id:
            if home_score > away_score:
                form.append("✅")
            elif home_score < away_score:
                form.append("❌")
            else:
                form.append("➖")
        else:
            if away_score > home_score:
                form.append("✅")
            elif away_score < home_score:
                form.append("❌")
            else:
                form.append("➖")
    
    return "".join(form) if form else "N/A"


def analyze_match_with_claude(match_data, odds_data=None, h2h_data=None, home_form=None, away_form=None):
    """Use Claude to analyze the match and make prediction"""
    
    if not claude_client:
        return "❌ Claude API не настроен"
    
    home_team = match_data.get("homeTeam", {}).get("name", "Unknown")
    away_team = match_data.get("awayTeam", {}).get("name", "Unknown")
    competition = match_data.get("competition", {}).get("name", "Unknown League")
    match_date = match_data.get("utcDate", "Unknown")
    
    try:
        dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        match_date_formatted = dt.strftime("%d %B %Y, %H:%M UTC")
    except:
        match_date_formatted = match_date
    
    odds_info = "Коэффициенты: недоступны"
    if odds_data:
        home_odds = odds_data.get(home_team) or odds_data.get(odds_data.get("home_team", ""), "N/A")
        away_odds = odds_data.get(away_team) or odds_data.get(odds_data.get("away_team", ""), "N/A")
        draw_odds = odds_data.get("Draw", "N/A")
        
        odds_info = f"""Текущие коэффициенты (1X2):
• {home_team}: {home_odds}
• Ничья: {draw_odds}
• {away_team}: {away_odds}"""
        
        over_25 = odds_data.get("total_Over_2.5")
        under_25 = odds_data.get("total_Under_2.5")
        if over_25 and under_25:
            odds_info += f"""

Тотал 2.5:
• Больше: {over_25}
• Меньше: {under_25}"""
    
    h2h_info = ""
    if h2h_data:
        aggregates = h2h_data.get("aggregates", {})
        total_matches = aggregates.get("numberOfMatches", 0)
        home_wins = aggregates.get("homeTeam", {}).get("wins", 0)
        away_wins = aggregates.get("awayTeam", {}).get("wins", 0)
        draws = aggregates.get("homeTeam", {}).get("draws", 0)
        
        if total_matches > 0:
            h2h_info = f"""
История личных встреч (последние {total_matches}):
• {home_team}: {home_wins} побед
• Ничьих: {draws}
• {away_team}: {away_wins} побед"""
    
    form_info = ""
    if home_form or away_form:
        form_info = f"""
Форма (последние 5 матчей):
• {home_team}: {home_form or 'N/A'}
• {away_team}: {away_form or 'N/A'}"""

    prompt = f"""Ты профессиональный спортивный аналитик с 15-летним опытом анализа футбольных матчей. 
Проанализируй предстоящий матч на основе предоставленных данных.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 ДАННЫЕ ДЛЯ АНАЛИЗА:

🏆 Турнир: {competition}
📅 Дата: {match_date_formatted}
🏠 Хозяева: {home_team}
✈️ Гости: {away_team}

{odds_info}
{h2h_info}
{form_info}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ЗАДАЧА: Дай структурированный прогноз.

Формат ответа:

📈 **ВЕРОЯТНОСТИ:**
• {home_team}: X%
• Ничья: X%
• {away_team}: X%

🎯 **ОСНОВНОЙ ПРОГНОЗ:**
[Твой прогноз на исход]
Уверенность: [низкая/средняя/высокая]

⚽ **ТОТАЛ:**
[Прогноз на тотал больше/меньше 2.5]

💡 **КРАТКИЙ АНАЛИЗ:**
[2-3 предложения почему именно такой прогноз]

⚠️ **РИСКИ:**
[Что может пойти не так]

Отвечай на русском, дружелюбно но профессионально."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return f"❌ Ошибка анализа: {e}"


def get_recommendations_with_claude(matches):
    """Use Claude to recommend best bets from list of matches"""
    
    if not claude_client or not matches:
        return None
    
    matches_info = ""
    for i, match in enumerate(matches, 1):
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        date = match.get("utcDate", "")[:10]
        matches_info += f"{i}. {home} vs {away} ({comp}) - {date}\n"
    
    prompt = f"""Ты профессиональный спортивный аналитик. 
Вот список ближайших матчей:

{matches_info}

Выбери 2-3 самых интересных матча для ставок и кратко объясни почему.
Для каждого матча укажи:
- Какой матч
- Рекомендуемая ставка (победа/ничья/тотал)
- Почему это интересно (1-2 предложения)

Формат:

🔥 **РЕКОМЕНДАЦИИ НА СЕГОДНЯ:**

1️⃣ **[Команда] vs [Команда]**
   Ставка: [рекомендация]
   Почему: [объяснение]

2️⃣ ...

Отвечай кратко и по делу, на русском."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    welcome_text = """🤖 **AI Betting Analyzer**

Привет! Я анализирую футбольные матчи с помощью AI и реальных данных.

**🎮 Как использовать:**

📝 **Свободная форма:**
• "Кто выиграет Арсенал или Челси?"
• "Что думаешь про матч Ливерпуля?"
• "Посоветуй на что поставить"

⚽ **Или просто напиши команду:**
• `Arsenal`, `Барселона`, `Bayern`

**📋 Команды:**
/matches — ближайшие матчи
/leagues — выбрать лигу
/recommend — лучшие ставки на сегодня
/help — помощь

⚠️ _Прогнозы носят информационный характер. Делайте ставки ответственно._
"""
    await update.message.reply_text(welcome_text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = """📚 **Инструкция**

**Способы запроса:**

1️⃣ **Свободная форма:**
   • "Кто выиграет Барселона или Реал?"
   • "Анализ матча Ливерпуля"
   • "Посоветуй интересные матчи"

2️⃣ **Название команды:**
   • `Arsenal`, `Барселона`, `Bayern Munich`

3️⃣ **Команды:**
   • /matches — все ближайшие матчи
   • /leagues — выбрать лигу
   • /recommend — AI рекомендации

**Доступные лиги:**
🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League
🇪🇸 La Liga  
🇩🇪 Bundesliga
🇮🇹 Serie A
🇫🇷 Ligue 1
🇪🇺 Champions League

**Понимаю на русском и английском!**
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')


async def recommend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get AI recommendations for best bets"""
    await update.message.reply_text("🔍 Анализирую лучшие матчи для ставок...")
    
    matches = get_best_matches_for_recommendation()
    
    if not matches:
        await update.message.reply_text("❌ Не удалось получить матчи. Попробуй позже.")
        return
    
    recommendations = get_recommendations_with_claude(matches)
    
    if recommendations:
        await update.message.reply_text(recommendations, parse_mode='Markdown')
    else:
        text = "⚽ **Интересные матчи:**\n\n"
        for match in matches[:5]:
            home = match.get("homeTeam", {}).get("name", "?")
            away = match.get("awayTeam", {}).get("name", "?")
            comp = match.get("competition", {}).get("name", "")
            text += f"• {home} vs {away}\n  🏆 {comp}\n\n"
        text += "_Напиши название команды для детального анализа_"
        await update.message.reply_text(text, parse_mode='Markdown')


async def show_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show league selection keyboard"""
    keyboard = [
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="league_BL1")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="league_FL1")],
        [InlineKeyboardButton("🇪🇺 Champions League", callback_data="league_CL")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚽ Выбери лигу:", reply_markup=reply_markup)


async def league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league selection"""
    query = update.callback_query
    await query.answer()
    
    league_code = query.data.replace("league_", "")
    league_name = COMPETITIONS.get(league_code, league_code)
    
    await query.edit_message_text(f"🔍 Загружаю матчи {league_name}...")
    
    matches = get_upcoming_matches(league_code)
    
    if not matches:
        await query.edit_message_text(f"❌ Нет матчей в {league_name} на ближайшие 7 дней")
        return
    
    text = f"⚽ **{league_name}** — ближайшие матчи:\n\n"
    
    for match in matches[:10]:
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        date = match.get("utcDate", "")
        
        try:
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            date_str = dt.strftime("%d.%m %H:%M")
        except:
            date_str = date[:10]
        
        text += f"📅 {date_str}\n"
        text += f"   {home} vs {away}\n\n"
    
    text += "_Напиши название команды для анализа_"
    await query.edit_message_text(text, parse_mode='Markdown')


async def show_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming matches"""
    await update.message.reply_text("🔍 Загружаю ближайшие матчи...")
    
    matches = get_upcoming_matches()
    
    if not matches:
        await update.message.reply_text("❌ Не удалось получить матчи. Попробуй позже.")
        return
    
    by_competition = {}
    for match in matches:
        comp = match.get("competition", {}).get("name", "Other")
        if comp not in by_competition:
            by_competition[comp] = []
        by_competition[comp].append(match)
    
    text = "⚽ **Ближайшие матчи (7 дней):**\n\n"
    
    for comp, comp_matches in list(by_competition.items())[:5]:
        text += f"🏆 **{comp}**\n"
        for match in comp_matches[:3]:
            home = match.get("homeTeam", {}).get("name", "?")
            away = match.get("awayTeam", {}).get("name", "?")
            date = match.get("utcDate", "")
            try:
                dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
                date_str = dt.strftime("%d.%m")
            except:
                date_str = ""
            text += f"  • {home} vs {away} ({date_str})\n"
        text += "\n"
    
    text += "_Напиши название команды для анализа_"
    
    await update.message.reply_text(text, parse_mode='Markdown')


async def analyze_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user message with intelligent intent detection"""
    query = update.message.text.strip()
    
    if len(query) < 2:
        await update.message.reply_text("⚠️ Слишком короткий запрос.")
        return
    
    intent = detect_intent(query)
    
    if intent == "recommend":
        await recommend_command(update, context)
        return
    
    if intent == "matches":
        await show_matches(update, context)
        return
    
    status_msg = await update.message.reply_text(f"🔍 Ищу матч...", parse_mode='Markdown')
    
    match = search_match(query)
    
    if not match:
        await status_msg.edit_text(
            f"🤔 Не нашёл подходящий матч.\n\n"
            "💡 **Попробуй:**\n"
            "• Название команды на английском (Arsenal, Liverpool)\n"
            "• /matches — посмотреть все матчи\n"
            "• /recommend — получить рекомендации\n"
            "• /leagues — выбрать лигу",
            parse_mode='Markdown'
        )
        return
    
    home_team = match.get("homeTeam", {}).get("name", "Unknown")
    away_team = match.get("awayTeam", {}).get("name", "Unknown")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    competition = match.get("competition", {}).get("name", "")
    
    await status_msg.edit_text(
        f"✅ Нашёл: **{home_team}** vs **{away_team}**\n"
        f"🏆 {competition}\n\n"
        "⏳ Собираю данные и анализирую...",
        parse_mode='Markdown'
    )
    
    odds = get_odds_for_match(home_team, away_team)
    h2h = get_head_to_head(match_id) if match_id else None
    
    home_matches = get_team_recent_matches(home_id) if home_id else []
    away_matches = get_team_recent_matches(away_id) if away_id else []
    home_form = format_recent_form(home_matches, home_id) if home_matches else None
    away_form = format_recent_form(away_matches, away_id) if away_matches else None
    
    await status_msg.edit_text(
        f"✅ **{home_team}** vs **{away_team}**\n"
        f"🏆 {competition}\n\n"
        "🤖 AI анализирует матч...",
        parse_mode='Markdown'
    )
    
    analysis = analyze_match_with_claude(match, odds, h2h, home_form, away_form)
    
    header = f"⚽ **{home_team}** vs **{away_team}**\n"
    header += f"🏆 {competition}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    await status_msg.edit_text(header + analysis, parse_mode='Markdown')


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.message:
        await update.message.reply_text(
            "❌ Произошла ошибка. Попробуй ещё раз или напиши /start"
        )


# ===== MAIN =====

def main():
    """Start the bot"""
    
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    if not FOOTBALL_API_KEY:
        print("❌ FOOTBALL_API_KEY not set!")
        return
    if not ODDS_API_KEY:
        print("⚠️ ODDS_API_KEY not set - odds will be unavailable")
    if not CLAUDE_API_KEY:
        print("⚠️ CLAUDE_API_KEY not set - AI analysis will be unavailable")
    
    print("🚀 Starting AI Betting Analyzer Bot...")
    print(f"   Telegram: ✅")
    print(f"   Football Data: ✅")
    print(f"   Odds API: {'✅' if ODDS_API_KEY else '❌'}")
    print(f"   Claude AI: {'✅' if CLAUDE_API_KEY else '❌'}")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("matches", show_matches))
    application.add_handler(CommandHandler("leagues", show_leagues))
    application.add_handler(CommandHandler("recommend", recommend_command))
    application.add_handler(CallbackQueryHandler(league_callback, pattern="^league_"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_message))
    
    application.add_error_handler(error_handler)
    
    print("✅ Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
