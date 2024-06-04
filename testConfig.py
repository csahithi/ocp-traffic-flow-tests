import io
import sys
import typing

from typing import Any
from typing import Mapping
from typing import Optional
from yaml import safe_load

import common
import host

from k8sClient import K8sClient
from logger import logger
from tftbase import ClusterMode
from tftbase import PodType
from tftbase import TestCaseType
from tftbase import TestType


class TestConfig:
    kubeconfig_tenant: str = "/root/kubeconfig.tenantcluster"
    kubeconfig_infra: str = "/root/kubeconfig.infracluster"
    kubeconfig_single: str = "/root/kubeconfig.nicmodecluster"
    kubeconfig_cx: str = "/root/kubeconfig.smartniccluster"

    mode: ClusterMode
    client_tenant: K8sClient
    client_infra: Optional[K8sClient]
    full_config: dict[str, Any]

    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            contents = f.read()
            self.full_config = safe_load(io.StringIO(contents))

        lh = host.LocalHost()

        # Find out what type of cluster are we in.
        self.client_infra = None
        if lh.file_exists(self.kubeconfig_single):
            self.mode = ClusterMode.SINGLE
            self.client_tenant = K8sClient(self.kubeconfig_single)
        elif lh.file_exists(self.kubeconfig_cx):
            self.mode = ClusterMode.SINGLE
            self.client_tenant = K8sClient(self.kubeconfig_cx)
        elif lh.file_exists(self.kubeconfig_tenant):
            if lh.file_exists(self.kubeconfig_infra):
                self.mode = ClusterMode.DPU
                self.client_tenant = K8sClient(self.kubeconfig_tenant)
                self.client_infra = K8sClient(self.kubeconfig_infra)
            else:
                logger.error(
                    "Assuming DPU...Cannot Find Infrastructure Cluster Config."
                )
                sys.exit(-1)
        else:
            logger.error("Cannot Find Kubeconfig.")
            sys.exit(-1)

        logger.info(self.GetConfig())

    def client(self, *, tenant: bool) -> K8sClient:
        if tenant:
            return self.client_tenant
        client = self.client_infra
        if client is None:
            raise RuntimeError("TestConfig has no infra client")
        return client

    def GetConfig(self) -> list[dict[str, Any]]:
        return typing.cast(list[dict[str, Any]], self.full_config["tft"])

    @staticmethod
    def parse_test_cases(input_str: str) -> list[TestCaseType]:
        return common.enum_convert_list(TestCaseType, input_str)

    @staticmethod
    def pod_type_from_config(connection_server: dict[str, str]) -> PodType:
        if "sriov" in connection_server:
            if "true" in connection_server["sriov"].lower():
                return PodType.SRIOV
        return PodType.NORMAL

    @staticmethod
    def default_network_from_config(connection: dict[str, str]) -> str:
        if "default-network" in connection:
            return connection["default-network"]
        return "default/default"

    @staticmethod
    def validate_test_type(connection: Mapping[str, Any]) -> TestType:
        input_ct = connection.get("type")
        try:
            return common.enum_convert(TestType, input_ct, default=TestType.IPERF_TCP)
        except Exception:
            raise ValueError(
                f"Invalid connection type {input_ct} provided. Supported connection types: iperf-tcp (default), iperf-udp, http"
            )
