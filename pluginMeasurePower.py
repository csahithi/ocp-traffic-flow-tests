import re
import time

import host
import perf
import pluginbase
import tftbase

from logger import logger
from task import PluginTask
from task import TaskOperation
from testSettings import TestSettings
from tftbase import BaseOutput
from tftbase import PluginOutput


class PluginMeasurePower(pluginbase.Plugin):
    PLUGIN_NAME = "measure_power"

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
        return [
            TaskMeasurePower(ts, node_server_name, tenant),
            TaskMeasurePower(ts, node_client_name, tenant),
        ]


plugin = PluginMeasurePower()


def _extract(r: host.Result) -> int:
    for e in r.out.split("\n"):
        if "Instantaneous power reading" in e:
            match = re.search(r"\d+", e)
            if match:
                return int(match.group())
    logger.error(f"Could not find Instantaneous power reading: {e}.")
    return 0


class TaskMeasurePower(PluginTask):
    @property
    def plugin(self) -> pluginbase.Plugin:
        return plugin

    def __init__(self, ts: TestSettings, node_name: str, tenant: bool):
        super().__init__(ts, 0, node_name, tenant)

        self.in_file_template = "./manifests/tools-pod.yaml.j2"
        self.out_file_yaml = (
            f"./manifests/yamls/tools-pod-{self.node_name}-measure-cpu.yaml"
        )
        self.pod_name = f"tools-pod-{self.node_name}-measure-cpu"
        self.node_name = node_name

    def get_template_args(self) -> dict[str, str]:
        return {
            **super().get_template_args(),
            "pod_name": self.pod_name,
            "test_image": tftbase.get_tft_test_image(),
        }

    def initialize(self) -> None:
        super().initialize()
        self.render_file("Server Pod Yaml")

    def _create_task_operation(self) -> TaskOperation:
        def _thread_action() -> BaseOutput:
            cmd = "ipmitool dcmi power reading"
            self.ts.clmo_barrier.wait()
            total_pwr = 0
            iteration = 0
            while not self.ts.event_client_finished.is_set():
                r = self.run_oc_exec(cmd)
                if r.returncode != 0:
                    logger.error(f"Failed to get power {cmd}: {r}")
                pwr = _extract(r)
                total_pwr += pwr
                iteration += 1
                time.sleep(0.2)

            return PluginOutput(
                plugin_metadata={
                    "name": "MeasurePower",
                    "node_name": self.node_name,
                    "pod_name": self.pod_name,
                },
                command=cmd,
                result={
                    "measure_power": f"{total_pwr/iteration}",
                },
                name=plugin.PLUGIN_NAME,
            )

        return TaskOperation(
            log_name=self.log_name,
            thread_action=_thread_action,
        )

    def _aggregate_output(
        self,
        result: tftbase.AggregatableOutput,
        out: tftbase.TftAggregateOutput,
    ) -> None:
        assert isinstance(result, PluginOutput)
        out.plugins.append(result)
        logger.info(f"measurePower results: {result.result['measure_power']}")
