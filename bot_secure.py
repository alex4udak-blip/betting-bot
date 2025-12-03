import os
import logging
import requests
import json
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

COMPETITIONS = {
    "PL": "Premier League",
    "PD": "La Liga", 
    "BL1": "Bundesliga",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "CL": "Champions League",
}


def detect_language(text):
    """Detect if text is Russian or English"""
    russian_chars = sum(1 for c in text if '\u0400' <= c <= '\u04FF')
    return "ru" if russian_chars > len(text) * 0.2 else "en"


def get_bank_percentage(confidence):
    """Get recommended bank percentage based on confidence"""
    if confidence >= 80:
        return "5%"
    elif confidence >= 75:
        return "3-4%"
    elif confidence >= 70:
        return "2-3%"
    elif confidence >= 65:
        return "1-2%"
    else:
        return "skip"


# ===== CLAUDE PARSER =====

def parse_user_query(user_message, lang="en"):
    """Parse user query with Claude"""
    
    if not claude_client:
        return {"intent": "team_search", "teams": [user_message], "lang": lang}
    
    prompt = f"""Analyze this football betting message and return JSON.

Message: "{user_message}"

Return ONLY this JSON format:
{{"intent": "X", "teams": ["Y"], "league": null}}

INTENT RULES:
- "team_search" = asks about specific team OR "who wins X" OR "X prediction" OR "analyze X" OR mentions any team name
- "recommend" = ONLY if asks for general tips WITHOUT mentioning specific team (like "best bets", "what to bet", "give tips")
- "matches_list" = wants to see all matches
- "greeting" = just hello/hi
- "help" = asks how to use

IMPORTANT: 
- "Who wins Bayern?" = team_search with teams=["Bayern Munich"]
- "Bayern prediction" = team_search with teams=["Bayern Munich"]  
- "What about Arsenal?" = team_search with teams=["Arsenal"]
- Translate: Бавария=Bayern Munich, Арсенал=Arsenal, Ливерпуль=Liverpool, Реал=Real Madrid, Барселона=Barcelona, Челси=Chelsea, ПСЖ=PSG

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
        
        result = json.loads(text)
        result["lang"] = lang
        return result
        
    except Exception as e:
        logger.error(f"Parse error: {e}")
        return {"intent": "team_search", "teams": [user_message], "lang": lang}


# ===== API FUNCTIONS =====

def get_matches(competition=None, days=7):
    """Get matches from all leagues"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    date_from = datetime.now().strftime("%Y-%m-%d")
    date_to = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    params = {"dateFrom": date_from, "dateTo": date_to}
    
    if competition:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{competition}/matches"
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                return response.json().get("matches", [])
        except Exception as e:
            logger.error(f"API error: {e}")
        return []
    
    # Get from all leagues
    all_matches = []
    for league in ["PL", "PD", "BL1", "SA", "FL1", "CL"]:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{league}/matches"
            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                matches = response.json().get("matches", [])
                all_matches.extend(matches)
                logger.info(f"Got {len(matches)} from {league}")
        except:
            pass
    
    logger.info(f"Total: {len(all_matches)} matches")
    return all_matches


def find_match(teams, matches):
    """Find match by team names"""
    if not matches or not teams:
        return None
    
    search_terms = []
    for team in teams:
        search_terms.append(team.lower())
        for word in team.lower().split():
            if len(word) >= 3:
                search_terms.append(word)
    
    best_match = None
    best_score = 0
    
    for match in matches:
        home = match.get("homeTeam", {}).get("name", "").lower()
        away = match.get("awayTeam", {}).get("name", "").lower()
        home_short = match.get("homeTeam", {}).get("shortName", "").lower()
        away_short = match.get("awayTeam", {}).get("shortName", "").lower()
        
        score = 0
        for term in search_terms:
            if term in home or term in home_short:
                score += 5
            if term in away or term in away_short:
                score += 5
        
        if score > best_score:
            best_score = score
            best_match = match
    
    return best_match if best_score >= 5 else None


def get_h2h(match_id):
    """Get head to head"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/matches/{match_id}/head2head",
            headers=headers, params={"limit": 10}, timeout=10
        )
        if response.status_code == 200:
            return response.json()
    except:
        pass
    return None


def get_form(team_id):
    """Get team form"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    try:
        response = requests.get(
            f"{FOOTBALL_API_URL}/teams/{team_id}/matches",
            headers=headers, params={"status": "FINISHED", "limit": 5}, timeout=10
        )
        if response.status_code == 200:
            matches = response.json().get("matches", [])
            form = []
            for m in matches[:5]:
                home_id = m.get("homeTeam", {}).get("id")
                hs = m.get("score", {}).get("fullTime", {}).get("home")
                aws = m.get("score", {}).get("fullTime", {}).get("away")
                if hs is None:
                    continue
                if home_id == team_id:
                    form.append("W" if hs > aws else "L" if hs < aws else "D")
                else:
                    form.append("W" if aws > hs else "L" if aws < hs else "D")
            return "-".join(form) if form else "N/A"
    except:
        pass
    return "N/A"


