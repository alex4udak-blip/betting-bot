import os
import logging
import requests
import json
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
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

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "EL": "Europa League",
}

# Status messages in different languages
STATUS_MESSAGES = {
    "ru": {
        "understanding": "Анализирую запрос...",
        "searching": "Ищу матч...",
        "gathering": "Собираю данные...",
        "analyzing": "AI анализирует матч...",
        "not_found": "Не нашел матч для: {}",
        "found": "Нашел: {} vs {}",
        "league": "Лига: {}",
        "recommendations": "Анализирую лучшие ставки...",
        "try_options": "Попробуй:",
        "hello": "Привет! Я AI-аналитик ставок.\n\nСпроси меня о любом футбольном матче или напиши /recommend для лучших ставок!",
        "interesting_matches": "Но вот интересные матчи:",
    },
    "en": {
        "understanding": "Understanding your request...",
        "searching": "Searching for match...",
        "gathering": "Gathering data...",
        "analyzing": "AI analyzing match...",
        "not_found": "Couldn't find match for: {}",
        "found": "Found: {} vs {}",
        "league": "League: {}",
        "recommendations": "Analyzing best bets...",
        "try_options": "Try:",
        "hello": "Hello! I'm your AI betting analyst.\n\nAsk me about any football match or write /recommend for best bets!",
        "interesting_matches": "But here are interesting matches:",
    }
}

def get_msg(key, lang="en", *args):
    """Get localized message"""
    msg = STATUS_MESSAGES.get(lang, STATUS_MESSAGES["en"]).get(key, key)
    if args:
        return msg.format(*args)
    return msg


def detect_language(text):
    """Detect if text is Russian or English"""
    russian_chars = sum(1 for c in text if '\u0400' <= c <= '\u04FF')
    return "ru" if russian_chars > len(text) * 0.3 else "en"


# ===== CLAUDE UNIVERSAL PARSER =====

def parse_user_query_with_claude(user_message, user_lang="en"):
    """Use Claude to understand ANY user query"""
    
    if not claude_client:
        return {"intent": "unknown", "teams": [], "original": user_message}
    
    prompt = f"""Analyze this user message about football/soccer betting.

User message: "{user_message}"

Return JSON:
{{
  "intent": "team_search" | "recommend" | "matches_list" | "greeting" | "help" | "unknown",
  "teams": ["team names in English"],
  "league": "PL" | "PD" | "BL1" | "SA" | "FL1" | "CL" | null
}}

Rules:
- "team_search" = asks about specific team/match
- "recommend" = wants betting tips/recommendations  
- "matches_list" = wants to see matches list
- Translate team names to English (Ливерпуль=Liverpool, Бавария=Bayern Munich, Арсенал=Arsenal, Реал Мадрид=Real Madrid, Барселона=Barcelona, Челси=Chelsea, Ювентус=Juventus, ПСЖ=PSG, Милан=AC Milan, Интер=Inter Milan)
- Extract team names even from questions like "who wins Arsenal?"

Return ONLY JSON."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        
        result = json.loads(text)
        result["original"] = user_message
        result["lang"] = user_lang
        return result
        
    except Exception as e:
        logger.error(f"Claude parse error: {e}")
        # Fallback - treat as team search
        return {"intent": "team_search", "teams": [user_message], "original": user_message, "lang": user_lang}


# ===== API FUNCTIONS =====

def get_upcoming_matches(competition=None, days=7):
    """Get upcoming matches - queries each league separately for free tier"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    
    params = {"dateFrom": date_from, "dateTo": date_to}
    
    # If specific competition requested
    if competition:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                matches = response.json().get("matches", [])
                logger.info(f"Got {len(matches)} matches from {competition}")
                return matches
            else:
                logger.error(f"Football API error for {competition}: {response.status_code}")
                return []
        except Exception as e:
            logger.error(f"Error fetching {competition}: {e}")
            return []
    
    # Free tier: query each league separately
    all_matches = []
    leagues = ["PL", "PD", "BL1", "SA", "FL1", "CL", "EL"]
    
    for league in leagues:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{league}/matches"
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                matches = response.json().get("matches", [])
                all_matches.extend(matches)
                logger.info(f"Got {len(matches)} matches from {league}")
            else:
                logger.warning(f"Could not get {league}: {response.status_code}")
        except Exception as e:
            logger.error(f"Error fetching {league}: {e}")
        
    logger.info(f"Total matches loaded: {len(all_matches)}")
    return all_matches


