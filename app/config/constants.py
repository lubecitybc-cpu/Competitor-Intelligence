"""Configuration constants for the competitor intelligence system."""
import os
from pathlib import Path

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
IMAGES_DIR = DATA_DIR / "images"

ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY")
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")

PROMO_TIMEOUT = int(os.getenv("PROMO_TIMEOUT", "30"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
OCR_TEMP_DIR = Path(os.getenv("OCR_TEMP_DIR", "/tmp/ocr_temp"))
PLAYWRIGHT_MODE = os.getenv("PLAYWRIGHT_MODE", "headless")

# Promo detection keywords
PROMO_KEYWORDS = [
    "offer", "offers", "promo", "promotions", "coupon", "coupons",
    "special", "discount", "save", "free", "rebate", "deal", "limited",
    "oil change", "brake", "battery", "seasonal", "tire", "service"
]

# Service mapping keywords -> categories
SERVICE_MAP = {
    "oil": "oil change",
    "synthetic": "oil change",
    "brake": "brakes",
    "battery": "battery",
    "exhaust": "exhaust",
    "tire": "tires",
    "seasonal": "seasonal",
    "coolant": "coolant flush",
    "transmission": "transmission",
}

