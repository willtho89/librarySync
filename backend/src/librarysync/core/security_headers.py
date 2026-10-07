"""Response security headers, including a Content-Security-Policy for pages."""

import base64
import hashlib
import re
from pathlib import Path

from fastapi import FastAPI, Request

from librarysync.config import settings

_INLINE_SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.DOTALL)


def inline_script_hashes(template_paths: list[Path]) -> list[str]:
    """CSP hashes of the inline <script> blocks the templates ship (e.g. the pre-paint theme script)."""
    hashes: list[str] = []
    for path in template_paths:
        for body in _INLINE_SCRIPT_RE.findall(path.read_text(encoding="utf-8")):
            digest = hashlib.sha256(body.encode("utf-8")).digest()
            hashes.append(f"'sha256-{base64.b64encode(digest).decode('ascii')}'")
    return hashes


def build_content_security_policy(script_hashes: list[str]) -> str:
    directives = {
        "default-src": ["'self'"],
        "script-src": ["'self'", *script_hashes],
        # Components set element.style and templates carry a small <style> block.
        "style-src": ["'self'", "'unsafe-inline'"],
        # Posters and artwork come from many provider CDNs.
        "img-src": ["'self'", "data:", "blob:", "https:"],
        "font-src": ["'self'", "data:"],
        "connect-src": ["'self'"],
        "manifest-src": ["'self'"],
        "worker-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
    }
    return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


def install_security_headers(app: FastAPI, template_paths: list[Path]) -> None:
    csp = build_content_security_policy(inline_script_hashes(template_paths))
    hsts = bool(settings.base_url and settings.base_url.startswith("https"))

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("X-Frame-Options", "DENY")
        if hsts:
            headers.setdefault("Strict-Transport-Security", "max-age=15552000")
        if response.headers.get("content-type", "").startswith("text/html"):
            headers.setdefault("Content-Security-Policy", csp)
        return response