def search_match_flexible(search_teams, matches):
    """Flexible search - finds match even with partial team name"""
    
    if not matches or not search_teams:
        logger.info(f"No matches or teams to search: matches={len(matches) if matches else 0}, teams={search_teams}")
        return None
    
    best_match = None
    best_score = 0
    
    # Normalize search terms
    search_terms = []
    for team in search_teams:
        # Add original and lowercase
        search_terms.append(team.lower())
        # Add individual words
        for word in team.lower().split():
            if len(word) >= 3:
                search_terms.append(word)
    
    logger.info(f"Search terms: {search_terms}")
    
    for match in matches:
        home_name = match.get("homeTeam", {}).get("name", "").lower()
        away_name = match.get("awayTeam", {}).get("name", "").lower()
        home_short = match.get("homeTeam", {}).get("shortName", "").lower()
        away_short = match.get("awayTeam", {}).get("shortName", "").lower()
        home_tla = match.get("homeTeam", {}).get("tla", "").lower()
        away_tla = match.get("awayTeam", {}).get("tla", "").lower()
        
        # All possible names to match against
        home_variants = [home_name, home_short, home_tla] + home_name.split()
        away_variants = [away_name, away_short, away_tla] + away_name.split()
        
        score = 0
        
        for term in search_terms:
            # Check home team
            for variant in home_variants:
                if term in variant or variant in term:
                    score += 5
                    break
            
            # Check away team  
            for variant in away_variants:
                if term in variant or variant in term:
                    score += 5
                    break
        
        if score > best_score:
            best_score = score
            best_match = match
            logger.info(f"New best match: {home_name} vs {away_name}, score={score}")
    
    if best_score >= 5:
        return best_match
    
    logger.info(f"No match found with score >= 5 (best was {best_score})")
    return None


def get_head_to_head(match_id):
    """Get H2H stats"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/matches/{match_id}/head2head",
            headers=headers, params={"limit": 10}, timeout=10
        )
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        logger.error(f"H2H error: {e}")
    return None


def get_team_form(team_id, limit=5):
    """Get team's recent matches"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/teams/{team_id}/matches",
            headers=headers, params={"status": "FINISHED", "limit": limit}, timeout=10
        )
        if response.status_code == 200:
            return response.json().get("matches", [])
    except Exception as e:
        logger.error(f"Team form error: {e}")
    return []


def get_odds(home_team, away_team):
    """Get odds"""
    sports = [
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_uefa_champs_league"
    ]
    
    for sport in sports:
        try:
            response = requests.get(
                f"{ODDS_API_URL}/sports/{sport}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "eu",
                    "markets": "h2h,totals,spreads",
                    "oddsFormat": "decimal"
                },
                timeout=10
            )
            
            if response.status_code == 200:
                for event in response.json():
                    eh = event.get("home_team", "").lower()
                    ea = event.get("away_team", "").lower()
                    
                    # Flexible matching
                    home_words = [w for w in home_team.lower().split() if len(w) >= 3]
                    away_words = [w for w in away_team.lower().split() if len(w) >= 3]
                    
                    home_match = any(w in eh for w in home_words)
                    away_match = any(w in ea for w in away_words)
                    
                    if home_match or away_match:
                        result = {"home_team": event.get("home_team"), "away_team": event.get("away_team")}
                        
                        for bm in event.get("bookmakers", [])[:1]:
                            for market in bm.get("markets", []):
                                if market["key"] == "h2h":
                                    for o in market["outcomes"]:
                                        result[o["name"]] = o["price"]
                                elif market["key"] == "totals":
                                    for o in market["outcomes"]:
                                        result[f"total_{o['name']}_{o.get('point', 2.5)}"] = o["price"]
                                elif market["key"] == "spreads":
                                    for o in market["outcomes"]:
                                        result[f"spread_{o['name']}_{o.get('point', 0)}"] = o["price"]
                        return result
        except Exception as e:
            logger.error(f"Odds error: {e}")
    return None


