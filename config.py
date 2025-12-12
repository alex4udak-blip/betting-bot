"""
Configuration module for AI Betting Bot v14
All environment variables and constants are defined here.
"""
import os
from typing import Set

# ===== API KEYS (from environment) =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# ===== API URLs =====
FOOTBALL_API_URL = "https://api.football-data.org/v4"
ODDS_API_URL = "https://api.the-odds-api.com/v4"

# ===== AFFILIATE & PAYMENTS =====
# MASTER SWITCH: Set to False to hide all monetization (affiliate links, crypto, 1win)
# When False, shows "Coming soon" placeholders instead
MONETIZATION_ENABLED = os.getenv("MONETIZATION_ENABLED", "false").lower() == "true"

# === PRESERVED ORIGINAL VALUES (uncomment MONETIZATION_ENABLED=true to activate) ===
# 1WIN Affiliate Link (Universal Router - auto GEO redirect)
_REAL_AFFILIATE_LINK = "https://1wfafs.life/?open=register&p=ex2m"

# Crypto wallets for manual payment
_REAL_CRYPTO_WALLETS = {
    "USDT_TRC20": os.getenv("CRYPTO_WALLET_USDT", "TYc8XA1kx4v3uSYjpRxbqjtM1gNYeV3rZC"),
    "TON": os.getenv("CRYPTO_WALLET_TON", "UQC5Du_luLDSdBudVJZ-BMLtnoUFHj5HgJ_fgF0YehshSwlL")
}

# CryptoBot API token
_REAL_CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")

# === ACTIVE VALUES (depend on MONETIZATION_ENABLED) ===
AFFILIATE_LINK = _REAL_AFFILIATE_LINK if MONETIZATION_ENABLED else ""
CRYPTO_WALLETS = _REAL_CRYPTO_WALLETS if MONETIZATION_ENABLED else {"USDT_TRC20": "", "TON": ""}
CRYPTOBOT_TOKEN = _REAL_CRYPTOBOT_TOKEN if MONETIZATION_ENABLED else ""

# Crypto prices (in USD)
CRYPTO_PRICES = {
    7: 15,      # 7 days = $15
    30: 40,     # 30 days = $40
    365: 100    # 1 year = $100
}

# ===== WEBHOOK SECURITY =====
WEBHOOK_SECRET_1WIN = os.getenv("WEBHOOK_SECRET_1WIN", "")
WEBHOOK_SECRET_CRYPTO = os.getenv("WEBHOOK_SECRET_CRYPTO", "")

# ===== LIMITS & DEFAULTS =====
# Daily free limit for predictions
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "3"))

# HTTP request timeout (seconds)
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))

# Web server port
WEB_SERVER_PORT = int(os.getenv("PORT", "8080"))

# ===== ADMIN CONFIGURATION =====
# Admin user IDs (add your Telegram user ID here)
# Get your ID by messaging @userinfobot on Telegram
ADMIN_IDS: Set[int] = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip().isdigit()
}

# Support username for manual payment/help (without @)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "alex4udak")

# ===== DATABASE =====
# Use /data directory if available (for Docker/persistent storage)
_data_dir = "/data" if os.path.exists("/data") else "."
DB_PATH = os.getenv("DB_PATH", f"{_data_dir}/betting_bot.db" if _data_dir != "." else "betting_bot.db")

# ML Models directory
ML_MODELS_DIR = os.getenv("ML_MODELS_DIR", f"{_data_dir}/ml_models" if _data_dir != "." else "ml_models")
ML_MIN_SAMPLES = int(os.getenv("ML_MIN_SAMPLES", "50"))

# ===== LOGGING =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def is_admin(user_id: int) -> bool:
    """Check if user is an admin"""
    return user_id in ADMIN_IDS


def validate_config() -> list[str]:
    """Validate required configuration and return list of missing/invalid items"""
    errors = []

    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN is not set")

    if not FOOTBALL_API_KEY:
        errors.append("FOOTBALL_API_KEY is not set (predictions will be limited)")

    if not CLAUDE_API_KEY:
        errors.append("CLAUDE_API_KEY is not set (AI analysis unavailable)")

    return errors
