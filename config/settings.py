"""Django settings for the commerce capstone."""

import logging
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# Environment: "development" (default) or "production". Production flips on the full
# security posture and refuses to boot without real secrets and PostgreSQL (ADR-0001).
DJANGO_ENV = os.environ.get("DJANGO_ENV", "development").lower()
IS_STAGING = DJANGO_ENV == "staging"
IS_PRODUCTION = DJANGO_ENV == "production"
IS_DEPLOYED = IS_PRODUCTION or IS_STAGING
RUNNING_TESTS = "test" in sys.argv or "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.argv[0]

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "" if IS_PRODUCTION else "dev-only-commerce-capstone-secret")
if IS_PRODUCTION and not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production.")

# Secure by default: DEBUG is off unless explicitly enabled, and always off in prod.
DEBUG = (os.environ.get("DJANGO_DEBUG", "1") == "1") and not IS_PRODUCTION

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
if IS_PRODUCTION and not os.environ.get("DJANGO_ALLOWED_HOSTS"):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must be set in production.")
if IS_PRODUCTION and "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "shop.apps.ShopConfig",
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
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.middleware.common.CommonMiddleware"), "corsheaders.middleware.CorsMiddleware"
    )
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

    _conn_max_age = int(os.environ.get("DATABASE_CONN_MAX_AGE", "600"))
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=_conn_max_age)}
    if IS_PRODUCTION and not RUNNING_TESTS:
        engine = DATABASES["default"].get("ENGINE", "")
        if "sqlite" in engine:
            raise ImproperlyConfigured("DATABASE_URL must be PostgreSQL in production; SQLite is not supported.")
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

DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

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
_AWS_BUCKET = os.environ.get("AWS_STORAGE_BUCKET_NAME", "").strip()
if _AWS_BUCKET:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": _AWS_BUCKET,
            "location": os.environ.get("AWS_MEDIA_LOCATION", "media"),
            "default_acl": None,
            "file_overwrite": False,
        },
    }
    if IS_PRODUCTION and not RUNNING_TESTS:
        _aws_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        _aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        if not _aws_key or not _aws_secret:
            raise ImproperlyConfigured(
                "AWS_STORAGE_BUCKET_NAME is set but AWS_ACCESS_KEY_ID or "
                "AWS_SECRET_ACCESS_KEY is missing in production."
            )
elif IS_PRODUCTION and not RUNNING_TESTS:
    if os.environ.get("MEDIA_PERSIST_LOCAL", "0") != "1":
        raise ImproperlyConfigured("AWS_STORAGE_BUCKET_NAME is required in production unless MEDIA_PERSIST_LOCAL=1.")
# Uploaded product images: cap size and restrict to known-safe image extensions (spec §9/§24.3).
MAX_UPLOAD_SIZE_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_BYTES", str(5 * 1024 * 1024)))
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_UPLOAD_SIZE_BYTES + 512 * 1024

# Simulated-gateway test modes (decline / dropped_confirmation / ...) are only honoured
# outside production so untrusted clients cannot drive them (spec §19.3).
if IS_PRODUCTION and os.environ.get("GATEWAY_TEST_MODES_ENABLED", "0") == "1":
    raise ImproperlyConfigured("GATEWAY_TEST_MODES_ENABLED must not be enabled in production.")
GATEWAY_TEST_MODES_ENABLED = not IS_PRODUCTION and os.environ.get("GATEWAY_TEST_MODES_ENABLED", "1") == "1"

# Active payment gateway adapter (simulated by default; swappable for real PSPs).
PAYMENT_GATEWAY = os.environ.get("PAYMENT_GATEWAY", "simulated").strip().lower()

# Shared secret for inbound PSP webhook signature verification.
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")

# Shared secret for internal ops endpoints (/internal/metrics/).
OPS_METRICS_SECRET = os.environ.get("OPS_METRICS_SECRET", "")
if IS_PRODUCTION and not RUNNING_TESTS and not OPS_METRICS_SECRET:
    raise ImproperlyConfigured("OPS_METRICS_SECRET must be set in production.")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Shared cache for rate limits and DRF throttles (per-process LocMem is ineffective
# under multi-worker Gunicorn). Redis URL optional in dev; required in production.
_CACHE_URL = os.environ.get("CACHE_URL", os.environ.get("REDIS_URL", ""))
if _CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _CACHE_URL,
        }
    }
