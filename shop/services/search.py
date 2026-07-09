# Product full-text search on Postgres with icontains fallback and category facets
from __future__ import annotations

from django.db import connection
from django.db.models import Count, Q


def search_products(queryset, query: str):
    """Full-text search on PostgreSQL (with SKU fallback), icontains elsewhere.

    Kept vendor-aware so tests on SQLite exercise the fallback while production uses FTS.
    """
    query = (query or "").strip()
    if not query:
        return queryset
    if connection.vendor == "postgresql":
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        vector = SearchVector("name", weight="A") + SearchVector("description", weight="B")
        search = SearchQuery(query, search_type="websearch")
        ranked = queryset.annotate(rank=SearchRank(vector, search))
        return (
            ranked.filter(Q(rank__gt=0) | Q(variants__sku__icontains=query))
            .order_by("-rank", "name")
            .distinct()
        )
    return queryset.filter(
        Q(name__icontains=query)
        | Q(description__icontains=query)
        | Q(variants__sku__icontains=query)
    ).distinct()


def category_facets(queryset):
    """Category facet counts for the current (filtered) product queryset."""
    return list(
        queryset.filter(category__isnull=False)
        .values("category__slug", "category__name")
        .annotate(count=Count("id", distinct=True))
        .order_by("-count", "category__name")
    )
