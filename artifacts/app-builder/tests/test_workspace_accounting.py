import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import PipelineError, bounded_tree_size


class WorkspaceAccountingTests(unittest.TestCase):
    def test_directory_disappearing_between_stat_and_scan_is_not_a_build_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / 'rustc-temporary'
            scratch.mkdir()
            (root / 'retained').write_bytes(b'1234')
            original = os.scandir

            def scan(path):
                if Path(path) == scratch:
                    scratch.rmdir()
                return original(path)

            with patch('common.os.scandir', side_effect=scan):
                self.assertEqual(bounded_tree_size(root, 4), 4)

    def test_scan_permission_errors_are_not_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('common.os.scandir', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    bounded_tree_size(Path(directory), 4)

    def test_symlinks_and_byte_overflow_still_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'file').write_bytes(b'12345')
            with self.assertRaises(PipelineError):
                bounded_tree_size(root, 4)
            (root / 'link').symlink_to(root / 'file')
            with self.assertRaises(PipelineError):
                bounded_tree_size(root, 100)
