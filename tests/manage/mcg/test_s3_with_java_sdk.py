import logging
import pytest

from ocs_ci.framework.pytest_customization.marks import bugzilla, polarion_id
from ocs_ci.ocs.bucket_utils import upload_objects_with_javasdk

logger = logging.getLogger(__name__)


class TestS3WithJavaSDK:
    @bugzilla("2064304")
    @polarion_id("OCS-3964")
    @pytest.mark.parametrize(
        argnames=["is_multipart"],
        argvalues=[
            pytest.param(*[False]),
            pytest.param(*[True]),
        ],
    )
    def test_s3_upload_with_java(
        self, bucket_factory, javasdk_pod_session, mcg_obj_session, is_multipart
    ):
        """
        Tests S3 upload with Java application
        """

        regular_bucket_name = bucket_factory()[0].name
        namespace_bucketclass_dict = {
            "interface": "OC",
            "namespace_policy_dict": {
                "type": "Single",
                "namespacestore_dict": {"aws": [(1, "eu-central-1")]},
            },
        }
        namespace_bucket_name = bucket_factory(bucketclass=namespace_bucketclass_dict)[
            0
        ].name

        if is_multipart:
            logger.info(
                f"Initiating Multipart upload operation on buckets {regular_bucket_name} and {namespace_bucket_name}"
            )
        else:
            logger.info(
                f"Initiating upload operation on buckets {regular_bucket_name} and {namespace_bucket_name}"
            )

        # initiating upload operation with regular s3 bucket
        assert upload_objects_with_javasdk(
            javas3_pod=javasdk_pod_session,
            s3_obj=mcg_obj_session,
            bucket_name=regular_bucket_name,
            is_multipart=is_multipart,
        ), f"Upload objects failed for bucket {regular_bucket_name}"
        logger.info(f"Uploaded objects successfully for bucket {regular_bucket_name}")

        # initiating upload operation with namespace bucket
        assert upload_objects_with_javasdk(
            javas3_pod=javasdk_pod_session,
            s3_obj=mcg_obj_session,
            bucket_name=namespace_bucket_name,
            is_multipart=is_multipart,
        ), f"Upload objects failed for namespace bucket {namespace_bucket_name}"
        logger.info(
            f"Uploaded objects successfully for namespace bucket {namespace_bucket_name}"
        )
