"""Shared repository regressions; all network/credentials are synthetic."""
import base64
import copy
import unittest
from unittest.mock import patch

import test_github_archive
import github_archive as archive
import github_build as build
import github_transport as transport
from source_catalog import publish_source_directory


class PublicArchiveTests(unittest.TestCase):
    setUp = test_github_archive.ArchiveTests.setUp

    def public_upload(self):
        with patch('source_catalog.publish_source_directory') as catalog:
            receipt = self.worker.upload_public('publisher.test', 'ai.vibapp.test', 'a' * 64, self.digest, self.root)
            catalog.assert_called_once()
            self.assertEqual(catalog.call_args.args[1], '/repos/vib-app/sources')
            return receipt

    def test_public_upload_uses_fixed_repo_and_scoped_token(self):
        self.assertEqual(self.public_upload()['repository'], 'sources')
        self.assertIs(self.api.repo['private'], False)
        tokens = [b for _, p, b in self.api.calls if p.endswith('/access_tokens')]
        self.assertEqual(tokens[-1]['repository_ids'], [12345])

    def test_does_not_convert_existing_private_repo(self):
        self.api.repo = {'id': 12345, 'full_name': 'vib-app/sources', 'private': True,
                         'owner': {'id': 6789, 'type': 'Organization'}}
        with self.assertRaisesRegex(archive.SetupError, 'archive_repository_mismatch'):
            self.public_upload()
        self.assertFalse(any(m in ('PATCH', 'PUT') for m, _, _ in self.api.calls))

    def test_shared_staging_namespaces_identical_source_across_apps(self):
        with patch('source_catalog.publish_source_directory'):
            for app in ('ai.vibapp.one', 'ai.vibapp.two'):
                self.worker.stage_source('publisher.test', app, self.digest, self.root, public_source_approved=True)
        self.assertEqual(len(self.api.refs), 2)
        self.assertTrue(all(k.startswith('tags/vibapp-source-') for k in self.api.refs))

    def test_public_source_validation_still_precedes_token(self):
        with self.assertRaisesRegex(archive.SetupError, 'source_digest_mismatch'):
            self.worker.upload_public('publisher.test', 'ai.vibapp.test', 'a' * 64, 'b' * 64, self.root)
        self.assertEqual(self.api.calls, [])

    def test_public_archive_replay_makes_no_duplicate_source_commits(self):
        receipt = self.public_upload()
        self.api.calls.clear()
        self.assertEqual(self.public_upload(), receipt)
        self.assertFalse(any(m == 'POST' and '/git/' in p for m, p, _ in self.api.calls))

    def test_build_public_consent_before_any_io(self):
        with patch.object(build, 'validate_handoff') as validate, self.assertRaisesRegex(archive.SetupError, 'approval_required'):
            build.start(self.root / 'nonexistent.json', 'publisher.test', self.root)
        validate.assert_not_called()

    def test_public_build_requires_consent_and_legacy_binding_remains_exact(self):
        state = {'source': {'repository': 'sources', 'repository_id': 12345},
                 'publisher_id': 'publisher.test', 'app_id': 'ai.vibapp.test'}
        with patch.object(build, 'ScopedAppApi') as scoped:
            with self.assertRaisesRegex(archive.SetupError, 'approval_required'):
                build.build_api(state)
            scoped.assert_not_called()
            build.build_api({**state, 'public_source_approved': True}, write=True)
            scoped.assert_called_once_with('sources', 12345,
                {'contents': 'write', 'actions': 'write', 'workflows': 'write'})
        self.assertFalse(archive.source_repository_matches('app-' + '0' * 64, 'publisher.test', 'ai.vibapp.test'))
        self.assertFalse(archive.source_repository_matches('packages', 'publisher.test', 'ai.vibapp.test'))

    def test_sources_transport_role_checks_public_org_identity(self):
        record = {'owner': {'id': 6789}}
        remote = {'id': 12345, 'full_name': 'vib-app/sources', 'private': False,
                  'owner': {'id': 6789, 'type': 'Organization'}}
        with patch.object(transport, 'CredentialStore'), patch.object(transport, 'credential_record', return_value=record), \
             patch.object(transport, 'verify_installation'), patch.object(transport.Archiver, 'token', return_value='synthetic'), \
             patch.object(transport.ScopedAppApi, 'request', return_value=remote):
            self.assertFalse(transport.ScopedAppApi('sources', 12345, {'contents': 'write'}).packages)
            remote['private'] = True
            with self.assertRaisesRegex(archive.SetupError, 'repository_scope_mismatch'):
                transport.ScopedAppApi('sources', 12345, {'contents': 'write'})