def get_odds(home, away):
    """Get odds for match"""
    sports = ["soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
              "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_uefa_champs_league"]
    
    for sport in sports:
        try:
            response = requests.get(
                f"{ODDS_API_URL}/sports/{sport}/odds",
                params={"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal"},
                timeout=10
            )
            if response.status_code == 200:
                for event in response.json():
                    eh = event.get("home_team", "").lower()
                    ea = event.get("away_team", "").lower()
                    
                    home_words = [w for w in home.lower().split() if len(w) >= 3]
                    if any(w in eh or w in ea for w in home_words):
                        result = {}
                        for bm in event.get("bookmakers", [])[:1]:
                            for market in bm.get("markets", []):
                                if market["key"] == "h2h":
                                    for o in market["outcomes"]:
                                        result[o["name"]] = o["price"]
                                elif market["key"] == "totals":
                                    for o in market["outcomes"]:
                                        result[f"{o['name']}_{o.get('point', 2.5)}"] = o["price"]
                        if result:
                            return result
        except:
            pass
    return None


# ===== CLAUDE ANALYSIS =====

def analyze_match(match, odds=None, h2h=None, home_form=None, away_form=None, lang="ru"):
    """Full match analysis with emojis"""
    
    if not claude_client:
        return "AI unavailable"
    
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    comp = match.get("competition", {}).get("name", "?")
    
    odds_text = "No odds"
    if odds:
        parts = []
        for k, v in odds.items():
            if not k.startswith("Over") and not k.startswith("Under"):
                parts.append(f"{k}: {v}")
        if parts:
            odds_text = ", ".join(parts)
        
        over = odds.get("Over_2.5")
        under = odds.get("Under_2.5")
        if over:
            odds_text += f" | Over 2.5: {over}"
        if under:
            odds_text += f", Under 2.5: {under}"
    
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

    lang_instr = "RESPOND IN RUSSIAN." if lang == "ru" else "RESPOND IN ENGLISH."
    
    prompt = f"""Expert betting analyst. Analyze:

{comp}: {home} vs {away}
Odds: {odds_text}
{h2h_text}
{form_text}

{lang_instr}

USE THIS EXACT FORMAT WITH EMOJIS:

📊 ВЕРОЯТНОСТИ:
• {home}: X%
• Ничья: X%
• {away}: X%

🎯 ЛУЧШАЯ СТАВКА (Уверенность: X%):
[Bet type] @ [coefficient if known]
💰 Рекомендация: X% от банка
[1-2 sentences why]

📈 ДРУГИЕ ВАРИАНТЫ:
1. [Bet] - X% уверенность - коэфф X.XX
2. [Bet] - X% уверенность - коэфф X.XX
3. [Bet] - X% уверенность - коэфф X.XX

⚠️ РИСКИ:
[Key risks]

✅ ВЕРДИКТ: [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ПРОПУСТИТЬ]

RULES:
- Include coefficients from odds data where available
- Bank % based on confidence: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%
- If confidence <65% for all bets, verdict = ПРОПУСТИТЬ
- Mark 70%+ bets as "⭐ VALUE"
- Use emojis as shown above"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        return f"Error: {e}"


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
    
    lang_instr = "RESPOND IN RUSSIAN." if lang == "ru" else "RESPOND IN ENGLISH."
    
    prompt = f"""Expert analyst. Matches:

{matches_text}

{lang_instr}

USE THIS FORMAT:

🔥 ТОП СТАВКИ:

1️⃣ [Team] vs [Team]
   ✅ Ставка: [specific bet] @ коэфф X.XX
   📊 Уверенность: X%
   💰 Банк: X%
   💡 Почему: [reason]

2️⃣ ...

3️⃣ ...

❌ ИЗБЕГАТЬ:
• [Match] - [why risky]

Only include bets with 65%+ confidence.
Bank %: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except:
        return None


# ===== LIVE ALERTS =====

