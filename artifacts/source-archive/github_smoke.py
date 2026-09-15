"""Make a synthetic, actually compilable app for the authorized archive-check repo."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parents[1] / "artifacts/app-builder"))
from automatic_runner_smoke import make_handoff
from app_builder import validate_handoff
from github_archive import canonical, source_digest


def create(root):
    path = make_handoff(root, "github-release-check-" + root.name)
    handoff = json.loads(path.read_text())
    source = root / "source"
    cargo = source / "Cargo.toml"
    cargo.write_text(cargo.read_text().replace('name = "vibapp-automatic-builder-smoke"', 'name = "ai_vibapp_archive-check"'))
    rust = source / "src/lib.rs"
    rust.write_text(rust.read_text().replace("ai.vibapp.automatic-builder-smoke", "ai.vibapp.archive-check")
                    .replace("Automatic Builder Smoke", "GitHub Release Check"))
    handoff["package_intent"].update(app_id="ai.vibapp.archive-check", display_name="GitHub Release Check")
    handoff["package_intent"]["entrypoints"][0]["label"] = "GitHub Release Check"
    files = {}
    for record in handoff["files"]:
        data = (source / record["path"]).read_bytes()
        record["sha256"], record["size_bytes"] = hashlib.sha256(data).hexdigest(), len(data)
        files[record["path"]] = data
    handoff["source_tree_sha256"] = source_digest(files)
    path.write_bytes(canonical(handoff))
    validate_handoff(path)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(create(args.output))
