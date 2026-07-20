#!/usr/bin/env python
"""Minimal capacity smoke test for core read endpoints (GROWTH load-test evidence).

Usage:
    python scripts/load_smoke.py http://127.0.0.1:8000
"""

from __future__ import annotations

import concurrent.futures
import sys
import time
import urllib.error
import urllib.request


def _hit(url: str, timeout: float = 5.0) -> tuple[int, float]:
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            elapsed = time.perf_counter() - start
            return resp.status, elapsed
    except urllib.error.HTTPError as exc:
        elapsed = time.perf_counter() - start
        return exc.code, elapsed


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: load_smoke.py <base-url> [requests] [workers]", file=sys.stderr)
        return 2

    base = argv[0].rstrip("/")
    requests_count = int(argv[1]) if len(argv) > 1 else 50
    workers = int(argv[2]) if len(argv) > 2 else 10
    paths = ("/healthz/", "/readyz/", "/api/v1/catalog/products/")
    urls = [f"{base}{path}" for path in paths for _ in range(requests_count // len(paths) + 1)]
    urls = urls[:requests_count]

    errors = 0
    latencies: list[float] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for status, elapsed in pool.map(_hit, urls):
            latencies.append(elapsed)
            if status != 200:
                errors += 1

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0.0
    print(
        f"load_smoke: requests={len(urls)} workers={workers} errors={errors} "
        f"p95={p95 * 1000:.0f}ms max={max(latencies, default=0) * 1000:.0f}ms"
    )
    if errors:
        return 1
    if p95 > 2.0:
        print("load_smoke: p95 exceeded 2s threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