async def check_live_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check for high-confidence matches and alert subscribers"""
    
    if not live_subscribers:
        return
    
    logger.info(f"Checking live matches for {len(live_subscribers)} subscribers...")
    
    matches = get_matches(days=2)
    
    if not matches:
        return
    
    # Get matches starting in next 3 hours
    now = datetime.utcnow()
    upcoming = []
    
    for m in matches:
        try:
            match_time = datetime.fromisoformat(m.get("utcDate", "").replace("Z", ""))
            if timedelta(hours=0) < (match_time - now) < timedelta(hours=3):
                upcoming.append(m)
        except:
            pass
    
    if not upcoming:
        return
    
    # Analyze and alert
    for match in upcoming[:3]:  # Max 3 alerts
        home = match.get("homeTeam", {}).get("name", "?")
        away = match.get("awayTeam", {}).get("name", "?")
        comp = match.get("competition", {}).get("name", "?")
        
        odds = get_odds(home, away)
        
        # Quick analysis
        if claude_client:
            try:
                prompt = f"""Quick bet check: {home} vs {away} ({comp})
Odds: {odds}

If there's a bet with 75%+ confidence, respond:
🚨 LIVE ALERT: [Team] vs [Team]
⚡ Ставка: [bet] @ [coeff]
📊 Уверенность: X%
💰 Банк: X%
⏰ Скоро начало!

If no good bet (all <75%), respond: NO_ALERT

Be brief. Russian."""

                message = claude_client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}]
                )
                
                response = message.content[0].text
                
                if "NO_ALERT" not in response and "LIVE ALERT" in response:
                    # Send to all subscribers
                    for chat_id in live_subscribers:
                        try:
                            await context.bot.send_message(chat_id=chat_id, text=response)
                            logger.info(f"Sent live alert to {chat_id}")
                        except Exception as e:
                            logger.error(f"Failed to send to {chat_id}: {e}")
                            
            except Exception as e:
                logger.error(f"Live analysis error: {e}")


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 **AI Betting Analyzer v4**

Анализирую футбольные матчи с помощью AI.

📝 **Как использовать:**
• "Арсенал" или "Arsenal"
• "Кто выиграет Бавария?"
• "Liverpool prediction"
• "Посоветуй ставки"

📋 **Команды:**
/recommend - топ ставки
/matches - все матчи
/leagues - по лигам
/live - включить live-алерты
/help - помощь

🎯 **Анализирую:**
• 1X2, Двойной шанс
• Форы, Тоталы
• Обе забьют
• % от банка

⚠️ Ставки - это риск. Играйте ответственно.
"""
    await update.message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📚 **Как пользоваться**

✍️ **Напиши:**
• Название команды: "Арсенал", "Bayern"
• Вопрос: "Кто выиграет Ливерпуль?"
• Матч: "Arsenal vs Chelsea"

📊 **Получишь:**
• Вероятности исходов
• Лучшую ставку с коэффициентом
• % от банка для ставки
• Риски матча
• Вердикт: ставить или нет

🔔 **Live режим** (/live):
Бот сам пришлёт алерт если найдёт 
ставку с 75%+ уверенностью!

💡 **Подсказки:**
• 65%+ уверенность = можно ставить
• 70%+ = ⭐ VALUE BET
• Следуй % от банка!
"""
    await update.message.reply_text(text)


async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle live alerts"""
    chat_id = update.effective_chat.id
    
    if chat_id in live_subscribers:
        live_subscribers.remove(chat_id)
        await update.message.reply_text(
            "🔕 **Live-алерты выключены**\n\n"
            "Напиши /live чтобы включить снова."
        )
    else:
        live_subscribers.add(chat_id)
        await update.message.reply_text(
            "🔔 **Live-алерты включены!**\n\n"
            "Я буду присылать уведомления когда найду\n"
            "ставку с 75%+ уверенностью на матч,\n"
            "который скоро начнётся.\n\n"
            "Напиши /live чтобы выключить."
        )


async def recommend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = detect_language(update.message.text or "")
    
    status = await update.message.reply_text("🔍 Анализирую лучшие ставки...")
    
    matches = get_matches(days=7)
    
    if not matches:
        await status.edit_text("❌ Не удалось загрузить матчи. Попробуй позже.")
        return
    
    recs = get_recommendations(matches, lang)
    
    if recs:
        await status.edit_text(recs)
    else:
        await status.edit_text("❌ Ошибка анализа. Попробуй позже.")


async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matches = get_matches(days=7)
    
    if not matches:
        await update.message.reply_text("❌ Нет матчей.")
        return
    
    by_comp = {}
    for m in matches:
        c = m.get("competition", {}).get("name", "Other")
        if c not in by_comp:
            by_comp[c] = []
        by_comp[c].append(m)
    
    text = "⚽ **Ближайшие матчи:**\n\n"
    for comp, ms in list(by_comp.items())[:5]:
        text += f"🏆 {comp}\n"
        for m in ms[:3]:
            h = m.get("homeTeam", {}).get("name", "?")
            a = m.get("awayTeam", {}).get("name", "?")
            text += f"  • {h} vs {a}\n"
        text += "\n"
    
    text += "_Напиши название команды для анализа_"
    await update.message.reply_text(text)


