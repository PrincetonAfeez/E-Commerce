"""Django settings for the commerce capstone."""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Environment: "development" (default) or "production". Production flips on the full
# security posture and refuses to boot without real secrets and PostgreSQL (ADR-0001).
DJANGO_ENV = os.environ.get("DJANGO_ENV", "development").lower()
IS_PRODUCTION = DJANGO_ENV == "production"
RUNNING_TESTS = "test" in sys.argv or "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.argv[0]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "" if IS_PRODUCTION else "dev-only-commerce-capstone-secret")
if IS_PRODUCTION and not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")

# Secure by default: DEBUG is off unless explicitly enabled, and always off in prod.
DEBUG = (os.environ.get("DJANGO_DEBUG", "1") == "1") and not IS_PRODUCTION

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
if IS_PRODUCTION and not os.environ.get("DJANGO_ALLOWED_HOSTS"):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "shop",
]


def _try_app(module: str, app: str) -> bool:
    try:
        __import__(module)
    except ImportError:
        return False
    INSTALLED_APPS.append(app)
    return True


# OpenAPI schema generation (spec §8/§25). Optional so the app still boots without it.
HAS_SPECTACULAR = _try_app("drf_spectacular", "drf_spectacular")
# Self-hosted Swagger/ReDoc assets so the docs UI works under a strict `script-src 'self'` CSP.
HAS_SPECTACULAR_SIDECAR = HAS_SPECTACULAR and _try_app("drf_spectacular_sidecar", "drf_spectacular_sidecar")
# CORS + CSP hardening (spec §9/§27.1). Optional so the app still boots without them.
HAS_CORS = _try_app("corsheaders", "corsheaders")
HAS_CSP = _try_app("csp", "csp")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collected static files under Gunicorn in production.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "shop.middleware.RequestIDMiddleware",
    "shop.middleware.TenantMiddleware",
]
if HAS_CORS:
    # CorsMiddleware must precede CommonMiddleware.
    MIDDLEWARE.insert(MIDDLEWARE.index("django.middleware.common.CommonMiddleware"), "corsheaders.middleware.CorsMiddleware")
if HAS_CSP:
    MIDDLEWARE.append("csp.middleware.CSPMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "shop.context_processors.store_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    import dj_database_url

    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    # SQLite is only acceptable for local dev / tests. Production requires PostgreSQL
    # because checkout correctness depends on real row-locking (ADR-0001).
    if IS_PRODUCTION and not RUNNING_TESTS:
        raise ImproperlyConfigured("DATABASE_URL (PostgreSQL) is required in production; SQLite is not supported.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_DATABASE_PATH", BASE_DIR / "db.sqlite3"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# WhiteNoise: compressed, hashed static files served by the app server in production.
# The manifest backend requires collectstatic to have run, so only use it in prod; dev
# and tests use the plain storage that resolves names without a manifest.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if IS_PRODUCTION
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
# Uploaded product images: cap size and restrict to known-safe image extensions (spec §9/§24.3).
MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_BYTES", str(5 * 1024 * 1024)))
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_BYTES + 512 * 1024

# Simulated-gateway test modes (decline / dropped_confirmation / ...) are only honoured
# outside production so untrusted clients cannot drive them (spec §19.3).
GATEWAY_TEST_MODES_ENABLED = os.environ.get("GATEWAY_TEST_MODES_ENABLED", "1" if not IS_PRODUCTION else "0") == "1"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "catalog:list"
LOGOUT_REDIRECT_URL = "catalog:list"

# Console backend in dev so password-reset/verification emails are observable; a real
# SMTP/provider backend is configured via env in production.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if not IS_PRODUCTION else "django.core.mail.backends.smtp.EmailBackend",
)
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL", "no-reply@aster-commerce.test")
# Absolute base URL used to build links in transactional emails (no request available).
SITE_URL = os.environ.get("DJANGO_SITE_URL", "http://localhost:8000")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_PAGINATION_CLASS": "shop.pagination.CreatedAtCursorPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "shop.api.exception_handler",
    # Production serves JSON only: the browsable API's inline scripts/styles are blocked
    # by CSP and it should not be exposed on a production API surface.
    "DEFAULT_RENDERER_CLASSES": (
        ["rest_framework.renderers.JSONRenderer"]
        if IS_PRODUCTION
        else [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
    ),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "60/min"),
        "user": os.environ.get("THROTTLE_USER", "240/min"),
        "checkout": os.environ.get("THROTTLE_CHECKOUT", "20/min"),
        "payment": os.environ.get("THROTTLE_PAYMENT", "20/min"),
        "refund": os.environ.get("THROTTLE_REFUND", "30/min"),
        "login": os.environ.get("THROTTLE_LOGIN", "10/min"),
    },
}
if HAS_SPECTACULAR:
    REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"] = "drf_spectacular.openapi.AutoSchema"

SPECTACULAR_SETTINGS = {
    "TITLE": "Aster Commerce API",
    "VERSION": "1.0.0",
    "DESCRIPTION": "Catalog, cart, checkout and staff operations API.",
    "SERVE_INCLUDE_SCHEMA": False,
}
if HAS_SPECTACULAR_SIDECAR:
    # Point the docs UIs at bundled, same-origin assets (satisfies script-src 'self').
    SPECTACULAR_SETTINGS.update(
        {
            "SWAGGER_UI_DIST": "SIDECAR",
            "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
            "REDOC_DIST": "SIDECAR",
        }
    )

# --- Security posture (spec §9 / §27.1) ---
SESSION_COOKIE_HTTPONLY = True
# Templates carry the CSRF token via {% csrf_token %}; no JS needs to read the cookie.
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# Let plain-HTTP health probes through without a 301 to HTTPS (load balancers/orchestrators).
SECURE_REDIRECT_EXEMPT = [r"^healthz/?$"]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]

# --- CORS (spec §9): locked to an explicit allow-list; empty by default (same-origin). ---
if HAS_CORS:
    CORS_ALLOWED_ORIGINS = [o for o in os.environ.get("DJANGO_CORS_ALLOWED_ORIGINS", "").split(",") if o]
    CORS_ALLOW_CREDENTIALS = True
    CORS_URLS_REGEX = r"^/api/.*$"

# --- Content Security Policy (spec §9/§27.1). Self-hosted assets only. ---
if HAS_CSP:
    CONTENT_SECURITY_POLICY = {
        "DIRECTIVES": {
            "default-src": ["'self'"],
            "script-src": ["'self'"],
            "style-src": ["'self'"],
            "img-src": ["'self'", "data:", "https:"],
            "connect-src": ["'self'"],
            "frame-ancestors": ["'none'"],
            "base-uri": ["'self'"],
            "form-action": ["'self'"],
            "object-src": ["'none'"],
        }
    }

if IS_PRODUCTION:
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "0") == "1"
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# --- Observability (spec §29) ---
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "structured"},
    },
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "shop": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# --- Error tracking (spec §29): opt-in Sentry, wired only when a DSN is configured. ---
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[DjangoIntegration()],
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production" if IS_PRODUCTION else "dev"),
            release=os.environ.get("SENTRY_RELEASE") or os.environ.get("GIT_SHA"),
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
            send_default_pii=False,  # never ship customer PII to the error tracker
        )
    except ImportError:
        # sentry-sdk not installed in this environment; degrade to console logging only.
        pass
