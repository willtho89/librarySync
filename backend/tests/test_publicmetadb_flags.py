import unittest

from librarysync.core.publicmetadb import (
    is_publicmetadb_metadata_enabled,
    is_publicmetadb_sync_enabled,
    set_publicmetadb_metadata_enabled,
    set_publicmetadb_sync_enabled,
)


class TestPublicMetaDbFlags(unittest.TestCase):
    def test_metadata_falls_back_to_sync_when_missing(self) -> None:
        self.assertTrue(is_publicmetadb_metadata_enabled({"sync_enabled": True}))
        self.assertFalse(is_publicmetadb_metadata_enabled({"sync_enabled": False}))

    def test_sync_falls_back_to_metadata_when_missing(self) -> None:
        self.assertTrue(is_publicmetadb_sync_enabled({"metadata_enabled": True}))
        self.assertFalse(is_publicmetadb_sync_enabled({"metadata_enabled": False}))

    def test_legacy_enabled_takes_precedence(self) -> None:
        config = {"enabled": True, "sync_enabled": False}
        self.assertTrue(is_publicmetadb_metadata_enabled(config))

        config = {"enabled": False, "metadata_enabled": True}
        self.assertFalse(is_publicmetadb_sync_enabled(config))

    def test_setters_keep_separate_flags(self) -> None:
        config: dict[str, object] = {}
        set_publicmetadb_metadata_enabled(config, True)
        set_publicmetadb_sync_enabled(config, False)
        self.assertTrue(is_publicmetadb_metadata_enabled(config))
        self.assertFalse(is_publicmetadb_sync_enabled(config))


if __name__ == "__main__":
    unittest.main()
