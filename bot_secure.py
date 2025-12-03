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

# ===== CLAUDE UNIVERSAL PARSER =====

def parse_user_query_with_claude(user_message):
    """Use Claude to understand ANY user query and extract intent + team names"""
    
    if not claude_client:
        return {"intent": "unknown", "teams": [], "original": user_message}
    
    prompt = f"""Analyze this user message about football/soccer betting and extract information.

User message: "{user_message}"

Return a JSON object with:
- "intent": one of ["team_search", "recommend", "matches_list", "greeting", "help", "unknown"]
  - "team_search" = user asks about specific team or match
  - "recommend" = user wants betting recommendations/tips
  - "matches_list" = user wants to see list of matches
  - "greeting" = user says hello/hi
  - "help" = user asks for help
  - "unknown" = cannot determine
- "teams": array of team names mentioned (in ENGLISH, translate if needed). Examples: ["Arsenal"], ["Liverpool", "Chelsea"], ["Bayern Munich"]
- "league": if specific league mentioned, otherwise null. Use codes: "PL", "PD", "BL1", "SA", "FL1", "CL"
- "timeframe": "today", "tomorrow", "week", "weekend", or null

IMPORTANT: 
- Translate ALL team names to English (Ливерпуль -> Liverpool, Бавария -> Bayern Munich, etc.)
- Be flexible with spelling variations
- If user asks "who will win X vs Y" - intent is "team_search", teams are [X, Y]
- If user asks "what do you think about X" - intent is "team_search", teams are [X]
- If user asks for tips/advice/recommendations - intent is "recommend"

Return ONLY valid JSON, no other text."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response_text = message.content[0].text.strip()
        # Clean up response if needed
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
        response_text = response_text.strip()
        
        result = json.loads(response_text)
        result["original"] = user_message
        return result
        
    except Exception as e:
        logger.error(f"Claude parse error: {e}")
        return {"intent": "team_search", "teams": [user_message], "original": user_message}


# ===== API FUNCTIONS =====

def get_upcoming_matches(competition=None, days=7):
    """Get upcoming matches"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    
    params = {"dateFrom": date_from, "dateTo": date_to}
    
    try:
        if competition:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
        else:
            url = f"{FOOTBALL_API_URL}/matches"
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            return response.json().get("matches", [])
        return []
    except Exception as e:
        logger.error(f"Error fetching matches: {e}")
        return []


def get_all_matches_extended(days=14):
    """Get matches for extended period to find any team"""
    return get_upcoming_matches(competition=None, days=days)


def search_match_smart(teams, matches=None):
    """Smart search for match by team names"""
    
    if matches is None:
        matches = get_all_matches_extended(days=14)
    
    if not matches or not teams:
        return None, matches
    
    best_match = None
    best_score = 0
    
    for match in matches:
        home_team = match.get("homeTeam", {}).get("name", "").lower()
        away_team = match.get("awayTeam", {}).get("name", "").lower()
        home_short = match.get("homeTeam", {}).get("shortName", "").lower()
        away_short = match.get("awayTeam", {}).get("shortName", "").lower()
        
        score = 0
        for team in teams:
            team_lower = team.lower()
            team_words = team_lower.split()
            
            # Check full name match
            if team_lower in home_team or team_lower in away_team:
                score += 10
            # Check short name
            if team_lower in home_short or team_lower in away_short:
                score += 8
            # Check partial word match
            for word in team_words:
                if len(word) >= 3:
                    if word in home_team or word in home_short:
                        score += 3
                    if word in away_team or word in away_short:
                        score += 3
        
        if score > best_score:
            best_score = score
            best_match = match
    
    return best_match if best_score >= 3 else None, matches


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
        return None
    except:
        return None


