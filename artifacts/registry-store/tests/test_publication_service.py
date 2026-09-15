import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location("publication_service", Path(__file__).parents[1] / "publication_service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.db, self.archive_db, self.archiver = MagicMock(), MagicMock(), MagicMock()
        self.cursor = self.db.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = ("app.test", "a" * 64)
        self.archiver.upload_public.return_value = {"organization": "vib-app"}

    def publish(self):
        return service.publish_with_source_archive(self.db, self.archive_db, self.archiver,
            publisher_id="publisher.test", package_digest="b" * 64, source_root=Path("/trusted/source"),
            moderation_evidence="c" * 64, public_source_approved=True)

    def test_upload_failure_never_calls_publish(self):
        self.archiver.upload_public.side_effect = RuntimeError("upload failed")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.archive_db.cursor.assert_not_called()

    def test_wrong_owner_does_not_upload(self):
        self.cursor.fetchone.return_value = None
        with self.assertRaises(PermissionError):
            self.publish()
        self.archiver.upload_public.assert_not_called()

    def test_source_lookup_failure_does_not_upload(self):
        self.cursor.execute.side_effect = RuntimeError("database unavailable")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.db.rollback.assert_called_once()
        self.archiver.upload_public.assert_not_called()

    def test_receipt_failure_does_not_publish(self):
        self.archive_db.commit.side_effect = RuntimeError("database failed")
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.archive_db.rollback.assert_called_once()

    def test_publication_follows_archive_and_uses_trusted_identity(self):
        self.assertEqual(self.publish()["publication_state"], "published")
        self.archiver.upload_public.assert_called_once_with("publisher.test", "app.test", "b" * 64,
                                                    "a" * 64, Path("/trusted/source"))
        self.assertIn("publish_release_v1", self.cursor.execute.call_args.args[0])
        self.archive_db.commit.assert_called_once()

    def test_publication_failure_rolls_back_without_discarding_archive(self):
        self.db.commit.side_effect = [None, RuntimeError("publication unavailable")]
        with self.assertRaises(RuntimeError):
            self.publish()
        self.db.rollback.assert_called_once()
        self.archive_db.commit.assert_called_once()
        self.archive_db.rollback.assert_not_called()

    def test_public_source_consent_required_before_database_or_upload(self):
        with self.assertRaisesRegex(PermissionError, "public-source-approval-required"):
            service.publish_with_source_archive(self.db, self.archive_db, self.archiver,
                publisher_id="publisher.test", package_digest="b" * 64,
                source_root=Path("/trusted/source"), moderation_evidence="c" * 64)
        self.db.cursor.assert_not_called()
        self.archiver.upload_public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
