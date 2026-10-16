import os


class Config:
    DATABASE = os.environ.get("MENTOR_DB", "mentor.db")
    # Set MENTOR_SECRET in production. If missing, a random key is generated at
    # startup (tokens then become invalid after every restart).
    SECRET_KEY = os.environ.get("MENTOR_SECRET")
    TOKEN_TTL_HOURS = int(os.environ.get("MENTOR_TOKEN_TTL_HOURS", "12"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MENTOR_MAX_UPLOAD_MB", "5")) * 1024 * 1024
    CORS_ORIGIN = os.environ.get("MENTOR_CORS_ORIGIN", "")  # e.g. https://portal.college.edu
    TODAY = None  # tests may pin a date (YYYY-MM-DD)
