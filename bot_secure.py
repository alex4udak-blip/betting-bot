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
    
    prompt = f"""You are a confident expert betting analyst. Analyze this match and ALWAYS give recommendations.

{comp}: {home} vs {away}
Odds: {odds_text}
{h2h_text}
{form_text}

{lang_instr}

IMPORTANT: You MUST give betting recommendations. Use your knowledge about these teams (league position, typical performance, historical strength). Don't refuse to analyze - make your best prediction based on available info.

USE THIS EXACT FORMAT:

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
[Key risks - 1-2 sentences]

✅ ВЕРДИКТ: [СИЛЬНАЯ СТАВКА / СРЕДНИЙ РИСК / ПРОПУСТИТЬ]

RULES:
- ALWAYS provide predictions - never refuse
- Use your knowledge about teams if data is limited
- Premier League vs Championship = clear favorite (75%+)
- Include coefficients from odds data
- Bank %: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%
- Mark 70%+ bets as "⭐ VALUE"
- Only ПРОПУСТИТЬ if genuinely unpredictable (both teams equal)"""

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
    
    prompt = f"""You are a CONFIDENT betting expert. Analyze and give TOP 3-4 picks:

{matches_text}

{lang_instr}

IMPORTANT: You MUST give recommendations. Use your football knowledge about these teams.
- Premier League vs Championship = obvious favorite
- Big teams at home = usually win
- Form, history, class difference matters

USE THIS FORMAT:

🔥 ТОП СТАВКИ:

1️⃣ [Team] vs [Team]
   ✅ Ставка: [specific bet] @ коэфф X.XX
   📊 Уверенность: X%
   💰 Банк: X%
   💡 Почему: [1 sentence]

2️⃣ ...

3️⃣ ...

❌ ИЗБЕГАТЬ:
• [Match] - [why risky]

RULES:
- ALWAYS give 3-4 picks
- Never refuse or say "not enough data"
- Bank %: 80%+=5%, 75-80%=3-4%, 70-75%=2-3%, 65-70%=1-2%"""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except:
        return None


# ===== PREDICTIONS TRACKING =====

# Store predictions: {match_id: {bet, confidence, odds, timestamp, result}}
predictions_db = {}

def save_prediction(match_id, home, away, bet_type, confidence, odds=None):
    """Save a prediction for tracking"""
    predictions_db[match_id] = {
        "match": f"{home} vs {away}",
        "home": home,
        "away": away,
        "bet": bet_type,
        "confidence": confidence,
        "odds": odds,
        "timestamp": datetime.now().isoformat(),
        "result": None,  # Will be filled after match
        "correct": None  # True/False after checking
    }
    logger.info(f"Saved prediction: {home} vs {away} - {bet_type}")


def check_prediction_result(prediction, home_score, away_score):
    """Check if prediction was correct based on final score"""
    bet = prediction.get("bet", "").lower()
    
    # Home win
    if "п1" in bet or "победа" in bet.lower() and prediction["home"].lower() in bet.lower():
        return home_score > away_score
    if "home" in bet or "win" in bet and prediction["home"].lower() in bet.lower():
        return home_score > away_score
    
    # Away win  
    if "п2" in bet or "победа" in bet.lower() and prediction["away"].lower() in bet.lower():
        return away_score > home_score
    
    # Draw
    if "ничья" in bet or "draw" in bet or "x" in bet.lower():
        return home_score == away_score
    
    # Over 2.5
    if "тб 2.5" in bet or "тб2.5" in bet or "over 2.5" in bet or "больше 2.5" in bet:
        return (home_score + away_score) > 2.5
    
    # Under 2.5
    if "тм 2.5" in bet or "тм2.5" in bet or "under 2.5" in bet or "меньше 2.5" in bet:
        return (home_score + away_score) < 2.5
    
    # BTTS Yes
    if "обе забьют" in bet or "btts" in bet:
        return home_score > 0 and away_score > 0
    
    # Default - can't determine
    return None


