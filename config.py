import os

BINANCE_BASE_URL         = "https://api.binance.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
