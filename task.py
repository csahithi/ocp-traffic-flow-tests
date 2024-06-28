import sys
import typing
import yaml

from abc import ABC
from abc import abstractmethod

import common
import host
import tftbase

from logger import logger
from testConfig import ClusterMode
from testConfig import TestConfig
from thread import ReturnValueThread


class Task(ABC):
    def __init__(
        self, tc: TestConfig, index: int, node_name: str, tenant: bool
    ) -> None:
        self.template_args: dict[str, str] = {}
        self.in_file_template = ""
        self.out_file_yaml = ""
        self.pod_name = ""
        self.exec_thread: ReturnValueThread
        self.lh = host.LocalHost()

        self.template_args["name_space"] = "default"
        self.template_args["test_image"] = tftbase.TFT_TOOLS_IMG
        self.template_args["command"] = ["/bin/sh", "-c"]
        self.template_args["args"] = "/sbin/init; sleep infinity"
        self.template_args["index"] = f"{index}"

        self.index = index
        self.node_name = node_name
        self.tenant = tenant
        if not self.tenant and tc.mode == ClusterMode.SINGLE:
            logger.error("Cannot have non-tenant Task when cluster mode is single.")
            sys.exit(-1)

        self.template_args["node_name"] = self.node_name
        self.tc = tc

    def run_oc(self, cmd: str) -> host.Result:
        if self.tenant:
            r = self.tc.client_tenant.oc(cmd)
        else:
            r = self.tc.client_infra.oc(cmd)
        return r

    def get_pod_ip(self) -> str:
        r = self.run_oc(f"get pod {self.pod_name} -o yaml")
        if r.returncode != 0:
            logger.info(r)
            sys.exit(-1)

        y = yaml.safe_load(r.out)
        return typing.cast(str, y["status"]["podIP"])

    def get_secondary_ip(self) -> str:
        jsonpath = '{.metadata.annotations.k8s\.ovn\.org\/pod-networks}'
        logger.info(f"get pod {self.pod_name} -o jsonpath='{jsonpath}'")        
        r = self.run_oc(f"get pod {self.pod_name} -o jsonpath='{jsonpath}'")
        if r.returncode != 0:
            logger.info(r)
            sys.exit(-1)

        y = yaml.safe_load(r.out)
        logger.info(f"Output y: {y}")
        ip_address_with_cidr = typing.cast(str, y["default/ovn-stream-veth"]["ip_address"])
        logger.info(f"IP CIDR: {ip_address_with_cidr}")
        ip_address = ip_address_with_cidr.split("/")[0] if ip_address_with_cidr else ""
        return typing.cast(str, ip_address)

    def create_network_attachment_definition(self):
        in_file_template = "./manifests/netAttachdef-ovn-stream-veth.yaml.j2"
        out_file_yaml = "./manifests/yamls/netAttachdef-ovn-stream-veth.yaml"

        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating NAD {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created NAD")
    
    def create_cluster_ip_service(self) -> str:
        in_file_template = "./manifests/svc-cluster-ip.yaml.j2"
        out_file_yaml = "./manifests/yamls/svc-cluster-ip.yaml"

        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Cluster IP Service {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)

        return self.run_oc(
            "get service tft-clusterip-service -o=jsonpath='{.spec.clusterIP}'"
        ).out

    def create_node_port_service(self, nodeport: int) -> str:
        in_file_template = "./manifests/svc-node-port.yaml.j2"
        out_file_yaml = "./manifests/yamls/svc-node-port.yaml"
        self.template_args["nodeport_svc_port"] = f"{nodeport}"

        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Node Port Service {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)

        return self.run_oc(
            "get service tft-nodeport-service -o=jsonpath='{.spec.clusterIP}'"
        ).out

    def create_allow_dns_network_policy(self):
        in_file_template = "./manifests/allow-dns-access.yaml.j2"
        out_file_yaml = "./manifests/yamls/allow-dns-access.yaml"
        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Allow DNS Network Policy {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created Allow DNS Network Policy")

    def create_ingress_network_policy(self):
        in_file_template = "./manifests/allow-iperf-5201-ingress.yaml.j2"
        out_file_yaml = "./manifests/yamls/allow-iperf-5201-ingress.yaml"
        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Ingress Network Policy {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created Ingress Network Policy")

    def create_egress_network_policy(self):
        in_file_template = "./manifests/allow-iperf-5201-egress.yaml.j2"
        out_file_yaml = "./manifests/yamls/allow-iperf-5201-egress.yaml"
        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Egress Network Policy {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created Egress Network Policy")

    def create_ingress_multi_network_policy(self):
        in_file_template = "./manifests/allow-iperf-5201-ingress-veth-mnp.yaml.j2"
        out_file_yaml = "./manifests/yamls/allow-iperf-5201-ingress-veth-mnp.yaml"
        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Ingress MNP {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created Ingress MNP")

    def create_egress_multi_network_policy(self):
        in_file_template = "./manifests/allow-iperf-5201-egress-veth-mnp.yaml.j2"
        out_file_yaml = "./manifests/yamls/allow-iperf-5201-egress-veth-mnp.yaml"
        common.j2_render(in_file_template, out_file_yaml, self.template_args)
        logger.info(f"Creating Egress MNP {out_file_yaml}")
        r = self.run_oc(f"apply -f {out_file_yaml}")
        if r.returncode != 0:
            if "already exists" not in r.err:
                logger.info(r)
                sys.exit(-1)
        logger.info("Created Egress MNP")

    def setup(self) -> None:
        # Check if pod already exists
        r = self.run_oc(f"get pod {self.pod_name} --output=json")
        if r.returncode != 0:
            # otherwise create the pod
            logger.info(f"Creating Pod {self.pod_name}.")
            r = self.run_oc(f"apply -f {self.out_file_yaml}")
            if r.returncode != 0:
                logger.info(r)
                sys.exit(-1)
        else:
            logger.info(f"Pod {self.pod_name} already exists.")

        logger.info(f"Waiting for Pod {self.pod_name} to become ready.")
        r = self.run_oc(f"wait --for=condition=ready pod/{self.pod_name} --timeout=1m")
        if r.returncode != 0:
            logger.info(r)
            sys.exit(-1)
        r = self.run_oc(f"get po {self.pod_name} --output=json")
        logger.info(r)

    @abstractmethod
    def run(self, duration: int) -> None:
        raise NotImplementedError(
            "Must implement run(). Use SyncManager.wait_barrier()"
        )

    def stop(self, timeout: float) -> None:
        class_name = self.__class__.__name__
        logger.info(f"Stopping execution on {class_name}")
        self.exec_thread.join_with_result(timeout=timeout * 1.5)
        if self.exec_thread.result is not None:
            r = self.exec_thread.result
            if r.returncode != 0:
                logger.error(
                    f"Error occurred while stopping {class_name}: errcode: {r.returncode} err {r.err}"
                )
            logger.debug(f"{class_name}.stop(): {r.out}")
            self._output = self.generate_output(data=r.out)
        else:
            logger.error(f"Thread {class_name} did not return a result")
            self._output = tftbase.BaseOutput("", {})

    """
    output() should be called to store the results of this task in a PluginOutput class object, and return this by appending the instance to the
    TftAggregateOutput Plugin fields. Additionally, this function should handle printing any required info/debug to the console. The results must
    be formated such that other modules can easily consume the output, such as a module to determine the success/failure/performance of a given run.
    """

    @abstractmethod
    def output(self, out: tftbase.TftAggregateOutput) -> None:
        raise NotImplementedError("Must implement output()")

    @abstractmethod
    def generate_output(self, data: str) -> tftbase.BaseOutput:
        raise NotImplementedError("Must implement generate_output()")