def get_team_form(team_id, limit=5):
    """Get team's recent form"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/teams/{team_id}/matches",
            headers=headers, params={"status": "FINISHED", "limit": limit}, timeout=10
        )
        if response.status_code == 200:
            return response.json().get("matches", [])
        return []
    except:
        return []


def get_odds(home_team, away_team):
    """Get odds from multiple leagues"""
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
                    event_home = event.get("home_team", "").lower()
                    event_away = event.get("away_team", "").lower()
                    
                    if (any(w in event_home for w in home_team.lower().split()[:2]) or
                        any(w in event_away for w in away_team.lower().split()[:2])):
                        
                        result = {
                            "home_team": event.get("home_team"),
                            "away_team": event.get("away_team")
                        }
                        
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
            logger.error(f"Odds error for {sport}: {e}")
            continue
    
    return None


def format_form(matches, team_id):
    """Format team form as emoji string"""
    form = []
    for m in matches[:5]:
        home_id = m.get("homeTeam", {}).get("id")
        hs = m.get("score", {}).get("fullTime", {}).get("home")
        aws = m.get("score", {}).get("fullTime", {}).get("away")
        
        if hs is None or aws is None:
            continue
        
        if home_id == team_id:
            form.append("✅" if hs > aws else "❌" if hs < aws else "➖")
        else:
            form.append("✅" if aws > hs else "❌" if aws < hs else "➖")
    
    return "".join(form) if form else "N/A"


# ===== CLAUDE ANALYSIS =====

def analyze_match_full(match_data, odds=None, h2h=None, home_form=None, away_form=None, user_lang="ru"):
    """Full match analysis with all bet types and confidence levels"""
    
    if not claude_client:
        return "❌ AI analysis unavailable"
    
    home = match_data.get("homeTeam", {}).get("name", "Unknown")
    away = match_data.get("awayTeam", {}).get("name", "Unknown")
    comp = match_data.get("competition", {}).get("name", "Unknown")
    date = match_data.get("utcDate", "")
    
    try:
        dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
        date_fmt = dt.strftime("%d %B %Y, %H:%M UTC")
    except:
        date_fmt = date
    
    # Build odds info
    odds_text = "Odds: not available"
    if odds:
        home_odds = odds.get(home) or odds.get(odds.get("home_team", ""), "N/A")
        away_odds = odds.get(away) or odds.get(odds.get("away_team", ""), "N/A")
        draw_odds = odds.get("Draw", "N/A")
        
        odds_text = f"""Current odds (1X2):
• {home}: {home_odds}
• Draw: {draw_odds}  
• {away}: {away_odds}"""
        
        over = odds.get("total_Over_2.5")
        under = odds.get("total_Under_2.5")
        if over and under:
            odds_text += f"\n\nTotal 2.5: Over {over} | Under {under}"
        
        # Handicaps
        for key, val in odds.items():
            if key.startswith("spread_"):
                odds_text += f"\nHandicap {key.replace('spread_', '')}: {val}"
    
    # H2H info
    h2h_text = ""
    if h2h:
        agg = h2h.get("aggregates", {})
        total = agg.get("numberOfMatches", 0)
        if total > 0:
            hw = agg.get("homeTeam", {}).get("wins", 0)
            aw = agg.get("awayTeam", {}).get("wins", 0)
            d = agg.get("homeTeam", {}).get("draws", 0)
            h2h_text = f"\nH2H last {total}: {home} {hw}W - {d}D - {aw}W {away}"
    
    # Form info
    form_text = ""
    if home_form or away_form:
        form_text = f"\nForm (last 5): {home} {home_form or 'N/A'} | {away} {away_form or 'N/A'}"

    prompt = f"""You are an expert sports betting analyst with 15 years of experience.
Analyze this match and provide betting recommendations.

MATCH DATA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏆 {comp}
📅 {date_fmt}
🏠 Home: {home}
✈️ Away: {away}

