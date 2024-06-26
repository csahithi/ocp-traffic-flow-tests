import json
import typing
from typing import Optional

import perf
import pluginbase

from host import Result
from logger import logger
from syncManager import SyncManager
from task import PluginTask
from testSettings import TestSettings
from tftbase import PluginOutput
from tftbase import PluginResult
from tftbase import PodType
from tftbase import TFT_TOOLS_IMG
from tftbase import TestMetadata
from tftbase import TftAggregateOutput
from thread import ReturnValueThread

VF_REP_TRAFFIC_THRESHOLD = 1000


def no_traffic_on_vf_rep(
    rx_start: int, tx_start: int, rx_end: int, tx_end: int
) -> bool:
    return (
        rx_end - rx_start < VF_REP_TRAFFIC_THRESHOLD
        and tx_end - tx_start < VF_REP_TRAFFIC_THRESHOLD
    )


class PluginValidateOffload(pluginbase.Plugin):
    PLUGIN_NAME = "validate_offload"

    def _enable(
        self,
        *,
        ts: TestSettings,
        node_server_name: str,
        node_client_name: str,
        perf_server: perf.PerfServer,
        perf_client: perf.PerfClient,
        tenant: bool,
    ) -> list[PluginTask]:
        # TODO allow this to run on each individual server + client pairs.
        return [
            TaskValidateOffload(ts, perf_server, tenant),
            TaskValidateOffload(ts, perf_client, tenant),
        ]

    def eval_log(
        self, plugin_output: PluginOutput, md: TestMetadata
    ) -> Optional[PluginResult]:
        rx_start = plugin_output.result.get("rx_start")
        tx_start = plugin_output.result.get("tx_start")
        rx_end = plugin_output.result.get("rx_end")
        tx_end = plugin_output.result.get("tx_end")

        if any(x is None for x in [rx_start, tx_start, rx_end, tx_end]):
            logger.error(
                f"Validate offload plugin is missing expected ethtool data in {md.test_case_id}"
            )
            success = False
        else:
            assert isinstance(rx_start, int)
            assert isinstance(tx_start, int)
            assert isinstance(rx_end, int)
            assert isinstance(tx_end, int)
            success = no_traffic_on_vf_rep(
                rx_start=rx_start,
                tx_start=tx_start,
                rx_end=rx_end,
                tx_end=tx_end,
            )

        return PluginResult(
            test_id=md.test_case_id,
            test_type=md.test_type,
            reverse=md.reverse,
            success=success,
        )


plugin = PluginValidateOffload()


