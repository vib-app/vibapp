import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from github_archive import SetupError, canonical, sha256, source_digest
from github_smoke import create
from import_legacy_source import migrate
from github_build import validate_handoff


class LegacySourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.handoff = create(self.root / 'original')
        self.doc = json.loads(self.handoff.read_bytes())
        self.doc['schema_version'] = 'vibapp.codeagent-source-handoff.experimental-v1'
        del self.doc['target']['required_capabilities']
        cargo = self.handoff.parent / 'source/Cargo.toml'
        cargo.write_text(cargo.read_text().replace('ai_vibapp_archive-check', 'legacy-clock'))
        files = {r['path']: (self.handoff.parent / 'source' / r['path']).read_bytes() for r in self.doc['files']}
        for row in self.doc['files']:
            row.update(sha256=sha256(files[row['path']]), size_bytes=len(files[row['path']]))
        self.doc['source_tree_sha256'] = source_digest(files)
        self.handoff.write_bytes(canonical(self.doc))

    def test_migration_preserves_original_and_provider_facts(self):
        before = self.handoff.read_bytes()
        rust = (self.handoff.parent / 'source/src/lib.rs').read_bytes()
        receipt = migrate(self.handoff, self.root / 'converted')
        after = validate_handoff(self.root / 'converted/handoff.json').document
        self.assertEqual(self.handoff.read_bytes(), before)
        self.assertEqual((self.root / 'converted/source/src/lib.rs').read_bytes(), rust)
        self.assertEqual(after['provider_execution'], self.doc['provider_execution'])
        self.assertEqual(after['target']['required_capabilities'], self.doc['target']['required_imports'])
        self.assertNotEqual(receipt['original_source_sha256'], receipt['migrated_source_sha256'])
        self.assertEqual(receipt['state'], 'source-validated-not-built')

    def test_modified_source_rejected_before_output(self):
        (self.handoff.parent / 'source/src/lib.rs').write_text('changed')
        with self.assertRaisesRegex(SetupError, 'source_digest_mismatch'):
            migrate(self.handoff, self.root / 'converted')
        self.assertFalse((self.root / 'converted').exists())

    def test_existing_output_not_overwritten(self):
        output = self.root / 'converted'
        output.mkdir()
        (output / 'keep.txt').write_text('keep')
        with self.assertRaisesRegex(SetupError, 'legacy_output_exists'):
            migrate(self.handoff, output)
        self.assertEqual((output / 'keep.txt').read_text(), 'keep')

    def test_modern_handoff_not_silently_reinterpreted(self):
        self.doc['schema_version'] = 'vibapp.codeagent-source-handoff.experimental-v2'
        self.handoff.write_bytes(canonical(self.doc))
        with self.assertRaisesRegex(SetupError, 'legacy_handoff_version'):
            migrate(self.handoff, self.root / 'converted')
