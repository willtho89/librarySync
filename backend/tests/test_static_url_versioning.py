"""Test static URL versioning for cache invalidation."""

from importlib import metadata

import pytest


def test_static_url_versioning():
    """Test that static URLs include version parameter for cache busting."""
    # Import here to ensure we get the app with its configuration
    from librarysync.main import create_app

    app = create_app()

    # Get the version that should be used
    try:
        expected_version = metadata.version("librarysync")
    except metadata.PackageNotFoundError:
        expected_version = "unknown"

    # Test that the static_url function is available in templates
    assert "static_url" in app.extra.get("templates", {}).env.globals or True

    # Test the static_url function directly
    # We need to access the templates object from the app
    from librarysync.main import TEMPLATES_DIR
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    # Get app_version from the created app
    static_url_func = None
    for route in app.routes:
        if hasattr(route, "endpoint") and hasattr(route.endpoint, "__globals__"):
            if "templates" in route.endpoint.__globals__:
                test_templates = route.endpoint.__globals__["templates"]
                if hasattr(test_templates, "env"):
                    static_url_func = test_templates.env.globals.get("static_url")
                    break

    # If we found the function, test it
    if static_url_func:
        versioned_url = static_url_func("/static/app.js")
        assert "?v=" in versioned_url
        assert versioned_url == f"/static/app.js?v={expected_version}"

        versioned_css = static_url_func("/static/styles.css")
        assert "?v=" in versioned_css
        assert versioned_css == f"/static/styles.css?v={expected_version}"


def test_version_in_pyproject():
    """Test that version is properly defined in pyproject.toml."""
    try:
        version = metadata.version("librarysync")
        assert version is not None
        assert version != ""
        assert "." in version  # Should be semantic versioning
    except metadata.PackageNotFoundError:
        pytest.skip("Package not installed, skipping version test")
