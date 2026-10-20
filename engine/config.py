import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / os.getenv("DB_PATH", "data/engine.db")
SEEDS = ROOT / "seeds"
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

# Adzuna country codes that overlap Rwazi's target markets.
ADZUNA_COUNTRIES = ["za", "in", "br", "mx", "sg"]

# Query terms that surface expansion and distribution intent.
ADZUNA_QUERIES = [
    "country manager",
    "distributor manager",
    "route to market",
    "trade marketing manager",
    "consumer insights",
    "category manager",
    "market expansion",
]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_SERVICE_KEY")
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")

MODEL_TRIAGE = os.getenv("MODEL_TRIAGE", "claude-haiku-5-5")
MODEL_REASONING = os.getenv("MODEL_REASONING", "claude-sonnet-5-5")

SENDER_NAME = os.getenv("SENDER_NAME", "Success Afeh")
SENDER_COMPANY = os.getenv("SENDER_COMPANY", "Rwazi")

# Markets where Rwazi's zero-party panel is the differentiator.
TARGET_MARKETS = [
    "Nigeria", "Kenya", "Ghana", "South Africa", "Egypt", "Morocco", "Tanzania",
    "Uganda", "Ethiopia", "Senegal", "Ivory Coast", "Côte d'Ivoire",
    "Indonesia", "Vietnam", "Philippines", "Thailand", "India", "Pakistan",
    "Bangladesh", "Malaysia",
    "Brazil", "Mexico", "Colombia", "Peru", "Chile", "Argentina",
    "LATAM", "Africa", "Southeast Asia", "MENA", "Emerging Markets",
]

# Titles that signal expansion or distribution change, not generic hiring.
SIGNAL_TITLE_HINTS = [
    "country manager", "country lead", "general manager", "market lead",
    "distributor", "distribution", "route to market", "rtm",
    "regional director", "market expansion", "commercial director",
    "trade marketing", "category manager", "shopper insights",
    "consumer insights", "market research", "head of sales",
]

# Who actually buys consumer intelligence. Not the CEO.
BUYER_TITLES = [
    "Consumer Insights", "Shopper Insights", "Market Insights",
    "Category Manager", "Category Director", "Market Research",
    "Brand Manager", "Trade Marketing", "Revenue Growth Management",
    "Commercial Strategy", "Head of Strategy",
]