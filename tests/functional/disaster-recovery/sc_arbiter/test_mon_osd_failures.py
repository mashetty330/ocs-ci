import random
import time

import pytest
import logging

from datetime import datetime, timezone

from ocs_ci.helpers.helpers import modify_deployment_replica_count
from ocs_ci.helpers.stretchcluster_helper import recover_workload_pods_post_recovery
from ocs_ci.ocs.exceptions import UnexpectedBehaviour
from ocs_ci.ocs.resources.pod import (
    get_not_running_pods,
    wait_for_pods_to_be_in_statuses,
    get_deployment_name,
)
from ocs_ci.ocs.resources.pvc import get_pvc_objs
from ocs_ci.ocs.resources.stretchcluster import StretchCluster
from ocs_ci.ocs import constants

logger = logging.getLogger(__name__)


@pytest.fixture(scope="class")
def setup_logwriter_workloads(
    request,
    setup_logwriter_cephfs_workload_class,
    setup_logwriter_rbd_workload_class,
    logreader_workload_class,
):
    sc_obj = StretchCluster()
    # Run the logwriter cephFs workloads
    (
        sc_obj.cephfs_logwriter_dep,
        sc_obj.cephfs_logreader_job,
    ) = setup_logwriter_cephfs_workload_class(read_duration=0)

    # Generate 5 minutes worth of logs before inducing the netsplit
    logger.info("Generating 2 mins worth of log")
    time.sleep(120)

    sc_obj.get_logwriter_reader_pods(label=constants.LOGWRITER_CEPHFS_LABEL)
    sc_obj.get_logwriter_reader_pods(label=constants.LOGREADER_CEPHFS_LABEL)
    sc_obj.get_logwriter_reader_pods(
        label=constants.LOGWRITER_RBD_LABEL, exp_num_replicas=2
    )
    logger.info("All the workloads pods are successfully up and running")

    start_time = datetime.now(timezone.utc)
    sc_obj.get_logfile_map(label=constants.LOGWRITER_CEPHFS_LABEL)
    sc_obj.get_logfile_map(label=constants.LOGWRITER_RBD_LABEL)

    def finalizer():
        """
        Check for data loss, data corruption at the end of the tests

        """
        end_time = datetime.now(timezone.utc)

        try:
            sc_obj.get_logwriter_reader_pods(label=constants.LOGWRITER_CEPHFS_LABEL)
            sc_obj.get_logwriter_reader_pods(
                label=constants.LOGREADER_CEPHFS_LABEL,
                statuses=[constants.STATUS_RUNNING, constants.STATUS_COMPLETED],
            )
            sc_obj.get_logwriter_reader_pods(
                label=constants.LOGWRITER_RBD_LABEL, exp_num_replicas=2
            )
        except UnexpectedBehaviour:

            logger.info("some pods are not running, so trying the work-around")
            pods_not_running = get_not_running_pods(
                namespace=constants.STRETCH_CLUSTER_NAMESPACE
            )
            recover_workload_pods_post_recovery(sc_obj, pods_not_running)

        sc_obj.post_failure_checks(start_time, end_time, wait_for_read_completion=False)
        logger.info("Successfully verified with post failure checks for the workloads")

        sc_obj.cephfs_logreader_job.delete()
        logger.info(sc_obj.cephfs_logreader_pods)
        for pod in sc_obj.cephfs_logreader_pods:
            pod.wait_for_pod_delete(timeout=120)
        logger.info("All old CephFS logreader pods are deleted")

        # check for any data loss
        assert sc_obj.check_for_data_loss(
            constants.LOGWRITER_CEPHFS_LABEL
        ), "[CephFS] Data is lost"
        logger.info("[CephFS] No data loss is seen")
        assert sc_obj.check_for_data_loss(
            constants.LOGWRITER_RBD_LABEL
        ), "[RBD] Data is lost"
        logger.info("[RBD] No data loss is seen")

        # check for data corruption
        pvc = get_pvc_objs(
            pvc_names=[
                sc_obj.cephfs_logwriter_dep.get()["spec"]["template"]["spec"][
                    "volumes"
                ][0]["persistentVolumeClaim"]["claimName"]
            ],
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )[0]
        logreader_workload_class(
            pvc=pvc, logreader_path=constants.LOGWRITER_CEPHFS_READER, duration=5
        )
        sc_obj.get_logwriter_reader_pods(constants.LOGREADER_CEPHFS_LABEL)

        wait_for_pods_to_be_in_statuses(
            expected_statuses=constants.STATUS_COMPLETED,
            pod_names=[pod.name for pod in sc_obj.cephfs_logreader_pods],
            timeout=900,
            namespace=constants.STRETCH_CLUSTER_NAMESPACE,
        )
        logger.info("[CephFS] Logreader job pods have reached 'Completed' state!")

        assert sc_obj.check_for_data_corruption(
            label=constants.LOGREADER_CEPHFS_LABEL
        ), "Data is corrupted for cephFS workloads"
        logger.info("No data corruption is seen in CephFS workloads")

        assert sc_obj.check_for_data_corruption(
            label=constants.LOGWRITER_RBD_LABEL
        ), "Data is corrupted for RBD workloads"
        logger.info("No data corruption is seen in RBD workloads")

    request.addfinalizer(finalizer)


