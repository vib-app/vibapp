"""Real local verifier regression tests; only remote transport is mocked."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parents[1]
REPO = BASE.parents[1]
sys.path[:0] = [str(BASE), str(REPO / 'artifacts/app-builder'),
                str(REPO / 'artifacts/app-builder/tests')]
import github_build as build
from github_archive import sha256, SetupError
from app_builder import SafeFixtureRunner, build_handoff
from verifier import verify_and_promote
from test_app_builder import make_handoff, COMPONENT


class ResumeVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vibapp-resume-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / 'build'
        self.out.mkdir(mode=0o700)
        handoff = make_handoff(self.root / 'input')
        document = json.loads(handoff.read_bytes())
        self.state = {'state':'cloud-compiled-awaiting-verifier', 'run_id':123,
            'handoff':str(handoff), 'handoff_sha256':sha256(handoff.read_bytes()),
            'publisher_id':'ai.vibapp.local', 'app_id':document['package_intent']['app_id'],
            'source':{'source_digest_sha256':document['source_tree_sha256']}}
        for name, value in [('build_api',MagicMock()), ('get_run',{'status':'completed','conclusion':'success'}),
                            ('download',({},{}))]:
            p = patch.object(build,name,return_value=value)
            p.start()
            self.addCleanup(p.stop)
        pipeline = self.out / 'pipeline'
        receipt = build_handoff(handoff,pipeline,SafeFixtureRunner(COMPONENT.read_bytes()))
        self.candidate = verify_and_promote(receipt,pipeline)
        self.before = self.candidate.read_bytes()

    def run_resume(self):
        build.save(self.out/'state.json',self.state)
        return build.resume(self.out)

    def test_promotion_before_state_save(self):
        result = self.run_resume()
        self.assertEqual(result['state'],'independently-verified')
        self.assertEqual(Path(result['candidate']).read_bytes(),self.before)

    def test_deleted_candidate_is_fully_reverified_and_reconstructed(self):
        self.candidate.parent.rename(self.root/'removed-candidate')
        self.state.update(state='independently-verified',candidate=str(self.candidate))
        result = self.run_resume()
        self.assertEqual(json.loads(Path(result['candidate']).read_bytes())['component'],
                         json.loads(self.before)['component'])

    def test_tampered_existing_record_rejected(self):
        record = json.loads(self.before)
        record['source_tree_sha256'] = '0'*64
        self.candidate.chmod(0o600)
        self.candidate.write_text(json.dumps(record))
        self.state.update(state='independently-verified',candidate=str(self.candidate))
        with self.assertRaisesRegex(SetupError,'independent_verifier_failed'):
            self.run_resume()

    def test_tampered_existing_component_rejected(self):
        component = self.candidate.parent/'package/component.wasm'
        component.chmod(0o600)
        component.write_bytes(b'corrupt')
        with self.assertRaisesRegex(SetupError,'independent_verifier_failed'):
            self.run_resume()
