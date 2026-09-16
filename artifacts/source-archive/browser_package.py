"""Trusted extension of a verified native candidate with a stateless Web runtime.

Compilation/source and native verifier evidence remain unchanged. The new manifest,
package digest and separate browser verification are new immutable artifacts.
Creation never uploads, signs, installs, or publishes them. Publication requires
the explicit operator-only --publish-from-job mode and verified public lineage.
"""
from __future__ import annotations
import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import package_release as packages

CORE = Path(__file__).resolve().parent.parent / "web-client-core"
sys.path.insert(0, str(packages.BUILDER))
from common import package_digest, now_utc
from verifier import _validate_manifest_schema


def create(base_candidate: Path, output: Path, *, timed_clock=False):
    files = packages.snapshot(base_candidate)
    base = packages.parse_json(files["candidate.json"])
    packages.validate_record(base)
    manifest = packages.parse_json(files["package/manifest.json"])
    _validate_manifest_schema(manifest)
    packages.require(not manifest["artifacts"]["browser_derivations"], "browser_base_already_derived")
    packages.require(base["package_digest_sha256"] == package_digest(manifest), "browser_base_digest")
    for key, path in (("component", "component.wasm"), ("manifest", "manifest.json")):
        data = files["package/" + path]
        packages.require(base[key] == {"path": path, "sha256": packages.sha256(data), "size_bytes": len(data)}, "browser_base_descriptor")
    for key in ("canonical_component", "provenance", "sbom"):
        descriptor = manifest["artifacts"][key]
        data = files["package/" + descriptor["path"]]
        packages.require(descriptor["sha256"] == packages.sha256(data) and descriptor["size_bytes"] == len(data), "browser_base_artifact")
    packages._run_independent_checks(base_candidate)
    packages.require(output.is_absolute() and not output.exists(), "browser_output_must_be_new")
    output.mkdir(parents=True, mode=0o700)
    node = shutil.which("node")
    packages.require(node, "browser_node_missing")
    with tempfile.TemporaryDirectory(prefix="browser-quarantine-", dir=output) as temporary:
        staging = Path(temporary)
        package = staging / "package"
        package.mkdir()
        for name, data in files.items():
            if name.startswith("package/"):
                (staging / name).write_bytes(data)
        derived = subprocess.run([node, str(CORE / "derive-product-browser.mjs"), str(package / "component.wasm"), str(package)],
            check=True, capture_output=True, timeout=180, env=os.environ.copy())
        derivation = packages.parse_json(derived.stdout)
        manifest["artifacts"]["browser_derivations"] = [derivation]
        manifest["runtime"]["profiles"].append({"profile": "web-runtime", "mode": "degraded", "background": "foreground-only",
            "artifact_role": "browser-derived", "degradation": "Foreground stateless UI only; read-only empty settings/state, no saved data or background service."})
        manifest["runtime"]["platforms"].append({"os": "browser", "arch": "wasm32", "profiles": ["web-runtime"]})
        for entry in manifest["entrypoints"]:
            entry["profiles"].append("web-runtime")
        for capability in manifest["capabilities"]:
            capability["profiles"].append({"profile": "web-runtime", "availability": "brokered",
                "behavior": "Bounded stateless foreground host; no persistence, network or background authority."})
        _validate_manifest_schema(manifest)
        manifest_bytes = packages.canonical(manifest)
        (package / "manifest.json").write_bytes(manifest_bytes)
        command = [node, str(CORE / "verify-product-browser.mjs"), str(package)]
        if timed_clock:
            command.append("--timed-clock")
        checked = subprocess.run(command, check=True, capture_output=True, timeout=180, env=os.environ.copy())
        evidence = packages.parse_json(checked.stdout)
        digest = package_digest(manifest)
        candidate = copy.deepcopy(base)
        candidate.update(package_digest_sha256=digest, manifest={"path": "manifest.json", "sha256": packages.sha256(manifest_bytes), "size_bytes": len(manifest_bytes)})
        candidate["verification"]["verified_at_utc"] = now_utc()
        candidate["verification"]["checks"].append({"id": "browser-independent-rederivation", "outcome": "pass",
            "tool": "verify-product-browser.v1", "detail": "Separate verifier rederived all browser bytes and executed 10003 fresh-instance foreground calls; evidence SHA256 " + packages.sha256(packages.canonical(evidence))})
        (staging / "candidate.json").write_bytes(packages.canonical(candidate))
        target = output / digest
        staging.rename(target)
        (output / "browser-verification.json").write_bytes(packages.canonical(evidence))
        (output / "derivation-lineage.json").write_bytes(packages.canonical({"schema_version": "vibapp.browser-package-lineage.v1",
            "base_candidate_sha256": packages.sha256(files["candidate.json"]), "base_package_digest_sha256": base["package_digest_sha256"],
            "package_digest_sha256": digest, "component_sha256": base["component"]["sha256"], "source_tree_sha256": base["source_tree_sha256"],
            "source_changed": False, "publication": "not-performed"}))
    return {"candidate": str(target / "candidate.json"), "package_digest_sha256": digest, "verification": evidence}


