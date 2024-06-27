import dataclasses

from dataclasses import dataclass
from enum import Enum
from typing import Any
from typing import Mapping
from typing import Optional

from common import dataclass_from_dict
from common import enum_convert


TFT_TOOLS_IMG = "quay.io/wizhao/tft-tools:latest"
TFT_TESTS = "tft-tests"


class TestType(Enum):
    IPERF_TCP = 1
    IPERF_UDP = 2
    HTTP = 3
    NETPERF_TCP_STREAM = 4
    NETPERF_TCP_RR = 5


class PodType(Enum):
    NORMAL = 1
    SRIOV = 2
    HOSTBACKED = 3


class TestCaseType(Enum):
    POD_TO_POD_SAME_NODE = 1
    POD_TO_POD_DIFF_NODE = 2
    POD_TO_HOST_SAME_NODE = 3
    POD_TO_HOST_DIFF_NODE = 4
    POD_TO_CLUSTER_IP_TO_POD_SAME_NODE = 5
    POD_TO_CLUSTER_IP_TO_POD_DIFF_NODE = 6
    POD_TO_CLUSTER_IP_TO_HOST_SAME_NODE = 7
    POD_TO_CLUSTER_IP_TO_HOST_DIFF_NODE = 8
    POD_TO_NODE_PORT_TO_POD_SAME_NODE = 9
    POD_TO_NODE_PORT_TO_POD_DIFF_NODE = 10
    POD_TO_NODE_PORT_TO_HOST_SAME_NODE = 11
    POD_TO_NODE_PORT_TO_HOST_DIFF_NODE = 12
    HOST_TO_HOST_SAME_NODE = 13
    HOST_TO_HOST_DIFF_NODE = 14
    HOST_TO_POD_SAME_NODE = 15
    HOST_TO_POD_DIFF_NODE = 16
    HOST_TO_CLUSTER_IP_TO_POD_SAME_NODE = 17
    HOST_TO_CLUSTER_IP_TO_POD_DIFF_NODE = 18
    HOST_TO_CLUSTER_IP_TO_HOST_SAME_NODE = 19
    HOST_TO_CLUSTER_IP_TO_HOST_DIFF_NODE = 20
    HOST_TO_NODE_PORT_TO_POD_SAME_NODE = 21
    HOST_TO_NODE_PORT_TO_POD_DIFF_NODE = 22
    HOST_TO_NODE_PORT_TO_HOST_SAME_NODE = 23
    HOST_TO_NODE_PORT_TO_HOST_DIFF_NODE = 24
    POD_TO_EXTERNAL = 25
    HOST_TO_EXTERNAL = 26
    POD_TO_POD_2ND_INTERFACE_SAME_NODE = 27
    POD_TO_POD_2ND_INTERFACE_DIFF_NODE = 28
    NETWORK_POLICY_TEST = 29
    MULTI_NETWORK_POLICY_TEST = 30


class ConnectionMode(Enum):
    POD_IP = 1
    CLUSTER_IP = 2
    NODE_PORT_IP = 3
    EXTERNAL_IP = 4
    MULTI_NETWORK = 5
    MULTI_HOME = 6
    NETWORK_POLICY = 7


class NodeLocation(Enum):
    SAME_NODE = 1
    DIFF_NODE = 2


@dataclass
class PodInfo:
    name: str
    pod_type: PodType
    is_tenant: bool
    index: int


@dataclass
class PluginResult:
    """Result of a single plugin from a given run

    Attributes:
        test_id: TestCaseType enum representing the type of traffic test (i.e. POD_TO_POD_SAME_NODE <1> )
        test_type: TestType enum representing the traffic protocol (i.e. iperf_tcp)
        reverse: Specify whether test is client->server or reversed server->client
        success: boolean representing whether the test passed or failed
    """

    test_id: TestCaseType
    test_type: TestType
    reverse: bool
    success: bool


@dataclass
class TestMetadata:
    reverse: bool
    test_case_id: TestCaseType
    test_type: TestType
    server: PodInfo
    client: PodInfo

    def __init__(
        self,
        reverse: bool,
        test_case_id: TestCaseType | str | int,
        test_type: TestType | str | int,
        server: PodInfo | dict[str, Any],
        client: PodInfo | dict[str, Any],
    ):
        if isinstance(server, dict):
            server = dataclass_from_dict(PodInfo, server)
        if isinstance(client, dict):
            client = dataclass_from_dict(PodInfo, client)
        self.reverse = reverse
        self.test_case_id = enum_convert(TestCaseType, test_case_id)
        self.test_type = enum_convert(TestType, test_type)
        self.server = server
        self.client = client


@dataclass
class BaseOutput:
    command: str
    result: dict[str, str | int]

    def __init__(self, command: str, result: Mapping[str, str | int]):
        if not isinstance(result, dict):
            result = dict(result)
        self.command = command
        self.result = result


@dataclass
class IperfOutput(BaseOutput):
    tft_metadata: TestMetadata

    def __init__(
        self,
        command: str,
        result: Mapping[str, str | int],
        tft_metadata: TestMetadata | dict[str, Any],
    ):
        if isinstance(tft_metadata, dict):
            tft_metadata = dataclass_from_dict(TestMetadata, tft_metadata)
        elif not isinstance(tft_metadata, TestMetadata):
            raise ValueError("tft_metadata must be a TestMetadata instance or a dict")
        super().__init__(command, result)
        self.tft_metadata = tft_metadata


@dataclass
class PluginOutput(BaseOutput):
    plugin_metadata: dict[str, str]
    name: str


@dataclass
class TftAggregateOutput:
    """Aggregated output of a single tft run. A single run of a trafficFlowTests._run_tests() will
    pass a reference to an instance of TftAggregateOutput to each task to which the task will append
    it's respective output. A list of this class will be the expected format of input provided to
    evaluator.py.

    Attributes:
        flow_test: an object of type IperfOutput containing the results of a flow test run
        plugins: a list of objects derivated from type PluginOutput for each optional plugin to append
        resulting output to."""

    flow_test: Optional[IperfOutput] = None
    plugins: list[PluginOutput] = dataclasses.field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.flow_test, dict):
            self.flow_test = dataclass_from_dict(IperfOutput, self.flow_test)
        elif self.flow_test is not None and not isinstance(self.flow_test, IperfOutput):
            raise ValueError("flow_test must be an IperfOutput instance or a dict")

        self.plugins = [
            (
                dataclass_from_dict(PluginOutput, plugin)
                if isinstance(plugin, dict)
                else plugin
            )
            for plugin in self.plugins
        ]