async def check_finished_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check results of finished matches and update predictions"""
    
    if not predictions_db:
        return
    
    logger.info("Checking finished matches...")
    
    # Get recent finished matches
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    for match_id, pred in list(predictions_db.items()):
        if pred.get("result") is not None:
            continue  # Already checked
        
        try:
            response = requests.get(
                f"{FOOTBALL_API_URL}/matches/{match_id}",
                headers=headers, timeout=10
            )
            
            if response.status_code == 200:
                match = response.json()
                status = match.get("status", "")
                
                if status == "FINISHED":
                    score = match.get("score", {}).get("fullTime", {})
                    home_score = score.get("home", 0)
                    away_score = score.get("away", 0)
                    
                    pred["result"] = f"{home_score}:{away_score}"
                    pred["correct"] = check_prediction_result(pred, home_score, away_score)
                    
                    logger.info(f"Result: {pred['match']} {pred['result']} - {'✅' if pred['correct'] else '❌'}")
                    
        except Exception as e:
            logger.error(f"Error checking match {match_id}: {e}")


def get_stats_summary():
    """Get prediction statistics"""
    if not predictions_db:
        return None
    
    total = len(predictions_db)
    checked = [p for p in predictions_db.values() if p.get("correct") is not None]
    correct = [p for p in checked if p.get("correct") == True]
    wrong = [p for p in checked if p.get("correct") == False]
    pending = [p for p in predictions_db.values() if p.get("result") is None]
    
    win_rate = (len(correct) / len(checked) * 100) if checked else 0
    
    # Calculate ROI (simplified)
    roi = 0
    for p in checked:
        odds = p.get("odds") or 1.5  # Default odds if not saved
        if p.get("correct"):
            roi += (odds - 1)  # Profit
        else:
            roi -= 1  # Loss
    
    roi_percent = (roi / len(checked) * 100) if checked else 0
    
    return {
        "total": total,
        "checked": len(checked),
        "correct": len(correct),
        "wrong": len(wrong),
        "pending": len(pending),
        "win_rate": win_rate,
        "roi": roi_percent,
        "recent": list(predictions_db.values())[-5:]
    }


# ===== LIVE ALERTS =====

# Track recommendations for stats
recommendations_history = []


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
                    # Extract bet info from response for tracking
                    bet_type = "Unknown"
                    confidence = 75
                    
                    # Try to extract bet type
                    if "Ставка:" in response:
                        try:
                            bet_line = [l for l in response.split("\n") if "Ставка:" in l][0]
                            bet_type = bet_line.split("Ставка:")[1].split("@")[0].strip()
                        except:
                            pass
                    
                    # Try to extract confidence
                    if "Уверенность:" in response:
                        try:
                            conf_line = [l for l in response.split("\n") if "Уверенность:" in l][0]
                            confidence = int(''.join(filter(str.isdigit, conf_line.split(":")[1][:5])))
                        except:
                            pass
                    
                    # Save prediction for tracking
                    match_id = match.get("id")
                    if match_id:
                        save_prediction(match_id, home, away, bet_type, confidence)
                    
                    # Track recommendation
                    recommendations_history.append(f"{home} vs {away}")
                    if len(recommendations_history) > 20:
                        recommendations_history.pop(0)
                    
                    # Send to all subscribers
                    for chat_id in live_subscribers:
                        try:
                            await context.bot.send_message(chat_id=chat_id, text=response)
                            logger.info(f"Sent live alert to {chat_id}")
                        except Exception as e:
                            logger.error(f"Failed to send to {chat_id}: {e}")
                            
            except Exception as e:
                logger.error(f"Live analysis error: {e}")


async def send_stats_summary(context: ContextTypes.DEFAULT_TYPE):
    """Send stats summary every 2 hours to subscribers"""
    
    if not live_subscribers:
        return
    
    logger.info(f"Sending stats summary to {len(live_subscribers)} subscribers...")
    
    # First check finished matches to update predictions
    await check_finished_matches(context)
    
    matches = get_matches(days=1)
    
    if not matches:
        return
    
    # Count matches by league
    by_league = {}
    for m in matches:
        league = m.get("competition", {}).get("name", "Other")
        by_league[league] = by_league.get(league, 0) + 1
    
    # Get prediction stats
    pred_stats = get_stats_summary()
    
    # Get top picks
    top_picks = ""
    if claude_client and matches:
        matches_text = ""
        for i, m in enumerate(matches[:6], 1):
            h = m.get("homeTeam", {}).get("name", "?")
            a = m.get("awayTeam", {}).get("name", "?")
            matches_text += f"{i}. {h} vs {a}\n"
        
        try:
            prompt = f"""You are a confident betting analyst. Give TOP 3 picks from these matches:

{matches_text}

RULES:
- ALWAYS give 3 picks - never refuse
- Use your football knowledge
- Premier League team vs lower league = easy pick
- Big team at home = usually good pick

Format (Russian):
1. [Match] - [Bet] @ коэфф - X%
2. [Match] - [Bet] @ коэфф - X%  
3. [Match] - [Bet] @ коэфф - X%

Be brief but ALWAYS provide 3 picks."""

            message = claude_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}]
            )
            top_picks = message.content[0].text
        except:
            top_picks = "Анализ недоступен"
    
    # Build summary
    summary = f"""📊 **СТАТИСТИКА (каждые 2 часа)**

⚽ **Матчей сегодня:** {len(matches)}

