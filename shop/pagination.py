# Cursor pagination helper ordering API lists by newest created_at first
from rest_framework.pagination import CursorPagination


class CreatedAtCursorPagination(CursorPagination):
    ordering = "-created_at"
