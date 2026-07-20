"""Django AppConfig registering the shop application"""
from django.apps import AppConfig


class ShopConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "shop"

    def ready(self) -> None:
        from django.conf import settings
        from django.db.utils import OperationalError

        if getattr(settings, "RUNNING_TESTS", False):
            return
        try:
            from shop.csrf_origins import extend_csrf_trusted_origins

            extend_csrf_trusted_origins()
        except OperationalError:
            pass
