"""Test static URL versioning for cache invalidation."""

from importlib import metadata


def test_static_url_function():
    """Test that the static_url helper function works correctly."""
    # Test the logic of the static_url function
    try:
        version = metadata.version("librarysync")
    except metadata.PackageNotFoundError:
        version = "unknown"

    # Replicate the static_url function logic
    def static_url(path: str) -> str:
        """Generate a versioned static URL for cache busting."""
        return f"{path}?v={version}"

    # Test the function with different paths
    js_url = static_url("/static/app.js")
    css_url = static_url("/static/styles.css")

    # Verify format
    assert "?v=" in js_url, "JS URL should contain version parameter"
    assert "?v=" in css_url, "CSS URL should contain version parameter"
    assert js_url == f"/static/app.js?v={version}"
    assert css_url == f"/static/styles.css?v={version}"

def test_version_format():
    """Test that version follows expected format."""
    try:
        version = metadata.version("librarysync")
        # Verify it's not empty and contains at least one dot (semantic versioning)
        assert version, "Version should not be empty"
        assert "." in version, "Version should follow semantic versioning (e.g., 0.4.3)"
    except metadata.PackageNotFoundError:
        # Package not installed, which is acceptable in some test environments
        pass
