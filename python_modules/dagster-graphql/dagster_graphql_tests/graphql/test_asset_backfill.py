from typing import Optional

from dagster import AssetKey, Definitions, StaticPartitionsDefinition, asset
from dagster._core.execution.asset_backfill import AssetBackfillData
from dagster._core.test_utils import instance_for_test
from dagster_graphql.client.query import LAUNCH_PARTITION_BACKFILL_MUTATION
from dagster_graphql.test.utils import define_out_of_process_context, execute_dagster_graphql

GET_PARTITION_BACKFILLS_QUERY = """
  query InstanceBackfillsQuery($cursor: String, $limit: Int) {
    partitionBackfillsOrError(cursor: $cursor, limit: $limit) {
      ... on PartitionBackfills {
        results {
          backfillId
          status
          numPartitions
          timestamp
          partitionNames
          partitionSetName
          partitionSet {
            id
            name
            mode
            pipelineName
            repositoryOrigin {
              id
              repositoryName
              repositoryLocationName
            }
          }
        }
      }
    }
  }
"""

SINGLE_BACKFILL_QUERY = """
  query SingleBackfillQuery($backfillId: String!) {
    partitionBackfillOrError(backfillId: $backfillId) {
      ... on PartitionBackfill {
        partitionStatuses {
          results {
            id
            partitionName
            runId
            runStatus
          }
        }
      }
    }
  }
"""


def get_repo():
    partitions_def = StaticPartitionsDefinition(["a", "b", "c"])

    @asset(partitions_def=partitions_def)
    def asset1():
        ...

    @asset(partitions_def=partitions_def)
    def asset2():
        ...

    return Definitions(assets=[asset1, asset2]).get_repository_def()


def get_repo_with_non_partitioned_asset():
    partitions_def = StaticPartitionsDefinition(["a", "b", "c"])

    @asset(partitions_def=partitions_def)
    def asset1():
        ...

    @asset
    def asset2(asset1):
        ...

    return Definitions(assets=[asset1, asset2]).get_repository_def()


def test_launch_asset_backfill():
    repo = get_repo()
    all_asset_keys = repo.asset_graph.all_asset_keys

    with instance_for_test() as instance:
        # read-only context fails
        with define_out_of_process_context(
            __file__, "get_repo", instance, read_only=True
        ) as read_only_context:
            assert read_only_context.read_only
            # launchPartitionBackfill
            launch_backfill_result = execute_dagster_graphql(
                read_only_context,
                LAUNCH_PARTITION_BACKFILL_MUTATION,
                variables={
                    "backfillParams": {
                        "partitionNames": ["a", "b"],
                        "assetSelection": [key.to_graphql_input() for key in all_asset_keys],
                    }
                },
            )
            assert launch_backfill_result
            assert launch_backfill_result.data

            assert (
                launch_backfill_result.data["launchPartitionBackfill"]["__typename"]
                == "UnauthorizedError"
            )

        with define_out_of_process_context(__file__, "get_repo", instance) as context:
            # launchPartitionBackfill
            launch_backfill_result = execute_dagster_graphql(
                context,
                LAUNCH_PARTITION_BACKFILL_MUTATION,
                variables={
                    "backfillParams": {
                        "partitionNames": ["a", "b"],
                        "assetSelection": [key.to_graphql_input() for key in all_asset_keys],
                    }
                },
            )
            backfill_id, asset_backfill_data = _get_backfill_data(
                launch_backfill_result, instance, repo
            )
            assert asset_backfill_data.target_subset.asset_keys == all_asset_keys

            # on PartitionBackfills
            get_backfills_result = execute_dagster_graphql(
                context, GET_PARTITION_BACKFILLS_QUERY, variables={}
            )
            assert not get_backfills_result.errors
            assert get_backfills_result.data
            backfill_results = get_backfills_result.data["partitionBackfillsOrError"]["results"]
            assert len(backfill_results) == 1
            assert backfill_results[0]["numPartitions"] == 2
            assert backfill_results[0]["backfillId"] == backfill_id
            assert backfill_results[0]["partitionSet"] is None
            assert backfill_results[0]["partitionSetName"] is None
            assert set(backfill_results[0]["partitionNames"]) == {"a", "b"}

            # on PartitionBackfill
            single_backfill_result = execute_dagster_graphql(
                context, SINGLE_BACKFILL_QUERY, variables={"backfillId": backfill_id}
            )
            assert not single_backfill_result.errors
            assert single_backfill_result.data
            partition_status_results = single_backfill_result.data["partitionBackfillOrError"][
                "partitionStatuses"
            ]["results"]
            assert len(partition_status_results) == 2
            assert {
                partition_status_result["partitionName"]
                for partition_status_result in partition_status_results
            } == {"a", "b"}


