import perf
import tftbase

from host import Result
from logger import logger
from syncManager import SyncManager
from testConfig import TestConfig
from testSettings import TestSettings
from tftbase import ConnectionMode
from tftbase import IperfOutput
from tftbase import TestType
from thread import ReturnValueThread


NETPERF_SERVER_EXE = "netserver"
NETPERF_CLIENT_EXE = "netperf"


class NetPerfServer(perf.PerfServer):
    def __init__(self, tc: TestConfig, ts: TestSettings):

        self.exec_persistent = ts.server_is_persistent
        if self.exec_persistent:
            self.template_args["command"] = NETPERF_SERVER_EXE
            self.template_args["args"] = f'["-p", "{self.port}", "-N"]'

        perf.PerfServer.__init__(self, tc, ts)

    def setup(self) -> None:
        if self.connection_mode == ConnectionMode.EXTERNAL_IP:
            cmd = f"docker run -it --rm -p {self.port} --entrypoint {NETPERF_SERVER_EXE} --name={self.pod_name} {tftbase.TFT_TOOLS_IMG} -p {self.port} -N"
            cleanup_cmd = f"docker rm --force {self.pod_name}"
        else:
            # Create the server pods
            super().setup()
            cmd = f"exec {self.pod_name} -- {NETPERF_SERVER_EXE} -p {self.port} -N"
            cleanup_cmd = f"exec -t {self.pod_name} -- killall {NETPERF_SERVER_EXE}"

        logger.info(f"Running {cmd}")

        def server(self: NetPerfServer, cmd: str) -> Result:
            if self.connection_mode == ConnectionMode.EXTERNAL_IP:
                return self.lh.run(cmd)
            elif self.exec_persistent:
                return Result("Server is persistent.", "", 0)
            return self.run_oc(cmd)

        self.exec_thread = ReturnValueThread(
            target=server,
            args=(self, cmd),
            cleanup_action=server,
            cleanup_args=(self, cleanup_cmd),
        )
        self.exec_thread.start()
        self.confirm_server_alive()


class NetPerfClient(perf.PerfClient):
    def __init__(self, tc: TestConfig, ts: TestSettings, server: NetPerfServer):
        perf.PerfClient.__init__(self, tc, ts, server)

    def run(self, duration: int) -> None:
        def client(self: NetPerfClient, cmd: str) -> Result:
            SyncManager.wait_on_barrier()
            r = self.run_oc(cmd)
            SyncManager.set_client_finished()
            return r

        server_ip = self.get_target_ip()
        if self.test_type == TestType.NETPERF_TCP_STREAM:
            self.cmd = f"exec {self.pod_name} -- {NETPERF_CLIENT_EXE} -H {server_ip} -p {self.port} -t TCP_STREAM -l {duration}"
        else:
            self.cmd = f"exec {self.pod_name} -- {NETPERF_CLIENT_EXE} -H {server_ip} -p {self.port} -t TCP_RR -l {duration}"

        if self.reverse:
            logger.info("Reverse is not supported by Netperf")

        logger.info(f"Running {self.cmd}")

        self.exec_thread = ReturnValueThread(target=client, args=(self, self.cmd))
        self.exec_thread.start()

    # FIXME: Refactor IperfOutput
    def generate_output(self, data: str) -> IperfOutput:
        lines = data.strip().split("\n")

        if self.test_type == TestType.NETPERF_TCP_STREAM:
            headers = [
                "Receive Socket Size Bytes",
                "Send Socket Size Bytes",
                "Send Message Size Bytes",
                "Elapsed Time Seconds",
                "Throughput 10^6bits/sec",
            ]
            values = lines[6].split()
        else:
            headers = [
                "Socket Send Bytes",
                "Size Receive Bytes",
                "Request Size Bytes",
                "Response Size Bytes",
                "Elapsed Time Seconds",
                "Transaction Rate Per Second",
            ]
            values = lines[6].split()

        parsed_data = dict(zip(headers, values))

        json_dump = IperfOutput(
            tft_metadata=self.ts.get_test_metadata(),
            command=self.cmd,
            result=parsed_data,
        )
        return json_dump

    def output(self, out: tftbase.TftAggregateOutput) -> None:
        # Return machine-readable output to top level
        assert isinstance(
            self._output, IperfOutput
        ), f"Expected variable to be of type IperfOutput, got {type(self._output)} instead."
        out.flow_test = self._output

        # Print summary to console logs
        logger.info(f"Results of {self.ts.get_test_str()}:")
        logger.info(f"{self._output.result}:")
