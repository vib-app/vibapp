"""No-provider Docker admission races and opt-in actual CLI overlap checks."""
import base64
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_executor as docker
import host_budget
import codeagent_launcher as launcher
from test_codex_container import CONTRACT, MODEL, PROMPT, SOURCE, SUPPORT, SyntheticResponsesGateway

IMAGE = "sha256:" + "a" * 64


def identity(index):
    execution = "vibapp-" + docker.digest(str(index).encode())
    return execution, "vibapp-ca-" + docker.digest(execution.encode())[:32]


def container(execution, name, *, running=False):
    return {"Id": name, "Image": IMAGE, "Config": {"Labels": {
        "ai.vibapp.execution": execution, "ai.vibapp.owner": str(os.getuid()),
        "ai.vibapp.role": "codeagent"}}, "State": {"Running": running}}


def fake_command(containers, arguments, **_):
    if arguments[0] == "ps":
        if "--all" not in arguments:
            raise AssertionError("created and exited workers must count")
        return mock.Mock(returncode=0, stdout="\n".join(containers.keys()).encode())
    if arguments[0] == "create":
        _, name, execution = arguments
        time.sleep(.03)  # Exposes a count/create race unless the lock is atomic.
        containers[name] = container(execution, name)
        return mock.Mock(returncode=0)
    if arguments[:3] == ["container", "rm", "--force"]:
        containers.pop(arguments[3], None)
        return mock.Mock(returncode=0)
    raise AssertionError(arguments)


def admission_worker(root, index, containers, start, results):
    execution, name = identity(index)
    executor = docker.DockerExecutor(image_id=IMAGE, input_payload={}, gateway=None, limits={}, state_root=Path(root) / str(index))
    with mock.patch.object(host_budget, "_runtime_directory", return_value=Path(root)), mock.patch.object(executor, "_inspect", side_effect=lambda name: containers.get(name)), mock.patch.object(docker, "docker_command", side_effect=lambda arguments: fake_command(containers, arguments)):
        start.wait(10)
        try:
            executor._create_reserved(name, execution, ["create", name, execution], threading.Event(), 5)
            results.put(("created", index))
        except docker.DockerError as error:
            results.put((error.code, index))


class DockerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vibapp-admission-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patcher = mock.patch.object(host_budget, "_runtime_directory", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def executor(self):
        return docker.DockerExecutor(image_id=IMAGE, input_payload={}, gateway=None, limits={}, state_root=self.root)

    def test_process_race_counts_created_orphan_workers_until_exact_cleanup(self):
        context = multiprocessing.get_context("spawn")
        with context.Manager() as manager:
            containers = manager.dict()
            start, results = context.Event(), context.Queue()
            processes = [context.Process(target=admission_worker, args=(str(self.root), index, containers, start, results)) for index in range(6)]
            try:
                for process in processes:
                    process.start()
                start.set()
                outcomes = [results.get(timeout=15) for _ in processes]
                self.assertEqual(sum(row[0] == "created" for row in outcomes), 2, outcomes)
                self.assertEqual(sum(row[0] == "docker-capacity-unavailable" for row in outcomes), 4, outcomes)
                self.assertEqual(len(containers), 2)
                for process in processes:
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
                # All owners have exited and no container has started; neither
                # fact frees an occupied Docker reservation.
                executor = self.executor()
                execution, name = identity(10)
                with mock.patch.object(executor, "_inspect", side_effect=lambda name: containers.get(name)), mock.patch.object(docker, "docker_command", side_effect=lambda arguments: fake_command(containers, arguments)):
                    with self.assertRaisesRegex(docker.DockerError, "capacity"):
                        executor._create_reserved(name, execution, ["create", name, execution], threading.Event(), 1)
                    victim = next(row[1] for row in outcomes if row[0] == "created")
                    victim_execution, victim_name = identity(victim)
                    self.assertTrue(executor._cleanup(victim_name, victim_execution))
                    executor._create_reserved(name, execution, ["create", name, execution], threading.Event(), 1)
                    self.assertEqual(len(containers), 2)
                    self.assertTrue(executor._cleanup(name, execution))
                    for other_name, other in list(containers.items()):
                        self.assertTrue(executor._cleanup(other_name, other["Config"]["Labels"]["ai.vibapp.execution"]))
                    self.assertEqual(len(containers), 0)
                with host_budget.private_directory("docker-admission") as directory:
                    self.assertEqual(executor._reservations(directory), {})
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                    process.close()

    def test_old_unreserved_created_and_running_containers_both_count(self):
        executor = self.executor()
        execution, name = identity(20)
        old = {identity(index)[1]: container(*identity(index), running=index == 1) for index in range(2)}
        with mock.patch.object(executor, "_inspect", return_value=None), mock.patch.object(docker, "docker_command", side_effect=lambda arguments: fake_command(old, arguments)) as command:
            with self.assertRaisesRegex(docker.DockerError, "capacity"):
                executor._create_reserved(name, execution, ["create", name, execution], threading.Event(), 1)
        self.assertEqual(command.call_count, 1)

    def test_uncertain_create_stays_reserved_without_visible_container(self):
        executor = self.executor()
        execution, name = identity(30)
        containers = {}
        def timeout_create(arguments):
            if arguments[0] == "create":
                raise docker.DockerError("docker-control-unavailable")
            return fake_command(containers, arguments)
        with mock.patch.object(executor, "_inspect", side_effect=lambda name: containers.get(name)), mock.patch.object(docker, "docker_command", side_effect=timeout_create):
            with self.assertRaisesRegex(docker.DockerError, "control-unavailable"):
                executor._create_reserved(name, execution, ["create", name, execution], threading.Event(), 1)
            self.assertFalse(executor._cleanup(name, execution))
        with host_budget.private_directory("docker-admission") as directory:
            self.assertEqual(executor._reservations(directory)[name]["phase"], "creating")
        # Delayed daemon completion is still counted, then exact cleanup frees it.
        containers[name] = container(execution, name)
        with mock.patch.object(executor, "_inspect", side_effect=lambda name: containers.get(name)), mock.patch.object(docker, "docker_command", side_effect=lambda arguments: fake_command(containers, arguments)):
            self.assertTrue(executor._cleanup(name, execution))
        with host_budget.private_directory("docker-admission") as directory:
            self.assertEqual(executor._reservations(directory), {})

    def test_precancel_never_creates_and_busy_mutex_wait_is_cancellable(self):
        executor = self.executor()
        execution, name = identity(40)
        cancellation = threading.Event()
        cancellation.set()
        with mock.patch.object(docker, "docker_command") as command:
            with self.assertRaisesRegex(docker.DockerError, "provider-cancelled"):
                executor._create_reserved(name, execution, ["create", name, execution], cancellation, 1)
        command.assert_not_called()
        cancellation.clear()
        errors = []
        def waiting():
            try:
                executor._create_reserved(name, execution, ["create", name, execution], cancellation, 5)
            except docker.DockerError as error:
                errors.append(error.code)
        with host_budget.lease("docker-admission"):
            thread = threading.Thread(target=waiting)
            thread.start()
            cancellation.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["provider-cancelled"])


class PausedSyntheticGateway(SyntheticResponsesGateway):
    def __init__(self):
        super().__init__()
        self.arrived, self.resume = threading.Event(), threading.Event()
        self.tool_outputs = []

    def relay(self, message, emit, cancelled):
        body = json.loads(base64.b64decode(message["body"], validate=True))
        self.tool_outputs = [{"type": item.get("type"), "call_id": item.get("call_id"),
                              "output": str(item.get("output"))[:4096]}
                             for item in body.get("input", []) if item.get("type") in
                             {"custom_tool_call_output", "function_call_output"}][:8]
        self.arrived.set()
        while not self.resume.wait(.02):
            if cancelled.is_set():
                raise docker.DockerError("provider-cancelled")
        super().relay(message, emit, cancelled)