def test_remove_partitions_defs_after_backfill():
    repo = get_repo()
    all_asset_keys = repo.asset_graph.all_asset_keys

    with instance_for_test() as instance:
        with define_out_of_process_context(__file__, "get_repo", instance) as context:
            # launchPartitionBackfill
            launch_backfill_result = execute_dagster_graphql(
                context,
                LAUNCH_PARTITION_BACKFILL_MUTATION,
                variables={
                    "backfillParams": {
                        "partitionNames": ["a", "b"],
                        "assetSelection": [key.to_graphql_input() for key in all_asset_keys],
                    }
                },
            )
            backfill_id, asset_backfill_data = _get_backfill_data(
                launch_backfill_result, instance, repo
            )
            assert asset_backfill_data.target_subset.asset_keys == all_asset_keys

        with define_out_of_process_context(
            __file__, "get_repo_with_non_partitioned_asset", instance
        ) as context:
            # on PartitionBackfills
            get_backfills_result = execute_dagster_graphql(
                context, GET_PARTITION_BACKFILLS_QUERY, variables={}
            )
            assert not get_backfills_result.errors
            assert get_backfills_result.data
            backfill_results = get_backfills_result.data["partitionBackfillsOrError"]["results"]
            assert len(backfill_results) == 1
            assert backfill_results[0]["numPartitions"] == 0
            assert backfill_results[0]["backfillId"] == backfill_id
            assert backfill_results[0]["partitionSet"] is None
            assert backfill_results[0]["partitionSetName"] is None
            assert set(backfill_results[0]["partitionNames"]) == set()

            # on PartitionBackfill
            single_backfill_result = execute_dagster_graphql(
                context, SINGLE_BACKFILL_QUERY, variables={"backfillId": backfill_id}
            )
            assert not single_backfill_result.errors
            assert single_backfill_result.data
            partition_status_results = single_backfill_result.data["partitionBackfillOrError"][
                "partitionStatuses"
            ]["results"]
            assert len(partition_status_results) == 0
            assert {
                partition_status_result["partitionName"]
                for partition_status_result in partition_status_results
            } == set()


def test_launch_asset_backfill_with_non_partitioned_asset():
    repo = get_repo_with_non_partitioned_asset()
    all_asset_keys = repo.asset_graph.all_asset_keys

    with instance_for_test() as instance:
        with define_out_of_process_context(
            __file__, "get_repo_with_non_partitioned_asset", instance
        ) as context:
            # launchPartitionBackfill
            launch_backfill_result = execute_dagster_graphql(
                context,
                LAUNCH_PARTITION_BACKFILL_MUTATION,
                variables={
                    "backfillParams": {
                        "partitionNames": ["a", "b"],
                        "assetSelection": [key.to_graphql_input() for key in all_asset_keys],
                    }
                },
            )
            backfill_id, asset_backfill_data = _get_backfill_data(
                launch_backfill_result, instance, repo
            )
            target_subset = asset_backfill_data.target_subset
            assert target_subset.asset_keys == all_asset_keys
            assert target_subset.get_partitions_subset(AssetKey("asset1")).get_partition_keys() == {
                "a",
                "b",
            }
            assert AssetKey("asset2") in target_subset.non_partitioned_asset_keys
            assert AssetKey("asset2") not in target_subset.partitions_subsets_by_asset_key


def _get_backfill_data(launch_backfill_result, instance, repo):
    assert launch_backfill_result
    assert launch_backfill_result.data
    assert (
        "backfillId" in launch_backfill_result.data["launchPartitionBackfill"]
    ), _get_error_message(launch_backfill_result)

    backfill_id = launch_backfill_result.data["launchPartitionBackfill"]["backfillId"]

    backfills = instance.get_backfills()
    assert len(backfills) == 1
    backfill = backfills[0]
    assert backfill.backfill_id == backfill_id
    assert backfill.serialized_asset_backfill_data

    return backfill_id, AssetBackfillData.from_serialized(
        backfill.serialized_asset_backfill_data, repo.asset_graph
    )


def _get_error_message(launch_backfill_result) -> Optional[str]:
    return (
        (
            "".join(launch_backfill_result.data["launchPartitionBackfill"]["stack"])
            + launch_backfill_result.data["launchPartitionBackfill"]["message"]
        )
        if "message" in launch_backfill_result.data["launchPartitionBackfill"]
        else None
    )