async def leagues_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League", callback_data="league_PL")],
        [InlineKeyboardButton("🇪🇸 La Liga", callback_data="league_PD")],
        [InlineKeyboardButton("🇩🇪 Bundesliga", callback_data="league_BL1")],
        [InlineKeyboardButton("🇮🇹 Serie A", callback_data="league_SA")],
        [InlineKeyboardButton("🇫🇷 Ligue 1", callback_data="league_FL1")],
        [InlineKeyboardButton("🇪🇺 Champions League", callback_data="league_CL")],
    ]
    await update.message.reply_text("⚽ Выбери лигу:", reply_markup=InlineKeyboardMarkup(keyboard))


async def league_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    code = query.data.replace("league_", "")
    name = COMPETITIONS.get(code, code)
    
    await query.edit_message_text(f"🔍 Загружаю {name}...")
    
    matches = get_matches(code, days=14)
    
    if not matches:
        await query.edit_message_text(f"❌ Нет матчей {name}")
        return
    
    text = f"🏆 **{name}**\n\n"
    for m in matches[:10]:
        h = m.get("homeTeam", {}).get("name", "?")
        a = m.get("awayTeam", {}).get("name", "?")
        try:
            dt = datetime.fromisoformat(m.get("utcDate", "").replace("Z", "+00:00"))
            ds = dt.strftime("%d.%m %H:%M")
        except:
            ds = ""
        text += f"📅 {ds}\n   {h} vs {a}\n\n"
    
    await query.edit_message_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler"""
    user_text = update.message.text.strip()
    
    if len(user_text) < 2:
        return
    
    lang = detect_language(user_text)
    
    status = await update.message.reply_text("🔍 Анализирую запрос...")
    
    # Parse
    parsed = parse_user_query(user_text, lang)
    intent = parsed.get("intent", "unknown")
    teams = parsed.get("teams", [])
    
    logger.info(f"Parsed: intent={intent}, teams={teams}, lang={lang}")
    
    # Handle intents
    if intent == "greeting":
        await status.edit_text("👋 Привет! Напиши название команды или /recommend для лучших ставок!")
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
                h = m.get("homeTeam", {}).get("name", "?")
                a = m.get("awayTeam", {}).get("name", "?")
                text += f"• {h} vs {a}\n"
            text += "\n💡 /recommend - получить рекомендации"
        await status.edit_text(text)
        return
    
    # Found!
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    home_id = match.get("homeTeam", {}).get("id")
    away_id = match.get("awayTeam", {}).get("id")
    match_id = match.get("id")
    comp = match.get("competition", {}).get("name", "")
    
    await status.edit_text(f"✅ Нашёл: {home} vs {away}\n🏆 {comp}\n\n⏳ Собираю данные...")
    
    # Get data
    odds = get_odds(home, away)
    h2h = get_h2h(match_id) if match_id else None
    home_form = get_form(home_id) if home_id else None
    away_form = get_form(away_id) if away_id else None
    
    await status.edit_text(f"✅ {home} vs {away}\n🏆 {comp}\n\n🤖 AI анализирует...")
    
    # Analyze
    analysis = analyze_match(match, odds, h2h, home_form, away_form, lang)
    
    header = f"⚽ **{home}** vs **{away}**\n🏆 {comp}\n{'─'*30}\n\n"
    
    await status.edit_text(header + analysis)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")
    if update and update.message:
        await update.message.reply_text("❌ Ошибка. Попробуй /start")


# ===== MAIN =====

def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN missing!")
        return
    
    print("🚀 Starting AI Betting Bot v4...")
    print(f"   ✅ Telegram")
    print(f"   ✅ Football Data")
    print(f"   {'✅' if ODDS_API_KEY else '⚠️'} Odds API")
    print(f"   {'✅' if CLAUDE_API_KEY else '⚠️'} Claude AI")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("matches", matches_cmd))
    app.add_handler(CommandHandler("leagues", leagues_cmd))
    app.add_handler(CommandHandler("recommend", recommend_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CallbackQueryHandler(league_cb, pattern="^league_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    # Live alerts job - every hour
    job_queue = app.job_queue
    job_queue.run_repeating(check_live_matches, interval=3600, first=60)
    
    print("✅ Bot v4 running with live alerts!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
