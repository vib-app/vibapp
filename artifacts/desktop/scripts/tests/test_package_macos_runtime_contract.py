#!/usr/bin/env python3
"""Focused macOS runtime-signing and immutable-Python-resource regressions."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


DESKTOP_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SCRIPT = DESKTOP_ROOT / "scripts" / "package-macos.sh"
ENTITLEMENTS = DESKTOP_ROOT / "packaging" / "macos" / "Runtime.entitlements.plist"
RUST_SOURCE_ROOT = DESKTOP_ROOT / "src-tauri" / "src"


class MacRuntimePackageContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.package = PACKAGE_SCRIPT.read_text(encoding="utf-8")
        cls.package_words = " ".join(cls.package.split())

    def test_service_runtime_receives_only_the_reviewed_jit_entitlements(self) -> None:
        with ENTITLEMENTS.open("rb") as handle:
            entitlements = plistlib.load(handle)
        self.assertEqual(
            entitlements,
            {
                "com.apple.security.cs.allow-jit": True,
                "com.apple.security.cs.allow-unsigned-executable-memory": True,
            },
        )

        service_target = (
            '"$contents/Resources/runtime-daemon/service-runtime/'
            'vibapp-service-runtime"'
        )
        expected = (
            '"$codesign_bin" --force --sign - --timestamp=none --options runtime '
            '--entitlements "$runtime_entitlements" '
            f"{service_target}"
        )
        service_signs = [
            line.strip()
            for line in self.package.splitlines()
            if "codesign_bin" in line and service_target in line
        ]
        self.assertEqual(service_signs, [expected])
        self.assertLess(
            self.package_words.index(expected),
            self.package_words.index(
                '"$codesign_bin" --force --sign - --timestamp=none --options runtime '
                '"$staged_bundle"'
            ),
        )

    def test_host_budget_is_a_required_staged_and_identity_bound_input(self) -> None:
        preflight = self.package.split("for required_file in ", 1)[1].split("; do", 1)[0]
        self.assertIn('"$codeagent_launcher_root/host_budget.py"', preflight)
        stage = next(line for line in self.package.splitlines() if line.startswith('cp "$codeagent_launcher_root/codeagent_launcher.py"'))
        self.assertIn('"$codeagent_launcher_root/host_budget.py"', stage)
        self.assertIn('"$contents/Resources/codeagent-launcher/"', stage)
        for source in (DESKTOP_ROOT / "src-tauri/build.rs", DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs"):
            self.assertIn("codeagent-launcher/host_budget.py", source.read_text())

    def test_task_archive_is_required_staged_and_identity_bound(self) -> None:
        preflight = self.package.split("for required_file in ", 1)[1].split("; do", 1)[0]
        self.assertIn('"$task_archive"', preflight)
        self.assertIn('cp "$task_archive" "$contents/Resources/orchestrator/task_archive.py"', self.package)
        self.assertIn('emit_desktop_build_input_entry "orchestrator/task_archive.py" "$task_archive"', self.package)
        self.assertIn('"task_archive.py"', (DESKTOP_ROOT / "src-tauri/build.rs").read_text())
        self.assertIn("orchestrator/task_archive.py", (DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs").read_text())

    def test_legacy_history_reader_is_required_staged_and_identity_bound(self) -> None:
        preflight = self.package.split("for required_file in ", 1)[1].split("; do", 1)[0]
        self.assertIn('"$delivery_history"', preflight)
        self.assertIn('cp "$delivery_history" "$contents/Resources/orchestrator/delivery_history.py"', self.package)
        self.assertIn('emit_desktop_build_input_entry "orchestrator/delivery_history.py" "$delivery_history"', self.package)
        self.assertIn('"delivery_history.py"', (DESKTOP_ROOT / "src-tauri/build.rs").read_text())
        self.assertIn("orchestrator/delivery_history.py", (DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs").read_text())

    def test_runtime_readiness_is_required_staged_and_identity_bound(self) -> None:
        preflight = self.package.split("for required_file in ", 1)[1].split("; do", 1)[0]
        self.assertIn('"$runtime_readiness"', preflight)
        self.assertIn('cp "$runtime_readiness" "$contents/Resources/orchestrator/runtime_readiness.py"', self.package)
        self.assertIn('emit_desktop_build_input_entry "orchestrator/runtime_readiness.py" "$runtime_readiness"', self.package)
        self.assertIn('"runtime_readiness.py"', (DESKTOP_ROOT / "src-tauri/build.rs").read_text())
        self.assertIn("orchestrator/runtime_readiness.py", (DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs").read_text())

    def test_every_isolated_python_process_uses_command_line_no_bytecode(self) -> None:
        isolated = 0
        protected = 0
        for source_path in sorted(RUST_SOURCE_ROOT.rglob("*.rs")):
            source = source_path.read_text(encoding="utf-8")
            isolated += len(re.findall(r'\.arg\("-I"\)|\.args\(\["-I"', source))
            protected += len(
                re.findall(r'\.arg\("-I"\)\s*\.arg\("-B"\)', source)
            )
            protected += len(re.findall(r'\.args\(\["-I",\s*"-B"', source))
        self.assertGreater(isolated, 0)
        self.assertEqual(
            protected,
            isolated,
            "Python -I ignores PYTHON* environment variables; every isolated process must also pass -B",
        )

    def test_signing_cannot_silently_change_the_accepted_inspector_digest(self) -> None:
        check = self.package.index('if [ "${source_runtime_digest_line%% *}" != "${packaged_runtime_digest_line%% *}" ]; then')
        self.assertLess(self.package.index('"$codesign_bin" --force --sign -'), check)
        self.assertLess(check, self.package.index('mv "$staged_bundle" "$bundle"'))
        self.assertIn('runtime signing changed the accepted inspector bytes', self.package)
        self.assertIn('from descriptor_reconciliation import descriptor_inspector_preflight; descriptor_inspector_preflight()', self.package)

    def test_package_carries_current_and_immutable_historical_task_schemas(self) -> None:
        current = "cloud-codeagent-task.schema.json"
        historical = "cloud-codeagent-task.experimental-v2.schema.json"
        self.assertIn(f'"$cloud_agent_root/schemas/{current}"', self.package)
        self.assertIn(f'"$cloud_agent_root/schemas/{historical}"', self.package)
        self.assertIn(
            f'"$contents/Resources/cloud-agent/schemas/{current}"', self.package
        )
        self.assertIn(
            f'"$contents/Resources/cloud-agent/schemas/{historical}"', self.package
        )
        build_script = (DESKTOP_ROOT / "src-tauri" / "build.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn(current, build_script)
        self.assertIn(historical, build_script)

    def test_opencode_trusted_inputs_are_explicit_receipt_preflight_and_resources(self) -> None:
        names = ("README.md", "opencode_instructions.txt", "codex_instructions.txt", "vibapp_support.rs")
        build_script = (DESKTOP_ROOT / "src-tauri/build.rs").read_text(encoding="utf-8")
        node_receipt = (DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs").read_text(encoding="utf-8")
        preflight = self.package.split("for required_file in ", 1)[1].split("; do", 1)[0]
        self.assertIn('artifacts_root.join("cloud-agent/starter").join(name)', build_script)
        for name in names:
            self.assertIn(f'"{name}"', build_script)
            self.assertIn(f"cloud-agent/starter/{name}", node_receipt)
            self.assertIn(f'"$cloud_agent_root/starter/{name}"', preflight)
            self.assertIn(
                f'cp "$cloud_agent_root/starter/{name}" "$contents/Resources/cloud-agent/starter/{name}"',
                self.package,
            )
        self.assertNotIn('cp -R "$cloud_agent_root/starter"', self.package)
        self.assertNotRegex(node_receipt, r"addTree\([^\n]*['\"]starter")
        self.assertNotRegex(build_script, r'collect_directory\([^;]*cloud-agent/starter')

    def test_opencode_consent_uses_embedded_bytes_and_python_parity_algorithm(self) -> None:
        source = (RUST_SOURCE_ROOT / "cloud_task_preview.rs").read_text(encoding="utf-8")
        for name in ("opencode_instructions.txt", "vibapp_support.rs", "README.md"):
            self.assertIn(f'../../../cloud-agent/starter/{name}', source)
        self.assertIn('fn instructions_digest(provider: &str) -> Result<String, String>', source)
        self.assertIn(
            r'"{OPENCODE_INSTRUCTIONS}\nUI support SHA256: {support_digest}\nUI guide SHA256: {guide_digest}\n"',
            source,
        )
        self.assertNotIn("OPENCODE_INSTRUCTIONS_DIGEST_SHA256", source)
        self.assertIn("fn opencode_instruction_digest_matches_actual_python_without_provider_call()", source)
        self.assertIn("module.provider_instructions_digest('opencode')", source)
        starter = DESKTOP_ROOT.parent / "cloud-agent/starter"
        names = ("opencode_instructions.txt", "vibapp_support.rs", "README.md")
        if not all((starter / name).is_file() for name in names):
            self.skipTest("trusted starter authoring has not supplied all three inputs yet")
        expected = (
            (starter / names[0]).read_bytes().decode("utf-8")
            + "\nUI support SHA256: " + hashlib.sha256((starter / names[1]).read_bytes()).hexdigest()
            + "\nUI guide SHA256: " + hashlib.sha256((starter / names[2]).read_bytes()).hexdigest()
            + "\n"
        )
        code = (
            "import importlib.util,sys;"
            "spec=importlib.util.spec_from_file_location('client_instruction_parity',sys.argv[1]);"
            "module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;"
            "spec.loader.exec_module(module);print(module.provider_instructions_digest('opencode'))"
        )
        observed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(starter.parent / "cloud_agent.py")],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, timeout=10,
        )
        self.assertEqual(observed.returncode, 0, observed.stderr.decode(errors="replace"))
        self.assertEqual(observed.stdout.decode().strip(), hashlib.sha256(expected.encode("utf-8")).hexdigest())

    def test_isolated_python_import_does_not_create_bundle_bytecode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vibapp-python-bytecode-") as temporary:
            resources = Path(temporary) / "VibApp.app" / "Contents" / "Resources"
            module_root = resources / "synthetic-helper"
            module_root.mkdir(parents=True)
            (module_root / "bundle_helper.py").write_text(
                "VALUE = 'loaded-from-bundle'\n", encoding="utf-8"
            )
            code = (
                "import importlib,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "raise SystemExit(0 if importlib.import_module('bundle_helper').VALUE == "
                "'loaded-from-bundle' else 1)"
            )
            environment = dict(os.environ)
            # -I ignores this environment setting; -B is the actual protection.
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code, str(module_root)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            self.assertEqual(list(resources.rglob("__pycache__")), [])
            self.assertEqual(list(resources.rglob("*.pyc")), [])
            self.assertEqual(list(resources.rglob("*.pyo")), [])

    def test_staged_resources_reject_bytecode_before_any_signing(self) -> None:
        required = (
            "reject_python_bytecode() {",
            "-type d -name '__pycache__'",
            "-name '*.pyc'",
            "-name '*.pyo'",
            'echo "packaged Resources contain mutable Python bytecode: $python_bytecode"',
            "reject_python_bytecode",
        )
        for fragment in required:
            self.assertIn(fragment, self.package)
        call = self.package.rindex("\nreject_python_bytecode\n")
        first_sign = self.package.index('\n"$codesign_bin" --force --sign -', call)
        self.assertLess(call, first_sign)

    def test_launcher_and_runtime_freshness_is_checked_before_staging(self) -> None:
        required = (
            'rust_source_root="$desktop_root/src-tauri/src"',
            '"$desktop_root/src-tauri/Cargo.toml"',
            '"$desktop_root/src-tauri/Cargo.lock"',
            '"$desktop_root/src-tauri/build.rs"',
            "-type f -name '*.rs' -newer \"$binary_path\" -print",
            'require_fresh_desktop_binary "$launcher" "vibapp-launcher"',
            'require_fresh_desktop_binary "$runtime" "vibapp-runtime"',
            "rebuild the exact binaries consumed by this package command:",
            'CARGO_TARGET_DIR=\\"$target_root\\" cargo build --manifest-path',
        )
        for fragment in required:
            self.assertIn(fragment, self.package)
        self.assertLess(
            self.package.index('require_fresh_desktop_binary "$runtime" "vibapp-runtime"'),
            self.package.index('mkdir -p "$desktop_root/dist"'),
        )

    def test_frontend_and_external_inputs_are_digest_bound_to_both_binaries(self) -> None:
        required = (
            "compute_desktop_build_inputs_sha256() {",
            'emit_desktop_build_input_tree "$desktop_root/ui" "desktop/ui"',
            'emit_desktop_build_input_tree "$desktop_root/src-tauri/src" "desktop/src-tauri/src"',
            '"wit/experimental-v0/contract.wit"',
            '"$cloud_agent_root/cloud_agent.py"',
            '"$app_builder_root/app_builder.py"',
            '"$app_builder_root/verifier.py"',
            '"$app_builder_root/common.py"',
            '"$app_builder_root/descriptor_reconciliation.py"',
            '"$app_builder_root/schemas/candidate.schema.json"',
            '"$app_builder_root/schemas/quarantine-receipt.schema.json"',
            '"$app_builder_root/schemas/verifier-decision.schema.json"',
            '"$cloud_agent_root/schemas/provider-result.schema.json"',
            '"$cloud_agent_root/schemas/source-handoff.schema.json"',
            '"$codeagent_adapter_root/codeagent_adapter.py"',
            '"orchestrator/delivery_controller.py"',
            '"orchestrator/dry_run_adapter.py"',
            '"orchestrator/orchestrator.py"',
            'expected_marker="VIBAPP_DESKTOP_BUILD_INPUTS_SHA256=$desktop_build_inputs_sha256"',
            'require_desktop_build_input_receipt "$launcher" "vibapp-launcher"',
            'require_desktop_build_input_receipt "$runtime" "vibapp-runtime"',
            "compute_public_registry_source_sha256() {",
            'expected_marker="VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256=$public_registry_source_sha256"',
            'require_public_registry_source_receipt "$launcher" "vibapp-launcher"',
            'require_public_registry_source_receipt "$runtime" "vibapp-runtime"',
            "require_unchanged_build_sources_before_sign() {",
            "desktop build inputs changed after binary receipt verification and resource staging",
            "public Registry source changed after binary receipt verification and resource staging",
            '"$contents/Resources/build-provenance/desktop-inputs.json"',
            '"schema_version":"vibapp.desktop-build-inputs.v1"',
            '"public_registry_source_sha256":"%s"',
            '"public_registry_snapshot_sha256":"%s"',
            '"public_registry_locator_index_sha256":"%s"',
        )
        for fragment in required:
            self.assertIn(fragment, self.package)
        receipt_check = self.package.index(
            'require_desktop_build_input_receipt "$runtime" "vibapp-runtime"'
        )
        self.assertLess(receipt_check, self.package.index('mkdir -p "$desktop_root/dist"'))
        final_source_check = self.package.rindex(
            "\nrequire_unchanged_build_sources_before_sign\n"
        )
        self.assertGreater(
            final_source_check,
            self.package.index(
                'cp "$contract_wit" "$contents/wit/experimental-v0/contract.wit"'
            ),
        )
        self.assertLess(final_source_check, self.package.index("\n/usr/bin/printf '{\"schema_version\""))

        build_script = (DESKTOP_ROOT / "src-tauri" / "build.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'cargo:rustc-env=VIBAPP_DESKTOP_BUILD_INPUTS_SHA256={digest}',
            build_script,
        )
        self.assertIn(
            'cargo:rustc-env=VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256={public_registry_digest}',
            build_script,
        )
        for binary_source in (
            DESKTOP_ROOT / "src-tauri" / "src" / "main.rs",
            DESKTOP_ROOT / "src-tauri" / "src" / "bin" / "vibapp-runtime.rs",
        ):
            source = binary_source.read_text(encoding="utf-8")
            self.assertIn('/desktop_build_input_receipt.rs"));', source)
            self.assertIn("std::hint::black_box(&DESKTOP_BUILD_INPUT_RECEIPT);", source)
        self.assertIn("VIBAPP_DESKTOP_BUILD_INPUTS_SHA256={digest}", build_script)
        self.assertIn("static DESKTOP_BUILD_INPUT_RECEIPT: [u8; {}]", build_script)
        self.assertIn('cargo:rerun-if-changed={}', build_script)
        self.assertIn('artifacts_root.join("app-builder").join(name)', build_script)
        self.assertIn('"descriptor_reconciliation.py"', build_script)
        critical_labels = (
            "cloud-agent/schemas/provider-result.schema.json",
            "cloud-agent/schemas/source-handoff.schema.json",
            "codeagent-adapter/codeagent_adapter.py",
            "orchestrator/delivery_controller.py",
            "orchestrator/dry_run_adapter.py",
            "orchestrator/orchestrator.py",
        )
        for critical_label in critical_labels:
            self.assertIn(critical_label, self.package)
        for critical_name in (
            "provider-result.schema.json",
            "source-handoff.schema.json",
            "codeagent_adapter.py",
            "delivery_controller.py",
            "dry_run_adapter.py",
            "orchestrator.py",
        ):
            self.assertIn(critical_name, build_script)
        self.assertIn('artifacts_root.join("cloud-agent/schemas").join(name)', build_script)
        self.assertIn('artifacts_root.join("orchestrator").join(name)', build_script)
        self.assertIn(
            'desktop_input_stream=$(mktemp "${TMPDIR:-/tmp}/vibapp-desktop-inputs.XXXXXX")',
            self.package,
        )
        self.assertIn(
            'emit_desktop_build_input_entries > "$desktop_input_stream"',
            self.package,
        )

    def test_frontend_digest_mismatch_fails_before_staging_even_with_old_mtime(self) -> None:
        base_ns = 1_700_000_000_000_000_000
        with tempfile.TemporaryDirectory(prefix="vibapp-frontend-receipt-") as temporary:
            project_root = Path(temporary)
            artifact_root = project_root / "artifacts"
            desktop_root = artifact_root / "desktop"
            script = desktop_root / "scripts" / "package-macos.sh"
            script.parent.mkdir(parents=True)
            script.write_text(self.package, encoding="utf-8")

            labeled_inputs: list[tuple[str, Path]] = []

            def add(label: str, path: Path, content: bytes = b"input\n") -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                os.utime(path, ns=(base_ns, base_ns))
                labeled_inputs.append((label, path))

            cloud = artifact_root / "cloud-agent"
            for name in ("local_appstore.py", "public_app_download.py"):
                add(f"registry-store/{name}", artifact_root / "registry-store" / name)
            add("desktop/packaging/macos/Info.plist", desktop_root / "packaging/macos/Info.plist")
            add("cloud-agent/cloud_agent.py", cloud / "cloud_agent.py")
            add("cloud-agent/skills/vibapp-ui-ux/SKILL.md", cloud / "skills/vibapp-ui-ux/SKILL.md")
            for name in ("README.md", "opencode_instructions.txt", "codex_instructions.txt", "vibapp_support.rs"):
                add(f"cloud-agent/starter/{name}", cloud / "starter" / name)
            add(
                "cloud-agent/fixtures/dry-run/provider-result.json",
                cloud / "fixtures" / "dry-run" / "provider-result.json",
            )
            add(
                "cloud-agent/fixtures/dry-run/source/Cargo.toml",
                cloud / "fixtures" / "dry-run" / "source" / "Cargo.toml",
            )
            add(
                "cloud-agent/fixtures/dry-run/source/src/lib.rs",
                cloud / "fixtures" / "dry-run" / "source" / "src" / "lib.rs",
            )
            add(
                "cloud-agent/schemas/cloud-codeagent-task.schema.json",
                cloud / "schemas" / "cloud-codeagent-task.schema.json",
            )
            add(
                "cloud-agent/schemas/cloud-codeagent-task.experimental-v2.schema.json",
                cloud / "schemas" / "cloud-codeagent-task.experimental-v2.schema.json",
            )
            add(
                "cloud-agent/schemas/provider-result.schema.json",
                cloud / "schemas" / "provider-result.schema.json",
            )
            add(
                "cloud-agent/schemas/source-handoff.schema.json",
                cloud / "schemas" / "source-handoff.schema.json",
            )
            app_builder = artifact_root / "app-builder"
            for name in (
                "app_builder.py",
                "verifier.py",
                "common.py",
                "descriptor_reconciliation.py",
            ):
                add(f"app-builder/{name}", app_builder / name)
            for name in (
                "candidate.schema.json",
                "quarantine-receipt.schema.json",
                "verifier-decision.schema.json",
            ):
                add(f"app-builder/schemas/{name}", app_builder / "schemas" / name)
            inspector_glue = app_builder / "descriptor_reconciliation.py"
            codeagent_adapter = artifact_root / "codeagent-adapter" / "codeagent_adapter.py"
            add("codeagent-adapter/codeagent_adapter.py", codeagent_adapter)
            add("codeagent-adapter/docker_provider.py", artifact_root / "codeagent-adapter" / "docker_provider.py")
            for name in ("codeagent_launcher.py", "docker_executor.py", "host_budget.py", "docker/entry.mjs", "docker/opencode-provider.mjs"):
                add(f"codeagent-launcher/{name}", artifact_root / "codeagent-launcher" / name)
            add(
                "desktop/fixtures/state.json",
                desktop_root / "fixtures" / "state.json",
            )
            add(
                "desktop/runtime-apps/hello/component.wasm",
                desktop_root / "runtime-apps" / "hello" / "component.wasm",
            )
            source_root = desktop_root / "src-tauri"
            for name in ("Cargo.lock", "Cargo.toml", "build.rs", "tauri.conf.json"):
                add(f"desktop/src-tauri/{name}", source_root / name)
            add(
                "desktop/src-tauri/src/main.rs",
                source_root / "src" / "main.rs",
            )
            frontend = desktop_root / "ui" / "app.js"
            add("desktop/ui/app.js", frontend, b"const VERSION = 1;\n")
            orchestrator = artifact_root / "orchestrator"
            for name in (
                "delivery_controller.py",
                "delivery_history.py",
                "runtime_readiness.py",
                "task_archive.py",
                "dry_run_adapter.py",
                "orchestrator.py",
            ):
                add(f"orchestrator/{name}", orchestrator / name)
            add(
                "wit/experimental-v0/contract.wit",
                project_root / "wit" / "experimental-v0" / "contract.wit",
            )

            aggregate = hashlib.sha256()
            for label, path in sorted(labeled_inputs, key=lambda item: item[0].encode()):
                data = path.read_bytes()
                aggregate.update(label.encode())
                aggregate.update(b"\0")
                aggregate.update(hashlib.sha256(data).hexdigest().encode())
                aggregate.update(b"\0")
                aggregate.update(str(len(data)).encode())
                aggregate.update(b"\0")
            marker = (
                "VIBAPP_DESKTOP_BUILD_INPUTS_SHA256=" + aggregate.hexdigest()
            ).encode()

            # Independently reconstructed inputs must agree with Node as well as
            # the production POSIX/Rust receipt inventory. Starter tests/evidence
            # are deliberately outside the exact three-file set.
            for relative in ("tests/fixture.rs", "output/private-evidence.json", "check_support.py"):
                excluded = cloud / "starter" / relative
                excluded.parent.mkdir(parents=True, exist_ok=True)
                excluded.write_bytes(b"not a shipped trusted input\n")
            node = shutil.which("node")
            self.assertIsNotNone(node, "Node is required to compare build receipt implementations")
            node_digest = subprocess.run(
                [node, "--input-type=module", "-e",
                 "const m=await import(process.argv[1]);console.log(JSON.stringify(await m.computeDesktopBuildInputs(process.argv[2])))",
                 (DESKTOP_ROOT / "scripts/desktop-build-inputs.mjs").as_uri(), str(desktop_root)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=10,
            )
            self.assertEqual(node_digest.returncode, 0, node_digest.stderr.decode(errors="replace"))
            node_receipt = json.loads(node_digest.stdout)
            self.assertEqual(node_receipt["sha256"], aggregate.hexdigest())
            self.assertEqual(node_receipt["file_count"], len(labeled_inputs))

            public_inputs: list[tuple[str, Path]] = []

            def add_public(label: str, path: Path, content: bytes = b"input\n") -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                os.utime(path, ns=(base_ns, base_ns))
                public_inputs.append((label, path))

            add_public(
                "product-platform/registry/package-locators/README.md",
                artifact_root
                / "product-platform"
                / "registry"
                / "package-locators"
                / "README.md",
            )
            add_public(
                "product-platform/registry/snapshots/registry.snapshot.json",
                artifact_root
                / "product-platform"
                / "registry"
                / "snapshots"
                / "registry.snapshot.json",
            )
            add_public(
                "web-client-core/sync-public-package-locators.mjs",
                artifact_root / "web-client-core" / "sync-public-package-locators.mjs",
            )
            add_public(
                "web-client-core/sync-web-gui.mjs",
                artifact_root / "web-client-core" / "sync-web-gui.mjs",
            )
            public_aggregate = hashlib.sha256()
            for label, path in sorted(public_inputs, key=lambda item: item[0].encode()):
                data = path.read_bytes()
                public_aggregate.update(label.encode())
                public_aggregate.update(b"\0")
                public_aggregate.update(hashlib.sha256(data).hexdigest().encode())
                public_aggregate.update(b"\0")
                public_aggregate.update(str(len(data)).encode())
                public_aggregate.update(b"\0")
            public_marker = (
                "VIBAPP_PUBLIC_REGISTRY_SOURCE_SHA256="
                + public_aggregate.hexdigest()
            ).encode()

            for name in ("vibapp-launcher", "vibapp-runtime"):
                binary = desktop_root / "target" / "debug" / name
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"\0" + marker + b"\0" + public_marker + b"\0")
                binary.chmod(0o755)
                os.utime(binary, ns=(base_ns + 20_000_000_000,) * 2)

            service_source = artifact_root / "runtime-daemon" / "service-runtime"
            for relative in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
                path = service_source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("service input\n", encoding="utf-8")
                os.utime(path, ns=(base_ns, base_ns))
            service_binary = (
                artifact_root
                / "runtime-daemon"
                / "target-service-1_98"
                / "release"
                / "vibapp-service-runtime"
            )
            service_binary.parent.mkdir(parents=True)
            service_binary.write_bytes(b"service runtime\n")
            service_binary.chmod(0o755)
            os.utime(service_binary, ns=(base_ns + 20_000_000_000,) * 2)

            self.assertEqual(
                len(labeled_inputs),
                len({label for label, _ in labeled_inputs}),
                "the independent digest oracle must contain each Rust build input once",
            )

            current = subprocess.run(
                ["/bin/sh", str(script), "debug"],
                cwd=desktop_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            current_stderr = current.stderr.decode(errors="replace")
            self.assertNotIn("desktop build-input receipt mismatch", current_stderr)
            self.assertTrue(
                (desktop_root / "dist").exists(),
                "matching Rust/POSIX digests must pass the receipt gate and reach staging",
            )
            shutil.rmtree(desktop_root / "dist")

            # Exercise the package's second digest gate with a mutation that occurs
            # only after both embedded receipts have already been accepted. The
            # shortened harness uses the production hash/check functions but stops
            # before unrelated macOS/RoomHash staging prerequisites.
            staging_marker = '\nmkdir -p "$desktop_root/dist"\n'
            post_receipt_harness = self.package.split(staging_marker, 1)[0] + (
                "\n/usr/bin/printf 'const VERSION = 2;\\n' > \"$desktop_root/ui/app.js\"\n"
                "require_unchanged_build_sources_before_sign\n"
                "exit 99\n"
            )
            script.write_text(post_receipt_harness, encoding="utf-8")
            post_receipt_mutation = subprocess.run(
                ["/bin/sh", str(script), "debug"],
                cwd=desktop_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            post_receipt_stderr = post_receipt_mutation.stderr.decode(errors="replace")
            self.assertEqual(post_receipt_mutation.returncode, 65, post_receipt_stderr)
            self.assertIn(
                "desktop build inputs changed after binary receipt verification and resource staging",
                post_receipt_stderr,
            )
            self.assertNotIn("desktop build-input receipt mismatch", post_receipt_stderr)
            script.write_text(self.package, encoding="utf-8")
            frontend.write_bytes(b"const VERSION = 1;\n")
            os.utime(frontend, ns=(base_ns, base_ns))

            # Keep the mutation older than both binaries. The legacy mtime guard
            # cannot see it; only the embedded digest receipt can reject it.
            frontend.write_bytes(b"const VERSION = 2;\n")
            os.utime(frontend, ns=(base_ns, base_ns))

            completed = subprocess.run(
                ["/bin/sh", str(script), "debug"],
                cwd=desktop_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            stderr = completed.stderr.decode(errors="replace")
            self.assertEqual(completed.returncode, 65, stderr)
            self.assertIn(
                "desktop build-input receipt mismatch: vibapp-launcher was not built "
                "from the current frontend and embedded inputs",
                stderr,
            )
            self.assertFalse(
                (desktop_root / "dist").exists(),
                "receipt mismatch must fail before a staging directory is created",
            )

            frontend.write_bytes(b"const VERSION = 1;\n")
            os.utime(frontend, ns=(base_ns, base_ns))

            inspector_glue.write_bytes(b"# changed inspector glue\n")
            os.utime(inspector_glue, ns=(base_ns, base_ns))
            inspector_mutation = subprocess.run(
                ["/bin/sh", str(script), "debug"],
                cwd=desktop_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            inspector_stderr = inspector_mutation.stderr.decode(errors="replace")
            self.assertEqual(inspector_mutation.returncode, 65, inspector_stderr)
            self.assertIn("desktop build-input receipt mismatch", inspector_stderr)
            self.assertFalse((desktop_root / "dist").exists())

            inspector_glue.write_bytes(b"input\n")
            os.utime(inspector_glue, ns=(base_ns, base_ns))
            for name in ("README.md", "opencode_instructions.txt", "codex_instructions.txt", "vibapp_support.rs"):
                support_input = cloud / "starter" / name
                support_input.write_bytes(b"changed trusted input\n")
                os.utime(support_input, ns=(base_ns, base_ns))
                mutation = subprocess.run(
                    ["/bin/sh", str(script), "debug"], cwd=desktop_root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=10,
                )
                self.assertEqual(mutation.returncode, 65, mutation.stderr.decode(errors="replace"))
                self.assertIn(b"desktop build-input receipt mismatch", mutation.stderr)
                self.assertFalse((desktop_root / "dist").exists())
                support_input.write_bytes(b"input\n")
                os.utime(support_input, ns=(base_ns, base_ns))
            for name in ("entry.mjs", "opencode-provider.mjs"):
                bridge_input = artifact_root / "codeagent-launcher" / "docker" / name
                bridge_input.write_bytes(b"changed trusted bridge\n")
                os.utime(bridge_input, ns=(base_ns, base_ns))
                mutation = subprocess.run(
                    ["/bin/sh", str(script), "debug"], cwd=desktop_root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=10,
                )
                self.assertEqual(mutation.returncode, 65, mutation.stderr.decode(errors="replace"))
                self.assertIn(b"desktop build-input receipt mismatch", mutation.stderr)
                self.assertFalse((desktop_root / "dist").exists())
                bridge_input.write_bytes(b"input\n")
                os.utime(bridge_input, ns=(base_ns, base_ns))
            (frontend.parent / "unsafe-link.js").symlink_to(frontend)
            unsafe = subprocess.run(
                ["/bin/sh", str(script), "debug"],
                cwd=desktop_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            unsafe_stderr = unsafe.stderr.decode(errors="replace")
            self.assertEqual(unsafe.returncode, 66, unsafe_stderr)
            self.assertIn("desktop build input must not be a symlink:", unsafe_stderr)
            self.assertNotIn("desktop build-input receipt mismatch", unsafe_stderr)
            self.assertFalse(
                (desktop_root / "dist").exists(),
                "input enumeration failure must propagate before staging",
            )

    def test_descriptor_reconciliation_is_packaged_next_to_verifier(self) -> None:
        required = (
            'app_descriptor_reconciliation="$app_builder_root/descriptor_reconciliation.py"',
            '"$app_descriptor_reconciliation"',
            'cp "$app_descriptor_reconciliation" "$contents/Resources/app-builder/descriptor_reconciliation.py"',
        )
        for fragment in required:
            self.assertIn(fragment, self.package)

        descriptor_module = (
            DESKTOP_ROOT.parent / "app-builder" / "descriptor_reconciliation.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'BASE.parent / "runtime-daemon/service-runtime/vibapp-service-runtime"',
            descriptor_module,
        )

    def test_service_runtime_freshness_is_checked_before_staging(self) -> None:
        required = (
            'service_runtime_source_root="$runtime_daemon_root/service-runtime"',
            '"$service_runtime_source_root/Cargo.toml"',
            '"$service_runtime_source_root/Cargo.lock"',
            'find "$service_rust_source_root" -type f -name \'*.rs\' -newer "$service_runtime" -print',
            "stale service runtime binary: vibapp-service-runtime is older than",
            "rebuild the service runtime consumed by this package command:",
            'CARGO_TARGET_DIR=\\"$runtime_daemon_root/target-service-1_98\\" cargo build --manifest-path \\"$service_runtime_source_root/Cargo.toml\\" --offline --locked --release',
            "require_fresh_service_runtime",
        )
        for fragment in required:
            self.assertIn(fragment, self.package)
        self.assertLess(
            self.package.index("\nrequire_fresh_service_runtime\n"),
            self.package.index('mkdir -p "$desktop_root/dist"'),
        )

    def test_each_newer_service_runtime_input_fails_before_packaging(self) -> None:
        base_ns = 1_700_000_000_000_000_000

        for stale_relative in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
            with self.subTest(input=stale_relative), tempfile.TemporaryDirectory(
                prefix="vibapp-stale-service-runtime-"
            ) as temporary:
                artifact_root = Path(temporary) / "artifacts"
                desktop_root = artifact_root / "desktop"
                script = desktop_root / "scripts" / "package-macos.sh"
                script.parent.mkdir(parents=True)
                script.write_text(self.package, encoding="utf-8")

                desktop_source = desktop_root / "src-tauri"
                (desktop_source / "src").mkdir(parents=True)
                desktop_inputs = (
                    desktop_source / "Cargo.toml",
                    desktop_source / "Cargo.lock",
                    desktop_source / "build.rs",
                    desktop_source / "src" / "main.rs",
                )
                for source in desktop_inputs:
                    source.write_text("synthetic contract input\n", encoding="utf-8")
                    os.utime(source, ns=(base_ns, base_ns))

                desktop_binaries = (
                    desktop_root / "target" / "debug" / "vibapp-launcher",
                    desktop_root / "target" / "debug" / "vibapp-runtime",
                )
                for binary in desktop_binaries:
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(b"synthetic desktop binary\n")
                    binary.chmod(0o755)
                    os.utime(binary, ns=(base_ns + 10_000_000_000,) * 2)

                runtime_root = artifact_root / "runtime-daemon"
                reported_runtime_root = desktop_root / ".." / "runtime-daemon"
                service_source = runtime_root / "service-runtime"
                reported_service_source = reported_runtime_root / "service-runtime"
                (service_source / "src").mkdir(parents=True)
                service_inputs = {
                    "Cargo.toml": service_source / "Cargo.toml",
                    "Cargo.lock": service_source / "Cargo.lock",
                    "src/main.rs": service_source / "src" / "main.rs",
                }
                for source in service_inputs.values():
                    source.write_text("synthetic service input\n", encoding="utf-8")
                    os.utime(source, ns=(base_ns, base_ns))

                service_binary = (
                    runtime_root
                    / "target-service-1_98"
                    / "release"
                    / "vibapp-service-runtime"
                )
                service_binary.parent.mkdir(parents=True)
                service_binary.write_bytes(b"synthetic service runtime\n")
                service_binary.chmod(0o755)
                os.utime(service_binary, ns=(base_ns + 10_000_000_000,) * 2)
                os.utime(
                    service_inputs[stale_relative],
                    ns=(base_ns + 20_000_000_000,) * 2,
                )

                completed = subprocess.run(
                    ["/bin/sh", str(script), "debug"],
                    cwd=desktop_root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )
                stderr = completed.stderr.decode(errors="replace")
                self.assertEqual(completed.returncode, 65, stderr)
                self.assertIn(
                    "stale service runtime binary: vibapp-service-runtime is older than "
                    f"{reported_service_source / stale_relative}",
                    stderr,
                )
                self.assertIn(
                    f'CARGO_TARGET_DIR="{reported_runtime_root}/target-service-1_98" cargo build '
                    f'--manifest-path "{reported_service_source}/Cargo.toml" '
                    "--offline --locked --release",
                    stderr,
                )
                self.assertFalse(
                    (desktop_root / "dist").exists(),
                    "freshness failure must happen before a staging directory is created",
                )

    def test_stale_launcher_and_runtime_each_fail_closed_with_exact_build_hint(self) -> None:
        stage_pattern = DESKTOP_ROOT / "dist"
        stage_roots_before = set(stage_pattern.glob(".vibapp-package.*"))
        newest_input_mtime = max(
            path.stat().st_mtime
            for path in (
                DESKTOP_ROOT / "src-tauri" / "Cargo.toml",
                DESKTOP_ROOT / "src-tauri" / "Cargo.lock",
                DESKTOP_ROOT / "src-tauri" / "build.rs",
                *sorted((DESKTOP_ROOT / "src-tauri" / "src").rglob("*.rs")),
            )
        )
        with tempfile.TemporaryDirectory(prefix="vibapp-stale-desktop-binary-") as temporary:
            target = Path(temporary) / "target"
            profile = target / "debug"
            profile.mkdir(parents=True)
            binaries = {
                name: profile / name for name in ("vibapp-launcher", "vibapp-runtime")
            }
            for binary in binaries.values():
                binary.write_bytes(b"synthetic contract-test binary\n")
                binary.chmod(0o755)

            for stale_name in binaries:
                with self.subTest(binary=stale_name):
                    for binary in binaries.values():
                        os.utime(binary, (newest_input_mtime + 60, newest_input_mtime + 60))
                    os.utime(binaries[stale_name], (1, 1))
                    environment = dict(os.environ)
                    environment["CARGO_TARGET_DIR"] = str(target)
                    completed = subprocess.run(
                        [str(PACKAGE_SCRIPT), "debug"],
                        cwd=DESKTOP_ROOT,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=10,
                    )
                    stderr = completed.stderr.decode(errors="replace")
                    self.assertEqual(completed.returncode, 65, stderr)
                    self.assertIn(f"stale desktop binary: {stale_name} is older than", stderr)
                    self.assertIn(
                        f'CARGO_TARGET_DIR="{target}" cargo build --manifest-path '
                        f'"{DESKTOP_ROOT}/src-tauri/Cargo.toml" --locked '
                        '--bin vibapp-launcher --bin vibapp-runtime',
                        stderr,
                    )

        self.assertEqual(set(stage_pattern.glob(".vibapp-package.*")), stage_roots_before)


if __name__ == "__main__":
    unittest.main()
