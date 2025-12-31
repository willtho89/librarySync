from librarysync.connectors.services.stremio_watched_bitfield import (
    new_bitfield8,
    watched_bitfield_from_array,
    watched_bitfield_from_string,
)


def test_parse_and_modify() -> None:
    videos = [
        "tt2934286:1:1",
        "tt2934286:1:2",
        "tt2934286:1:3",
        "tt2934286:1:4",
        "tt2934286:1:5",
        "tt2934286:1:6",
        "tt2934286:1:7",
        "tt2934286:1:8",
        "tt2934286:1:9",
    ]
    watched = "tt2934286:1:5:5:eJyTZwAAAEAAIA=="

    wb = watched_bitfield_from_string(watched, videos)
    assert wb.get_video("tt2934286:1:5") is True
    assert wb.get_video("tt2934286:1:6") is False

    serialized = wb.to_string()
    roundtrip = watched_bitfield_from_string(serialized, videos)
    assert roundtrip.get_video("tt2934286:1:5") is True
    assert roundtrip.get_video("tt2934286:1:6") is False

    wb.set_video("tt2934286:1:6", True)
    assert wb.get_video("tt2934286:1:6") is True


def test_construct_from_array() -> None:
    video_ids = [f"tt2934286:1:{idx + 1}" for idx in range(50)]
    wb = watched_bitfield_from_array([False] * len(video_ids), video_ids)
    for idx, video_id in enumerate(video_ids):
        assert wb.get(idx) is False
        assert wb.get_video(video_id) is False
    for idx in range(len(video_ids)):
        wb.set(idx, idx % 2 == 0)
    serialized = wb.to_string()
    roundtrip = watched_bitfield_from_string(serialized, video_ids)
    for idx, video_id in enumerate(video_ids):
        assert roundtrip.get(idx) is (idx % 2 == 0)
        assert roundtrip.get_video(video_id) is (idx % 2 == 0)


def test_to_string_empty() -> None:
    watched = watched_bitfield_from_array([], [])
    serialized = watched.to_string()
    assert serialized.startswith("undefined:1:")


def test_deserialize_empty() -> None:
    watched = watched_bitfield_from_string("undefined:1:eJwDAAAAAAE=", [])
    expected = new_bitfield8(0)
    assert watched.bitfield.Length == expected.Length
    assert watched.video_ids == []
