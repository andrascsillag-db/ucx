import json
import logging
from datetime import timedelta
from functools import partial

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import DatabricksError
from databricks.sdk.errors import InternalError, NotFound, PermissionDenied
from databricks.sdk.retries import retried
from databricks.sdk.service import iam
from databricks.sdk.service.iam import Group, Patch, PatchSchema

from databricks.labs.ucx.mixins.hardening import rate_limited
from databricks.labs.ucx.workspace_access.base import AclSupport, Permissions
from databricks.labs.ucx.workspace_access.groups import MigrationState

logger = logging.getLogger(__name__)


class ScimSupport(AclSupport):
    def __init__(self, ws: WorkspaceClient, verify_timeout: timedelta | None = timedelta(minutes=1)):
        self._ws = ws
        self._verify_timeout = verify_timeout

    @staticmethod
    def _is_item_relevant(item: Permissions, migration_state: MigrationState) -> bool:
        return any(g.id_in_workspace == item.object_id for g in migration_state.groups)

    def get_crawler_tasks(self):
        for g in self._get_groups():
            if g.roles and len(g.roles) > 0:
                yield partial(self._crawler_task, g, "roles")
            if g.entitlements and len(g.entitlements) > 0:
                yield partial(self._crawler_task, g, "entitlements")

    # TODO remove after ES-892977 is fixed
    @retried(on=[DatabricksError])
    def _get_groups(self):
        return list(self._list_workspace_groups(attributes="id,displayName,roles,entitlements"))

    def object_types(self) -> set[str]:
        return {"roles", "entitlements"}

    def get_apply_task(self, item: Permissions, migration_state: MigrationState):
        if not self._is_item_relevant(item, migration_state):
            return None
        value = [iam.ComplexValue.from_dict(e) for e in json.loads(item.raw)]
        target_group_id = migration_state.get_target_id(item.object_id)
        return partial(self._applier_task, group_id=target_group_id, value=value, property_name=item.object_type)

    @staticmethod
    def _crawler_task(group: iam.Group, property_name: str):
        return Permissions(
            object_id=group.id,
            object_type=property_name,
            raw=json.dumps([e.as_dict() for e in getattr(group, property_name)]),
        )

    def _inflight_check(self, group_id: str, value: list[iam.ComplexValue], property_name: str):
        # in-flight check for the applied permissions
        # the api might be inconsistent, therefore we need to check that the permissions were applied
        group = self._safe_get_group(group_id)
        if group:
            if property_name == "roles" and group.roles:
                if all(elem in group.roles for elem in value):
                    return True
            if property_name == "entitlements" and group.entitlements:
                if all(elem in group.entitlements for elem in value):
                    return True
            msg = f"""Couldn't apply appropriate role for group {group_id}
                            acl to be applied={[e.as_dict() for e in value]}
                            acl found in the object={group.as_dict()}
                            """
            raise ValueError(msg)
        return False

    @rate_limited(max_requests=10, burst_period_seconds=60)
    def _applier_task(self, group_id: str, value: list[iam.ComplexValue], property_name: str):
        operations = [iam.Patch(op=iam.PatchOp.ADD, path=property_name, value=[e.as_dict() for e in value])]
        schemas = [iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]

        patch_retry_on_value_error = retried(on=[DatabricksError], timeout=self._verify_timeout)
        patch_retried_check = patch_retry_on_value_error(self._safe_patch_group)
        patch_retried_check(group_id=group_id, operations=operations, schemas=schemas)

        retry_on_value_error = retried(on=[ValueError, DatabricksError], timeout=self._verify_timeout)
        retried_check = retry_on_value_error(self._inflight_check)
        return retried_check(group_id, value, property_name)

    def _safe_patch_group(
        self, group_id: str, operations: list[Patch] | None = None, schemas: list[PatchSchema] | None = None
    ):
        try:
            return self._ws.groups.patch(id=group_id, operations=operations, schemas=schemas)
        except PermissionDenied:
            logger.warning(f"permission denied: {group_id}")
            return None
        except NotFound:
            logger.warning(f"removed on backend: {group_id}")
            return None

    def _safe_get_group(self, group_id: str) -> Group | None:
        try:
            return self._ws.groups.get(group_id)
        except PermissionDenied:
            logger.warning(f"permission denied: {group_id}")
            return None
        except NotFound:
            logger.warning(f"removed on backend: {group_id}")
            return None

    def _is_group_out_of_scope(self, group: iam.Group) -> bool:
        if group.display_name in self._SYSTEM_GROUPS:
            return True
        return False

    @retried(on=[InternalError])
    @rate_limited(max_requests=255, burst_period_seconds=60)
    def _get_group_with_retries(self, group_id: str) -> iam.Group | None:
        return self._ws.groups.get(group_id)

    def _list_workspace_groups(self, scim_attributes: str) -> list[iam.Group]:
        results = []
        logger.info(f"Listing workspace groups with {scim_attributes}...")
        # these attributes can get too large causing the api to timeout
        # so we're fetching groups without these attributes first
        # and then calling get on each of them to fetch all attributes
        if "members" in scim_attributes or "roles" in scim_attributes or "entitlements" in scim_attributes:
            for g in self._ws.groups.list(attributes="id,displayName,meta"):
                if self._is_group_out_of_scope(g):
                    continue
                group_with_all_attributes = self._get_group_with_retries(g.id)
                results.append(group_with_all_attributes)
        else:
            for g in self._ws.groups.list(attributes=scim_attributes):
                if self._is_group_out_of_scope(g):
                    continue
                results.append(g)
        logger.info(f"Found {len(results)} groups")
        return results
