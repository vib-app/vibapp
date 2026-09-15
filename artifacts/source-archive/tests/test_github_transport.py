import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_transport as t
import github_app_setup as setup


class ScopedTransportTests(unittest.TestCase):
    def test_host_and_credential_redirect_denial(self):
        for url in ('http://raw.githubusercontent.com/test', 'https://github.com.evil.test/a',
                    'https://token@github.com/a', 'https://127.0.0.1/a', 'https://github.com:444/a'):
            with self.subTest(url=url), self.assertRaises(setup.SetupError):
                t.public_download(url)

    def test_public_download_rejects_credential_headers(self):
        with self.assertRaises(setup.SetupError):
            t.public_download('https://github.com/vib-app/packages/a', headers={'Authorization': 'synthetic'})

    def test_endpoint_roles_and_no_delete(self):
        self.assertFalse(t.endpoint_allowed('DELETE', '/releases/1', True))
        self.assertFalse(t.endpoint_allowed('POST', '/actions/workflows/vibapp-build.yml/dispatches', True))
        self.assertFalse(t.endpoint_allowed('POST', '/releases', False))
        self.assertFalse(t.endpoint_allowed('PUT', '/contents/.github/workflows/evil.yml', True))
        self.assertFalse(t.endpoint_allowed('GET', '/git/trees/../../other', True))
        self.assertTrue(t.endpoint_allowed('POST', '/releases', True))

    def test_scope_rejection_precedes_credentials(self):
        for repo, number, perms in [('other', 1, {'contents': 'write'}),
                                    ('packages', 1, {'contents': 'write'}),
                                    ('packages', t.PACKAGES_REPOSITORY_ID, {'actions':'write'})]:
            with patch.object(t, 'CredentialStore') as store, self.assertRaises(setup.SetupError):
                t.ScopedAppApi(repo, number, perms)
            store.assert_not_called()

    def test_authenticated_redirect_discards_token(self):
        api = object.__new__(t.ScopedAppApi)
        api.calls = 0
        import time
        api.deadline = time.monotonic() + 60
        opener = MagicMock()
        location = 'https://productionresultssa1.blob.core.windows.net/artifact/download'
        opener.open.side_effect = HTTPError('https://api.github.com/test', 302, '', {'Location': location}, None)
        with patch.object(t, 'build_opener', return_value=opener), patch.object(t, 'public_download', return_value=b'ok') as download:
            self.assertEqual(api._send(Request('https://api.github.com/test', headers={'Authorization':'Bearer SYNTHETIC'}), True), b'ok')
            download.assert_called_once_with(location)

    def test_force_push_denied_before_request(self):
        api = object.__new__(t.ScopedAppApi)
        api.packages = True
        with self.assertRaises(setup.SetupError):
            api.request('PATCH','/git/refs/heads/main',{'sha':'a'*40,'force':True})


if __name__ == '__main__':
    unittest.main()