@pytest.mark.usefixtures("setup_logwriter_workloads")
class TestMonAndOSDFailures:
    def test_single_mon_failures(self):
        """
        Test mon failure with IO in the background

        """
        logger.info("testing single mon failures scenario")
        sc_obj = StretchCluster()

        # get mon-pod of a single zone
        mon_pods_in_zone = sc_obj.get_mon_pods_in_a_zone("data-1")
        mon_pod_to_fail = random.choice(mon_pods_in_zone).name

        # get the deployment of the mon-pod
        mon_dep = get_deployment_name(mon_pod_to_fail)

        # scale the deployment of mon to 0
        # and wait 10 mins
        logger.info(f"failing mon {mon_dep} now...")
        if modify_deployment_replica_count(mon_dep, 0):
            time.sleep(600)

        # scale the deployment back to 1
        logger.info(f"recovering mon {mon_dep} now...")
        modify_deployment_replica_count(mon_dep, 1)

    def test_both_mon_failure(self):
        """
        Test mon failure with IO for both the data-zones

        """
        logger.info("testing mon failures at both the data-zones")
        sc_obj = StretchCluster()
        data_zones = constants.ZONES_LABELS
        data_zones.remove("arbiter")
        mon_deps = list()
        for zone in data_zones:
            # get mon-pod of a single zone
            mon_pods_in_zone = sc_obj.get_mon_pods_in_a_zone(zone)
            mon_pod_to_fail = random.choice(mon_pods_in_zone).name

            # get the deployment of the mon-pod
            mon_dep = get_deployment_name(mon_pod_to_fail)

            # scale the deployment of mon to 0
            # and wait 10 mins
            modify_deployment_replica_count(mon_dep, 0)
            logger.info(f"Failing mon {mon_dep} from data-zone {zone}")
            mon_deps.append(mon_dep)

        time.sleep(600)

        # scale the deployments back to 1
        for mon_dep in mon_deps:
            logger.info(f"recovering mon {mon_dep}")
            modify_deployment_replica_count(mon_dep, 1)

    def test_single_osd_failure(self):
        """
        Test single osd failure while IO's running

        """
        logger.info("testing single osd failure scenarios")
        sc_obj = StretchCluster()

        # get osd-pod of a single zone
        osd_pods_in_zone = sc_obj.get_osd_pods_in_a_zone("data-1")
        osd_pod_to_fail = random.choice(osd_pods_in_zone).name

        # get the deployment of the osd-pod
        osd_dep = get_deployment_name(osd_pod_to_fail)

        # scale the deployment of osd to 0
        # and wait 10 mins
        logger.info(f"failing osd {osd_dep} now...")
        if modify_deployment_replica_count(osd_dep, 0):
            time.sleep(600)

        # scale the deployment back to 1
        logger.info(f"recovering osd {osd_dep} now...")
        modify_deployment_replica_count(osd_dep, 1)