def format_form(matches, team_id):
    """Format team form"""
    form = []
    for m in matches[:5]:
        home_id = m.get("homeTeam", {}).get("id")
        hs = m.get("score", {}).get("fullTime", {}).get("home")
        aws = m.get("score", {}).get("fullTime", {}).get("away")
        if hs is None or aws is None:
            continue
        if home_id == team_id:
            form.append("W" if hs > aws else "L" if hs < aws else "D")
        else:
            form.append("W" if aws > hs else "L" if aws < hs else "D")
    return "-".join(form) if form else "N/A"


# ===== CLAUDE ANALYSIS =====

def analyze_match_full(match_data, odds=None, h2h=None, home_form=None, away_form=None, lang="ru"):
    """Full match analysis"""
    
    if not claude_client:
        return "AI analysis unavailable"
    
    home = match_data.get("homeTeam", {}).get("name", "Unknown")
    away = match_data.get("awayTeam", {}).get("name", "Unknown")
    comp = match_data.get("competition", {}).get("name", "Unknown")
    date = match_data.get("utcDate", "")
    
    try:
        dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
        date_fmt = dt.strftime("%d.%m.%Y %H:%M")
    except:
        date_fmt = date
    
    odds_text = "Odds: N/A"
    if odds:
        ho = odds.get(home) or odds.get(odds.get("home_team", ""), "?")
        ao = odds.get(away) or odds.get(odds.get("away_team", ""), "?")
        do = odds.get("Draw", "?")
        odds_text = f"1X2: {home}={ho}, Draw={do}, {away}={ao}"
        
        ov = odds.get("total_Over_2.5")
        un = odds.get("total_Under_2.5")
        if ov and un:
            odds_text += f" | Total 2.5: O{ov}/U{un}"
    
    h2h_text = ""
    if h2h:
        agg = h2h.get("aggregates", {})
        n = agg.get("numberOfMatches", 0)
        if n > 0:
            hw = agg.get("homeTeam", {}).get("wins", 0)
            aw = agg.get("awayTeam", {}).get("wins", 0)
            d = agg.get("homeTeam", {}).get("draws", 0)
            h2h_text = f"H2H({n}): {hw}-{d}-{aw}"
    
    form_text = ""
    if home_form or away_form:
        form_text = f"Form: {home}={home_form or '?'}, {away}={away_form or '?'}"

    lang_instruction = "Respond in Russian." if lang == "ru" else "Respond in English."
    
    prompt = f"""Expert betting analyst. Analyze this match:

{comp} | {date_fmt}
{home} vs {away}
{odds_text}
{h2h_text}
{form_text}

Give structured analysis:

PROBABILITIES:
- {home}: X%
- Draw: X%  
- {away}: X%

BEST BET (Confidence X%):
[Main pick with reason]

OTHER OPTIONS:
1. [Bet] - X% confidence - [reason]
2. [Bet] - X% confidence - [reason]

RISKS:
[Key risks]

VERDICT: STRONG BET / MEDIUM / SKIP

Include: 1X2, Double Chance, Handicaps, Totals, BTTS.
Only recommend if confidence >= 65%.
Mark 70%+ as VALUE BET.

{lang_instruction}"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return f"Analysis error: {e}"


def get_recommendations(matches, lang="ru"):
    """Get AI recommendations"""
    
    if not claude_client or not matches:
        return None
    
    matches_text = ""
    for i, m in enumerate(matches[:8], 1):
        h = m.get("homeTeam", {}).get("name", "?")
        a = m.get("awayTeam", {}).get("name", "?")
        c = m.get("competition", {}).get("name", "?")
        matches_text += f"{i}. {h} vs {a} ({c})\n"
    
    lang_instruction = "Respond in Russian." if lang == "ru" else "Respond in English."
    
    prompt = f"""Expert betting analyst. Upcoming matches:

