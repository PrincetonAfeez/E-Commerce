# Custom domains & automatic TLS (multi-tenant)

Tenant routing is host-based: `shop.middleware.TenantMiddleware` resolves the active
tenant from the request `Host` header (custom domain → subdomain → default tenant) and
scopes every ORM query to it. To let a merchant bring their own domain with HTTPS, you
only need a reverse proxy that terminates TLS and forwards to Gunicorn — the app already
knows how to route once the request arrives.

## 1. Merchant setup

1. In Django admin, set the tenant's **Primary domain** (e.g. `shop.acme.com`).
2. The merchant points that hostname at your load balancer / proxy via DNS
   (`CNAME` to your platform host, or `A`/`AAAA` records).
3. Add the domain to `DJANGO_ALLOWED_HOSTS` (or use a wildcard/subdomain policy).
4. Ensure the domain is trusted for HTTPS POST: add it to `DJANGO_CSRF_TRUSTED_ORIGINS`
   (e.g. `https://shop.acme.com`) or rely on startup extension from active tenant
   `primary_domain` values (see `shop/csrf_origins.py`).

## 2. Automatic per-tenant certificates (Caddy)

[Caddy](https://caddyserver.com) issues and renews Let's Encrypt certificates
**on demand** for any host it serves. To avoid minting certs for arbitrary domains
pointed at you, Caddy first asks the app whether a host is a known tenant — that's what
the `/internal/tls-check/` endpoint is for (returns `200` only for active tenant domains).

`Caddyfile`:

```
{
    on_demand_tls {
        ask http://web:8000/internal/tls-check/
    }
}

# Storefront: any host, TLS issued on demand after the ask check passes.
https:// {
    tls {
        on_demand
    }
    reverse_proxy web:8000
}
```

Caddy calls `GET http://web:8000/internal/tls-check/?domain=<host>`; the app returns
`200` if a `Tenant` with that `primary_domain` is active, else `404`, so certificates are
only provisioned for legitimate tenant domains. Renewal is automatic.

## 3. Behind another proxy / PaaS

If you terminate TLS elsewhere (ALB, Cloudflare, nginx + certbot, a PaaS with managed
certs), just forward the original `Host` header to Gunicorn and set
`SECURE_PROXY_SSL_HEADER` (already configured in production settings). The tenant
middleware does the rest. The `/internal/tls-check/` endpoint is only needed for
on-demand certificate issuance.

## Notes

- `/internal/tls-check/` should not be exposed publicly on the storefront domains; scope
  it to the proxy network, gate it with `TLS_CHECK_SECRET`, or restrict by source IP at the proxy.
- The default tenant handles the platform's own apex domain and any unmatched host.
