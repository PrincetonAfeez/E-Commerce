import os

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from shop import views as shop_views
from shop.ratelimit import ratelimit

# The Django admin is the platform-operator console (unscoped across tenants), so restrict
# it to superusers — store staff use the tenant-scoped /staff/ area, never /admin/.
admin.site.has_permission = lambda request: bool(
    request.user.is_active and request.user.is_superuser
)

# Rate limits for the auth surface (DRF throttles do not cover Django auth views) — §9/§27.1.
_LOGIN_RATE = os.environ.get("THROTTLE_LOGIN", "10/min")
_RESET_RATE = os.environ.get("THROTTLE_PASSWORD_RESET", "5/min")

account_patterns = [
    path(
        "login/",
        ratelimit("login", rate=_LOGIN_RATE, field="username")(
            auth_views.LoginView.as_view(template_name="shop/account/login.html")
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("register/", ratelimit("register", rate=_LOGIN_RATE)(shop_views.register), name="register"),
    path("verify/<uidb64>/<token>/", shop_views.verify_email, name="verify_email"),
    path("verify/resend/", shop_views.resend_verification, name="resend_verification"),
    path(
        "password-reset/",
        ratelimit("password_reset", rate=_RESET_RATE)(
            auth_views.PasswordResetView.as_view(
                template_name="shop/account/password_reset_form.html",
                email_template_name="shop/account/password_reset_email.html",
                subject_template_name="shop/account/password_reset_subject.txt",
            )
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="shop/account/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="shop/account/password_reset_confirm.html"
        ),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="shop/account/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", shop_views.healthz, name="healthz"),
    path("signup/", shop_views.store_signup, name="store_signup"),
    path("unsubscribe/<str:token>/", shop_views.unsubscribe, name="unsubscribe"),
    path("legal/terms/", shop_views.legal_terms, name="terms"),
    path("legal/privacy/", shop_views.legal_privacy, name="privacy"),
    path("internal/tls-check/", shop_views.tls_check, name="tls_check"),
    path("sitemap.xml", shop_views.sitemap_xml, name="sitemap"),
    path("robots.txt", shop_views.robots_txt, name="robots"),
    path("theme.css", shop_views.theme_css, name="theme_css"),
    path("accounts/", include(account_patterns)),
    path("", include("shop.urls")),
    path("api/v1/", include("shop.api_urls")),
]

if getattr(settings, "HAS_SPECTACULAR", False):
    from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

    urlpatterns += [
        path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
        path(
            "api/v1/docs/",
            SpectacularSwaggerView.as_view(url_name="schema"),
            name="swagger-ui",
        ),
    ]