class TaskValidateOffload(PluginTask):
    @property
    def plugin(self) -> pluginbase.Plugin:
        return plugin

    def __init__(
        self,
        ts: TestSettings,
        perf_instance: perf.PerfServer | perf.PerfClient,
        tenant: bool,
    ):
        super().__init__(ts, 0, perf_instance.node_name, tenant)

        self.in_file_template = "./manifests/tools-pod.yaml.j2"
        self.out_file_yaml = (
            f"./manifests/yamls/tools-pod-{self.node_name}-validate-offload.yaml"
        )
        self.pod_name = f"tools-pod-{self.node_name}-validate-offload"
        self._perf_instance = perf_instance
        self.perf_pod_name = perf_instance.pod_name
        self.perf_pod_type = perf_instance.pod_type
        self.ethtool_cmd = ""

    def get_template_args(self) -> dict[str, str]:
        return {
            **super().get_template_args(),
            "pod_name": self.pod_name,
            "test_image": TFT_TOOLS_IMG,
        }

    def initialize(self) -> None:
        super().initialize()
        self.render_file("Server Pod Yaml")

    def extract_vf_rep(self) -> str:
        if self.perf_pod_type == PodType.HOSTBACKED:
            logger.info("The VF representor is: ovn-k8s-mp0")
            return "ovn-k8s-mp0"

        if self.perf_pod_name == perf.EXTERNAL_PERF_SERVER:
            logger.info("There is no VF on an external server")
            return "external"

        self.get_vf_rep_cmd = f'exec -n default {self.pod_name} -- /bin/sh -c "crictl --runtime-endpoint=unix:///host/run/crio/crio.sock ps -a --name={self.perf_pod_name} -o json "'
        r = self.run_oc(self.get_vf_rep_cmd)

        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.error(f"Extract_vf_rep: {r.err}, {r.returncode}")

        vf_rep_json = r.out
        data = json.loads(vf_rep_json)
        logger.info(
            f"The VF representor is: {data['containers'][0]['podSandboxId'][:15]}"
        )
        return typing.cast(str, data["containers"][0]["podSandboxId"][:15])

    def run_ethtool_cmd(self, ethtool_cmd: str) -> tuple[bool, Result]:
        logger.info(f"Running {ethtool_cmd}")
        success = True
        r = self.run_oc(ethtool_cmd)
        if self.perf_pod_type != PodType.HOSTBACKED:
            if r.returncode != 0:
                if "already exists" not in r.err:
                    logger.error(f"Run_ethtool_cmd: {r.err}, {r.returncode}")
                    success = False
        return success, r

    def parse_packets(self, output: str, packet_type: str) -> int:
        # Case1: Try to parse rx_packets and tx_packets from ethtool output
        prefix = f"{packet_type}_packets"
        if prefix in output:
            for line in output.splitlines():
                stripped_line = line.strip()
                if stripped_line.startswith(prefix):
                    return int(stripped_line.split(":")[1])
        # Case2: Ethtool output does not provide these fields, so we need to sum the queues manually
        total_packets = 0
        prefix = f"{packet_type}_queue_"
        packet_suffix = "_xdp_packets:"

        for line in output.splitlines():
            stripped_line = line.strip()
            if prefix in stripped_line and packet_suffix in stripped_line:
                packet_count = int(stripped_line.split(":")[1].strip())
                total_packets += packet_count

        return total_packets

    def run(self, duration: int) -> None:
        def stat(self: TaskValidateOffload, duration: int) -> Result:
            SyncManager.wait_on_barrier()
            vf_rep = self.extract_vf_rep()
            self.ethtool_cmd = (
                f'exec -n default {self.pod_name} -- /bin/sh -c "ethtool -S {vf_rep}"'
            )
            if vf_rep == "ovn-k8s-mp0":
                return Result(out="Hostbacked pod", err="", returncode=0)
            if vf_rep == "external":
                return Result(out="External Iperf Server", err="", returncode=0)

            success1, r1 = self.run_ethtool_cmd(self.ethtool_cmd)
            if not success1 or not r1.returncode != 0:
                logger.error("Ethtool command failed")
                return r1

            SyncManager.wait_on_client_finish()

            success2, r2 = self.run_ethtool_cmd(self.ethtool_cmd)

            combined_out = f"{r1.out}--DELIMIT--{r2.out}"
            return Result(out=combined_out, err=r2.err, returncode=r2.returncode)

        self.exec_thread = ReturnValueThread(target=stat, args=(self, duration))
        self.exec_thread.start()

    def output(self, out: TftAggregateOutput) -> None:
        if not isinstance(self._output, PluginOutput):
            return

        out.plugins.append(self._output)

        if self.perf_pod_type == PodType.HOSTBACKED:
            if isinstance(self._perf_instance, perf.PerfClient):
                logger.info("The client VF representor ovn-k8s-mp0_0 does not exist")
            else:
                logger.info("The server VF representor ovn-k8s-mp0_0 does not exist")

        logger.info(
            f"validateOffload results on {self.perf_pod_name}: {self._output.result}"
        )

    def generate_output(self, data: str) -> PluginOutput:
        # Different behavior has been seen from the ethtool output depending on the driver in question
        # Log the output of ethtool temporarily until this is more stable.
        # TODO: switch to debug
        logger.info(f"generate hwol output from data: {data}")
        split_data = data.split("--DELIMIT--")
        parsed_data: dict[str, str | int] = {}

        if len(split_data) >= 1:
            parsed_data["rx_start"] = self.parse_packets(split_data[0], "rx")
            parsed_data["tx_start"] = self.parse_packets(split_data[0], "tx")

        if len(split_data) >= 2:
            parsed_data["rx_end"] = self.parse_packets(split_data[1], "rx")
            parsed_data["tx_end"] = self.parse_packets(split_data[1], "tx")

        if len(split_data) >= 3:
            parsed_data["additional_info"] = "--DELIMIT--".join(split_data[2:])

        logger.info(
            f"rx_packet_start: {parsed_data.get('rx_start', 'N/A')}\n"
            f"tx_packet_start: {parsed_data.get('tx_start', 'N/A')}\n"
            f"rx_packet_end: {parsed_data.get('rx_end', 'N/A')}\n"
            f"tx_packet_end: {parsed_data.get('tx_end', 'N/A')}\n"
        )
        return PluginOutput(
            command=self.ethtool_cmd,
            plugin_metadata={
                "name": "GetEthtoolStats",
                "node_name": self.node_name,
                "pod_name": self.pod_name,
            },
            result=parsed_data,
            name=plugin.PLUGIN_NAME,
        )
