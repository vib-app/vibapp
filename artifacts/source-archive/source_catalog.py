"""Append-only, browsable public sources; never overwrite another app or CI files."""
from github_archive import MARKER, SHA1, SetupError, blob_sha, canonical, repository_name


def publish_source_directory(api, prefix, token, publisher_id, app_id, digest, files):
    if prefix != "/repos/vib-app/sources":
        raise SetupError("source_catalog_repository")
    directory = "apps/" + repository_name(publisher_id, app_id) + "/" + digest + "/"
    expected = {directory + p: blob_sha(data) for p, data in files.items()}
    marker = canonical({"publisher_id": publisher_id, "app_id": app_id, "source_digest_sha256": digest})
    expected[directory + MARKER] = blob_sha(marker)
    # Source blobs already exist in the verified immutable source snapshot.
    marker_created = False
    for attempt in range(4):
        ref = api.request("GET", prefix + "/git/ref/heads/main", token)
        parent = ref.get("object", {}).get("sha", "")
        if ref.get("object", {}).get("type") != "commit" or not SHA1.fullmatch(parent):
            raise SetupError("source_catalog_head")
        commit = api.request("GET", prefix + "/git/commits/" + parent, token)
        tree_sha = commit.get("tree", {}).get("sha", "")
        if not SHA1.fullmatch(tree_sha):
            raise SetupError("source_catalog_tree")
        tree = api.request("GET", prefix + "/git/trees/" + tree_sha + "?recursive=1", token)
        if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
            raise SetupError("source_catalog_tree")
        observed = {}
        parents = {directory.rstrip("/"), "apps", "apps/" + repository_name(publisher_id, app_id)}
        for entry in tree["tree"]:
            path = entry.get("path", "")
            if path in parents and (entry.get("type"), entry.get("mode")) != ("tree", "040000"):
                raise SetupError("source_catalog_path_conflict")
            if not path.startswith(directory):
                continue
            if (entry.get("type"), entry.get("mode")) == ("tree", "040000"):
                continue
            if (entry.get("type"), entry.get("mode")) != ("blob", "100644") or path in observed:
                raise SetupError("source_catalog_path_conflict")
            observed[path] = entry.get("sha")
        if observed:
            if observed != expected:
                raise SetupError("source_catalog_conflict")
            return parent
        if attempt == 3:
            break
        if not marker_created:
            import base64
            blob = api.request("POST", prefix + "/git/blobs", token,
                               {"content": base64.b64encode(marker).decode(), "encoding": "base64"})
            if blob.get("sha") != blob_sha(marker):
                raise SetupError("source_catalog_blob")
            marker_created = True
        result = api.request("POST", prefix + "/git/trees", token,
            {"base_tree": tree_sha, "tree": [{"path": p, "mode": "100644", "type": "blob", "sha": s}
                                               for p, s in sorted(expected.items())]})
        if not SHA1.fullmatch(result.get("sha", "")):
            raise SetupError("source_catalog_tree")
        result = api.request("POST", prefix + "/git/commits", token,
            {"message": "Archive public VibApp source " + digest, "tree": result["sha"], "parents": [parent]})
        if not SHA1.fullmatch(result.get("sha", "")):
            raise SetupError("source_catalog_commit")
        try:
            api.request("PATCH", prefix + "/git/refs/heads/main", token, {"sha": result["sha"], "force": False})
        except SetupError as error:
            # Another app may have published since the read. Re-read and build
            # on the new head; never force-push or reuse a stale tree.
            if error.code not in ("archive_github_git_409", "archive_github_git_422"):
                raise
        # Also re-read after a successful update; receipts require remote proof.
    raise SetupError("source_catalog_busy_retry")