def publish(base_job: Path, candidate_path: Path, output: Path):
    """Explicit operator-only publication; never invoked by create/derive."""
    import release_pipeline as releases
    import store_catalog
    import github_build
    packages.require(output.is_absolute() and not output.is_symlink(), "browser_publication_output_path")
    state = github_build.resume(base_job)
    trusted_state, handoff = releases.ledger_input(base_job)
    packages.require(state == trusted_state and state.get("state") == "independently-verified"
                     and state.get("public_source_approved") is True, "browser_base_reverification")
    _, base_files, base, run = releases.verified_candidate(base_job, state, handoff)
    files = packages.snapshot(candidate_path)
    candidate = packages.parse_json(files["candidate.json"])
    packages.validate_record(candidate)
    manifest = packages.parse_json(files["package/manifest.json"])
    _validate_manifest_schema(manifest)
    packages.require(candidate["source_tree_sha256"] == base["source_tree_sha256"] and candidate["component"] == base["component"]
        and all(files[name] == base_files[name] for name in ("package/component.wasm", "package/provenance.json", "package/sbom.cdx.json")), "browser_canonical_lineage")
    packages._run_independent_checks(candidate_path)
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    worker = releases.archiver()
    source = worker.upload_public(state["publisher_id"], state["app_id"], candidate["package_digest_sha256"], candidate["source_tree_sha256"], handoff.source_root)
    packages.validate_bindings(files, source, run)
    releases.save_exact(output / "source-archive-receipt.json", source)
    receipt = packages.publish_candidate(candidate_path, source, run, output / "publication")
    releases.save_exact(output / "release-receipt.json", receipt)
    zip_bytes = packages.archive(files)
    app = manifest["app"]
    entry = {"app_id": app["id"], "display_name": app["display_name"], "summary": app["description"], "version": app["version"],
        "kind": app["kind"], "publisher": app["publisher"]["display_name"], "permissions": [item["interface"] for item in manifest["capabilities"]],
        "package_digest_sha256": candidate["package_digest_sha256"], "component_sha256": candidate["component"]["sha256"],
        "source": source, "source_url": f"https://github.com/vib-app/sources/tree/{source['commit_sha']}", "release_url": receipt["release_url"],
        "download": {"url": receipt["asset_url"], "sha256": packages.sha256(zip_bytes), "size_bytes": len(zip_bytes)},
        "torrent_url": receipt["torrent_url"], "build_url": run["run_url"], "publication_state": "published",
        "verification_state": "package-verified", "browser_runtime_available": True}
    api = store_catalog.ScopedAppApi("packages", packages.PACKAGES_REPOSITORY_ID, {"contents": "write"})
    commit = store_catalog.publish_entry(entry, api)
    releases.save_exact(output / "store-entry.json", entry)
    releases.save_exact(output / "store-publication-receipt.json", {"state": "listed", "catalog_commit": commit, "package_digest_sha256": candidate["package_digest_sha256"]})
    return {"state": "listed", "package_digest_sha256": candidate["package_digest_sha256"], "release_url": receipt["release_url"], "catalog_commit": commit}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-candidate", type=Path)
    parser.add_argument("--publish-from-job", type=Path, help="explicitly authorize public source/package/Store publication")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timed-clock", action="store_true")
    args = parser.parse_args()
    result = publish(args.publish_from_job.absolute(), args.candidate.absolute(), args.output.absolute()) if args.publish_from_job else create(args.base_candidate.absolute(), args.output.absolute(), timed_clock=args.timed_clock)
    print(json.dumps(result, sort_keys=True))