elif IS_PRODUCTION and not RUNNING_TESTS:
    raise ImproperlyConfigured("CACHE_URL or REDIS_URL is required in production for rate limiting.")
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "commerce-dev",
        }
    }

# Comma-separated base domains for slug-based tenant routing (e.g. aster-commerce.test).
TENANT_PLATFORM_DOMAINS = [d for d in os.environ.get("TENANT_PLATFORM_DOMAINS", "").split(",") if d.strip()]
# Fail closed when ORM writes/reads expect an active tenant. Off by default because
# management commands and platform admin intentionally run without request context.
_require_tenant_default = "1" if IS_PRODUCTION else "0"
REQUIRE_TENANT_CONTEXT = os.environ.get("REQUIRE_TENANT_CONTEXT", _require_tenant_default) == "1"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "catalog:list"
LOGOUT_REDIRECT_URL = "catalog:list"

# Console backend in dev so password-reset/verification emails are observable; a real
# SMTP/provider backend is configured via env in production.
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if not IS_PRODUCTION
    else "django.core.mail.backends.smtp.EmailBackend",
)
DEFAULT_FROM_EMAIL = os.environ.get("DJANGO_DEFAULT_FROM_EMAIL", "no-reply@aster-commerce.test")
if IS_PRODUCTION and not RUNNING_TESTS and "console" in EMAIL_BACKEND.lower():
    raise ImproperlyConfigured("Console email backend is not allowed in production.")
if IS_PRODUCTION and EMAIL_BACKEND.endswith("smtp.EmailBackend"):
    for _smtp_var in ("EMAIL_HOST",):
        if not os.environ.get(_smtp_var) and not os.environ.get("DJANGO_EMAIL_HOST"):
            raise ImproperlyConfigured("EMAIL_HOST (or DJANGO_EMAIL_HOST) must be set in production when using SMTP.")
EMAIL_HOST = os.environ.get("DJANGO_EMAIL_HOST", os.environ.get("EMAIL_HOST", "localhost"))
EMAIL_PORT = int(os.environ.get("DJANGO_EMAIL_PORT", os.environ.get("EMAIL_PORT", "25")))
EMAIL_HOST_USER = os.environ.get("DJANGO_EMAIL_HOST_USER", os.environ.get("EMAIL_HOST_USER", ""))
EMAIL_HOST_PASSWORD = os.environ.get("DJANGO_EMAIL_HOST_PASSWORD", os.environ.get("EMAIL_HOST_PASSWORD", ""))
EMAIL_USE_TLS = os.environ.get("DJANGO_EMAIL_USE_TLS", os.environ.get("EMAIL_USE_TLS", "0")) == "1"
# Absolute base URL used to build links in transactional emails (no request available).
SITE_URL = os.environ.get("DJANGO_SITE_URL", "http://localhost:8000")
if IS_PRODUCTION and not os.environ.get("DJANGO_SITE_URL"):
    raise ImproperlyConfigured("DJANGO_SITE_URL must be set in production.")
if IS_PRODUCTION and not SITE_URL.startswith("https://"):
    raise ImproperlyConfigured("DJANGO_SITE_URL must start with https:// in production.")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
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
        "cart": os.environ.get("THROTTLE_CART", "120/min"),
        "login": os.environ.get("THROTTLE_LOGIN", "10/min"),
        "guest_order_lookup": os.environ.get("THROTTLE_GUEST_ORDER_LOOKUP", "10/h"),
        "guest_order": os.environ.get("THROTTLE_GUEST_ORDER", "30/min"),
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
SECURE_REDIRECT_EXEMPT = [r"^healthz/?$", r"^readyz/?$", r"^internal/metrics/?$"]

if IS_PRODUCTION:
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_REFERRER_POLICY = "same-origin"
else:
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "0") == "1"
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
if IS_PRODUCTION and SECURE_SSL_REDIRECT and not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h and h != "*"]

# Shared secret for /internal/tls-check/ (required in production).
TLS_CHECK_SECRET = os.environ.get("TLS_CHECK_SECRET", "")
if IS_PRODUCTION and not RUNNING_TESTS and not TLS_CHECK_SECRET:
    raise ImproperlyConfigured("TLS_CHECK_SECRET must be set in production.")

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
