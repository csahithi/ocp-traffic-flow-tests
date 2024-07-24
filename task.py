import enum
import json
import logging
import os
import sys
import threading
import time
import typing
import yaml

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterable
from threading import Thread
from typing import Any
from typing import Callable
from typing import Optional
from typing import TypeVar

import common
import host
import tftbase

from k8sClient import K8sClient
from logger import logger
from pluginbase import Plugin
from testSettings import TestSettings
from tftbase import BaseOutput
from tftbase import ClusterMode
from tftbase import ConnectionMode
from tftbase import PodType


EXTERNAL_PERF_SERVER = "external-perf-server"


T = TypeVar("T")


class _OperationState(enum.Enum):
    NEW = (1,)
    STARTING = (2,)
    RUNNING = (3,)
    STOPPING = 4
    STOPPED = 5


class TaskOperation:
    @typing.overload
    def __init__(
        self,
        *,
        log_name: str,
        thread_action: None = None,
        collect_action: Callable[[], BaseOutput],
        cancel_action: Optional[Callable[[], None]] = None,
        wait_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        pass

    @typing.overload
    def __init__(
        self,
        *,
        log_name: str,
        thread_action: Callable[[], BaseOutput],
        collect_action: None = None,
        cancel_action: Optional[Callable[[], None]] = None,
        wait_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        pass

    @typing.overload
    def __init__(
        self,
        *,
        log_name: str,
        thread_action: Callable[[], T],
        collect_action: Callable[[T], BaseOutput],
        cancel_action: Optional[Callable[[], None]] = None,
        wait_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        pass

    def __init__(
        self,
        *,
        log_name: str,
        thread_action: Optional[Callable[[], Any]] = None,
        collect_action: Optional[
            Callable[[], BaseOutput] | Callable[[Any], BaseOutput]
        ] = None,
        cancel_action: Optional[Callable[[], None]] = None,
        wait_ready: Optional[Callable[[], None]] = None,
    ) -> None:
        if thread_action is None and collect_action is None:
            raise ValueError("either thread_action or collect_action must be provided")
        if cancel_action is not None and thread_action is None:
            raise ValueError("cannot set cancel_action without thread_action")
        super().__init__()
        self.log_name = log_name

        self._thread_action = thread_action
        self._collect_action = collect_action
        self._cancel_action = cancel_action
        self._wait_ready = wait_ready

        self._thread: Optional[Thread] = None

        self._intermediate_result: Any

        self._state = _OperationState.NEW
        self._lock = threading.Lock()

    def access_thread(self) -> Optional[Thread]:
        with self._lock:
            if self._thread is None:
                if self._thread_action is None:
                    return None
                self._thread = Thread(
                    target=self._run_thread_action,
                    name=self.log_name,
                )
                # this also starts the thread right away
                self._thread.start()
            return self._thread

    def _run_thread_action(self) -> None:
        assert not hasattr(self, "_intermediate_result")
        assert self._thread_action is not None
        logger.debug(f"thread[{self.log_name}]: call action")

        try:
            result = self._thread_action()
        except Exception as e:
            import traceback

            logger.error(f"thread[{self.log_name}]: action raised exception {e}")
            logger.error(f"backtrace:\n{traceback.format_exc()}")
            os._exit(-1)

        with self._lock:
            assert not hasattr(self, "_intermediate_result")
            self._intermediate_result = result
        logger.debug(f"thread[{self.log_name}]: action completed ({result})")

    def start(self) -> None:
        with self._lock:
            assert self._state == _OperationState.NEW
            self._state = _OperationState.STARTING
        self.access_thread()
        self._start_wait_ready()

    def _start_wait_ready(self) -> None:
        with self._lock:
            if self._state.value > _OperationState.STARTING.value:
                return
            assert self._state == _OperationState.STARTING
        if self._wait_ready is not None:
            self._wait_ready()
        with self._lock:
            if self._state == _OperationState.STARTING:
                self._state = _OperationState.RUNNING

    def _cancel(self) -> None:
        if self._cancel_action is None:
            return
        logger.debug(f"thread[{self.log_name}]: cancel thread")
        self._cancel_action()
        logger.debug(f"thread[{self.log_name}]: cancel thread done")

    def finish(self, timeout: Optional[float] = None) -> BaseOutput:
        th = self.access_thread()
        if th is not None:
            with self._lock:
                assert self._state in (
                    _OperationState.STARTING,
                    _OperationState.RUNNING,
                )
                self._state = _OperationState.STOPPING
            t1 = timeout
            if self._cancel_action is not None:
                t1 = 0
            th.join(t1)
            if th.is_alive():
                # Abort and try to join again.
                if self._cancel_action is not None:
                    self._cancel()
                    th.join(timeout)
                if th.is_alive():
                    logger.error(
                        f"thread[{self.log_name}] did not terminate within the timeout {timeout}"
                    )
                    raise RuntimeError(
                        f"Thread {self.log_name} did not terminate within timeout {timeout}"
                    )

        result: Optional[BaseOutput] = None

        intermediate_result: Any

        with self._lock:
            if th is not None:
                assert self._state == _OperationState.STOPPING
                self._state = _OperationState.STOPPED
            else:
                assert self._state in (
                    _OperationState.STARTING,
                    _OperationState.RUNNING,
                )
                self._state = _OperationState.STOPPED

            if self._thread_action is None:
                intermediate_result = None
            elif not hasattr(self, "_intermediate_result"):
                # This really can only happen if we failed to join the thread.
                logger.error(f"thread[{self.log_name}] no result form thread received")
                result = BaseOutput(success=False, msg="failure to get thread result")
            else:
                intermediate_result = self._intermediate_result

        if result is not None:
            pass
        elif self._collect_action is None:
            assert self._thread_action
            result = intermediate_result
        elif self._thread_action:
            cb1 = typing.cast(Callable[[Any], BaseOutput], self._collect_action)
            result = cb1(intermediate_result)
        else:
            cb2 = typing.cast(Callable[[], BaseOutput], self._collect_action)
            result = cb2()

        assert isinstance(result, BaseOutput)

        logger.debug(f"thread[{self.log_name}]: got result {result}")
        return result


class Task(ABC):
    def __init__(
        self, ts: TestSettings, index: int, node_name: str, tenant: bool
    ) -> None:
        self.in_file_template = ""
        self.out_file_yaml = ""
        self.pod_name = ""
        self._setup_operation: Optional[TaskOperation] = None
        self._task_operation: Optional[TaskOperation] = None
        self._result: Optional[BaseOutput] = None
        self.lh = host.local
        self.index = index
        self.node_name = node_name
        self.tenant = tenant
        self.ts = ts
        self.tc = ts.cfg_descr.tc

        if not self.tenant and self.tc.mode == ClusterMode.SINGLE:
            raise ValueError("Cannot have non-tenant Task when cluster mode is single")

    @property
    def log_name(self) -> str:
        return self.__class__.__name__

    @property
    def log_name_setup(self) -> str:
        return f"{self.log_name}.setup"

    def get_namespace(self) -> str:
        return self.ts.cfg_descr.get_tft().namespace

    def get_duration(self) -> int:
        return self.ts.cfg_descr.get_tft().duration

    def get_template_args(self) -> dict[str, str | list[str]]:
        return {
            "name_space": self.get_namespace(),
            "test_image": tftbase.get_tft_test_image(),
            "image_pull_policy": tftbase.get_tft_image_pull_policy(),
            "command": ["/usr/bin/container-entry-point.sh"],
            "args": [],
            "index": f"{self.index}",
            "node_name": self.node_name,
        }

    def render_file(
        self,
        log_info: str,
        in_file_template: Optional[str] = None,
        out_file_yaml: Optional[str] = None,
        template_args: Optional[dict[str, str | list[str]]] = None,
    ) -> None:
        if in_file_template is None:
            in_file_template = self.in_file_template
        if out_file_yaml is None:
            out_file_yaml = self.out_file_yaml
        if template_args is None:
            template_args = self.get_template_args()
        logger.info(
            f'Generate {log_info} "{out_file_yaml}" (from "{in_file_template}", for {self.log_name})'
        )
        rendered = common.j2_render(in_file_template, out_file_yaml, template_args)

        rendered_dict = yaml.safe_load(rendered)
        logger.debug(f'"{in_file_template}" contains: {json.dumps(rendered_dict)}')

    def initialize(self) -> None:
        pass

    @property
    def client(self) -> K8sClient:
        return self.tc.client(tenant=self.tenant)

    def _get_run_oc_namespace(
        self,
        namespace: Optional[str] | common._MISSING_TYPE = common.MISSING,
    ) -> Optional[str]:
        if isinstance(namespace, common._MISSING_TYPE):
            # By default, set use self.get_namespace(). You can select another
            # namespace or no namespace (by setting to None).
            namespace = self.get_namespace()
        return namespace

    def run_oc(
        self,
        cmd: str | Iterable[str],
        *,
        may_fail: bool = False,
        die_on_error: bool = False,
        namespace: Optional[str] | common._MISSING_TYPE = common.MISSING,
    ) -> host.Result:
        return self.client.oc(
            cmd,
            may_fail=may_fail,
            die_on_error=die_on_error,
            namespace=self._get_run_oc_namespace(namespace),
        )

    def run_oc_exec(
        self,
        cmd: str | Iterable[str],
        *,
        may_fail: bool = False,
        die_on_error: bool = False,
        pod_name: Optional[str] = None,
        namespace: Optional[str] | common._MISSING_TYPE = common.MISSING,
    ) -> host.Result:
        if pod_name is None:
            pod_name = self.pod_name
        return self.client.oc_exec(
            cmd,
            pod_name=pod_name,
            may_fail=may_fail,
            die_on_error=die_on_error,
            namespace=self._get_run_oc_namespace(namespace),
        )

    def run_oc_get(
        self,
        what: str,
        *,
        may_fail: bool = False,
        die_on_error: bool = False,
        namespace: Optional[str] | common._MISSING_TYPE = common.MISSING,
    ) -> typing.Optional[dict[str, typing.Any]]:
        return self.client.oc_get(
            what,
            may_fail=may_fail,
            die_on_error=die_on_error,
            namespace=self._get_run_oc_namespace(namespace),
        )

    def get_pod_ip(self) -> str:
        y = self.run_oc_get(f"pod/{self.pod_name}", die_on_error=True)
        pod_ip = None
        try:
            if y:
                pod_ip = y["status"]["podIP"]
        except Exception:
            pass
        if not isinstance(pod_ip, str):
            raise RuntimeError("Failure to get static.podIP for {self.pod_name}")
        return pod_ip

    def create_cluster_ip_service(self) -> str:
        in_file_template = "./manifests/svc-cluster-ip.yaml.j2"
        out_file_yaml = "./manifests/yamls/svc-cluster-ip.yaml"

        self.render_file("Cluster IP Service", in_file_template, out_file_yaml)
        r = self.run_oc(f"apply -f {out_file_yaml}", may_fail=True)
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.error(r)
                sys.exit(-1)

        return self.run_oc(
            "get service tft-clusterip-service -o=jsonpath='{.spec.clusterIP}'"
        ).out

    def create_node_port_service(self, nodeport: int) -> str:
        in_file_template = "./manifests/svc-node-port.yaml.j2"
        out_file_yaml = "./manifests/yamls/svc-node-port.yaml"

        template_args = {
            **self.get_template_args(),
            "nodeport_svc_port": f"{nodeport}",
        }

        self.render_file(
            "Node Port Service", in_file_template, out_file_yaml, template_args
        )
        r = self.run_oc(f"apply -f {out_file_yaml}", may_fail=True)
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.error(r)
                sys.exit(-1)

        return self.run_oc(
            "get service tft-nodeport-service -o=jsonpath='{.spec.clusterIP}'"
        ).out

    def start_setup(self) -> None:
        assert self._setup_operation is None
        self._setup_operation = self._create_setup_operation()
        if self._setup_operation is not None:
            self._setup_operation.start()

    def _create_setup_operation(self) -> Optional[TaskOperation]:
        self.setup_pod()
        return None

    def finish_setup(self) -> None:
        if self._setup_operation is None:
            return
        to = self._setup_operation
        self._setup_operation = None
        to.finish(timeout=5)

    def setup_pod(self) -> None:
        # Check if pod already exists
        v = self.run_oc_get(f"pod/{self.pod_name}", may_fail=True)
        if v is None:
            logger.info(f"Creating Pod {self.pod_name}.")
            self.run_oc(f"apply -f {self.out_file_yaml}", die_on_error=True)
        else:
            logger.info(f"Pod {self.pod_name} already exists.")

        logger.info(f"Waiting for Pod {self.pod_name} to become ready.")
        self.run_oc(
            f"wait --for=condition=ready pod/{self.pod_name} --timeout=1m",
            die_on_error=True,
        )

    def start_task(self) -> None:
        assert self._task_operation is None
        self._task_operation = self._create_task_operation()
        if self._task_operation:
            self._task_operation.start()

    def _create_task_operation(self) -> Optional[TaskOperation]:
        return None

    def finish_task(self) -> None:
        if self._task_operation is None:
            return
        assert self._result is None
        logger.info(f"Completing execution on {self.log_name}")
        self._result = self._task_operation.finish(timeout=self.get_duration() * 1.5)

    def aggregate_output(self, out: tftbase.TftAggregateOutput) -> None:
        if self._result is None:
            return
        if not isinstance(self._result, tftbase.AggregatableOutput):
            # This output has nothing to collect. We are done.
            return

        result = self._result

        if isinstance(result, tftbase.FlowTestOutput):
            if out.flow_test is not None:
                raise RuntimeError("There can only be one FlowTestOutput")
            out.flow_test = result
            if result.success:
                log_level = logging.INFO
                log_msg = "success"
            else:
                log_level = logging.ERROR
                log_msg = "failure"
            logger.log(log_level, f"Results of {self.ts.get_test_str()}: {log_msg}")
            logger.debug(f"result: {common.dataclass_to_json(result)}")

            if type(self)._aggregate_output is Task._aggregate_output:
                # This instance did not overwrite _aggregate_output(). This is
                # fine for a task that returned the FlowTestOutput. Don't call
                # _aggregate_output.
                return

        self._aggregate_output(result, out)

    def _aggregate_output(
        self,
        result: tftbase.AggregatableOutput,
        out: tftbase.TftAggregateOutput,
    ) -> None:
        # This should never happen.
        #
        # A task that returns an AggregatableOutput *must* implement _aggregate_output().
        #
        # Exception: if the task is a test and returns a flow_test, then it may
        # not override this method. aggregate_output() will take care to not
        # call in that case.
        raise RuntimeError(
            f"Task {self.log_name} should not be called to aggregate output {result} "
        )


class ServerTask(Task, ABC):
    def __init__(self, ts: TestSettings):
        super().__init__(ts, ts.server_index, ts.node_server_name, ts.server_is_tenant)

        connection_mode = ts.connection_mode
        pod_type = ts.server_pod_type
        node_name = self.node_name
        port = 5201 + self.index

        if connection_mode == ConnectionMode.EXTERNAL_IP:
            in_file_template = ""
            out_file_yaml = ""
            pod_name = EXTERNAL_PERF_SERVER
        elif pod_type == PodType.SRIOV:
            in_file_template = "./manifests/sriov-pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/sriov-pod-{node_name}-server.yaml"
            pod_name = f"sriov-pod-{node_name}-server-{port}"
        elif pod_type == PodType.NORMAL:
            in_file_template = "./manifests/pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/pod-{node_name}-server.yaml"
            pod_name = f"normal-pod-{node_name}-server-{port}"
        elif pod_type == PodType.HOSTBACKED:
            in_file_template = "./manifests/host-pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/host-pod-{node_name}-server.yaml"
            pod_name = f"host-pod-{node_name}-server-{port}"
        else:
            raise ValueError("Invalid pod_type {pod_type}")

        self.exec_persistent = ts.conf_server.persistent
        self.port = port
        self.pod_type = pod_type
        self.connection_mode = ts.connection_mode
        self.in_file_template = in_file_template
        self.out_file_yaml = out_file_yaml
        self.pod_name = pod_name

    def get_template_args(self) -> dict[str, str | list[str]]:

        extra_args: dict[str, str] = {}
        if self.connection_mode != ConnectionMode.EXTERNAL_IP:
            extra_args["pod_name"] = self.pod_name
            extra_args["port"] = f"{self.port}"

        return {
            **super().get_template_args(),
            "default_network": self.ts.conf_server.default_network,
            **extra_args,
        }

    def initialize(self) -> None:
        super().initialize()

        assert (self.in_file_template == "") == (self.out_file_yaml == "")

        if self.in_file_template != "":
            self.render_file("Server Pod Yaml")

            self.cluster_ip_addr = self.create_cluster_ip_service()
            self.nodeport_ip_addr = self.create_node_port_service(self.port + 25000)

    def confirm_server_alive(self) -> None:
        if self.connection_mode == ConnectionMode.EXTERNAL_IP:
            # Podman scenario
            end_time = time.monotonic() + 60
            while time.monotonic() < end_time:
                r = self.lh.run(
                    f"podman ps --filter status=running --filter name={self.pod_name} --format '{{{{.Names}}}}'"
                )
                if self.pod_name in r.out:
                    break
                time.sleep(5)
        else:
            # Kubernetes/OpenShift scenario
            r = self.run_oc(
                f"wait --for=condition=ready pod/{self.pod_name} --timeout=1m"
            )
        if not r or r.returncode != 0:
            logger.error(f"Failed to start server: {r.err}")
            sys.exit(-1)

        self.ts.event_server_alive.set()

    @abstractmethod
    def _create_setup_operation_get_thread_action_cmd(self) -> str:
        pass

    @abstractmethod
    def _create_setup_operation_get_cancel_action_cmd(self) -> str:
        pass

    def _create_setup_operation(self) -> Optional[TaskOperation]:
        # We don't chain up super()._create_setup_operation(). Depending on
        # the connection_mode we call setup_pod().

        th_cmd = self._create_setup_operation_get_thread_action_cmd()

        if self.connection_mode == ConnectionMode.EXTERNAL_IP:
            cmd = f"podman run -it --init --replace --rm -p {self.port} --name={self.pod_name} {tftbase.get_tft_test_image()} {th_cmd}"
            cancel_cmd = f"podman rm --force {self.pod_name}"
        else:
            self.setup_pod()
            ca_cmd = self._create_setup_operation_get_cancel_action_cmd()
            cmd = f"{th_cmd}"
            cancel_cmd = f"{ca_cmd}"

        logger.info(f"Running {cmd}")

        def _run_cmd(cmd: str, *, ignore_failure: bool) -> BaseOutput:
            force_success: Optional[bool] = None
            if ignore_failure:
                # We ignore the exit code of the command, that is because this is
                # commonly a long running process that needs to get killed with the
                # "cancel_action".  In that case, the exit code will be non-zero, but
                # it's the normal termination of the command. Suppress such "failures".
                force_success = True

            if self.connection_mode == ConnectionMode.EXTERNAL_IP:
                return BaseOutput.from_cmd(self.lh.run(cmd), success=force_success)
            if self.exec_persistent:
                return BaseOutput(msg="Server is persistent")
            return BaseOutput.from_cmd(
                self.run_oc_exec(cmd, may_fail=ignore_failure),
                success=force_success,
            )

        def _thread_action() -> BaseOutput:
            return _run_cmd(cmd, ignore_failure=True)

        def _cancel_action() -> None:
            _run_cmd(cancel_cmd, ignore_failure=False)

        return TaskOperation(
            log_name=self.log_name_setup,
            thread_action=_thread_action,
            wait_ready=lambda: self.confirm_server_alive(),
            cancel_action=_cancel_action,
        )


class ClientTask(Task, ABC):
    def __init__(self, ts: TestSettings, server: ServerTask):
        super().__init__(ts, ts.client_index, ts.conf_client.name, ts.client_is_tenant)

        pod_type = ts.client_pod_type
        node_name = self.node_name
        port = server.port

        if pod_type == PodType.SRIOV:
            in_file_template = "./manifests/sriov-pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/sriov-pod-{node_name}-client.yaml"
            pod_name = f"sriov-pod-{node_name}-client-{port}"
        elif pod_type == PodType.NORMAL:
            in_file_template = "./manifests/pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/pod-{node_name}-client.yaml"
            pod_name = f"normal-pod-{node_name}-client-{port}"
        elif pod_type == PodType.HOSTBACKED:
            in_file_template = "./manifests/host-pod.yaml.j2"
            out_file_yaml = f"./manifests/yamls/host-pod-{node_name}-client.yaml"
            pod_name = f"host-pod-{node_name}-client-{port}"
        else:
            raise ValueError("Invalid pod_type {pod_type}")

        self.server = server
        self.port = port
        self.pod_type = pod_type
        self.connection_mode = ts.connection_mode
        self.test_type = ts.connection.test_type
        self.test_case_id = ts.test_case_id
        self.reverse = ts.reverse
        self.in_file_template = in_file_template
        self.out_file_yaml = out_file_yaml
        self.pod_name = pod_name

    def get_template_args(self) -> dict[str, str | list[str]]:
        return {
            **super().get_template_args(),
            "default_network": self.ts.conf_client.default_network,
            "pod_name": self.pod_name,
        }

    def initialize(self) -> None:
        super().initialize()
        self.render_file("Client Pod Yaml")

    def get_target_ip(self) -> str:
        if self.connection_mode == ConnectionMode.CLUSTER_IP:
            logger.debug(
                f"get_target_ip() ClusterIP connection to {self.server.cluster_ip_addr}"
            )
            return self.server.cluster_ip_addr
        elif self.connection_mode == ConnectionMode.NODE_PORT_IP:
            logger.debug(
                f"get_target_ip() NodePortIP connection to {self.server.nodeport_ip_addr}"
            )
            return self.server.nodeport_ip_addr
        elif self.connection_mode == ConnectionMode.EXTERNAL_IP:
            external_pod_ip = self.get_podman_ip(self.server.pod_name)
            logger.debug(f"get_target_ip() External connection to {external_pod_ip}")
            return external_pod_ip
        server_ip = self.server.get_pod_ip()
        logger.debug(f"get_target_ip() Connection to server at {server_ip}")
        return server_ip

    def get_podman_ip(self, pod_name: str) -> str:
        cmd = "podman inspect --format '{{.NetworkSettings.IPAddress}}' " + pod_name

        for _ in range(5):
            ret = self.lh.run(cmd)
            if ret.returncode == 0:
                ip_address = ret.out.strip()
                if ip_address:
                    logger.debug(f"get_podman_ip({pod_name}) found: {ip_address}")
                    return ip_address

            time.sleep(2)

        raise Exception(
            f"get_podman_ip(): failed to get {pod_name} ip after 5 attempts"
        )


class PluginTask(Task, ABC):
    @property
    @abstractmethod
    def plugin(self) -> Plugin:
        pass

    def get_plugin_metadata(self) -> tftbase.PluginMetadata:
        return tftbase.PluginMetadata(
            plugin_name=self.plugin.PLUGIN_NAME,
            node_name=self.node_name,
            pod_name=self.pod_name,
        )
