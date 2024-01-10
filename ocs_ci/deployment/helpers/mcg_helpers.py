"""
This module contains helper functions which is needed for MCG only deployment
"""

import logging
import tempfile

from ocs_ci.framework import config
from ocs_ci.ocs import constants, ocp
from ocs_ci.ocs.utils import enable_console_plugin
from ocs_ci.utility import templating, version
from ocs_ci.utility.utils import run_cmd

logger = logging.getLogger(__name__)


def mcg_only_deployment():
    """
    Creates cluster with MCG only deployment
    """
    logger.info("Creating storage cluster with MCG only deployment")
    cluster_data = templating.load_yaml(constants.STORAGE_CLUSTER_YAML)
    cluster_data["spec"]["multiCloudGateway"] = {}
    cluster_data["spec"]["multiCloudGateway"]["reconcileStrategy"] = "standalone"
    del cluster_data["spec"]["storageDeviceSets"]
    cluster_data_yaml = tempfile.NamedTemporaryFile(
        mode="w+", prefix="cluster_storage", delete=False
    )
    templating.dump_data_to_temp_yaml(cluster_data, cluster_data_yaml.name)
    run_cmd(f"oc create -f {cluster_data_yaml.name}", timeout=1200)


def mcg_only_post_deployment_checks():
    """
    Verification of MCG only after deployment
    """
    # check for odf-console
    ocs_version = version.get_semantic_ocs_version_from_config()
    pod = ocp.OCP(kind=constants.POD, namespace=config.ENV_DATA["cluster_namespace"])
    if ocs_version >= version.VERSION_4_9:
        assert pod.wait_for_resource(
            condition="Running", selector="app=odf-console", timeout=600
        )

    # Enable console plugin
    enable_console_plugin()


def check_if_mcg_root_secret_public():
    """
    Verify if MCG root secret is public

    Returns:
        False if the secrets are not public and True otherwise

    """

    noobaa_endpoint_dep = ocp.OCP(
        kind="Deployment",
        namespace=config.ENV_DATA["cluster_namespace"],
        resource_name=constants.NOOBAA_ENDPOINT_DEPLOYMENT,
    ).get()

    noobaa_core_sts = ocp.OCP(
        kind="Statefulset",
        namespace=config.ENV_DATA["cluster_namespace"],
        resource_name=constants.NOOBAA_CORE_STATEFULSET,
    ).get()

    nb_endpoint_env = noobaa_endpoint_dep["spec"]["template"]["spec"]["containers"]
    nb_core_env = noobaa_core_sts["spec"]["template"]["spec"]["containers"]

    def _check_env_vars(env_vars):
        """
        Method verifies the environment variable lists
        if the root secret is public

        """
        for env in env_vars:
            if env["name"] == "NOOBAA_ROOT_SECRET" and "value" in env.keys():
                return True
        return False

    return _check_env_vars(nb_core_env) or _check_env_vars(nb_endpoint_env)
