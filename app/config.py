import os
from pathlib import Path

# Base directories
DATA_DIR = Path(os.getenv("KB_DATA_DIR", "/data"))
DB_PATH = Path(os.getenv("KB_DB_PATH", str(DATA_DIR / "knowledge.db")))
IMAGES_DIR = DATA_DIR / "images"
GALLERY_DL_CONFIG = Path("/tmp/gallery-dl-config.json")

# Twitter cookies
TWITTER_AUTH_TOKEN=os.getenv("TWITTER_AUTH_TOKEN", "")
TWITTER_CT0 = os.getenv("TWITTER_CT0", "")
# Server
HOST = os.getenv("KB_HOST", "0.0.0.0")
PORT = int(os.getenv("KB_PORT", "8501"))

# Ensure directories exist
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def get_gallery_dl_config() -> dict:
    """Generate gallery-dl config with Twitter cookies."""
    config = {
        "extractor": {
            "twitter": {
                "cookies": {
                    "auth_token": TWITTER_AUTH_TOKEN,
                    "ct0": TWITTER_CT0,
                },
                "cards": True,
                "conversations": True,
                "expand": True,
                "fallback": True,
                "include": ["timeline"],
                "likes": False,
                "quoted": True,
                "replies": True,
                "retweets": True,
                "text-tweets": True,
                "users": "user",
                "videos": True,
            }
        },
        "downloader": {
            "directory": ["images"],
            "archive": str(DATA_DIR / "gallery-dl-archive.db"),
        },
        "output": {
            "mode": "json",
        },
    }
    return config


# AI Configuration for title generation
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "stepfun/step-3.7-flash")
