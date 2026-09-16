import { readdirSync, writeFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';

const quote = value => '"' + String(value).replaceAll('$', '$$').replaceAll('"', '$\\"') + '"';
export function installerScript({ packageRoot, output, icon, inventory }) {
  if (inventory.some(path => /(?:^|[\\/])\.\.(?:[\\/]|$)|[\r\n"]/.test(path) || /^[\\/]/.test(path))) throw Error('Invalid installer inventory');
  const directories = [...new Set(inventory.flatMap(file => {
    const parts = file.split(/[\\/]/).slice(0, -1);
    return parts.map((_, index) => parts.slice(0, index + 1).join('\\'));
  }))].sort((a, b) => b.length - a.length);
  return `Unicode true
Name "VibApp"
OutFile ${quote(output)}
InstallDir "$LOCALAPPDATA\\Programs\\VibApp"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon ${quote(icon)}
UninstallIcon ${quote(icon)}
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles
Section "VibApp"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r ${quote(join(packageRoot, '*'))}
  WriteUninstaller "$INSTDIR\\Uninstall.exe"
  CreateShortcut "$SMPROGRAMS\\VibApp.lnk" "$INSTDIR\\vibapp-launcher.exe"
  WriteRegStr HKCU "Software\\Classes\\vibapp" "" "URL:VibApp Protocol"
  WriteRegStr HKCU "Software\\Classes\\vibapp" "URL Protocol" ""
  WriteRegStr HKCU "Software\\Classes\\vibapp\\DefaultIcon" "" '"$INSTDIR\\vibapp-launcher.exe",0'
  WriteRegStr HKCU "Software\\Classes\\vibapp\\shell\\open\\command" "" '"$INSTDIR\\vibapp-launcher.exe" "%1"'
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VibApp" "DisplayName" "VibApp (Preview)"
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VibApp" "UninstallString" '"$INSTDIR\\Uninstall.exe"'
  WriteRegStr HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VibApp" "DisplayIcon" "$INSTDIR\\vibapp-launcher.exe"
SectionEnd
Section "Uninstall"
  SetShellVarContext current
  Delete "$SMPROGRAMS\\VibApp.lnk"
  ReadRegStr $0 HKCU "Software\\Classes\\vibapp\\shell\\open\\command" ""
  StrCmp $0 '"$INSTDIR\\vibapp-launcher.exe" "%1"' 0 +2
  DeleteRegKey HKCU "Software\\Classes\\vibapp"
  DeleteRegKey HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\VibApp"
${inventory.map(path => '  Delete "$INSTDIR\\' + path.replaceAll('/', '\\').replaceAll('$', '$$') + '"').join('\n')}
${directories.map(path => '  RMDir "$INSTDIR\\' + path.replaceAll('$', '$$') + '"').join('\n')}
  Delete "$INSTDIR\\Uninstall.exe"
  RMDir "$INSTDIR"
  ; Application data lives elsewhere and is deliberately retained.
SectionEnd
`;
}

export function packageWindowsInstaller({ root, packageRoot, output }) {
  if (process.platform !== 'win32') throw Error('Native Windows installer required');
  const compiler = 'C:\\Program Files (x86)\\NSIS\\makensis.exe';
  const version = execFileSync(compiler, ['/VERSION'], { encoding: 'utf8', timeout: 10000 }).trim();
  if (version !== 'v3.10') throw Error('Unexpected NSIS compiler: ' + version);
  const inventory = [];
  const walk = (directory, prefix = '') => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const relative = prefix + entry.name;
      if (entry.isSymbolicLink()) throw Error('Installer source cannot contain symlinks: ' + relative);
      if (entry.isDirectory()) walk(join(directory, entry.name), relative + '/');
      else if (entry.isFile()) inventory.push(relative);
      else throw Error('Invalid installer source');
    }
  };
  walk(packageRoot);
  const script = resolve(root, 'generated/client-staging/vibapp-installer.nsi');
  writeFileSync(script, installerScript({ packageRoot, output, icon: join(root, 'artifacts/desktop/src-tauri/icons/icon.ico'), inventory }));
  execFileSync(compiler, ['/V2', script], { stdio: 'inherit', timeout: 300000 });
  execFileSync(output, ['/S'], { stdio: 'inherit', timeout: 120000 });
  const installed = join(process.env.LOCALAPPDATA, 'Programs/VibApp/vibapp-launcher.exe');
  if (!existsSync(installed)) throw Error('Installer did not install the launcher');
  const command = execFileSync('reg.exe', ['query', 'HKCU\\Software\\Classes\\vibapp\\shell\\open\\command', '/ve'], { encoding: 'utf8', timeout: 10000 });
  if (!command.includes('"' + installed + '" "%1"')) throw Error('Installed deep-link command does not match launcher');
  execFileSync(installed, ['--help'], { stdio: 'inherit', timeout: 20000 });
  return { compiler: 'NSIS 3.10', checks: ['silent-user-install', 'installed-launcher-help', 'registered-vibapp-url-handler'] };
}
