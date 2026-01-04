"""Tests for anime detection and classification."""

from librarysync.core.anime import get_anime_provider_ids, is_anime
from librarysync.db.models import MediaItem


def test_is_anime_by_media_type():
    """Test anime detection via media_type field."""
    item = MediaItem(
        id="test-1",
        media_type="anime",
        title="Test Anime",
    )
    assert is_anime(item) is True


def test_is_anime_by_kitsu_id():
    """Test anime detection via kitsu_id presence."""
    item = MediaItem(
        id="test-2",
        media_type="tv",
        title="Test Show",
        kitsu_id="12345",
    )
    assert is_anime(item) is True


def test_is_anime_by_myanimelist_id():
    """Test anime detection via myanimelist_id presence."""
    item = MediaItem(
        id="test-3",
        media_type="tv",
        title="Test Show",
        myanimelist_id="54321",
    )
    assert is_anime(item) is True


def test_is_anime_by_anilist_id():
    """Test anime detection via anilist_id presence."""
    item = MediaItem(
        id="test-4",
        media_type="tv",
        title="Test Show",
        anilist_id="99999",
    )
    assert is_anime(item) is True


def test_is_anime_by_raw_metadata():
    """Test anime detection via raw metadata type field."""
    item = MediaItem(
        id="test-5",
        media_type="tv",
        title="Test Show",
        raw={"type": "anime"},
    )
    assert is_anime(item) is True


def test_is_not_anime():
    """Test non-anime content is correctly identified."""
    item = MediaItem(
        id="test-6",
        media_type="movie",
        title="Test Movie",
        imdb_id="tt1234567",
    )
    assert is_anime(item) is False


def test_is_anime_with_parameters():
    """Test anime detection using direct parameters."""
    assert is_anime(media_type="anime") is True
    assert is_anime(kitsu_id="12345") is True
    assert is_anime(myanimelist_id="54321") is True
    assert is_anime(anilist_id="99999") is True
    assert is_anime(raw={"type": "anime"}) is True
    assert is_anime(media_type="movie") is False


def test_get_anime_provider_ids():
    """Test extraction of anime-specific provider IDs."""
    item = MediaItem(
        id="test-7",
        media_type="anime",
        title="Test Anime",
        kitsu_id="123",
        myanimelist_id="456",
        anilist_id="789",
        imdb_id="tt0000000",
        tmdb_id="999",
    )
    
    ids = get_anime_provider_ids(item)
    
    assert ids == {
        "kitsu": "123",
        "myanimelist": "456",
        "anilist": "789",
    }
    # Verify IMDb and TMDB are not included
    assert "imdb" not in ids
    assert "tmdb" not in ids


def test_get_anime_provider_ids_empty():
    """Test extraction returns empty dict when no anime IDs present."""
    item = MediaItem(
        id="test-8",
        media_type="movie",
        title="Test Movie",
        imdb_id="tt0000000",
        tmdb_id="999",
    )
    
    ids = get_anime_provider_ids(item)
    assert ids == {}