class CatalogApi:
    def __init__(self):
        self.calls = []
        self.trees, self.commits = {}, {}
        self.race = False
        self.head = self.commit({'README.md': {'path': 'README.md', 'type': 'blob', 'mode': '100644', 'sha': 'a' * 40},
            '.github/workflows/vibapp-build.yml': {'path': '.github/workflows/vibapp-build.yml',
                'type': 'blob', 'mode': '100644', 'sha': 'b' * 40}})

    def commit(self, tree):
        tree_sha = archive.blob_sha(archive.canonical(tree))
        self.trees[tree_sha] = copy.deepcopy(tree)
        sha = archive.blob_sha(archive.canonical({'tree': tree_sha}))
        self.commits[sha] = {'tree': {'sha': tree_sha}}
        return sha

    def request(self, method, path, token, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        suffix = path.removeprefix('/repos/vib-app/sources')
        if method == 'GET' and suffix == '/git/ref/heads/main':
            return {'object': {'type': 'commit', 'sha': self.head}}
        if method == 'GET' and suffix.startswith('/git/commits/'):
            return self.commits[suffix.rsplit('/', 1)[1]]
        if method == 'GET' and suffix.startswith('/git/trees/'):
            return {'truncated': False, 'tree': list(self.trees[suffix.rsplit('/', 1)[1].split('?')[0]].values())}
        if method == 'POST' and suffix == '/git/blobs':
            return {'sha': archive.blob_sha(base64.b64decode(body['content']))}
        if method == 'POST' and suffix == '/git/trees':
            tree = copy.deepcopy(self.trees[body['base_tree']])
            tree.update({e['path']: e for e in body['tree']})
            sha = archive.blob_sha(archive.canonical(tree))
            self.trees[sha] = tree
            return {'sha': sha}
        if method == 'POST' and suffix == '/git/commits':
            sha = archive.blob_sha(archive.canonical(body))
            self.commits[sha] = {'tree': {'sha': body['tree']}, 'parents': body['parents']}
            return {'sha': sha}
        if method == 'PATCH':
            assert body['force'] is False
            if self.race:
                self.race = False
                tree = copy.deepcopy(self.trees[self.commits[self.head]['tree']['sha']])
                tree['another-app.txt'] = {'path': 'another-app.txt', 'type': 'blob', 'mode': '100644', 'sha': 'e' * 40}
                self.head = self.commit(tree)
                raise archive.SetupError('archive_github_git_422')
            assert self.commits[body['sha']]['parents'] == [self.head]
            self.head = body['sha']
            return {}
        raise AssertionError((method, suffix))


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.api = CatalogApi()
        self.files = {'Cargo.toml': b'[package]', 'src/lib.rs': b'// source'}
        self.digest = archive.source_digest(self.files)

    def publish(self, app='ai.vibapp.one'):
        return publish_source_directory(self.api, '/repos/vib-app/sources', 'synthetic',
                                         'publisher.test', app, self.digest, self.files)

    def tree(self):
        return self.api.trees[self.api.commits[self.api.head]['tree']['sha']]

    def test_two_apps_keep_separate_directories_and_existing_workflow(self):
        self.publish()
        self.publish('ai.vibapp.two')
        self.assertEqual(len(self.tree()), 8)  # 2 originals + 2 * (2 source files + marker).
        self.assertEqual(self.tree()['.github/workflows/vibapp-build.yml']['sha'], 'b' * 40)

    def test_replay_is_read_only(self):
        self.publish()
        self.api.calls.clear()
        self.publish()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_concurrent_app_commit_is_preserved(self):
        self.api.race = True
        self.publish()
        self.assertIn('another-app.txt', self.tree())
        patches = [b for m, _, b in self.api.calls if m == 'PATCH']
        self.assertEqual(len(patches), 2)
        self.assertTrue(all(b['force'] is False for b in patches))

    def test_existing_immutable_source_cannot_be_overwritten(self):
        self.publish()
        target = next(k for k in self.tree() if k.endswith('/src/lib.rs'))
        self.tree()[target]['sha'] = '0' * 40
        self.api.calls.clear()
        with self.assertRaisesRegex(archive.SetupError, 'source_catalog_conflict'):
            self.publish()
        self.assertTrue(all(method == 'GET' for method, _, _ in self.api.calls))

    def test_non_directory_parent_is_not_replaced(self):
        self.tree()['apps'] = {'path': 'apps', 'type': 'blob', 'mode': '100644', 'sha': 'e' * 40}
        with self.assertRaisesRegex(archive.SetupError, 'source_catalog_path_conflict'):
            self.publish()


if __name__ == '__main__':
    unittest.main()
