"""
Kagaz settings.

Rule of thumb:
- Secrets and anything that differs per machine come from `.env` (see `.env.example`).
- Everything else has a fixed value here.
- Product rules (plan limits, PIN timing, upload size) live in the app that uses them.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")


# --- Basics -----------------------------------------------------------------

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "7c3f-2409-4090-210c-6200-9c08-cf64-b6fb-c058.ngrok-free.app"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Address of this site. Links we send to users (PIN pages) start with it.
PUBLIC_BASE_URL = (env("PUBLIC_BASE_URL", default="") or "http://localhost:8000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "huey.contrib.djhuey",
    "core",
    "accounts",
    "vault",
    "bot",
    "billing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kagaz.urls"
WSGI_APPLICATION = "kagaz.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Logins for you and your team (the admin). People using Kagaz are accounts.Member.
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Encrypted document files. Never served over HTTP.
MEDIA_ROOT = BASE_DIR / "storage"

# Keep web uploads in memory (never in a temp file) so an unencrypted copy never
# touches the disk. A little above the 25 MB upload limit; bigger uploads are refused.
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]
FILE_UPLOAD_MAX_MEMORY_SIZE = 26 * 1024 * 1024


# --- Database: PostgreSQL ---------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", default="kagaz"),
        "USER": env("DB_USER", default="postgres"),
        "PASSWORD": env("DB_PASSWORD"),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="5432"),
    }
}


# --- Email ------------------------------------------------------------------
# With no EMAIL_HOST_USER in .env, emails are printed in the terminal instead
# of being sent. (Django 6.1 configures email with MAILERS.)

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Kagaz <noreply@localhost>")
# Shown to users who need a person (WhatsApp requires a way to reach a human).
SUPPORT_EMAIL = env("SUPPORT_EMAIL", default="")
# The business behind Kagaz, shown on the Contact, Terms and Refund pages.
# Use the name exactly as in your Razorpay KYC.
BUSINESS_NAME = env("BUSINESS_NAME", default="") or "Kagaz"
BUSINESS_ADDRESS = env("BUSINESS_ADDRESS", default="")
SUPPORT_PHONE = env("SUPPORT_PHONE", default="")

if env("EMAIL_HOST_USER", default=""):
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": env("EMAIL_HOST", default="smtp-relay.brevo.com"),
                "port": env.int("EMAIL_PORT", default=587),
                "username": env("EMAIL_HOST_USER"),
                "password": env("EMAIL_HOST_PASSWORD"),
                "use_tls": True,
            },
        }
    }
else:
    MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}


# --- Background jobs (Huey) -------------------------------------------------
# Locally, jobs run immediately inside the same process: nothing else to start.
# In production, set REDIS_URL and run a worker: python manage.py run_huey

REDIS_URL = env("REDIS_URL", default="")

HUEY = {
    "huey_class": "huey.RedisHuey" if REDIS_URL else "huey.MemoryHuey",
    "name": "kagaz",
    "immediate": not REDIS_URL,
    "consumer": {"workers": 4, "worker_type": "thread"},
}
if REDIS_URL:
    HUEY["url"] = REDIS_URL
    # Short-lived counters (admin sign-in attempts), shared by every web process.
    # Without Redis, Django keeps them in memory, which is fine locally.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}


# --- Admin ------------------------------------------------------------------
# On the server, set ADMIN_URL in .env to a random word so the admin isn't at
# /admin/, where bots look. Wrong-password limit: accounts/admin_login.py.

ADMIN_URL = (env("ADMIN_URL", default="") or "admin").strip("/")


# --- Security ---------------------------------------------------------------

# PIN links carry a token in the URL. "same-origin" never sends it to other sites.
# (Not "no-referrer": browsers then send `Origin: null` on form posts, which fails CSRF.)
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30


# --- Logging ----------------------------------------------------------------
# Log ids and events only, never document contents or message text.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "httpx": {"level": "WARNING"},
        "httpx2": {"level": "WARNING"},
        "google_genai": {"level": "WARNING"},
        "pikepdf": {"level": "WARNING"},
    },
}


# --- Kagaz ------------------------------------------------------------------

# Encrypts every user's document key. Generate: python manage.py generate_master_key
# Back it up: if it is lost, every stored document is lost.
KAGAZ_MASTER_KEY = env("KAGAZ_MASTER_KEY", default="")

# The AI that reads documents and understands messages: "gemini" or "claude".
AI_PROVIDER = env("AI_PROVIDER", default="") or "gemini"
DEFAULT_AI_MODELS = {"gemini": "gemini-3.5-flash-lite", "claude": "claude-opus-5-5"}
AI_MODEL = env("AI_MODEL", default="") or DEFAULT_AI_MODELS.get(AI_PROVIDER, "")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

# "console": chat in the terminal (python manage.py chat). "cloud": real WhatsApp.
WHATSAPP_BACKEND = env("WHATSAPP_BACKEND", default="") or "console"
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")
# Kagaz's own WhatsApp number in digits (e.g. 919876543210). Goes into the
# invite links people share; without it they say "send REF … to Kagaz".
WHATSAPP_DISPLAY_NUMBER = env("WHATSAPP_DISPLAY_NUMBER", default="")

# Razorpay (payments). Test keys start with rzp_test_. The webhook secret is the
# one you type when adding the webhook in the Razorpay dashboard.
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID", default="")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET", default="")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET", default="")