{matches_text}

Pick 3-4 BEST bets (confidence >= 65%):

TOP PICKS:

1. [Team] vs [Team]
   Bet: [specific bet]
   Confidence: X%
   Why: [reason]

2. ...

AVOID:
[1-2 risky matches]

{lang_instruction}"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return None


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """AI Betting Analyzer

I analyze football matches with AI.

How to use:
- "Who wins Arsenal vs Chelsea?"
- "Liverpool prediction"  
- "Best bets today"
- Just write team name

Commands:
/recommend - Top picks
/matches - Upcoming matches
/leagues - By league
/help - Help

Works in English & Russian!
"""
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """How to Use

Ask about any match:
- "Arsenal analysis"
- "Who wins Bayern?"
- "Liverpool vs City prediction"

Or get recommendations:
- /recommend
- "Best bets"
- "What to bet on?"

I analyze:
- Win/Draw/Lose
- Handicaps  
- Totals
- BTTS
- Double Chance

Tips:
- I search 14 days ahead
- 65%+ = worth betting
- 70%+ = VALUE BET
"""
    await update.message.reply_text(text)


async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = detect_language(update.message.text or "")
    
    await update.message.reply_text(get_msg("recommendations", lang))
    
    matches = get_upcoming_matches(days=7)
    
    if not matches:
        await update.message.reply_text("No matches found. Try later.")
        return
    
    recs = get_recommendations(matches, lang)
    
    if recs:
        await update.message.reply_text(recs)
    else:
        text = "Matches:\n"
        for m in matches[:5]:
            text += f"- {m.get('homeTeam', {}).get('name')} vs {m.get('awayTeam', {}).get('name')}\n"
        await update.message.reply_text(text)


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = get_upcoming_matches(days=7)
    
    if not matches:
        await update.message.reply_text("No matches found.")
        return
    
    by_comp = {}
    for m in matches:
        c = m.get("competition", {}).get("name", "Other")
        if c not in by_comp:
            by_comp[c] = []
        by_comp[c].append(m)
    
    text = "Upcoming matches:\n\n"
    for comp, ms in list(by_comp.items())[:5]:
        text += f"[{comp}]\n"
        for m in ms[:3]:
            h = m.get("homeTeam", {}).get("name", "?")
            a = m.get("awayTeam", {}).get("name", "?")
            text += f"  {h} vs {a}\n"
        text += "\n"
    
    await update.message.reply_text(text)


async def leagues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Premier League", callback_data="league_PL")],
        [InlineKeyboardButton("La Liga", callback_data="league_PD")],
        [InlineKeyboardButton("Bundesliga", callback_data="league_BL1")],
        [InlineKeyboardButton("Serie A", callback_data="league_SA")],
        [InlineKeyboardButton("Ligue 1", callback_data="league_FL1")],
        [InlineKeyboardButton("Champions League", callback_data="league_CL")],
    ]
    await update.message.reply_text("Select league:", reply_markup=InlineKeyboardMarkup(keyboard))


