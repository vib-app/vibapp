export function stampInspectorPin(source, digest) {
  if (!/^[0-9a-f]{64}$/.test(digest)) throw new Error('Invalid inspector digest');
  const pattern = /^COMPONENT_INSPECTOR_SHA256 = (?:"[0-9a-f]{64}"|\(\r?\n    "[0-9a-f]{64}"\r?\n\))$/gm;
  if ([...source.matchAll(pattern)].length !== 1) throw new Error('Inspector pin source format changed');
  return source.replace(pattern, `COMPONENT_INSPECTOR_SHA256 = "${digest}"`);
}

export function releaseTag(value) {
  if (!/^client-v\d+\.\d+\.\d+-preview\.\d+$/.test(value)) throw new Error('Expected client-vX.Y.Z-preview.N');
  return value;
}
