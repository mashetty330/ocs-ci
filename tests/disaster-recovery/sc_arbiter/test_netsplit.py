import pytest
import logging
import time
import ocpnetsplit

from ocs_ci.utility.retry import retry
from ocs_ci.ocs.exceptions import CommandFailed, CephHealthException
from ocs_ci.ocs import constants
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.node import get_all_nodes, wait_for_nodes_status
from ocs_ci.helpers.sanity_helpers import Sanity
from datetime import datetime, timedelta, timezone
from ocs_ci.ocs.resources.pvc import get_pvc_objs
from ocs_ci.ocs.resources.pod import (
    Pod,
    wait_for_pods_to_be_in_statuses,
    get_pods_having_label,
    get_pod_logs,
    get_pod_node,
    get_ceph_tools_pod,
)
from ocs_ci.helpers.stretchcluster_helpers import (
    check_for_write_pause,
    check_for_read_pause,
)

logger = logging.getLogger(__name__)


@retry(CommandFailed, tries=4, delay=5)
def get_logfile_map_from_logwriter_pods(logwriter_pods, is_rbd=False):
    """
    This function fetches all the logfiles generated by logwriter instances
    and maps it with a string representing the start time of the logging

    Args:
        logwriter_pods (List): List of logwriter pod objects
        is_rbd (bool): True if it's an RBD RWO workload else False
    Returns:
        Dict: Representing map containing file name key and start time value

    """
    log_file_map = {}

    if not is_rbd:
        for file_name in list(
            filter(
                None,
                (
                    logwriter_pods[0]
                    .exec_sh_cmd_on_pod(command="ls -l | awk 'NR>1' | awk '{print $9}'")
                    .split("\n")
                ),
            )
        ):
            start_time = (
                logwriter_pods[0]
                .exec_sh_cmd_on_pod(command=f"cat {file_name} | grep -i started")
                .split(" ")[0]
            )
            log_file_map[file_name] = start_time.split("T")[1]
    else:
        for logwriter_pod in logwriter_pods:
            log_file_map[logwriter_pod.name] = {}
            for file_name in logwriter_pod.exec_sh_cmd_on_pod(
                command="ls -l | awk 'NR>1' | awk '{print $9}'"
            ).split("\n"):
                if file_name not in ("", "lost+found"):
                    start_time = logwriter_pod.exec_sh_cmd_on_pod(
                        command=f"cat {file_name} | grep -i started"
                    ).split(" ")[0]
                    log_file_map[logwriter_pod.name][file_name] = start_time.split("T")[
                        1
                    ]

    return log_file_map


