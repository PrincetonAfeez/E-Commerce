"""Seeds demo catalog products, stock, coupons, plans, and a staff user for development"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from shop.models import (
    AccountProfile,
    Category,
    Collection,
    CouponCode,
    InventoryMovement,
    Plan,
    Product,
    ProductImage,
    ProductVariant,
    Promotion,
    StoreSettings,
    Subscription,
)


class Command(BaseCommand):
    help = "Seed a demo catalog, stock, and coupons."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow seeding in production (normally blocked).",
        )

    def handle(self, *args, **options):
        if settings.IS_PRODUCTION and not options["force"]:
            raise CommandError("seed_demo is disabled in production; pass --force to override.")
        apparel, _ = Category.objects.get_or_create(name="Apparel", slug="apparel")
        gear, _ = Category.objects.get_or_create(name="Gear", slug="gear")
        home, _ = Category.objects.get_or_create(name="Home", slug="home")
        collection, _ = Collection.objects.get_or_create(
            name="Launch Picks",
            slug="launch-picks",
            defaults={"description": "Fresh, stocked, and ready to ship.", "active": True},
        )

        products = [
            {
                "name": "Weatherproof Field Jacket",
                "slug": "weatherproof-field-jacket",
                "category": apparel,
                "description": "A compact shell with taped seams, roomy pockets, and an easy everyday fit.",
                "image": "https://images.unsplash.com/photo-1591047139829-d91aecb6caea?auto=format&fit=crop&w=1000&q=80",
                "variants": [
                    ("JACKET-OLIVE-S", "Olive / Small", {"color": "Olive", "size": "S"}, "128.00", 8),
                    ("JACKET-OLIVE-M", "Olive / Medium", {"color": "Olive", "size": "M"}, "128.00", 4),
                    ("JACKET-BLACK-M", "Black / Medium", {"color": "Black", "size": "M"}, "132.00", 2),
                ],
            },
            {
                "name": "Studio Monitor Headphones",
                "slug": "studio-monitor-headphones",
                "category": gear,
                "description": "Closed-back headphones tuned for clean mids, soft pads, and long sessions.",
                "image": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=1000&q=80",
                "variants": [
                    ("HEADPHONES-MATTE", "Matte Black", {"color": "Black"}, "89.00", 10),
                    ("HEADPHONES-SILVER", "Silver", {"color": "Silver"}, "94.00", 6),
                ],
            },
            {
                "name": "Road Tempo Sneakers",
                "slug": "road-tempo-sneakers",
                "category": apparel,
                "description": "Lightweight knit trainers with a grippy sole and cushioned heel.",
                "image": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=1000&q=80",
                "variants": [
                    ("SNEAKER-RED-9", "Red / 9", {"color": "Red", "size": "9"}, "112.00", 5),
                    ("SNEAKER-RED-10", "Red / 10", {"color": "Red", "size": "10"}, "112.00", 1),
                ],
            },
            {
                "name": "Task Lamp",
                "slug": "task-lamp",
                "category": home,
                "description": "A steel desk lamp with warm dimming and a weighted base.",
                "image": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=1000&q=80",
                "variants": [
                    ("LAMP-STEEL", "Brushed Steel", {"finish": "Steel"}, "68.00", 12),
                    ("LAMP-CLAY", "Clay", {"finish": "Clay"}, "72.00", 3),
                ],
            },
        ]

        for data in products:
            product, _ = Product.objects.update_or_create(
                slug=data["slug"],
                defaults={
                    "name": data["name"],
                    "category": data["category"],
                    "description": data["description"],
                    "status": Product.Status.ACTIVE,
                    "featured": True,
                    "seo_title": data["name"],
                    "meta_description": data["description"][:300],
                },
            )
            product.collections.add(collection)
            ProductImage.objects.update_or_create(
                product=product,
                sort_order=0,
                defaults={"image_url": data["image"], "alt_text": data["name"]},
            )
            for sku, title, attributes, price, quantity in data["variants"]:
                variant, created = ProductVariant.objects.update_or_create(
                    sku=sku,
                    defaults={
                        "product": product,
                        "title": title,
                        "attributes": attributes,
                        "price": Decimal(price),
                        "quantity": quantity,
                        "active": True,
                    },
                )
                if created:
                    InventoryMovement.objects.create(
                        variant=variant,
                        quantity_delta=quantity,
                        reason=InventoryMovement.Reason.SEED,
                        note="Demo stock seed",
                    )

        promotion, _ = Promotion.objects.update_or_create(
            name="Launch 15",
            defaults={
                "type": Promotion.Type.PERCENTAGE,
                "active": True,
                "discount_percent": Decimal("15.00"),
                "min_subtotal": Decimal("50.00"),
                "usage_limit": 100,
                "per_customer_usage_limit": 1,
                "ends_at": timezone.now() + timedelta(days=30),
            },
        )
        CouponCode.objects.update_or_create(
            normalized_code="LAUNCH15",
            defaults={"code": "LAUNCH15", "promotion": promotion, "active": True},
        )

        free_ship, _ = Promotion.objects.update_or_create(
            name="Free Ship",
            defaults={
                "type": Promotion.Type.FREE_SHIPPING,
                "active": True,
                "min_subtotal": Decimal("25.00"),
                "usage_limit": 50,
                "ends_at": timezone.now() + timedelta(days=30),
            },
        )
        CouponCode.objects.update_or_create(
            normalized_code="SHIPFREE",
            defaults={"code": "SHIPFREE", "promotion": free_ship, "active": True},
        )

        self._seed_operations_staff()
        self._seed_plans()

        self.stdout.write(self.style.SUCCESS("Demo catalog seeded. Try coupon LAUNCH15 or SHIPFREE."))

    def _seed_plans(self):
        StoreSettings.get_solo()
        starter, _ = Plan.objects.update_or_create(
            slug="starter",
            defaults={
                "name": "Starter",
                "price_monthly": Decimal("0.00"),
                "max_products": 25,
                "max_orders_per_month": 100,
                "features": ["Storefront", "Cart & checkout", "Email support"],
                "sort_order": 0,
            },
        )
        Plan.objects.update_or_create(
            slug="growth",
            defaults={
                "name": "Growth",
                "price_monthly": Decimal("49.00"),
                "max_products": 1000,
                "max_orders_per_month": 5000,
                "features": ["Everything in Starter", "Webhooks", "Gift cards", "Analytics"],
                "sort_order": 1,
            },
        )
        Plan.objects.update_or_create(
            slug="scale",
            defaults={
                "name": "Scale",
                "price_monthly": Decimal("199.00"),
                "max_products": None,
                "max_orders_per_month": None,
                "features": ["Everything in Growth", "Unlimited catalog", "Priority support"],
                "sort_order": 2,
            },
        )
        sub = Subscription.get_solo()
        if sub.plan_id is None:
            sub.plan = starter
            sub.status = Subscription.Status.ACTIVE
            sub.save(update_fields=["plan", "status", "updated_at"])

    def _seed_operations_staff(self):
        """Create a least-privilege operations group + demo staff user (spec §27.3)."""
        codenames = ["fulfill_orders", "cancel_orders", "process_refunds", "adjust_inventory"]
        perms = list(Permission.objects.filter(content_type__app_label="shop", codename__in=codenames))
        group, _ = Group.objects.get_or_create(name="Operations")
        group.permissions.set(perms)

        User = get_user_model()
        staff, created = User.objects.get_or_create(
            username="ops",
            defaults={"email": "ops@aster-commerce.test", "is_staff": True},
        )
        if created:
            staff.set_password("ops-demo-pass")
            staff.save()
        staff.groups.add(group)
        AccountProfile.objects.update_or_create(
            user=staff, defaults={"email_verified": True, "email_verified_at": timezone.now()}
        )
        # Membership in the default store so the demo operator can use /staff/.
        from shop.models import TenantMembership
        from shop.tenancy import default_tenant_id

        TenantMembership.objects.get_or_create(
            tenant_id=default_tenant_id(), user=staff, defaults={"role": TenantMembership.Role.OWNER}
        )
        self.stdout.write(self.style.SUCCESS("Operations staff user 'ops' (password 'ops-demo-pass') ready."))
