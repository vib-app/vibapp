import test from 'node:test';
import assert from 'node:assert/strict';
import { installerScript } from './windows-installer.mjs';

test('installer quotes the handler path and URL and retains user data', () => {
  const source = installerScript({ packageRoot: 'C:\\build\\VibApp', output: 'C:\\build\\client.exe', icon: 'C:\\icon.ico', inventory: ['vibapp-launcher.exe', 'resources/python/python.exe'] });
  assert.match(source, /RequestExecutionLevel user/);
  assert.ok(source.includes("'\"$INSTDIR\\vibapp-launcher.exe\" \"%1\"'"));
  assert.ok(!source.includes('$"'));
  assert.ok(!source.includes('RMDir /r'));
  assert.ok(!source.includes('Page directory'));
  assert.ok(source.includes('Delete "$INSTDIR\\resources\\python\\python.exe"'));
  assert.ok(!source.includes('Delete "$APPDATA'));
});

test('uninstaller refuses an escaping inventory', () => {
  for (const value of ['../secret', 'resources/../../secret', '/secret', 'resources/evil"']) {
    assert.throws(() => installerScript({ inventory: [value] }), /Invalid installer inventory/);
  }
});