class TestNetSplit:
    @pytest.fixture()
    def init_sanity(self, request):
        """
        Initial Cluster sanity
        """
        self.sanity_helpers = Sanity()

        def finalizer():
            """
            Make sure the ceph health is OK at the end of the test
            """
            try:
                logger.info("Making sure ceph health is OK")
                self.sanity_helpers.health_check(tries=50)
            except CephHealthException as e:
                assert all(
                    err in e.args[0]
                    for err in ["HEALTH_WARN", "daemons have recently crashed"]
                ), f"[CephHealthException]: {e.args[0]}"
                get_ceph_tools_pod().exec_ceph_cmd(ceph_cmd="ceph crash archive-all")
                logger.info("Archived ceph crash!")

        request.addfinalizer(finalizer)

    @pytest.mark.parametrize(
        argnames="zones, duration",
        argvalues=[
            pytest.param(constants.NETSPLIT_DATA_1_DATA_2, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1_AND_ARBITER_DATA_2, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1_AND_DATA_1_DATA_2, 15),
        ],
        ids=[
            "Data-1-Data-2",
            "Arbiter-Data-1",
            "Arbiter-Data-1-and-Arbiter-Data-2",
            "Arbiter-Data-1-and-Data-1-Data-2",
        ],
    )
    def test_netsplit_cephfs(
        self,
        setup_logwriter_cephfs_workload_factory,
        logreader_workload_factory,
        nodes,
        zones,
        duration,
        init_sanity,
    ):
        """
        This test will test the netsplit scenarios when active-active CephFS workload
        is running.
        Steps:
            1) Run both the logwriter and logreader CephFS workload using single RWX volume
            2) Induce the network split
            3) Make sure logreader job pods have Completed state.
               Check if there is any write or read pause. Fail only when neccessary.
            4) For bc/ab-bc netsplit cases, it is expected for logreader/logwriter pods to go CLBO
               Make sure the above pods run fine after the nodes are restarted
            5) Delete the old logreader job and create new logreader job to verify the data corruption
            6) Make sure there is no data loss
            7) Do a complete cluster sanity and make sure there is no issue post recovery

        """

        # run cephfs workload for both logwriter and logreader
        logwriter_workload, logreader_workload = setup_logwriter_cephfs_workload_factory
        time.sleep(60)
        logger.info("Workloads are running")

        # note all the pod names
        logwriter_pods = [
            Pod(**pod)
            for pod in get_pods_having_label(
                label="app=logwriter-cephfs",
                namespace=constants.STRETCH_CLUSTER_NAMESPACE,
            )
        ]

        logreader_pods = [
            Pod(**pod)
            for pod in get_pods_having_label(
                label="app=logreader-cephfs",
                namespace=constants.STRETCH_CLUSTER_NAMESPACE,
            )
        ]

        # note the file names created and each file start write time
        log_file_map = get_logfile_map_from_logwriter_pods(logwriter_pods)

        # Generate 5 minutes worth of logs before inducing the netsplit
        logger.info("Generating 5 mins worth of log")
        time.sleep(300)

        # note the start time (UTC)
        target_time = datetime.now() + timedelta(minutes=5)
        start_time = target_time.astimezone(timezone.utc)
        ocpnetsplit.main.schedule_split(
            nodes=get_all_nodes(),
            split_name=zones,
            target_dt=target_time,
            target_length=duration,
        )
        logger.info(f"Netsplit induced at {start_time} for zones {zones}")

        # note the end time (UTC)
        time.sleep((duration + 5) * 60)
        end_time = datetime.now(timezone.utc)
        logger.info(f"Ended netsplit at {end_time}")

        # wait for the logreader workload to finish
        statuses = ["Completed"]
        if zones in ("bc", "ab-bc"):
            statuses.append("Error")

        wait_for_pods_to_be_in_statuses(
            expected_statuses=statuses,
            pod_names=[pod.name for pod in logreader_pods],
            timeout=900,
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )
        logger.info("Logreader job pods have reached 'Completed' state!")

        # check if all the read operations are successful during the failure window, check for every minute
        if check_for_read_pause(logreader_pods, start_time, end_time):
            logger.info(f"Read operations are paused during {zones} netsplit window")
        else:
            logger.info("All the read operations are successful!!")

        # check if all the write operations are successful during the failure window, check for every minute
        for i in range(len(logwriter_pods)):
            try:
                if check_for_write_pause(
                    logwriter_pods[i], log_file_map.keys(), start_time, end_time
                ):
                    logger.info(
                        f"Write operations paused during {zones} netsplit window"
                    )
                else:
                    logger.info("All the write operations are successful!!")
                    break
            except CommandFailed as e:
                if (
                    "Permission Denied" in e.args[0]
                    or "unable to upgrade connection" in e.args[0]
                ) and zones in ["bc", "ab-bc"]:
                    continue
                else:
                    assert (
                        False
                    ), f"{logwriter_pods[i].name} pod failed to exec command with the following eror: {e.args[0]}"

        # reboot the nodes where the pods are not running
        pods_not_running = [
            pod
            for pod in logwriter_pods
            if OCP(
                kind="Pod", namespace=constants.STRETCH_CLUSTER_NAMESPACE
            ).get_resource_status(pod.name)
            != constants.STATUS_RUNNING
        ]
        assert (
            zones in ("bc", "ab-bc") or len(pods_not_running) == 0
        ), f"Unexpectedly these pods {pods_not_running} are not running after the {zones} failure"

        for pod in pods_not_running:
            node_obj = get_pod_node(pod)
            nodes.stop_nodes(nodes=[node_obj], wait=False)
            wait_for_nodes_status(
                node_names=[node_obj.name], status=constants.NODE_NOT_READY
            )
            nodes.start_nodes(nodes=[node_obj])
            wait_for_nodes_status(node_names=[node_obj.name])

        wait_for_pods_to_be_in_statuses(
            expected_statuses=constants.STATUS_RUNNING,
            pod_names=[pod.name for pod in logwriter_pods],
            timeout=900,
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )

        # check if all the already written data and files before netsplit started is intact
        log_files_after = [
            file_name
            for file_name in logwriter_pods[0]
            .exec_sh_cmd_on_pod(command="ls -l | awk 'NR>1' | awk '{print $9}'")
            .split("\n")
            if file_name != ""
        ]

        assert set([file for file in log_file_map.keys()]).issubset(
            log_files_after
        ), f"Log files mismatch before and after the netsplit {zones} failure"

        # check for data corruption
        logreader_pods = [
            Pod(**pod)
            for pod in get_pods_having_label(
                label="app=logreader-cephfs",
                namespace=constants.STRETCH_CLUSTER_NAMESPACE,
            )
        ]
        logreader_workload.delete()
        for pod in logreader_pods:
            pod.wait_for_pod_delete(timeout=120)
        logger.info("All old logreader pods are deleted")
        pvc = get_pvc_objs(
            pvc_names=[
                logwriter_workload.get()["spec"]["template"]["spec"]["volumes"][0][
                    "persistentVolumeClaim"
                ]["claimName"]
            ],
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )[0]
        logreader_workload_factory(
            pvc=pvc, logreader_path=constants.LOGWRITER_CEPHFS_READER, duration=5
        )
        logger.info("Getting new logreader pods!")
        new_logreader_pods = [
            Pod(**pod).name
            for pod in get_pods_having_label(
                label="app=logreader-cephfs",
                namespace=constants.STRETCH_CLUSTER_NAMESPACE,
            )
        ]
        for pod in logreader_pods:
            if pod.name in new_logreader_pods:
                new_logreader_pods.remove(pod.name)

        logger.info(f"New logreader pods: {new_logreader_pods}")

        wait_for_pods_to_be_in_statuses(
            expected_statuses=constants.STATUS_COMPLETED,
            pod_names=[pod_name for pod_name in new_logreader_pods],
            timeout=900,
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )
        logger.info("Logreader job pods have reached 'Completed' state!")

        for pod_name in new_logreader_pods:
            pod_logs = get_pod_logs(
                pod_name=pod_name, namespace=constants.STRETCH_CLUSTER_NAMESPACE
            )
            assert "corrupt" not in pod_logs, "Data is corrupted!!"
        logger.info("No data corruption is seen!")

    @pytest.mark.parametrize(
        argnames="zones, duration",
        argvalues=[
            pytest.param(constants.NETSPLIT_DATA_1_DATA_2, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1_AND_ARBITER_DATA_2, 15),
            pytest.param(constants.NETSPLIT_ARBITER_DATA_1_AND_DATA_1_DATA_2, 15),
        ],
        ids=[
            "Data-1-Data-2",
            "Arbiter-Data-1",
            "Arbiter-Data-1-and-Arbiter-Data-2",
            "Arbiter-Data-1-and-Data-1-Data-2",
        ],
    )
    def test_netsplit_rbd(
        self, setup_logwriter_rbd_workload_factory, zones, duration, init_sanity
    ):
        """
        This test will test the netsplit scenarios for the active-active RBD workloads
        Steps:
            1) Run a logwriter RBD workload with RWO volumes
            2) Check for write pause and fail when neccessary
            3) Run logreader script inside the logwriter pods
               and make sure no data corruption is seen
            4) Make sure no data loss seen
            5) Perfrom the complete cluste sanity and
            make sure no issues post recovery

        """
        time.sleep(60)
        logger.info("Logwriter statefulset is up!")

        # note all the pod names
        logwriter_pods = [
            Pod(**pod)
            for pod in get_pods_having_label(
                label="app=logwriter-rbd", namespace=constants.STRETCH_CLUSTER_NAMESPACE
            )
        ]

        # note the start time and files
        log_file_map = get_logfile_map_from_logwriter_pods(logwriter_pods, is_rbd=True)

        # Generate 5 minutes worth of logs before inducing the netsplit
        logger.info("Generating 5 mins worth of log")
        time.sleep(300)

        # note the start time (UTC)
        target_time = datetime.now() + timedelta(minutes=5)
        start_time = target_time.astimezone(timezone.utc)
        ocpnetsplit.main.schedule_split(
            nodes=get_all_nodes(),
            split_name=zones,
            target_dt=target_time,
            target_length=duration,
        )
        logger.info(f"Netsplit induced at {start_time} for zones {zones}")

        # note the end time (UTC)
        time.sleep((duration + 5) * 60)
        end_time = datetime.now(timezone.utc)
        logger.info(f"Ended netsplit at {end_time}")

        # check if all the write operations are successful during the failure window, check for every minute
        if check_for_write_pause(
            logwriter_pods[0],
            list(log_file_map[logwriter_pods[0].name].keys()),
            start_time,
            end_time,
        ) or check_for_write_pause(
            logwriter_pods[1],
            list(log_file_map[logwriter_pods[1].name].keys()),
            start_time,
            end_time,
        ):
            logger.info(f"Write operations paused during {zones} netsplit window")
        else:
            logger.info("All the write operations are successful!!")

        # check if all the already written data and files before netsplit started is intact
        log_files_after = []
        for logwriter_pod in logwriter_pods:
            for file_name in logwriter_pod.exec_sh_cmd_on_pod(
                command="ls -l | awk 'NR>1' | awk '{print $9}'"
            ).split("\n"):
                if file_name != "":
                    log_files_after.append(file_name)

        for logwriter_pod in log_file_map.keys():
            for file_name in log_file_map[logwriter_pod].keys():
                assert (
                    file_name in log_files_after
                ), f"{file_name} is missing after the netsplit failure"

        # run logreader script inside logrwriter pods and make sure no corruption is seen
        for logwriter_pod in logwriter_pods:
            output = logwriter_pod.exec_cmd_on_pod(
                command=f"/opt/logreader.py -t 5 {list(log_file_map[logwriter_pod.name].keys())[0]} -d",
                out_yaml_format=False,
            )
            assert "corrupt" not in output, "Data is corrupted!!"

        logger.info("No data corruption is seen")