{odds_text}
{h2h_text}
{form_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TASK: Provide comprehensive betting analysis with multiple bet types.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS:

📊 **PROBABILITIES:**
• {home}: X%
• Draw: X%
• {away}: X%

🎯 **BEST BET (Confidence: X%):**
[Your main recommendation - can be: Win, Double Chance, Handicap, Total, BTTS, etc.]
[1-2 sentences why]

📈 **ALL RECOMMENDATIONS:**

1️⃣ **[Bet Type]** - Confidence: X%
   Odds: X.XX | Expected Value: [positive/negative]
   [Why this bet]

2️⃣ **[Bet Type]** - Confidence: X%
   Odds: X.XX | Expected Value: [positive/negative]
   [Why this bet]

3️⃣ **[Bet Type]** - Confidence: X%
   Odds: X.XX | Expected Value: [positive/negative]
   [Why this bet]

⚠️ **RISKS:**
[Key risks for this match]

💡 **VERDICT:**
[Final recommendation: STRONG BET / MEDIUM RISK / SKIP if confidence < 65%]

RULES:
- Include different bet types: 1X2, Double Chance (1X, X2, 12), Handicap/Spread, Total Over/Under, BTTS
- Confidence must be realistic (50-90% range)
- If ALL bets have confidence < 65%, recommend to SKIP this match
- Mark bets with confidence >= 70% as "⭐ VALUE BET"
- Respond in the same language as the user's query (Russian if unclear)

User's language preference: {"Russian" if user_lang == "ru" else "English"}"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return f"❌ Analysis error: {e}"


def get_smart_recommendations(matches, user_lang="ru"):
    """Get AI recommendations for best matches"""
    
    if not claude_client or not matches:
        return None
    
    matches_text = ""
    for i, m in enumerate(matches[:8], 1):
        home = m.get("homeTeam", {}).get("name", "?")
        away = m.get("awayTeam", {}).get("name", "?")
        comp = m.get("competition", {}).get("name", "?")
        date = m.get("utcDate", "")[:10]
        matches_text += f"{i}. {home} vs {away} ({comp}) - {date}\n"
    
    prompt = f"""You are an expert betting analyst. Here are upcoming matches:

{matches_text}

Select the 3-4 BEST matches for betting and explain why.

For EACH recommended match provide:
- Match name
- Recommended bet (be specific: Win, Handicap -1.5, Over 2.5, BTTS Yes, etc.)
- Confidence level (only recommend if >= 65%)
- Brief reason (1-2 sentences)

FORMAT:

🔥 **TOP PICKS:**

1️⃣ **[Team] vs [Team]**
   ✅ Bet: [specific bet]
   📊 Confidence: X%
   💡 Why: [reason]

2️⃣ ...

⚠️ **Matches to AVOID:**
[List 1-2 matches that are too unpredictable]

Respond in {"Russian" if user_lang == "ru" else "English"}."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return None


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message with examples"""
    text = """🤖 **AI Betting Analyzer**

I analyze football matches using AI and real data to give you smart betting recommendations.

**💬 Just write naturally:**
• "Who will win Arsenal vs Chelsea?"
• "What do you think about Liverpool?"
• "Bayern Munich analysis"
• "Give me betting tips"
• "Best bets for today"

**📋 Commands:**
/recommend — AI picks best bets
/matches — upcoming matches
/leagues — browse by league
/help — how to use

**🎯 I analyze:**
• Win / Draw / Lose (1X2)
• Double Chance
• Handicaps / Spreads
• Total Goals (Over/Under)
• Both Teams to Score

**🌍 Works in English & Russian!**

⚠️ _Betting involves risk. Gamble responsibly._
"""
    await update.message.reply_text(text, parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📚 **How to Use**

**Ask me anything about football matches:**

✅ "Who wins Liverpool vs Man City?"
✅ "Barcelona match analysis"  
✅ "What's good to bet on today?"
✅ "Бавария прогноз" (Russian works too!)
✅ "Arsenal" (just team name)

**I'll give you:**
• Win probabilities
• Best bet with confidence %
• Multiple bet options (1X2, handicap, totals, BTTS)
• Risks analysis
• Clear verdict: BET or SKIP

**Commands:**
/recommend — My top picks
/matches — All upcoming matches
/leagues — Filter by league

**Tips:**
• I search 14 days ahead to find matches
• Confidence >= 65% = worth betting
• Look for ⭐ VALUE BET markers
"""
    await update.message.reply_text(text, parse_mode='Markdown')


async def recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart recommendations"""
    await update.message.reply_text("🔍 Analyzing best betting opportunities...")
    
    matches = get_upcoming_matches(days=7)
    
    if not matches:
        await update.message.reply_text("❌ Couldn't fetch matches. Try again later.")
        return
    
    # Detect language from user
    user_lang = "ru"  # Default, could detect from context
    
    recs = get_smart_recommendations(matches, user_lang)
    
    if recs:
        await update.message.reply_text(recs, parse_mode='Markdown')
    else:
        text = "⚽ **Upcoming matches:**\n\n"
        for m in matches[:5]:
            text += f"• {m.get('homeTeam', {}).get('name')} vs {m.get('awayTeam', {}).get('name')}\n"
        text += "\n_Write team name for detailed analysis_"
        await update.message.reply_text(text, parse_mode='Markdown')


async def show_matches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming matches"""
    await update.message.reply_text("🔍 Loading matches...")
    
    matches = get_upcoming_matches(days=7)
    
    if not matches:
        await update.message.reply_text("❌ No matches found.")
        return
    
    by_comp = {}
    for m in matches:
        comp = m.get("competition", {}).get("name", "Other")
        if comp not in by_comp:
            by_comp[comp] = []
        by_comp[comp].append(m)
    
    text = "⚽ **Upcoming Matches (7 days):**\n\n"
    
    for comp, comp_matches in list(by_comp.items())[:6]:
        text += f"🏆 **{comp}**\n"
        for m in comp_matches[:3]:
            home = m.get("homeTeam", {}).get("name", "?")
            away = m.get("awayTeam", {}).get("name", "?")
            try:
                dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
                ds = dt.strftime("%d.%m")
            except:
                ds = ""
            text += f"  • {home} vs {away} ({ds})\n"
        text += "\n"
    
    text += "_Write team name for analysis_"
    await update.message.reply_text(text, parse_mode='Markdown')


async def show_leagues(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """League selection"""
    keyboard = [
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="league_BL1")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="league_FL1")],
        [InlineKeyboardButton("🇪🇺 Champions League", callback_data="league_CL")],
    ]
    await update.message.reply_text("⚽ Select league:", reply_markup=InlineKeyboardMarkup(keyboard))


async def league_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle league selection"""
    query = update.callback_query
    await query.answer()
    
    code = query.data.replace("league_", "")
    name = COMPETITIONS.get(code, code)
    
    await query.edit_message_text(f"🔍 Loading {name} matches...")
    
    matches = get_upcoming_matches(code, days=14)
    
    if not matches:
        await query.edit_message_text(f"❌ No {name} matches in next 14 days")
        return
    
    text = f"⚽ **{name}** — upcoming:\n\n"
    
    for m in matches[:10]:
        home = m.get("homeTeam", {}).get("name", "?")
        away = m.get("awayTeam", {}).get("name", "?")
        try:
            dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
            ds = dt.strftime("%d.%m %H:%M")
        except:
            ds = ""
        text += f"📅 {ds}\n   {home} vs {away}\n\n"
    
    text += "_Write team name for analysis_"
    await query.edit_message_text(text, parse_mode='Markdown')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal message handler with Claude parsing"""
    user_text = update.message.text.strip()
    
    if len(user_text) < 2:
        return
    
    # Detect language (simple check)
    user_lang = "ru" if any(ord(c) > 1000 for c in user_text) else "en"
    
    status = await update.message.reply_text("🔍 Understanding your request...")
    
    # Parse with Claude
    parsed = parse_user_query_with_claude(user_text)
    intent = parsed.get("intent", "unknown")
    teams = parsed.get("teams", [])
    
    logger.info(f"Parsed: intent={intent}, teams={teams}")
    
    # Handle different intents
    if intent == "greeting":
        await status.edit_text(
            "👋 Hello! I'm your AI betting analyst.\n\n"
            "Ask me about any football match or write /recommend for today's best bets!"
        )
        return
    
    if intent == "help":
        await status.delete()
        await help_command(update, context)
        return
    
    if intent == "recommend":
        await status.delete()
        await recommend(update, context)
        return
    
    if intent == "matches_list":
        await status.delete()
        await show_matches(update, context)
        return
    
    # For team_search or unknown - try to find match
    await status.edit_text("🔍 Searching for match...")
    
    # Get all matches (14 days)
    all_matches = get_all_matches_extended(days=14)
    
    # Search for match
    match = None
    if teams:
        match, _ = search_match_smart(teams, all_matches)
    
    if not match and user_text:
        # Try with original text as fallback
        match, _ = search_match_smart([user_text], all_matches)
    
    if not match:
        # No match found - offer alternatives
        no_match_text = f"🤔 Couldn't find a match for: {', '.join(teams) if teams else user_text}\n\n"
        
        if all_matches:
            no_match_text += "**📋 But here are some interesting matches:**\n\n"
            
            for m in all_matches[:5]:
                home = m.get("homeTeam", {}).get("name", "?")
                away = m.get("awayTeam", {}).get("name", "?")
                comp = m.get("competition", {}).get("name", "")
                no_match_text += f"• {home} vs {away}\n  🏆 {comp}\n\n"
            
            no_match_text += "💡 **Try:**\n"
            no_match_text += "• /recommend — get my best picks\n"
            no_match_text += "• /leagues — browse by league\n"
            no_match_text += "• Write exact team name in English"
        
        await status.edit_text(no_match_text, parse_mode='Markdown')
        return
    
    # Found match - analyze it
    home = match.get("homeTeam", {}).get("name", "Unknown")
    away = match.get("awayTeam", {}).get("name", "Unknown")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "")
    
    await status.edit_text(
        f"✅ Found: **{home}** vs **{away}**\n"
        f"🏆 {comp}\n\n"
        "⏳ Gathering data...",
        parse_mode='Markdown'
    )
    
    # Get additional data
    odds = get_odds(home, away)
    h2h = get_head_to_head(match_id) if match_id else None
    
    home_matches = get_team_form(home_id) if home_id else []
    away_matches = get_team_form(away_id) if away_id else []
    home_form = format_form(home_matches, home_id) if home_matches else None
    away_form = format_form(away_matches, away_id) if away_matches else None
    
    await status.edit_text(
        f"✅ **{home}** vs **{away}**\n"
        f"🏆 {comp}\n\n"
        "🤖 AI analyzing match...",
        parse_mode='Markdown'
    )
    
    # Full analysis
    analysis = analyze_match_full(match, odds, h2h, home_form, away_form, user_lang)
    
    header = f"⚽ **{home}** vs **{away}**\n"
    header += f"🏆 {comp}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    await status.edit_text(header + analysis, parse_mode='Markdown')


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Error: {context.error}")
    if update and update.message:
        await update.message.reply_text("❌ Something went wrong. Try /start")


# ===== MAIN =====

def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    if not FOOTBALL_API_KEY:
        print("❌ FOOTBALL_API_KEY not set!")
        return
    
    print("🚀 Starting AI Betting Analyzer Bot v2...")
    print(f"   Telegram: ✅")
    print(f"   Football Data: ✅")
    print(f"   Odds API: {'✅' if ODDS_API_KEY else '⚠️'}")
    print(f"   Claude AI: {'✅' if CLAUDE_API_KEY else '⚠️'}")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("matches", show_matches))
    app.add_handler(CommandHandler("leagues", show_leagues))
    app.add_handler(CommandHandler("recommend", recommend))
    app.add_handler(CallbackQueryHandler(league_callback, pattern="^league_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    print("✅ Bot v2 is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