async def league_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    code = query.data.replace("league_", "")
    name = COMPETITIONS.get(code, code)
    
    await query.edit_message_text(f"Loading {name}...")
    
    matches = get_upcoming_matches(code, days=14)
    
    if not matches:
        await query.edit_message_text(f"No {name} matches found")
        return
    
    text = f"{name}:\n\n"
    for m in matches[:10]:
        h = m.get("homeTeam", {}).get("name", "?")
        a = m.get("awayTeam", {}).get("name", "?")
        try:
            dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
            ds = dt.strftime("%d.%m")
        except:
            ds = ""
        text += f"{ds} {h} vs {a}\n"
    
    await query.edit_message_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    user_text = update.message.text.strip()
    
    if len(user_text) < 2:
        return
    
    lang = detect_language(user_text)
    
    status = await update.message.reply_text(get_msg("understanding", lang))
    
    # Parse with Claude
    parsed = parse_user_query_with_claude(user_text, lang)
    intent = parsed.get("intent", "unknown")
    teams = parsed.get("teams", [])
    
    logger.info(f"Parsed: intent={intent}, teams={teams}, lang={lang}")
    
    # Handle intents
    if intent == "greeting":
        await status.edit_text(get_msg("hello", lang))
        return
    
    if intent == "help":
        await status.delete()
        await help_cmd(update, context)
        return
    
    if intent == "recommend":
        await status.delete()
        await recommend_cmd(update, context)
        return
    
    if intent == "matches_list":
        await status.delete()
        await matches_cmd(update, context)
        return
    
    # Team search
    await status.edit_text(get_msg("searching", lang))
    
    # Get matches (14 days)
    all_matches = get_upcoming_matches(days=14)
    logger.info(f"Total matches loaded: {len(all_matches)}")
    
    # Log some team names for debugging
    if all_matches:
        sample = all_matches[:3]
        for m in sample:
            logger.info(f"Sample match: {m.get('homeTeam', {}).get('name')} vs {m.get('awayTeam', {}).get('name')}")
    
    # Search
    match = None
    if teams:
        match = search_match_flexible(teams, all_matches)
    
    if not match and user_text:
        # Try with original text
        match = search_match_flexible([user_text], all_matches)
    
    if not match:
        # Not found - show alternatives
        search_term = ', '.join(teams) if teams else user_text
        text = get_msg("not_found", lang, search_term) + "\n\n"
        
        if all_matches:
            text += get_msg("interesting_matches", lang) + "\n\n"
            for m in all_matches[:5]:
                h = m.get("homeTeam", {}).get("name", "?")
                a = m.get("awayTeam", {}).get("name", "?")
                text += f"- {h} vs {a}\n"
            
            text += f"\n{get_msg('try_options', lang)}\n"
            text += "- /recommend\n- /matches\n- /leagues"
        
        await status.edit_text(text)
        return
    
    # Found match!
    home = match.get("homeTeam", {}).get("name", "Unknown")
    away = match.get("awayTeam", {}).get("name", "Unknown")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "")
    
    await status.edit_text(
        f"{get_msg('found', lang, home, away)}\n"
        f"{get_msg('league', lang, comp)}\n\n"
        f"{get_msg('gathering', lang)}"
    )
    
    # Get additional data
    odds = get_odds(home, away)
    h2h = get_head_to_head(match_id) if match_id else None
    
    home_matches = get_team_form(home_id) if home_id else []
    away_matches = get_team_form(away_id) if away_id else []
    hf = format_form(home_matches, home_id) if home_matches else None
    af = format_form(away_matches, away_id) if away_matches else None
    
    await status.edit_text(
        f"{get_msg('found', lang, home, away)}\n"
        f"{get_msg('league', lang, comp)}\n\n"
        f"{get_msg('analyzing', lang)}"
    )
    
    # Full analysis
    analysis = analyze_match_full(match, odds, h2h, hf, af, lang)
    
    header = f"{home} vs {away}\n{comp}\n{'='*30}\n\n"
    
    await status.edit_text(header + analysis)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        await update.message.reply_text("Error. Try /start")


# ===== MAIN =====

def main():
    if not TELEGRAM_TOKEN:
        print("TELEGRAM_TOKEN missing!")
        return
    if not FOOTBALL_API_KEY:
        print("FOOTBALL_API_KEY missing!")
        return
    
    print("Starting AI Betting Bot v3...")
    print(f"  Telegram: OK")
    print(f"  Football: OK")
    print(f"  Odds: {'OK' if ODDS_API_KEY else 'MISSING'}")
    print(f"  Claude: {'OK' if CLAUDE_API_KEY else 'MISSING'}")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("leagues", leagues_cmd))
    app.add_handler(CommandHandler("recommend", recommend_cmd))
    app.add_handler(CallbackQueryHandler(league_cb, pattern="^league_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("Bot v3 running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
