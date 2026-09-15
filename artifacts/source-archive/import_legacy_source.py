"""Explicit, offline v1-source migration; preserves the original and records changes.

This does not attest a CodeAgent run, build, publish, or weaken the v2 validator.
It conservatively preserves v1's all-imports-required capability policy.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import tomllib

from github_archive import SetupError, canonical, sha256, snapshot, source_digest, unique_object
from github_build import validate_handoff
from app_builder import _cargo_package_name
from common import PipelineError


def migrate(handoff_path, output):
    handoff_path = handoff_path.resolve(strict=True)
    if handoff_path.stat().st_size > 1048576:
        raise SetupError('legacy_handoff_limit')
    original = handoff_path.read_bytes()
    document = json.loads(original, object_pairs_hook=unique_object)
    if document.get('schema_version') != 'vibapp.codeagent-source-handoff.experimental-v1' or document.get('source_directory') != 'source':
        raise SetupError('legacy_handoff_version')
    files = snapshot(handoff_path.parent / 'source', document['source_tree_sha256'])
    inventory = {r['path']: r for r in document['files']}
    if len(inventory) != len(document['files']) or set(files) != set(inventory) or any(
        inventory[p]['sha256'] != sha256(b) or inventory[p]['size_bytes'] != len(b) for p, b in files.items()
    ):
        raise SetupError('legacy_source_inventory')
    upgraded = copy.deepcopy(document)
    upgraded['schema_version'] = 'vibapp.codeagent-source-handoff.experimental-v2'
    if 'required_capabilities' in upgraded['target']:
        raise SetupError('legacy_capabilities_conflict')
    upgraded['target']['required_capabilities'] = list(upgraded['target']['required_imports'])
    # Only rename the package. Do not rewrite dependency/features/build options.
    cargo = tomllib.loads(files['Cargo.toml'].decode())
    old_name = cargo['package']['name']
    new_name = _cargo_package_name(upgraded['package_intent']['app_id'])
    changes = ['handoff-v1-to-v2', 'preserve-all-imports-required']
    if old_name != new_name:
        text, count = re.subn(r'(?m)^name\s*=\s*"' + re.escape(old_name) + r'"\s*$',
                             'name = "' + new_name + '"', files['Cargo.toml'].decode())
        if count != 1:
            raise SetupError('legacy_cargo_name_ambiguous')
        converted = tomllib.loads(text)
        expected = copy.deepcopy(cargo)
        expected['package']['name'] = new_name
        if converted != expected:
            raise SetupError('legacy_cargo_changed')
        files['Cargo.toml'] = text.encode()
        changes.append('cargo-package-name-aligned-to-app-id')
    for row in upgraded['files']:
        row['sha256'] = sha256(files[row['path']])
        row['size_bytes'] = len(files[row['path']])
    upgraded['source_tree_sha256'] = source_digest(files)
    # Create-only output; no overwrite, links, or changes to the original tree.
    if output.exists() or output.is_symlink():
        raise SetupError('legacy_output_exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.resolve() != output.parent.absolute():
        raise SetupError('legacy_output_parent_link')
    output.mkdir(mode=0o700)
    for path, payload in files.items():
        target = output / 'source' / path
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(payload)
        target.chmod(0o600)
    target = output / 'handoff.json'
    with target.open('xb') as stream:
        stream.write(canonical(upgraded))
    target.chmod(0o600)
    validate_handoff(target)  # Full current validator, not a migration bypass.
    receipt = {'schema_version': 'vibapp.legacy-source-migration.v1',
        'state': 'source-validated-not-built', 'changes': changes,
        'original_handoff_sha256': sha256(original), 'migrated_handoff_sha256': sha256(target.read_bytes()),
        'original_source_sha256': document['source_tree_sha256'],
        'migrated_source_sha256': upgraded['source_tree_sha256'],
        'provider_facts': 'preserved-historical-metadata-not-a-new-agent-execution',
        'app_id': upgraded['package_intent']['app_id']}
    with (output / 'migration.json').open('xb') as stream:
        stream.write(canonical(receipt))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.handoff, args.output.absolute())))
        return 0
    except (SetupError, PipelineError) as error:
        print(json.dumps({'state': 'failed', 'error': error.code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
