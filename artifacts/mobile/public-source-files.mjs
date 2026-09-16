import { lstat, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// Exact public-source allowlist. No recursive copy of gen/, icons/, or this
// artifact tree: they also contain local paths, credentials and build outputs.
const mobileFiles = [
  '.gitignore', 'README.md', 'VALIDATION-20260916.md',
  'package.json', 'package-lock.json', 'prepare-ui.mjs', 'build-android.mjs',
  'collect-ci-artifact.mjs', 'ensure-preview-key.mjs', 'sign-preview.mjs',
  'capture-smoke.mjs', 'public-source-files.mjs',
  'ci-browser-deps/package.json', 'ci-browser-deps/package-lock.json',
  'host/index.html', 'host/mobile-host.css', 'host/mobile-host.mjs',
  'host/mobile-launcher.css', 'host/mobile-protocol.mjs',
  'tests/mobile-protocol.test.mjs',
  'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/build.rs',
  'src-tauri/tauri.conf.json', 'src-tauri/src/lib.rs', 'src-tauri/icons/icon.png',
];
const androidFiles = [
  '.editorconfig', '.gitignore', 'gradle.properties', 'settings.gradle',
  'build.gradle.kts', 'gradlew', 'gradlew.bat',
  'gradle/wrapper/gradle-wrapper.jar', 'gradle/wrapper/gradle-wrapper.properties',
  'buildSrc/build.gradle.kts',
  'buildSrc/src/main/java/ai/vibapp/client/preview/kotlin/BuildTask.kt',
  'buildSrc/src/main/java/ai/vibapp/client/preview/kotlin/RustPlugin.kt',
  'app/.gitignore', 'app/build.gradle.kts', 'app/proguard-rules.pro',
  'app/src/main/AndroidManifest.xml',
  'app/src/main/java/ai/vibapp/client/preview/MainActivity.kt',
  'app/src/main/res/drawable-v24/ic_launcher_foreground.xml',
  'app/src/main/res/drawable/ic_launcher_background.xml',
  'app/src/main/res/layout/activity_main.xml',
  'app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml',
  'app/src/main/res/values-night/themes.xml', 'app/src/main/res/values/colors.xml',
  'app/src/main/res/values/ic_launcher_background.xml',
  'app/src/main/res/values/strings.xml', 'app/src/main/res/values/themes.xml',
  'app/src/main/res/xml/file_paths.xml',
  ...['hdpi', 'mdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi'].flatMap(density =>
    ['ic_launcher.png', 'ic_launcher_foreground.png', 'ic_launcher_round.png']
      .map(name => `app/src/main/res/mipmap-${density}/${name}`)),
];

export const mobilePublicSourceFiles = Object.freeze([
  '.github/workflows/android-build.yml',
  ...mobileFiles.map(path => `artifacts/mobile/${path}`),
  ...androidFiles.map(path => `artifacts/mobile/src-tauri/gen/android/${path}`),
].sort());

export async function verifyMobilePublicSourceFiles(repoRoot = resolve(import.meta.dirname, '../..')) {
  for (const path of mobilePublicSourceFiles) {
    const stat = await lstat(resolve(repoRoot, path));
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4 * 1024 * 1024) {
      throw new Error(`Public mobile source is not a bounded regular file: ${path}`);
    }
  }
  const wrapper = await readFile(resolve(repoRoot, 'artifacts/mobile/src-tauri/gen/android/gradle/wrapper/gradle-wrapper.jar'));
  if (createHash('sha256').update(wrapper).digest('hex') !== 'e996d452d2645e70c01c11143ca2d3742734a28da2bf61f25c82bdc288c9e637') {
    throw new Error('Generated Gradle wrapper JAR differs from the reviewed scaffold');
  }
  return mobilePublicSourceFiles;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.stdout.write((await verifyMobilePublicSourceFiles()).join('\n') + '\n');
}
