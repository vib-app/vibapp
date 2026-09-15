"""Explicit Store listing of a publicly released package; never runtime approval.

The mutable discovery index lives at vib-app/packages/main/registry.json.
Artifacts and source remain addressed by immutable commits/digests. A listing
does not grant installation, browser execution, or Stage 0 activation authority.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import package_release as packages
import release_pipeline as releases
from github_archive import SetupError, blob_sha, canonical, sha256
from github_transport import ScopedAppApi, public_download

VERSION = "vibapp.store-catalog.v1"
CATALOG_PATH = "registry.json"


def listing(output):
    out = Path(output).absolute()
    state, handoff = releases.ledger_input(out)
    packages.require(state.get("public_source_approved") is True and
                     state["source"]["repository"] == "sources", "store_public_source_required")
    path, files, candidate, run = releases.verified_candidate(out, state, handoff)
    source = releases.read_json(out / "source-archive-receipt.json")
    packages.validate_bindings(files, source, run)
    receipt = releases.read_json(out / "release-receipt.json")
    digest = candidate["package_digest_sha256"]
    packages.require(receipt.get("schema_version") == "vibapp.package-release-receipt.experimental-v1" and
                     receipt.get("state") == "public-downloads-verified" and
                     receipt.get("package_digest_sha256") == digest and
                     receipt.get("binding") == {
                         "schema_version": "vibapp.package-release-binding.experimental-v1",
                         "package_digest_sha256": digest,
                         "candidate_sha256": sha256(files["candidate.json"]),
                         "source_receipt_sha256": sha256(canonical(source)),
                         "run_binding_sha256": sha256(canonical(run)),
                     }, "store_release_binding")
    # Recheck the public bytes, not just a local receipt's success prose.
    zip_bytes = packages.archive(files)
    asset_url = f"https://github.com/vib-app/packages/releases/download/vibapp-package-{digest}/{digest}.zip"
    packages.require(receipt.get("asset_url") == asset_url and
                     public_download(asset_url, limit=len(zip_bytes)) == zip_bytes, "store_download_binding")
    manifest = packages.parse_json(files["package/manifest.json"])
    app = manifest["app"]
    return {
        "app_id": app["id"], "display_name": app["display_name"],
        "summary": app["description"], "version": app["version"], "kind": app["kind"],
        "publisher": app["publisher"]["display_name"],
        "permissions": [item["interface"] for item in manifest["capabilities"]],
        "package_digest_sha256": digest, "component_sha256": candidate["component"]["sha256"],
        "source": source, "source_url": f"https://github.com/vib-app/sources/tree/{source['commit_sha']}",
        "release_url": receipt["release_url"],
        "download": {"url": asset_url, "sha256": sha256(zip_bytes), "size_bytes": len(zip_bytes)},
        "torrent_url": receipt["torrent_url"], "build_url": run["run_url"],
        "publication_state": "published", "verification_state": "package-verified",
        "browser_runtime_available": False,
    }


def publish_entry(entry, api):
    """Read/merge/CAS main, preserving other files and other app identities."""
    remote = api.request("GET", "")
    packages.require(remote.get("id") == packages.PACKAGES_REPOSITORY_ID and
                     remote.get("full_name") == "vib-app/packages" and remote.get("private") is False and
                     remote.get("default_branch") == "main", "store_repository_binding")
    for attempt in range(3):
        head = packages.git_sha(api.request("GET", "/git/ref/heads/main")["object"]["sha"])
        base = packages.git_sha(api.request("GET", "/git/commits/" + head)["tree"]["sha"])
        entries = packages.read_tree(api, base)
        catalog = {"schema_version": VERSION, "apps": []}
        if CATALOG_PATH in entries:
            kind, mode, old_sha = entries[CATALOG_PATH]
            packages.require((kind, mode) == ("blob", "100644"), "store_catalog_path")
            blob = api.request("GET", "/git/blobs/" + old_sha)
            packages.require(blob.get("encoding") == "base64" and 0 < blob.get("size", 0) <= packages.MAX_JSON,
                             "store_catalog_size")
            raw = base64.b64decode(blob["content"])
            packages.require(blob_sha(raw) == old_sha, "store_catalog_readback")
            catalog = packages.parse_json(raw)
        packages.require(set(catalog) == {"schema_version", "apps"} and catalog["schema_version"] == VERSION and
                         isinstance(catalog["apps"], list) and len(catalog["apps"]) < 500 and
                         all(isinstance(app, dict) and isinstance(app.get("app_id"), str) for app in catalog["apps"]),
                         "store_catalog_schema")
        indexed = {app["app_id"]: app for app in catalog["apps"]}
        packages.require(len(indexed) == len(catalog["apps"]), "store_catalog_duplicate")
        if indexed.get(entry["app_id"]) == entry:
            return head
        indexed[entry["app_id"]] = entry
        data = canonical({"schema_version": VERSION, "apps": [indexed[key] for key in sorted(indexed)]})
        packages.require(len(data) <= packages.MAX_JSON, "store_catalog_size")
        blob = api.request("POST", "/git/blobs", {"encoding": "base64", "content": base64.b64encode(data).decode()})
        packages.require(blob.get("sha") == blob_sha(data), "store_catalog_blob")
        tree = api.request("POST", "/git/trees", {"base_tree": base, "tree": [
            {"path": CATALOG_PATH, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
        packages.require(packages.read_tree(api, tree["sha"]) ==
                         entries | {CATALOG_PATH: ("blob", "100644", blob["sha"])}, "store_catalog_preservation")
        commit = api.request("POST", "/git/commits", {"message": "List VibApp " + entry["app_id"],
                                                      "tree": tree["sha"], "parents": [head]})
        commit_sha = packages.git_sha(commit["sha"])
        try:
            api.request("PATCH", "/git/refs/heads/main", {"sha": commit_sha, "force": False})
        except SetupError as error:
            if attempt < 2 and error.code in {"github_role_http_409", "github_role_http_422"}:
                continue
            raise
        # Read back main on the next iteration, including concurrent publishers.
        head = packages.git_sha(api.request("GET", "/git/ref/heads/main")["object"]["sha"])
        if head == commit_sha:
            return commit_sha
    raise SetupError("store_catalog_concurrent_update")


def publish(output):
    entry = listing(output)
    api = ScopedAppApi("packages", packages.PACKAGES_REPOSITORY_ID, {"contents": "write"})
    commit = publish_entry(entry, api)
    receipt = {"schema_version": "vibapp.store-publication-receipt.v1", "state": "listed",
               "app_id": entry["app_id"], "package_digest_sha256": entry["package_digest_sha256"],
               "catalog_commit": commit,
               "catalog_url": "https://raw.githubusercontent.com/vib-app/packages/main/registry.json",
               "browser_runtime_available": False}
    releases.save_json(Path(output) / "store-publication-receipt.json", receipt)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(publish(args.output), sort_keys=True))
    except Exception as error:
        print(json.dumps({"state": "failed", "error": releases.safe_code(error)}))
        raise SystemExit(1) from None
