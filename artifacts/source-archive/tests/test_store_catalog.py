"""Offline discovery-index tests; synthetic entries never certify packages."""
import base64
import json
import unittest
from unittest.mock import patch
from test_package_release import FakeApi
import store_catalog as catalog


class CatalogApi(FakeApi):
    def request(self, method, suffix, body=None, **kwargs):
        if method == 'GET' and suffix.startswith('/git/blobs/'):
            data = self.blobs[suffix.rsplit('/', 1)[1]]
            return {'encoding': 'base64', 'content': base64.b64encode(data).decode(), 'size': len(data)}
        if method == 'PATCH' and suffix == '/git/refs/heads/main':
            self.calls.append((method, suffix, body))
            assert body['force'] is False
            self.refs['refs/heads/main']['object']['sha'] = body['sha']
            return self.refs['refs/heads/main']
        return super().request(method, suffix, body, **kwargs)


class StoreCatalogTests(unittest.TestCase):
    def test_preserves_other_apps_and_replay_is_read_only(self):
        api = CatalogApi()
        catalog.publish_entry({'app_id': 'ai.vibapp.one'}, api)
        head = catalog.publish_entry({'app_id': 'ai.vibapp.two'}, api)
        tree = api.trees[api.commits[head]['tree']['sha']]
        self.assertEqual(tree['README.md'], api.readme)
        record = json.loads(api.blobs[tree['registry.json'][2]])
        self.assertEqual([app['app_id'] for app in record['apps']], ['ai.vibapp.one', 'ai.vibapp.two'])
        api.calls.clear()
        self.assertEqual(catalog.publish_entry({'app_id': 'ai.vibapp.two'}, api), head)
        self.assertTrue(all(method == 'GET' for method, _, _ in api.calls))

    def test_changed_base_tree_cannot_drop_other_files(self):
        api = CatalogApi(); api.drop_existing = True
        with self.assertRaisesRegex(catalog.SetupError, 'store_catalog_preservation'):
            catalog.publish_entry({'app_id': 'ai.vibapp.one'}, api)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in api.calls))

    def test_unverified_input_never_obtains_credentials(self):
        with patch.object(catalog, 'listing', side_effect=catalog.SetupError('store_release_binding')), \
             patch.object(catalog, 'ScopedAppApi') as api:
            with self.assertRaises(catalog.SetupError): catalog.publish('/nonexistent')
            api.assert_not_called()


if __name__ == '__main__': unittest.main()