🏆 **По лигам:**
"""
    for league, count in list(by_league.items())[:5]:
        summary += f"• {league}: {count}\n"
    
    # Add prediction tracking stats
    if pred_stats and pred_stats["total"] > 0:
        summary += f"\n📈 **МОИ РЕЗУЛЬТАТЫ:**\n"
        if pred_stats["checked"] > 0:
            emoji = "🔥" if pred_stats["win_rate"] >= 70 else "✅" if pred_stats["win_rate"] >= 50 else "📉"
            summary += f"{emoji} Точность: {pred_stats['correct']}/{pred_stats['checked']} ({pred_stats['win_rate']:.0f}%)\n"
            roi_sign = "+" if pred_stats["roi"] > 0 else ""
            summary += f"💰 ROI: {roi_sign}{pred_stats['roi']:.1f}%\n"
        if pred_stats["pending"] > 0:
            summary += f"⏳ Ожидают: {pred_stats['pending']}\n"
    
    summary += f"""
🎯 **Топ ставки (70%+):**
{top_picks}

💡 /stats - полная статистика
💡 Напиши название команды для анализа!
"""
    
    # Send to subscribers
    for chat_id in live_subscribers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=summary)
            logger.info(f"Sent stats to {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send stats to {chat_id}: {e}")


# ===== IN-PLAY LIVE BETTING =====

# In-play subscribers (separate from pre-match)
inplay_subscribers = set()

# Track already alerted matches to avoid spam
inplay_alerted = {}


def get_live_matches():
    """Get matches currently in play"""
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    
    live_matches = []
    
    for league in ["PL", "PD", "BL1", "SA", "FL1", "CL"]:
        try:
            url = f"{FOOTBALL_API_URL}/competitions/{league}/matches"
            params = {"status": "IN_PLAY,PAUSED"}
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                matches = response.json().get("matches", [])
                live_matches.extend(matches)
        except Exception as e:
            logger.error(f"Error getting live matches: {e}")
    
    return live_matches


def analyze_inplay_opportunity(match):
    """Analyze live match for betting opportunity"""
    
    if not claude_client:
        return None
    
    home = match.get("homeTeam", {}).get("name", "?")
    away = match.get("awayTeam", {}).get("name", "?")
    
    score = match.get("score", {})
    home_score = score.get("fullTime", {}).get("home") or score.get("halfTime", {}).get("home") or 0
    away_score = score.get("fullTime", {}).get("away") or score.get("halfTime", {}).get("away") or 0
    
    # Get match minute (approximate)
    match_status = match.get("status", "")
    minute = "?"
    
    # Try to get minute from match data
    if match.get("minute"):
        minute = match.get("minute")
    elif match_status == "PAUSED":
        minute = "HT"
    
    comp = match.get("competition", {}).get("name", "?")
    
    prompt = f"""You are a LIVE betting expert. Analyze this in-play match:

🔴 LIVE: {home} {home_score}:{away_score} {away}
⏱️ Minute: {minute}
🏆 {comp}

Based on the score and time, identify if there's a good IN-PLAY betting opportunity.

Consider:
- Current score vs expected (is there value?)
- Time remaining (enough for goals?)
- Match situation (team needs to score? parking the bus?)

If you find opportunity with 70%+ confidence, respond with:

🔴 IN-PLAY ALERT!