@contextmanager
def container_test_directory():
    retained_root = os.environ.get("VIBAPP_CODEX_TEST_EVIDENCE_ROOT")
    if retained_root:
        destination = Path(retained_root)
        if not destination.is_absolute():
            raise ValueError("VIBAPP_CODEX_TEST_EVIDENCE_ROOT must be absolute")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = tempfile.mkdtemp(prefix="parallel-offline-", dir=destination)
        print("Offline parallel Docker evidence: " + directory, flush=True)
        yield directory
    else:
        with tempfile.TemporaryDirectory(prefix="vibapp-parallel-offline-") as directory:
            yield directory


@unittest.skipUnless(os.environ.get("VIBAPP_TEST_CODEX_PARALLEL") == "1", "explicit offline parallel Docker opt-in required")
class DockerParallelContainerTests(unittest.TestCase):
    def test_two_actual_clis_overlap_third_refused_cancel_is_scoped(self):
        observed = docker.image_identity(docker.IMAGE, provider="codex")
        with container_test_directory() as directory:
            inputs = {"contracts/diagnostic.txt": CONTRACT, "source/src/vibapp_support.rs": SUPPORT}
            files = [{"path": path, "base64": base64.b64encode(content).decode(), "sha256": docker.digest(content)} for path, content in inputs.items()]
            limits = {"memory_bytes": 2 * 1024**3, "pids": 64, "wall_time_seconds": 90,
                      "cpu_seconds": 45, "workspace_bytes": 16 * 1024**2}
            profile = launcher.ProviderProfile(
                provider_profile_id="codex-offline-parallel", provider_profile_digest_sha256=docker.digest(docker.canonical(observed)),
                provider_id="codex", allowed_models=frozenset({MODEL}), executor_kind=launcher.ExecutorKind.DOCKER,
                image_digest_sha256=observed["image_id"][7:], resource_policy_id="offline-parallel", resource_policy_digest_sha256=docker.digest(docker.canonical(limits)),
                network_policy_id="synthetic-responses-only", network_policy_digest_sha256=observed["policy_sha256"], max_output_bytes=4 * 1024**2,
                output_media_type="application/vnd.vibapp.source-files+json")
            jobs = []
            def submit(index):
                root = Path(directory) / str(index)
                gateway = PausedSyntheticGateway()
                executor = docker.DockerExecutor(image_id=observed["image_id"], input_payload={"files": files, "prompt": PROMPT, "model": MODEL}, gateway=gateway, limits=limits, state_root=root)
                request = launcher.LaunchRequest.from_mapping({
                    "schema_version": launcher.SCHEMA_VERSION, "job_id": "offline-parallel-" + str(index), "attempt_id": Path(directory).name, "idempotency_key": str(index),
                    "provider_profile_id": profile.provider_profile_id, "provider_id": "codex", "model": MODEL,
                    "task_digest_sha256": docker.digest(PROMPT.encode()), "input_digest_sha256": docker.digest(docker.canonical(files)), "prompt_digest_sha256": docker.digest(PROMPT.encode()),
                    "resource_policy_id": profile.resource_policy_id, "network_policy_id": profile.network_policy_id})
                execution = "vibapp-" + request.canonical_digest_sha256()
                executor.launch(request, profile, execution)
                jobs.append((executor, execution, gateway))
                return executor, execution, gateway
            with mock.patch.object(docker, "codex_connection", side_effect=AssertionError("credentials forbidden")), mock.patch.object(docker.http.client, "HTTPConnection", side_effect=AssertionError("network forbidden")), mock.patch.object(docker.http.client, "HTTPSConnection", side_effect=AssertionError("network forbidden")):
                try:
                    first, first_id, first_gateway = submit(1)
                    second, second_id, second_gateway = submit(2)
                    self.assertTrue(first_gateway.arrived.wait(30), first.failure_code)
                    self.assertTrue(second_gateway.arrived.wait(30), second.failure_code)
                    names = {first.jobs[first_id]["name"], second.jobs[second_id]["name"]}
                    self.assertTrue(names <= docker.DockerExecutor._owned_containers())
                    for executor, execution, _ in jobs:
                        inspected = executor._inspect(executor.jobs[execution]["name"])
                        thread_probe = docker.docker_command([
                            "container", "top", executor.jobs[execution]["name"], "-eo", "pid,ppid,nlwp,comm"])
                        self.assertEqual(thread_probe.returncode, 0)
                        process_threads = [{"threads": int(row.split()[2]), "command": row.split()[3]}
                                           for row in thread_probe.stdout.decode().splitlines()[1:]]
                        self.assertTrue(inspected["State"]["Running"])
                        self.assertEqual(inspected["HostConfig"]["NetworkMode"], "none")
                        self.assertEqual(inspected["Mounts"], [])
                        self.assertEqual(inspected["HostConfig"]["Memory"], 2 * 1024**3)
                        self.assertEqual(inspected["HostConfig"]["NanoCpus"], 2 * 10**9)
                        (executor.state_root / "offline-container-inspection.json").write_bytes(docker.canonical({
                            "container_id": inspected["Id"], "running": inspected["State"]["Running"],
                            "image_id": inspected["Image"], "mounts": inspected["Mounts"],
                            "network_mode": inspected["HostConfig"]["NetworkMode"],
                            "memory_bytes": inspected["HostConfig"]["Memory"],
                            "nano_cpus": inspected["HostConfig"]["NanoCpus"],
                            "paused_process_threads": process_threads,
                            "paused_total_threads": sum(row["threads"] for row in process_threads),
                            "provider_mode": "offline-synthetic-responses-no-external-io"}))
                    third, third_id, third_gateway = submit(3)
                    third.jobs[third_id]["thread"].join(20)
                    self.assertEqual(third.failure_code, "docker-capacity-unavailable")
                    self.assertEqual(third_gateway.requests, 0)
                    self.assertTrue(third.status(third_id).cleanup_confirmed)
                    first.cancel(first_id)
                    first.jobs[first_id]["thread"].join(20)
                    self.assertEqual(first.status(first_id).state, launcher.BackendState.CANCELLED)
                    self.assertTrue(second._inspect(second.jobs[second_id]["name"])["State"]["Running"])
                    self.assertFalse(second_gateway.closed)
                    second_gateway.resume.set()
                    second.jobs[second_id]["thread"].join(40)
                    self.assertEqual(second.status(second_id).state, launcher.BackendState.SUCCEEDED,
                                     (second.failure_code, second_gateway.used_exec_fallback, second_gateway.advertised_tools, second_gateway.tool_outputs))
                    self.assertTrue(second_gateway.tool_result_verified)
                    exported = json.loads(second.status(second_id).success.output)["files"]
                    self.assertEqual({record["path"]: base64.b64decode(record["base64"]) for record in exported}, {**inputs, "source/src/lib.rs": SOURCE})
                    (second.state_root / "offline-exported-files.json").write_bytes(second.status(second_id).success.output)
                finally:
                    for executor, execution, gateway in jobs:
                        executor.cancel(execution)
                        gateway.resume.set()
                        executor.jobs[execution]["thread"].join(20)
                        self.assertFalse(executor.jobs[execution]["thread"].is_alive())
                        self.assertTrue(executor.status(execution).cleanup_confirmed)
                        self.assertTrue(executor.status(execution).whole_job_quiescent)
                        self.assertIsNone(executor._inspect(executor.jobs[execution]["name"]))
                        (executor.state_root / "offline-tool-diagnostics.json").write_bytes(docker.canonical({
                            "synthetic_only": True, "used_exec_fallback": gateway.used_exec_fallback,
                            "advertised_tools": gateway.advertised_tools, "tool_outputs": gateway.tool_outputs}))
                (Path(directory) / "offline-parallel-verification.json").write_bytes(docker.canonical({
                    "schema_version": "vibapp.offline-parallel-test-v1", "passed": True,
                    "two_actual_codex_clis_overlapped": True, "third_refused_before_model_request": True,
                    "cancel_one_preserved_other_container": True, "survivor_exported_bytes_verified": True,
                    "all_cleanup_confirmed": True, "live_provider_requests": 0, "credential_discovery": False,
                    "image_id": observed["image_id"], "model": MODEL,
                    "execution_ids": [execution for _, execution, _ in jobs]}))


if __name__ == "__main__":
    unittest.main()
