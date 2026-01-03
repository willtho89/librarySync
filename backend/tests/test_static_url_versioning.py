"""Test static URL versioning for cache invalidation."""

from librarysync.main import get_app_version, make_static_url


def test_static_url_function():
    """Test that the static_url helper function works correctly."""
    # Get the version using the actual function from main
    version = get_app_version()

    # Create the static_url function using the actual factory from main
    static_url = make_static_url(version)

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
    version = get_app_version()
    if version != "unknown":
        # Verify it's not empty and contains at least one dot (semantic versioning)
        assert version, "Version should not be empty"
        assert "." in version, "Version should follow semantic versioning (e.g., 0.4.3)"
