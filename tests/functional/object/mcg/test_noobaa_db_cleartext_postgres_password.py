import logging

from ocs_ci.framework.testlib import tier2, BaseTest, bugzilla, polarion_id
from ocs_ci.framework.pytest_customization.marks import red_squad, mcg
from ocs_ci.framework import config
from ocs_ci.ocs.resources import pod
from ocs_ci.ocs.resources.pod import filter_pod_logs

log = logging.getLogger(__name__)


@tier2
class TestNoobaaSecurity(BaseTest):
    """
    Test Noobaa Security

    """

    @mcg
    @red_squad
    @bugzilla("2274193")
    @polarion_id("OCS-5787")
    def test_noobaa_db_cleartext_postgres_password(self):
        """
        Verify postgres password is not clear text

        Test Process:

        1.Get noobaa db pod
        2.Get logs from all containers in pod oc logs "noobaa-db-pg-0 --all-containers"
        3.Verify postgres password does not exist in noobaa-db pod logs
        """
        nooobaa_db_pod_obj = pod.get_noobaa_db_pod()
        log.info(
            "Get logs from all containers in pod 'oc logs noobaa-db-pg-0 --all-containers'"
        )
        nooobaa_db_pod_logs = pod.get_pod_logs(
            pod_name=nooobaa_db_pod_obj.name,
            namespace=config.ENV_DATA["cluster_namespace"],
            all_containers=True,
        )
        log.info("Verify postgres password does not exist in noobaa-db pod logs")
        assert (
            "set=password" not in nooobaa_db_pod_logs
        ), f"noobaa-db pod logs include password logs:{nooobaa_db_pod_logs}"

    def test_nb_db_password_in_core_and_endpoint(self):
        """
        Verify that postgres password is not exposed in
        noobaa core and endpoint logs

        1. Get the noobaa core log
        2. Get the noobaa endpoint log
        3. Verify postgres password doesnt exist in the endpoint and core logs

        """

        # get noobaa core log and verify that the password is not
        # present in the log
        nooba_core_pod = pod.get_noobaa_core_pod()
        noobaa_core_pod_logs = pod.get_pod_logs(nooba_core_pod.name)
        filtered_log = filter_pod_logs(
            pod_logs=noobaa_core_pod_logs,
            filter=[
                "host",
                "noobaa-db-pg-0.noobaa-db-pg",
                "user",
                "noobaa",
                "database",
                "nbcore",
                "port",
                "5432",
                "password",
            ],
        )
        assert (
            len(filtered_log) == 0
        ), f"Noobaa db password seems to be present in the noobaa core logs:\n{filtered_log}"
        log.info(
            "Verified that noobaa db password is not present in the noobaa core log."
        )

        # get noobaa endpoint log and verify that the password is not
        # present in the log
        noobaa_endpoint_pod = pod.get_noobaa_endpoint_pods()[0]
        noobaa_endpoint_logs = pod.get_pod_logs(noobaa_endpoint_pod.name)
        filtered_log = filter_pod_logs(
            pod_logs=noobaa_endpoint_logs,
            filter=[
                "host",
                "noobaa-db-pg-0.noobaa-db-pg",
                "user",
                "noobaa",
                "database",
                "nbcore",
                "port",
                "5432",
                "password",
            ],
        )
        assert (
            len(filtered_log) == 0
        ), f"Noobaa db password seems to be present in the noobaa endpoint logs:\n{filtered_log}"
        log.info(
            "Verified that noobaa db password is not present in the noobaa endpoint log."
        )