⚽ {home} {home_score}:{away_score} {away} ({minute}')

⚡ СТАВКА: [specific bet]
📊 Уверенность: X%
💰 Банк: X%
🎯 Почему: [1 sentence]

⏰ Действуй быстро!

If NO good opportunity (all <70%), respond exactly: NO_OPPORTUNITY

Be aggressive but smart. RUSSIAN language."""

    try:
        message = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        
        response = message.content[0].text
        
        if "NO_OPPORTUNITY" in response:
            return None
        
        if "IN-PLAY ALERT" in response:
            return response
        
        return None
        
    except Exception as e:
        logger.error(f"In-play analysis error: {e}")
        return None


async def check_inplay_matches(context: ContextTypes.DEFAULT_TYPE):
    """Check live matches for betting opportunities - runs every minute"""
    
    if not inplay_subscribers:
        return
    
    logger.info(f"Checking in-play matches for {len(inplay_subscribers)} subscribers...")
    
    live_matches = get_live_matches()
    
    if not live_matches:
        logger.info("No live matches")
        return
    
    logger.info(f"Found {len(live_matches)} live matches")
    
    for match in live_matches:
        match_id = match.get("id")
        
        if not match_id:
            continue
        
        # Check if already alerted for this match recently (within 10 minutes)
        last_alert = inplay_alerted.get(match_id, 0)
        now = datetime.now().timestamp()
        
        if now - last_alert < 600:  # 10 minutes cooldown
            continue
        
        # Analyze opportunity
        alert = analyze_inplay_opportunity(match)
        
        if alert:
            home = match.get("homeTeam", {}).get("name", "?")
            away = match.get("awayTeam", {}).get("name", "?")
            
            # Save prediction for tracking
            save_prediction(match_id, home, away, "IN-PLAY", 70)
            
            # Mark as alerted
            inplay_alerted[match_id] = now
            
            # Send to subscribers
            for chat_id in inplay_subscribers:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=alert)
                    logger.info(f"Sent in-play alert to {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to send in-play alert: {e}")


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """🤖 **AI Betting Analyzer v4**

Анализирую футбольные матчи с помощью AI.

📝 **Как использовать:**
• "Арсенал" или "Arsenal"
• "Кто выиграет Бавария?"
• "Liverpool prediction"

📋 **Команды:**
/recommend - топ ставки
/matches - все матчи  
/leagues - по лигам
/live - pre-match алерты (за 1-3ч до матча)
/inplay - 🔴 LIVE ставки (во время матча!)
/stats - моя статистика
/help - помощь

🎯 **Режимы алертов:**

📢 **/live** - Pre-match:
• Проверяет каждые 5 минут
• Алерт за 1-3 часа до матча
• Успеешь спокойно поставить

🔴 **/inplay** - Live:
• Проверяет каждую минуту
• Алерт ВО ВРЕМЯ матча
• Реагируй быстро!

📈 **Отслеживаю результаты:**
• Проверяю угадал или нет
• Считаю точность и ROI

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


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show prediction statistics"""
    
    # First check finished matches
    await check_finished_matches(context)
    
    stats = get_stats_summary()
    
    if not stats or stats["total"] == 0:
        await update.message.reply_text(
            "📊 **Статистика пока пуста**\n\n"
            "Я начну отслеживать прогнозы после первых рекомендаций.\n"
            "Напиши /recommend чтобы получить прогнозы!"
        )
        return
    
    text = "📈 **МОЯ СТАТИСТИКА:**\n\n"
    
    if stats["checked"] > 0:
        emoji = "🔥" if stats["win_rate"] >= 70 else "✅" if stats["win_rate"] >= 50 else "📉"
        text += f"{emoji} **Точность:** {stats['correct']}/{stats['checked']} ({stats['win_rate']:.1f}%)\n"
        
        roi_emoji = "💰" if stats["roi"] > 0 else "📉"
        text += f"{roi_emoji} **ROI:** {'+' if stats['roi'] > 0 else ''}{stats['roi']:.1f}%\n\n"
    
    if stats["pending"] > 0:
        text += f"⏳ **Ожидают результата:** {stats['pending']}\n\n"
    
    if stats["recent"]:
        text += "📋 **Последние прогнозы:**\n"
        for p in stats["recent"][-5:]:
            if p.get("result"):
                emoji = "✅" if p.get("correct") else "❌"
                text += f"{emoji} {p['match']} {p['result']} - {p['bet']}\n"
            else:
                text += f"⏳ {p['match']} - {p['bet']}\n"
    
    text += f"\n📊 Всего прогнозов: {stats['total']}"
    
    await update.message.reply_text(text)


async def inplay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle in-play live alerts"""
    chat_id = update.effective_chat.id
    
    if chat_id in inplay_subscribers:
        inplay_subscribers.remove(chat_id)
        await update.message.reply_text(
            "🔕 **In-Play алерты выключены**\n\n"
            "Напиши /inplay чтобы включить снова."
        )
    else:
        inplay_subscribers.add(chat_id)
        await update.message.reply_text(
            "🔴 **In-Play режим включён!**\n\n"
            "Я буду следить за LIVE матчами каждую минуту.\n"
            "Когда найду хорошую возможность (70%+) — пришлю алерт.\n\n"
            "**Типы ставок:**\n"
            "• Тоталы (0:0 → ТБ0.5)\n"
            "• Следующий гол\n"
            "• Исход матча\n\n"
            "⚡ Реагируй быстро - это LIVE!\n\n"
            "Напиши /inplay чтобы выключить."
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
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("inplay", inplay_cmd))
    app.add_handler(CallbackQueryHandler(league_cb, pattern="^league_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    
    # Live alerts - every 5 minutes (test mode)
    # Stats summary - every 2 hours
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_live_matches, interval=300, first=60)  # 5 min - pre-match
        job_queue.run_repeating(send_stats_summary, interval=7200, first=120)  # 2 hours
        job_queue.run_repeating(check_inplay_matches, interval=60, first=30)  # 1 min - in-play
        print("✅ Jobs: pre-match(5m), stats(2h), in-play(1m)")
    else:
        print("⚠️ Job queue not available")
    
    print("✅ Bot v4 running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
