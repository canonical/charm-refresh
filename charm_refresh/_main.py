import abc
import collections.abc
import dataclasses
import enum
import functools
import json
import logging
import os
import pathlib
import platform
import subprocess
import time
import typing

import charm_ as charm
import charm_json
import httpx
import lightkube
import lightkube.models.authorization_v1
import lightkube.resources.apps_v1
import lightkube.resources.authorization_v1
import lightkube.resources.core_v1
import ops
import packaging.version
import tomli
import yaml

# Use package name instead of module name
logger = logging.getLogger(__name__.rsplit(".", maxsplit=1)[0])


def _removeprefix(text: str, /, *, prefix: str) -> str:
    """python 3.8 compatible equivalent to `str.removeprefix()`"""
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


@functools.total_ordering
class CharmVersion:
    """Charm code version

    Stored as a git tag on charm repositories

    TODO: link to docs about versioning spec
    """

    def __init__(self, version: str, /):
        # Example 1: "16/1.19.0"
        # Example 2: "16/1.19.0.post1.dev0+71201f4.dirty"
        self._version = version
        track, pep440_version = self._version.split("/")
        # Example 1: "16"
        self.track = track
        """Charmhub track"""

        if "!" in pep440_version:
            raise ValueError(
                f"Invalid charm version {repr(str(self))}. PEP 440 epoch ('!' character) not "
                "supported"
            )
        try:
            self._pep440_version = packaging.version.Version(pep440_version)
        except packaging.version.InvalidVersion:
            raise ValueError(f"Invalid charm version {repr(str(self))}")
        if len(self._pep440_version.release) != 3:
            raise ValueError(
                f"Invalid charm version {repr(str(self))}. Expected 3 number components after "
                f"track; got {len(self._pep440_version.release)} components: "
                f"{repr(self._pep440_version.base_version)}"
            )
        # Example 1: True
        # Example 2: False
        self.released = pep440_version == self._pep440_version.base_version
        """Whether version was released & correctly tagged

        `True` for charm code correctly released to Charmhub
        `False` for development builds
        """

        # Example 1: 1
        self.major = self._pep440_version.release[0]
        """Incremented if refresh not supported or only supported with intermediate charm version

        If a change is made to the charm code that causes refreshes to not be supported or to only
        be supported with the use of an intermediate charm version, this number is incremented.

        If this number is equivalent on two charm code versions with equivalent tracks, refreshing
        from the lower to higher charm code version is supported without the use of an intermediate
        charm version.
        """
        # TODO: add info about intermediate charms & link to docs about versioning spec

    def __str__(self):
        return self._version

    def __repr__(self):
        return f"{type(self).__name__}({repr(str(self))})"

    def __eq__(self, other):
        if isinstance(other, str):
            return str(self) == other
        return isinstance(other, CharmVersion) and self._version == other._version

    def __gt__(self, other):
        if not isinstance(other, CharmVersion):
            return NotImplemented
        if self.track != other.track:
            raise ValueError(
                f"Unable to compare versions with different tracks: {repr(self.track)} and "
                f"{repr(other.track)} ({repr(self)} and {repr(other)})"
            )
        return self._pep440_version > other._pep440_version


class PrecheckFailed(Exception):
    """Pre-refresh health check or preparation failed

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/pre-refresh-checks/
    """

    def __init__(self, message: str, /):
        if len(message) == 0:
            raise ValueError(f"{type(self).__name__} message must be longer than 0 characters")
        self.message = message
        super().__init__(message)


@dataclasses.dataclass(eq=False)
class CharmSpecificCommon(abc.ABC):
    """Charm-specific callbacks & configuration for in-place refreshes on Kubernetes & machines

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/charm-specific/
    """

    workload_name: str
    """Human readable workload name (e.g. PostgreSQL)"""

    charm_name: str
    """Charm name in metadata.yaml (e.g. postgresql-k8s)"""

    def __post_init__(self):
        workload_name_and_version = f"{self.workload_name} {_RefreshVersions().workload}"
        if len(workload_name_and_version) > 43:
            # Lower priority unit status may exceed 120 characters and get truncated in `juju
            # status`
            raise ValueError(
                "The combined length of `workload_name` and the workload version in "
                "refresh_versions.toml (plus one space) must be <= 43 characters. Got "
                f"{len(workload_name_and_version)} characters: {repr(workload_name_and_version)}"
            )

    @staticmethod
    @abc.abstractmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        """Run pre-refresh health checks & preparations after the first unit has already refreshed.

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/pre-refresh-checks/

        Raises:
            PrecheckFailed: A pre-refresh health check or preparation failed
        """

    def run_pre_refresh_checks_before_any_units_refreshed(self) -> None:
        """Run pre-refresh health checks & preparations before any unit is refreshed.

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/pre-refresh-checks/

        Raises:
            PrecheckFailed: A pre-refresh health check or preparation failed
        """
        self.run_pre_refresh_checks_after_1_unit_refreshed()

    @staticmethod
    def _is_charm_version_compatible(*, old: CharmVersion, new: CharmVersion):
        """Check that new charm version is higher than old and that major versions are identical

        TODO talk about intermediate charms

        TODO talk about recommendation to not support charm code downgrade
        """
        if not (old.released and new.released):
            # Unreleased charms contain changes that do not affect the version number
            # Those changes could affect compatability
            return False
        if old.track != new.track:
            return False
        if old.major != new.major:
            return False
        # By default, charm code downgrades are not supported (rollbacks are supported)
        return new >= old

    @classmethod
    @abc.abstractmethod
    def is_compatible(
        cls,
        *,
        old_charm_version: CharmVersion,
        new_charm_version: CharmVersion,
        old_workload_version: str,
        new_workload_version: str,
    ) -> bool:
        """Whether refresh is supported from old to new workload and charm code versions

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/is-compatible/
        """
        if not cls._is_charm_version_compatible(old=old_charm_version, new=new_charm_version):
            return False
        return True


@dataclasses.dataclass(eq=False)
class CharmSpecificKubernetes(CharmSpecificCommon, abc.ABC):
    """Charm-specific callbacks & configuration for in-place refreshes on Kubernetes

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/charm-specific/
    """

    oci_resource_name: str
    """Resource name for workload OCI image in metadata.yaml `resources` (e.g. postgresql-image)

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/instantiate/kubernetes/
    """


@dataclasses.dataclass(eq=False)
class CharmSpecificMachines(CharmSpecificCommon, abc.ABC):
    """Charm-specific callbacks & configuration for in-place refreshes on machines

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/charm-specific/
    """

    @abc.abstractmethod
    def refresh_snap(self, *, snap_name: str, snap_revision: str, refresh: "Machines") -> None:
        """Refresh workload snap

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/refresh-snap/
        """


class Common(abc.ABC):
    """In-place rolling refreshes of stateful charmed applications

    This class provides the common interface across Kubernetes & machines and cannot be
    instantiated directly. Instantiate `Kubernetes` or `Machines`
    """

    @property
    @abc.abstractmethod
    def in_progress(self) -> bool:
        """Whether a refresh is currently in progress

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/in-progress/
        """

    @property
    @abc.abstractmethod
    def next_unit_allowed_to_refresh(self) -> bool:
        """Whether the next unit is allowed to refresh

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/next-unit-allowed-to-refresh/
        """

    @next_unit_allowed_to_refresh.setter
    @abc.abstractmethod
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        pass

    @property
    @abc.abstractmethod
    def workload_allowed_to_start(self) -> bool:
        """Whether this unit's workload is allowed to start

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/workload-allowed-to-start/
        """

    @property
    @abc.abstractmethod
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """App status with higher priority than any other app status in the charm

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/status/
        """

    @property
    @abc.abstractmethod
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with higher priority than any other unit status in the charm

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/status/
        """

    @abc.abstractmethod
    def unit_status_lower_priority(
        self, *, workload_is_running: bool = True
    ) -> typing.Optional[ops.StatusBase]:
        """Unit status with lower priority than any other unit status with a message in the charm

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/status/
        """

    @abc.abstractmethod
    def __init__(self, charm_specific: CharmSpecificCommon, /):
        pass


class PeerRelationNotReady(Exception):
    """Refresh peer relation is not yet available or not all units have joined yet

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/peer-relation-not-ready/
    """


class _PeerRelationMissing(PeerRelationNotReady):
    """Refresh peer relation is not yet available"""


class UnitTearingDown(Exception):
    """This unit is being removed

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/unit-tearing-down/
    """


class KubernetesJujuAppNotTrusted(Exception):
    """Juju app is not trusted (needed to patch StatefulSet partition)

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/kubernetes-juju-app-not-trusted/
    """


def _convert_to_ops_status(
    status: typing.Optional[charm.Status],
) -> typing.Optional[ops.StatusBase]:
    if status is None:
        return None
    ops_types = {
        charm.ActiveStatus: ops.ActiveStatus,
        charm.WaitingStatus: ops.WaitingStatus,
        charm.MaintenanceStatus: ops.MaintenanceStatus,
        charm.BlockedStatus: ops.BlockedStatus,
    }
    for charm_type, ops_type in ops_types.items():
        if isinstance(status, charm_type):
            return ops_type(str(status))
    raise ValueError(f"Unknown type {repr(type(status).__name__)}: {repr(status)}")


@functools.total_ordering
class _PauseAfter(str, enum.Enum):
    """`pause_after_unit_refresh` config option"""

    NONE = "none"
    FIRST = "first"
    ALL = "all"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN

    def __gt__(self, other):
        if not isinstance(other, _PauseAfter):
            # Raise instead of `return NotImplemented` since this class inherits from `str`
            raise TypeError
        priorities = {self.NONE: 0, self.FIRST: 1, self.ALL: 2, self.UNKNOWN: 3}
        return priorities[self] > priorities[other]


class _RefreshVersions:
    """Versions pinned in this unit's refresh_versions.toml"""

    def __init__(self):
        with pathlib.Path("refresh_versions.toml").open("rb") as file:
            self._versions = tomli.load(file)
        try:
            self.charm = CharmVersion(self._versions["charm"])
            self.workload: str = self._versions["workload"]
        except KeyError:
            raise KeyError(
                "Required key missing from refresh_versions.toml. Docs: "
                "https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/refresh-versions-toml/"
            )
        except ValueError:
            raise ValueError("Invalid charm version in refresh_versions.toml")


class _MachinesRefreshVersions(_RefreshVersions):
    """Versions pinned in this (machines) unit's refresh_versions.toml

    On machines, the pinned workload versions (`_MachinesRefreshVersions.workload` and
    `_MachinesRefreshVersions.snap_revision`) may be different from the installed workload versions
    """

    def __init__(self):
        super().__init__()
        try:
            self.snap_name: str = self._versions["snap"]["name"]
            snap_revisions = self._versions["snap"]["revisions"]
        except KeyError:
            raise KeyError(
                "Required key missing from refresh_versions.toml. Docs: "
                "https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/refresh-versions-toml/"
            )
        try:
            self.snap_revision: str = snap_revisions[platform.machine()]
        except KeyError:
            raise KeyError(
                f"Snap revision missing for architecture {repr(platform.machine())}. Docs: "
                "https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/refresh-versions-toml/"
            )


def snap_name() -> str:
    """Get workload snap name

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/remove-duplicate-hardcoded-snap/
    """
    return _MachinesRefreshVersions().snap_name


_LOCAL_STATE = pathlib.Path(".charm_refresh_v3")
"""Local state for this unit

On Kubernetes, deleted when pod is deleted
This directory is stored in /var/lib/juju/ on the charm container
(e.g. in /var/lib/juju/agents/unit-postgresql-k8s-0/charm/)
As of Juju 3.5.3, /var/lib/juju/ is stored in a Kubernetes emptyDir volume
https://kubernetes.io/docs/concepts/storage/volumes/#emptydir
This means that it will not be deleted on container restart—it will only be deleted if the pod is
deleted
"""

_dot_juju_charm = pathlib.Path(".juju-charm")


class _RawCharmRevision(str):
    """Charm revision in .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-602")"""

    @classmethod
    def from_file(cls):
        """Charm revision in this unit's .juju-charm file"""
        return cls(_dot_juju_charm.read_text().strip())

    @property
    def charmhub_revision(self) -> typing.Optional[str]:
        if self.startswith("ch:"):
            return self.split("-")[-1]


def _dot_juju_charm_modified_time():
    """Modified time of .juju-charm file (e.g. 1727768259.4063382)"""
    return _dot_juju_charm.stat().st_mtime


@dataclasses.dataclass(frozen=True)
class _OriginalVersions:
    """Versions (of all units) immediately after the last completed refresh

    Or, if no completed refreshes, immediately after juju deploy and (on machines) initial
    installation
    """

    workload: typing.Optional[str]
    """Original upstream workload version (e.g. "16.8")

    Always a str if `installed_workload_container_matched_pinned_container` is `True`
    `None` if `installed_workload_container_matched_pinned_container` is `False`
    """
    workload_container: str
    """Original workload image digest (Kubernetes) or snap revision (machines)

    (Kubernetes example: "sha256:e53eb99abd799526bb5a5e6c58180ee47e2790c95d433a1352836aa27d0914a4")
    (machines example: "182")
    """
    installed_workload_container_matched_pinned_container: bool
    """Whether original workload container matched container pinned in original charm code"""
    charm: CharmVersion
    """Original charm version"""
    charm_revision_raw: _RawCharmRevision
    """Original charm revision in .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-602")"""

    def __post_init__(self):
        if self.installed_workload_container_matched_pinned_container and self.workload is None:
            raise ValueError(
                "`workload` cannot be `None` if "
                "`installed_workload_container_matched_pinned_container` is `True`"
            )
        elif (
            not self.installed_workload_container_matched_pinned_container
            and self.workload is not None
        ):
            raise ValueError(
                "`workload` must be `None` if "
                "`installed_workload_container_matched_pinned_container` is `False`"
            )

    @classmethod
    def from_app_databag(cls, databag: collections.abc.Mapping, /):
        try:
            return cls(
                workload=databag["original_workload_version"],
                workload_container=databag["original_workload_container_version"],
                installed_workload_container_matched_pinned_container=databag[
                    "original_installed_workload_container_matched_pinned_container"
                ],
                charm=CharmVersion(databag["original_charm_version"]),
                charm_revision_raw=_RawCharmRevision(databag["original_charm_revision"]),
            )
        except (KeyError, ValueError):
            # This should only happen if user refreshes from a charm without refresh v3
            raise ValueError(
                "Refresh failed. Automatic recovery not possible. Original versions in app "
                "databag are missing or invalid"
            )

    def write_to_app_databag(self, databag: collections.abc.MutableMapping, /):
        new_values = {
            "original_workload_version": self.workload,
            "original_workload_container_version": self.workload_container,
            "original_installed_workload_container_matched_pinned_container": self.installed_workload_container_matched_pinned_container,
            "original_charm_version": str(self.charm),
            "original_charm_revision": self.charm_revision_raw,
        }
        for key, value in new_values.items():
            if databag.get(key) != value:
                diff = True
                break
        else:
            diff = False
        databag.update(new_values)
        if diff:
            logger.info(f"Saved versions to app databag for next refresh: {repr(self)}")


class _KubernetesUnit(charm.Unit):
    def __new__(cls, name: str, /, *, controller_revision: str, pod_uid: str):
        instance: _KubernetesUnit = super().__new__(cls, name)
        instance.controller_revision = controller_revision
        instance.pod_uid = pod_uid
        return instance

    def __repr__(self):
        return (
            f"{type(self).__name__}({repr(str(self))}, "
            f"controller_revision={repr(self.controller_revision)}, pod_uid={repr(self.pod_uid)})"
        )

    @classmethod
    def from_pod(cls, pod: lightkube.resources.core_v1.Pod, /):
        # Example: "postgresql-k8s-0"
        pod_name = pod.metadata.name
        app_name, unit_number = pod_name.rsplit("-", maxsplit=1)
        # Example: "postgresql-k8s/0"
        unit_name = f"{app_name}/{unit_number}"
        return cls(
            unit_name,
            controller_revision=pod.metadata.labels["controller-revision-hash"],
            pod_uid=pod.metadata.uid,
        )


class Kubernetes(Common):
    """In-place rolling refreshes of stateful charmed applications on Kubernetes

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/instantiate/

    Raises:
        KubernetesJujuAppNotTrusted: https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/kubernetes-juju-app-not-trusted/
        UnitTearingDown: https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/unit-tearing-down/
        PeerRelationNotReady: https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/peer-relation-not-ready/
    """

    # Use `@Common.in_progress.getter` instead of `@property` to preserve docstring from parent
    # class
    @Common.in_progress.getter
    def in_progress(self) -> bool:
        return self._in_progress

    @Common.next_unit_allowed_to_refresh.getter
    def next_unit_allowed_to_refresh(self) -> bool:
        return (
            self._relation.my_unit.get(
                "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
            )
            # Compare to `self._unit_controller_revision` instead of
            # `self._app_controller_revision` since this is checking whether this unit has allowed
            # the next unit to refresh—not whether the next unit is allowed to refresh.
            == self._unit_controller_revision
        )

    # Do not use `@Common.next_unit_allowed_to_refresh.setter` so that the getter defined in this
    # class is not overridden
    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        if value is not True:
            raise ValueError("`next_unit_allowed_to_refresh` can only be set to `True`")
        if not self.workload_allowed_to_start:
            raise Exception(
                "`next_unit_allowed_to_refresh` cannot be set to `True` when "
                "`workload_allowed_to_start` is `False`"
            )
        if (
            self._relation.my_unit.get(
                "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
            )
            != self._unit_controller_revision
        ):
            logger.info(
                "Allowed next unit to refresh if app's StatefulSet controller revision is "
                f"{self._unit_controller_revision} and if permitted by pause_after_unit_refresh "
                "config option or resume-refresh action"
            )
            self._relation.my_unit[
                "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
            ] = self._unit_controller_revision
            self._set_partition_and_app_status(handle_action=False)

    @Common.workload_allowed_to_start.getter
    def workload_allowed_to_start(self) -> bool:
        if not self._in_progress:
            return True
        for unit in self._units:
            if (
                self._unit_controller_revision
                # During scale up or scale down, `unit` may be missing from relation
                in self._relation.get(unit, {}).get(
                    "refresh_started_if_app_controller_revision_hash_in", tuple()
                )
            ):
                return True
        if self._unit_controller_revision in self._relation.my_app_ro.get(
            "refresh_started_if_app_controller_revision_hash_in", tuple()
        ):
            return True
        original_versions = _OriginalVersions.from_app_databag(self._relation.my_app_ro)
        if (
            original_versions.charm == self._installed_charm_version
            and original_versions.workload_container == self._installed_workload_container_version
        ):
            # This unit has not refreshed
            # (If this unit is rolling back, `True` should have been returned earlier)
            return True
        return False

    @Common.app_status_higher_priority.getter
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        return _convert_to_ops_status(self._app_status_higher_priority)

    @Common.unit_status_higher_priority.getter
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        return _convert_to_ops_status(self._unit_status_higher_priority)

    def unit_status_lower_priority(
        self, *, workload_is_running: bool = True
    ) -> typing.Optional[ops.StatusBase]:
        if not self._in_progress:
            return None
        workload_container_matches_pin = (
            self._installed_workload_container_version == self._pinned_workload_container_version
        )
        if workload_container_matches_pin:
            message = f"{self._charm_specific.workload_name} {self._pinned_workload_version}"
        else:
            # The user refreshed to a workload container that is not pinned by the charm code. This
            # is likely a mistake, but may be intentional.
            # We don't know what workload version is in the workload container
            message = f"{self._charm_specific.workload_name}"
        restart_pending = self._unit_controller_revision != self._app_controller_revision
        if workload_is_running:
            message += " running"
            if restart_pending:
                message += " (restart pending)"
        if self._installed_charm_revision_raw.charmhub_revision:
            # Charm was deployed from Charmhub; use revision
            message += f"; Charm revision {self._installed_charm_revision_raw.charmhub_revision}"
        else:
            # Charmhub revision is not available; fall back to charm version
            message += f"; Charm version {self._installed_charm_version}"
        if not workload_container_matches_pin:
            if self._installed_workload_container_version:
                message += (
                    "; Unexpected container "
                    f"{_removeprefix(self._installed_workload_container_version, prefix='sha256:')[:6]}"
                )
            else:
                # This message is unlikely to be displayed—the status will probably be overridden
                # by a Kubernetes ImagePullBackOff error
                message += "; Unable to check container"
        if not workload_is_running and restart_pending:
            # Display at end of message instead of next to workload to avoid implying that the
            # workload is running
            message += " (restart pending)"
        if workload_is_running:
            return ops.ActiveStatus(message)
        return ops.WaitingStatus(message)

    @staticmethod
    def _get_partition() -> int:
        """Get Kubernetes StatefulSet rollingUpdate partition

        Specifies which units can refresh

        Unit numbers >= partition can refresh
        Unit numbers < partition cannot refresh

        If the partition is lowered (e.g. to 1) and then raised (e.g. to 2), the unit (unit 1) that
        refreshed will stay on the new version unless its pod is deleted. After its pod is deleted,
        it will be re-created on the old version (if the partition is higher than its unit number).

        Lowering the partition does not guarantee that a unit will refresh.
        > The Kubernetes control plane waits until an updated Pod is Running and Ready prior to
          updating its predecessor.

        https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions
        """
        stateful_set = lightkube.Client().get(lightkube.resources.apps_v1.StatefulSet, charm.app)
        partition = stateful_set.spec.updateStrategy.rollingUpdate.partition
        assert partition is not None
        return partition

    @staticmethod
    def _set_partition(value: int, /):
        """Set Kubernetes StatefulSet rollingUpdate partition

        Specifies which units can refresh

        Unit numbers >= partition can refresh
        Unit numbers < partition cannot refresh

        If the partition is lowered (e.g. to 1) and then raised (e.g. to 2), the unit (unit 1) that
        refreshed will stay on the new version unless its pod is deleted. After its pod is deleted,
        it will be re-created on the old version (if the partition is higher than its unit number).

        Lowering the partition does not guarantee that a unit will refresh.
        > The Kubernetes control plane waits until an updated Pod is Running and Ready prior to
          updating its predecessor.

        https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions
        """
        lightkube.Client().patch(
            lightkube.resources.apps_v1.StatefulSet,
            charm.app,
            {"spec": {"updateStrategy": {"rollingUpdate": {"partition": value}}}},
        )

    def _start_refresh(self):
        """Run automatic checks after `juju refresh` on highest unit & set `self._refresh_started`

        Automatic checks include:

        - workload container check
        - compatibility checks
        - pre-refresh checks

        Handles force-refresh-start action

        Sets `self._refresh_started` if `self._in_progress`

        If this unit is the highest number unit, this unit is up-to-date, and the refresh to
        `self._app_controller_revision` has not already started, this method will check for one of
        the following conditions:

        - this unit is rolling back
        - run all the automatic checks & check that all were successful
        - run the automatic checks (if any) that were not skipped by the force-refresh-start action
          and check that they were successful

        If one of those conditions is met, this method will append this unit's controller revision
        to "refresh_started_if_app_controller_revision_hash_in" in this unit's databag and will
        touch `self._refresh_started_local_state`

        Sets `self._unit_status_higher_priority` & unit status. Unit status only set if
        `self._unit_status_higher_priority` (unit status is not cleared if
        `self._unit_status_higher_priority` is `None`—that is the responsibility of the charm)
        """

        class _InvalidForceEvent(ValueError):
            """Event is not valid force-refresh-start action event"""

        class _ForceRefreshStartAction(charm.ActionEvent):
            def __init__(
                self, event: charm.Event, /, *, first_unit_to_refresh: charm.Unit, in_progress: bool
            ):
                if not isinstance(event, charm.ActionEvent):
                    raise _InvalidForceEvent
                super().__init__()
                if event.action != "force-refresh-start":
                    raise _InvalidForceEvent
                if charm.unit != first_unit_to_refresh:
                    event.fail(f"Must run action on unit {first_unit_to_refresh.number}")
                    raise _InvalidForceEvent
                if not in_progress:
                    event.fail("No refresh in progress")
                    raise _InvalidForceEvent
                self.check_workload_container: bool = event.parameters["check-workload-container"]
                self.check_compatibility: bool = event.parameters["check-compatibility"]
                self.run_pre_refresh_checks: bool = event.parameters["run-pre-refresh-checks"]
                for parameter in (
                    self.check_workload_container,
                    self.check_compatibility,
                    self.run_pre_refresh_checks,
                ):
                    if parameter is False:
                        break
                else:
                    event.fail(
                        "Must run with at least one of `check-compatibility`, "
                        "`run-pre-refresh-checks`, or `check-workload-container` parameters "
                        "`=false`"
                    )
                    raise _InvalidForceEvent

        force_start: typing.Optional[_ForceRefreshStartAction]
        try:
            force_start = _ForceRefreshStartAction(
                charm.event, first_unit_to_refresh=self._units[0], in_progress=self._in_progress
            )
        except _InvalidForceEvent:
            force_start = None
        self._unit_status_higher_priority: typing.Optional[charm.Status] = None
        if not self._in_progress:
            return
        self._refresh_started = any(
            self._app_controller_revision
            # During scale up or scale down, `unit` may be missing from relation
            in self._relation.get(unit, {}).get(
                "refresh_started_if_app_controller_revision_hash_in", tuple()
            )
            for unit in self._units
        ) or self._app_controller_revision in self._relation.my_app_ro.get(
            "refresh_started_if_app_controller_revision_hash_in", tuple()
        )
        """Whether this app has started to refresh to `self._app_controller_revision`

        `True` if this app is rolling back, if automatic checks have succeeded, or if the user
        successfully forced the refresh to start with the force-refresh-start action
        `False` otherwise

        Automatic checks include:

        - workload container check
        - compatibility checks
        - pre-refresh checks

        If the user runs `juju refresh` while a refresh is in progress, this will be reset to
        `False` unless the `juju refresh` is a rollback
        """

        if charm.unit != self._units[0]:
            return
        if self._unit_controller_revision != self._app_controller_revision:
            if force_start:
                force_start.fail(
                    f"Unit {charm.unit.number} is outdated and waiting for its pod to be updated "
                    "by Kubernetes"
                )
            return
        # If `self._unit_controller_revision == self._app_controller_revision` and
        # `len(self._units) == 1`, `self._in_progress` should be `False`
        assert len(self._units) > 1

        original_versions = _OriginalVersions.from_app_databag(self._relation.my_app_ro)
        if not self._refresh_started:
            # Check if this unit is rolling back
            if (
                original_versions.charm == self._installed_charm_version
                and original_versions.workload_container
                == self._installed_workload_container_version
            ):
                # Rollback to original charm code & workload container version; skip checks

                if (
                    self._installed_workload_container_version
                    == self._pinned_workload_container_version
                ):
                    workload_version = (
                        f"{self._charm_specific.workload_name} {self._pinned_workload_version} "
                        f"(container {repr(self._installed_workload_container_version)})"
                    )
                else:
                    workload_version = (
                        f"{self._charm_specific.workload_name} container "
                        f"{repr(self._installed_workload_container_version)}"
                    )
                if self._installed_charm_revision_raw.charmhub_revision:
                    charm_version = (
                        f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                        f"({repr(self._installed_charm_version)})"
                    )
                else:
                    charm_version = f"{repr(self._installed_charm_version)}"
                logger.info(
                    "Rollback detected. Automatic refresh checks skipped. Refresh started for "
                    f"StatefulSet controller revision {self._unit_controller_revision}. Rolling "
                    f"back to {workload_version} and charm {charm_version}"
                )

                self._refresh_started = True
                hashes: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                    "refresh_started_if_app_controller_revision_hash_in", tuple()
                )
                if self._unit_controller_revision not in hashes:
                    hashes.append(self._unit_controller_revision)
                self._refresh_started_local_state.touch()
        if self._refresh_started:
            if force_start:
                force_start.fail(f"Unit {charm.unit.number} already refreshed")
            return

        # Run automatic checks

        # Log workload & charm versions we're refreshing from & to
        from_to_message = f"from {self._charm_specific.workload_name} "
        if original_versions.installed_workload_container_matched_pinned_container:
            from_to_message += (
                f"{original_versions.workload} (container "
                f"{repr(original_versions.workload_container)}) "
            )
        else:
            from_to_message += f"container {repr(original_versions.workload_container)} "
        from_to_message += "and charm "
        if original_versions.charm_revision_raw.charmhub_revision:
            from_to_message += (
                f"revision {original_versions.charm_revision_raw.charmhub_revision} "
                f"({repr(original_versions.charm)}) "
            )
        else:
            from_to_message += f"{repr(original_versions.charm)} "
        from_to_message += f"to {self._charm_specific.workload_name} "
        if self._installed_workload_container_version == self._pinned_workload_container_version:
            from_to_message += (
                f"{self._pinned_workload_version} (container "
                f"{repr(self._installed_workload_container_version)}) "
            )
        else:
            from_to_message += f"container {repr(self._installed_workload_container_version)} "
        from_to_message += "and charm "
        if self._installed_charm_revision_raw.charmhub_revision:
            from_to_message += (
                f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                f"({repr(self._installed_charm_version)})"
            )
        else:
            from_to_message += f"{repr(self._installed_charm_version)}"
        if force_start:
            false_values = []
            if not force_start.check_workload_container:
                false_values.append("check-workload-container")
            if not force_start.check_compatibility:
                false_values.append("check-compatibility")
            if not force_start.run_pre_refresh_checks:
                false_values.append("run-pre-refresh-checks")
            from_to_message += (
                ". force-refresh-start action ran with "
                f"{' '.join(f'{key}=false' for key in false_values)}"
            )
        logger.info(
            f"Attempting to start refresh (for StatefulSet controller revision "
            f"{self._unit_controller_revision}) {from_to_message}"
        )

        if force_start and not force_start.check_workload_container:
            force_start.log(
                f"Skipping check that refresh is to {self._charm_specific.workload_name} "
                "container version that has been validated to work with the charm revision"
            )
        else:
            # Check workload container
            if (
                self._installed_workload_container_version
                == self._pinned_workload_container_version
            ):
                if force_start:
                    force_start.log(
                        f"Checked that refresh is to {self._charm_specific.workload_name} "
                        "container version that has been validated to work with the charm revision"
                    )
            else:
                logger.info(
                    f"Expected {self._charm_specific.workload_name} container digest "
                    f"{repr(self._pinned_workload_container_version)}, got "
                    f"{repr(self._installed_workload_container_version)} instead"
                )
                self._unit_status_higher_priority = charm.BlockedStatus(
                    "`juju refresh` was run with missing/incorrect OCI resource. Rollback with "
                    "instructions in docs or see `juju debug-log`"
                )
                charm.unit_status = self._unit_status_higher_priority
                logger.error(
                    "`juju refresh` was run with missing or incorrect OCI resource. Rollback by "
                    f"running `{self._rollback_command}`. If you are intentionally attempting to "
                    f"refresh to a {self._charm_specific.workload_name} container version that is "
                    "not validated with this release, you may experience data loss and/or "
                    "downtime as a result of refreshing. The refresh can be forced to continue "
                    "with the `force-refresh-start` action and the `check-workload-container` "
                    f"parameter. Run `juju show-action {charm.app} force-refresh-start` for more "
                    "information"
                )
                if force_start:
                    force_start.fail(
                        f"Refresh is to {self._charm_specific.workload_name} container version "
                        "that has not been validated to work with the charm revision. Rollback by "
                        f"running `{self._rollback_command}`"
                    )
                return
        if force_start and not force_start.check_compatibility:
            force_start.log(
                "Skipping check for compatibility with previous "
                f"{self._charm_specific.workload_name} version and charm revision"
            )
        else:
            # Check compatibility
            if (
                # If original workload container did not match pinned workload container or if
                # current workload container does not match pinned workload container, the refresh
                # is incompatible—unless it is a rollback (which was checked for earlier in this
                # method).
                original_versions.installed_workload_container_matched_pinned_container
                and self._installed_workload_container_version
                == self._pinned_workload_container_version
                # Original & current workload containers match(ed) pinned containers
                #
                and self._charm_specific.is_compatible(
                    old_charm_version=original_versions.charm,
                    new_charm_version=self._installed_charm_version,
                    # `original_versions.workload` is not `None` since
                    # `original_versions.installed_workload_container_matched_pinned_container` is
                    # `True`
                    old_workload_version=original_versions.workload,
                    new_workload_version=self._pinned_workload_version,
                )
            ):
                if force_start:
                    force_start.log(
                        f"Checked that refresh from previous {self._charm_specific.workload_name} "
                        "version and charm revision to current versions is compatible"
                    )
            else:
                # Log reason why compatibility check failed
                if not original_versions.installed_workload_container_matched_pinned_container:
                    if original_versions.charm_revision_raw.charmhub_revision:
                        # Charm was deployed from Charmhub; use revision
                        charm_version = (
                            f"revision {original_versions.charm_revision_raw.charmhub_revision}"
                        )
                    else:
                        # Charmhub revision is not available; fall back to charm version
                        charm_version = f"{repr(original_versions.charm)}"
                    logger.info(
                        "Refresh incompatible because original "
                        f"{self._charm_specific.workload_name} container version "
                        f"({repr(original_versions.workload_container)}) did not match container "
                        f"pinned in original charm ({charm_version})"
                    )
                elif (
                    self._installed_workload_container_version
                    != self._pinned_workload_container_version
                ):
                    logger.info(
                        f"Refresh incompatible because {self._charm_specific.workload_name} "
                        f"container version ({repr(self._installed_workload_container_version)}) "
                        "does not match container pinned in charm "
                        f"({repr(self._pinned_workload_container_version)})"
                    )
                else:
                    logger.info(
                        "Refresh incompatible because new version of "
                        f"{self._charm_specific.workload_name} "
                        f"({repr(self._pinned_workload_version)}) and/or charm "
                        f"({repr(self._installed_charm_version)}) is not compatible with previous "
                        f"version of {self._charm_specific.workload_name} "
                        f"({repr(original_versions.workload)}) and/or charm "
                        f"({repr(original_versions.charm)})"
                    )

                self._unit_status_higher_priority = charm.BlockedStatus(
                    "Refresh incompatible. Rollback with instructions in Charmhub docs or see "
                    "`juju debug-log`"
                )
                charm.unit_status = self._unit_status_higher_priority
                logger.info(
                    f"Refresh incompatible. Rollback by running `{self._rollback_command}`. "
                    "Continuing this refresh may cause data loss and/or downtime. The refresh can "
                    "be forced to continue with the `force-refresh-start` action and the "
                    f"`check-compatibility` parameter. Run `juju show-action {charm.app} "
                    "force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Refresh incompatible. Rollback by running `{self._rollback_command}`"
                    )
                return
        if force_start and not force_start.run_pre_refresh_checks:
            force_start.log("Skipping pre-refresh checks")
        else:
            # Run pre-refresh checks
            if force_start:
                force_start.log("Running pre-refresh checks")
            try:
                self._charm_specific.run_pre_refresh_checks_after_1_unit_refreshed()
            except PrecheckFailed as exception:
                self._unit_status_higher_priority = charm.BlockedStatus(
                    f"Rollback with `juju refresh`. Pre-refresh check failed: {exception.message}"
                )
                charm.unit_status = self._unit_status_higher_priority
                logger.error(
                    f"Pre-refresh check failed: {exception.message}. Rollback by running "
                    f"`{self._rollback_command}`. Continuing this refresh may cause data loss "
                    "and/or downtime. The refresh can be forced to continue with the "
                    "`force-refresh-start` action and the `run-pre-refresh-checks` parameter. Run "
                    f"`juju show-action {charm.app} force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Pre-refresh check failed: {exception.message}. Rollback by running "
                        f"`{self._rollback_command}`"
                    )
                return
            if force_start:
                force_start.log("Pre-refresh checks successful")
        # All checks that ran succeeded
        logger.info(
            f"Automatic checks succeeded{' or skipped' if force_start else ''}. Refresh started "
            f"for StatefulSet controller revision {self._unit_controller_revision}. Starting "
            f"{self._charm_specific.workload_name} on this unit. Refresh is {from_to_message}"
        )
        self._refresh_started = True
        hashes: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
            "refresh_started_if_app_controller_revision_hash_in", tuple()
        )
        if self._unit_controller_revision not in hashes:
            hashes.append(self._unit_controller_revision)
        self._refresh_started_local_state.touch()
        if force_start:
            force_start.result = {
                "result": (
                    f"{self._charm_specific.workload_name} refreshed on unit "
                    f"{charm.unit.number}. Starting {self._charm_specific.workload_name} on unit "
                    f"{charm.unit.number}"
                )
            }

    def _set_partition_and_app_status(self, *, handle_action: bool):
        """Lower StatefulSet partition and set `self._app_status_higher_priority` & app status

        Handles resume-refresh action if `handle_action`

        App status only set if `self._app_status_higher_priority` (app status is not cleared if
        `self._app_status_higher_priority` is `None`—that is the responsibility of the charm)
        """
        # `handle_action` parameter needed to prevent duplicate action logs if this method is
        # called twice in one Juju event

        self._app_status_higher_priority: typing.Optional[charm.Status] = None

        class _ResumeRefreshAction(charm.ActionEvent):
            def __init__(self, event: charm.ActionEvent, /):
                super().__init__()
                assert event.action == "resume-refresh"
                self.check_health_of_refreshed_units: bool = event.parameters[
                    "check-health-of-refreshed-units"
                ]

        action: typing.Optional[_ResumeRefreshAction] = None
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "resume-refresh":
            action = _ResumeRefreshAction(charm.event)
        if not charm.is_leader:
            if handle_action and action:
                action.fail(
                    f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader "
                    "resume-refresh`)"
                )
            return
        if self._pause_after is _PauseAfter.UNKNOWN:
            self._app_status_higher_priority = charm.BlockedStatus(
                'pause_after_unit_refresh config must be set to "all", "first", or "none"'
            )
        if not self._in_progress:
            if self._get_partition() != 0:
                self._set_partition(0)
                logger.info("Set StatefulSet partition to 0 since refresh not in progress")
            if handle_action and action:
                action.fail("No refresh in progress")
            if self._app_status_higher_priority:
                charm.app_status = self._app_status_higher_priority
            return
        if (
            handle_action
            and action
            and self._pause_after is _PauseAfter.NONE
            and action.check_health_of_refreshed_units
        ):
            action.fail(
                "`pause_after_unit_refresh` config is set to `none`. This action is not applicable."
            )
            # Do not log any additional information to action output
            action = None

        # If the StatefulSet partition exceeds the highest unit number, `juju refresh` will not
        # trigger any Juju events.
        # If a unit is tearing down, the leader unit may not receive another Juju event after that
        # unit has torn down. Therefore, the leader unit needs to exclude units that are tearing
        # down when determining the partition.

        for index, unit in enumerate(self._units_not_tearing_down):
            if unit.controller_revision != self._app_controller_revision:
                break
        next_unit_to_refresh = unit
        next_unit_to_refresh_index = index

        # Determine if `next_unit_to_refresh` is allowed to refresh and the `reason` why/why not
        if action and not action.check_health_of_refreshed_units:
            allow_next_unit_to_refresh = True
            reason = "resume-refresh action ran with check-health-of-refreshed-units=false"
            if handle_action:
                action.log("Ignoring health of refreshed units")
                # Include "Attempting to" since we only control the partition, not which units
                # refresh.
                # Lowering the partition does not guarantee that a unit will refresh.
                # > The Kubernetes control plane waits until an updated Pod is Running and Ready
                #   prior to updating its predecessor.
                action.result = {
                    "result": f"Attempting to refresh unit {next_unit_to_refresh.number}"
                }
        elif not self._refresh_started:
            allow_next_unit_to_refresh = False
            reason = (
                "highest number unit's workload has not started for StatefulSet controller "
                f"revision {self._app_controller_revision}"
            )
            if handle_action and action:
                assert action.check_health_of_refreshed_units
                action.fail(f"Unit {self._units[0].number} is unhealthy. Refresh will not resume.")
        else:
            # Check if up-to-date units have allowed the next unit to refresh
            up_to_date_units = self._units_not_tearing_down[:next_unit_to_refresh_index]
            for unit in up_to_date_units:
                if (
                    # During scale up or scale down, `unit` may be missing from relation
                    self._relation.get(unit, {}).get(
                        "next_unit_allowed_to_refresh_if_app_controller_revision_hash_equals"
                    )
                    != self._app_controller_revision
                ):
                    # `unit` has not allowed the next unit to refresh
                    allow_next_unit_to_refresh = False
                    reason = f"unit {unit.number} has not allowed the next unit to refresh"
                    if handle_action and action:
                        action.fail(f"Unit {unit.number} is unhealthy. Refresh will not resume.")
                    break
            else:
                # All up-to-date units (that are not tearing down) have allowed the next unit to
                # refresh
                if (
                    action
                    or self._pause_after is _PauseAfter.NONE
                    or (self._pause_after is _PauseAfter.FIRST and next_unit_to_refresh_index >= 2)
                ):
                    allow_next_unit_to_refresh = True
                    if action:
                        assert action.check_health_of_refreshed_units
                        reason = "resume-refresh action ran"
                    else:
                        reason = (
                            f"pause_after_unit_refresh config is {repr(self._pause_after.value)}"
                        )
                        if self._pause_after is _PauseAfter.FIRST:
                            reason += " and second unit already refreshed"
                    if handle_action and action:
                        assert self._pause_after is not _PauseAfter.NONE
                        if self._pause_after is _PauseAfter.FIRST:
                            action.result = {
                                "result": (
                                    f"Refresh resumed. Unit {next_unit_to_refresh.number} "
                                    "is refreshing next"
                                )
                            }
                        else:
                            assert (
                                self._pause_after is _PauseAfter.ALL
                                or self._pause_after is _PauseAfter.UNKNOWN
                            )
                            action.result = {
                                "result": f"Unit {next_unit_to_refresh.number} is refreshing next"
                            }
                else:
                    # User must run resume-refresh action to refresh `next_unit_to_refresh`
                    allow_next_unit_to_refresh = False
                    reason = (
                        "waiting for user to run resume-refresh action "
                        f"(pause_after_unit_refresh_config is {repr(self._pause_after.value)})"
                    )

        if allow_next_unit_to_refresh:
            target_partition = next_unit_to_refresh.number
        else:
            # Use unit before `next_unit_to_refresh`, if it exists (and is not tearing down), to
            # determine `target_partition`
            target_partition = self._units_not_tearing_down[
                max(next_unit_to_refresh_index - 1, 0)
            ].number

        # Only lower the partition—do not raise it
        # If the partition is lowered and then quickly raised, the unit that is refreshing will not
        # be able to start. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073473
        # (If this method is called during the resume-refresh action and then called in another
        # Juju event a few seconds later, `target_partition` can be higher than it was during the
        # resume-refresh action.)
        partition = self._get_partition()
        if target_partition < partition:
            self._set_partition(target_partition)
            partition = target_partition
            message = f"Set StatefulSet partition to {target_partition} because {reason}"
            if units_tearing_down := [
                unit for unit in self._units if unit not in self._units_not_tearing_down
            ]:
                message += (
                    ". Computed by excluding units that are tearing down: "
                    f"{', '.join(str(unit.number) for unit in units_tearing_down)}"
                )
            logger.info(message)
        if partition == self._units[-1].number:
            # Last unit is able to refresh
            # At this point, a rollback is probably only possible if Kubernetes decides to not
            # refresh the last unit even though the partition allows it to refresh. The
            # pause_after_unit_refresh config option cannot be used to halt the refresh since the
            # partition is already set to the lowest unit.
            self._app_status_higher_priority = charm.MaintenanceStatus(
                "Refreshing. To rollback, see docs or `juju debug-log`"
            )
        elif self._pause_after is _PauseAfter.ALL or (
            self._pause_after is _PauseAfter.FIRST
            # Whether only the first unit (that is not tearing down) is allowed to refresh
            and partition >= self._units_not_tearing_down[0].number
        ):
            self._app_status_higher_priority = charm.BlockedStatus(
                f"Refreshing. Check units >={partition} are healthy & run `resume-refresh` on "
                "leader. To rollback, see docs or `juju debug-log`"
            )
        else:
            self._app_status_higher_priority = charm.MaintenanceStatus(
                f"Refreshing. To pause refresh, run `juju config {charm.app} "
                "pause_after_unit_refresh=all`"
            )
        assert self._app_status_higher_priority is not None
        charm.app_status = self._app_status_higher_priority

    def __init__(self, charm_specific: CharmSpecificKubernetes, /):
        if not isinstance(charm_specific, CharmSpecificKubernetes):
            raise TypeError(
                f"expected type 'CharmSpecificKubernetes', got {repr(type(charm_specific).__name__)}"
            )
        self._charm_specific = charm_specific

        _LOCAL_STATE.mkdir(exist_ok=True)
        # Save state if this unit is tearing down.
        # Used in future Juju events
        tearing_down = _LOCAL_STATE / "kubernetes_unit_tearing_down"
        if (
            isinstance(charm.event, charm.RelationDepartedEvent)
            and charm.event.departing_unit == charm.unit
        ):
            # This unit is tearing down
            tearing_down.touch()

        # Check if Juju app was deployed with `--trust` (needed to patch StatefulSet partition)
        if not (
            lightkube.Client()
            .create(
                lightkube.resources.authorization_v1.SelfSubjectAccessReview(
                    spec=lightkube.models.authorization_v1.SelfSubjectAccessReviewSpec(
                        resourceAttributes=lightkube.models.authorization_v1.ResourceAttributes(
                            name=charm.app,
                            namespace=charm.model,
                            resource="statefulset",
                            verb="patch",
                        )
                    )
                )
            )
            .status.allowed
        ):
            logger.warning(
                f"Run `juju trust {charm.app} --scope=cluster`. Needed for in-place refreshes"
            )
            if charm.is_leader:
                charm.app_status = charm.BlockedStatus(
                    f"Run `juju trust {charm.app} --scope=cluster`. Needed for in-place refreshes"
                )
            raise KubernetesJujuAppNotTrusted

        # Get app & unit controller revisions from Kubernetes API
        # Each `juju refresh` updates the app's StatefulSet which creates a new controller revision
        # https://kubernetes.io/docs/reference/kubernetes-api/workload-resources/controller-revision-v1/
        # Controller revisions are used by Kubernetes for StatefulSet rolling updates
        self._app_controller_revision: str = (
            lightkube.Client()
            .get(lightkube.resources.apps_v1.StatefulSet, charm.app)
            .status.updateRevision
        )
        """This app's controller revision"""
        assert self._app_controller_revision is not None
        pods = lightkube.Client().list(
            lightkube.resources.core_v1.Pod, labels={"app.kubernetes.io/name": charm.app}
        )
        unsorted_units = []
        for pod in pods:
            unit = _KubernetesUnit.from_pod(pod)
            unsorted_units.append(unit)
            if unit == charm.unit:
                this_pod = pod
        assert this_pod
        self._units = sorted(unsorted_units, reverse=True)
        """Sorted from highest to lowest unit number (refresh order)"""
        self._unit_controller_revision = next(
            unit for unit in self._units if unit == charm.unit
        ).controller_revision
        """This unit's controller revision"""

        # Check if this unit is tearing down
        if tearing_down.exists():
            if isinstance(charm.event, charm.ActionEvent) and charm.event.action in (
                "pre-refresh-check",
                "force-refresh-start",
                "resume-refresh",
            ):
                charm.event.fail("Unit tearing down")

            tearing_down_logged = _LOCAL_STATE / "kubernetes_unit_tearing_down_logged"
            if not tearing_down_logged.exists():
                logger.info(
                    "Unit tearing down (pod uid "
                    f"{next(unit for unit in self._units if unit == charm.unit).pod_uid})"
                )
                tearing_down_logged.touch()

            raise UnitTearingDown

        # Determine `self._in_progress`
        for unit in self._units:
            if unit.controller_revision != self._app_controller_revision:
                self._in_progress = True
                break
        else:
            self._in_progress = False

        self._relation = charm_json.PeerRelation.from_endpoint("refresh-v-three")

        # Raise StatefulSet partition during stop event
        if (
            isinstance(charm.event, charm.StopEvent)
            # `self._in_progress` will be `True` even when the first unit to refresh is stopping
            # after `juju refresh`—since the StatefulSet is updated (and therefore
            # `self._app_controller_revision` is updated) before the first unit stops to refresh
            and self._in_progress
        ):
            # If `tearing_down.exists()`, this unit is being removed and we should not raise the
            # partition—so that the partition never exceeds the highest unit number (which would
            # cause `juju refresh` to not trigger any Juju events).
            assert not tearing_down.exists()
            # This unit could be refreshing or just restarting.
            # Raise StatefulSet partition to prevent other units from refreshing.
            # If the unit is just restarting, the leader unit will lower the partition.
            if self._get_partition() < charm.unit.number:
                # Raise partition
                self._set_partition(charm.unit.number)
                logger.info(f"Set StatefulSet partition to {charm.unit.number} during stop event")
                if self._relation:
                    # Trigger Juju event on leader unit to lower partition if needed
                    # Use timestamp instead of pod uid because of
                    # https://bugs.launchpad.net/juju/+bug/2068500/comments/8
                    self._relation.my_unit[
                        "_unused_timestamp_during_last_stop_event_where_partition_raised"
                    ] = time.time()

        if not self._relation:
            raise _PeerRelationMissing

        if (
            isinstance(charm.event, charm.StopEvent)
            and self._in_progress
            and charm.unit == self._units[0]
        ):
            # Trigger Juju event on other units so that they quickly update app & unit status after
            # a refresh starts
            # Use `self._app_controller_revision` instead of `self._unit_controller_revision`
            # because of https://bugs.launchpad.net/juju/+bug/2068500/comments/8
            # Usually, during the stop event immediately after a refresh starts, the partition will
            # be raised (and this is redundant). This is only useful for the exceptionally rare
            # case where the partition was set to the highest unit number before the refresh
            # started.
            self._relation.my_unit[
                "_unused_app_controller_revision_during_last_stop_event_as_highest_unit"
            ] = self._app_controller_revision

        # Raise StatefulSet partition after pod restart
        # Raise partition in case of rollback from charm code that was raising uncaught exception.
        # If the charm code was raising an uncaught exception, Juju may have skipped the stop event
        # when that unit's pod was deleted for rollback.
        # This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2068500
        had_opportunity_to_raise_partition_after_pod_restart = (
            _LOCAL_STATE / "kubernetes_had_opportunity_to_raise_partition_after_pod_restart"
        )
        if not had_opportunity_to_raise_partition_after_pod_restart.exists() and self._in_progress:
            # If `tearing_down.exists()`, this unit is being removed and we should not raise the
            # partition—so that the partition never exceeds the highest unit number (which would
            # cause `juju refresh` to not trigger any Juju events).
            assert not tearing_down.exists()
            # This unit could have been refreshing or just restarting.
            # Raise StatefulSet partition to prevent other units from refreshing.
            # If the unit was just restarting, the leader unit will lower the partition.
            if self._get_partition() < charm.unit.number:
                # Raise partition
                self._set_partition(charm.unit.number)
                logger.info(f"Set StatefulSet partition to {charm.unit.number} after pod restart")

                # Trigger Juju event on leader unit to lower partition if needed
                self._relation.my_unit["_unused_pod_uid_after_pod_restart_and_partition_raised"] = (
                    next(unit for unit in self._units if unit == charm.unit).pod_uid
                )
        had_opportunity_to_raise_partition_after_pod_restart.touch()

        # Outdated units are not able to access the current config values
        # This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2084886
        # Workaround: each unit sets the config value it sees in its unit databag
        # To determine the current config value, we can look at the config value in the databag of
        # up-to-date units
        self._relation.my_unit["pause_after_unit_refresh_config"] = charm.config[
            "pause_after_unit_refresh"
        ]

        self._pod_uids_of_units_that_are_tearing_down_local_state = (
            _LOCAL_STATE / "kubernetes_pod_ids_of_units_that_are_tearing_down.json"
        )
        # Propagate local state to this unit's databag.
        # Used to persist data to databag in case an uncaught exception was raised (or the charm
        # code was terminated) in the Juju event where the data was originally set
        if self._pod_uids_of_units_that_are_tearing_down_local_state.exists():
            tearing_down_uids1: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                "pod_uids_of_units_that_are_tearing_down", tuple()
            )
            for uid in json.loads(
                self._pod_uids_of_units_that_are_tearing_down_local_state.read_text()
            ):
                if uid not in tearing_down_uids1:
                    tearing_down_uids1.append(uid)

        # Save state in databag if this unit sees another unit tearing down.
        # Used by the leader unit to set the StatefulSet partition so that the partition does not
        # exceed the highest unit number (which would cause `juju refresh` to not trigger any Juju
        # events). Also used to determine `self._pause_after`.
        # The unit that is tearing down cannot set its own unit databag during a relation departed
        # event, since other units will not see those changes.
        if (
            isinstance(charm.event, charm.RelationDepartedEvent)
            and charm.event.departing_unit.app == charm.app
        ):
            uids = [unit.pod_uid for unit in self._units if unit == charm.event.departing_unit]
            # `uids` will be empty if the departing unit's pod has already been deleted
            if uids:
                assert len(uids) == 1
                uid = uids[0]
                tearing_down_uids2: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                    "pod_uids_of_units_that_are_tearing_down", tuple()
                )
                if uid not in tearing_down_uids2:
                    tearing_down_uids2.append(uid)
                # Save state locally in case uncaught exception raised later in this Juju event.
                # Or, if this unit is leader and this unit lowers the partition to refresh itself,
                # Juju will terminate the charm code process for this event and any changes to
                # databags will not be saved.
                self._pod_uids_of_units_that_are_tearing_down_local_state.write_text(
                    json.dumps(list(tearing_down_uids2), indent=4)
                )

        tearing_down_uids3 = set()
        for unit in self._units:
            tearing_down_uids3.update(
                # During scale up, scale down, or initial install, `unit` may be missing from
                # relation
                self._relation.get(unit, {}).get("pod_uids_of_units_that_are_tearing_down", tuple())
            )
        self._units_not_tearing_down = [
            unit for unit in self._units if unit.pod_uid not in tearing_down_uids3
        ]
        """Sorted from highest to lowest unit number (refresh order)"""

        self._refresh_started_local_state = _LOCAL_STATE / "kubernetes_refresh_started"
        # Propagate local state to this unit's databag.
        # Used to persist data to databag in case an uncaught exception was raised (or the charm
        # code was terminated) in the Juju event where the data was originally set
        if self._refresh_started_local_state.exists():
            hashes1: typing.MutableSequence[str] = self._relation.my_unit.setdefault(
                "refresh_started_if_app_controller_revision_hash_in", tuple()
            )
            if self._unit_controller_revision not in hashes1:
                hashes1.append(self._unit_controller_revision)

        # Propagate "refresh_started_if_app_controller_revision_hash_in" in unit databags to app
        # databag. Preserves data if this app is scaled down (prevents workload container check,
        # compatibility checks, and pre-refresh checks from running again on scale down).
        # Whether this unit is leader
        if self._relation.my_app_rw is not None:
            hashes2: typing.MutableSequence[str] = self._relation.my_app_rw.setdefault(
                "refresh_started_if_app_controller_revision_hash_in", tuple()
            )
            for unit in self._units:
                # During scale up, scale down, or initial install, `unit` may be missing from
                # relation
                for hash_ in self._relation.get(unit, {}).get(
                    "refresh_started_if_app_controller_revision_hash_in", tuple()
                ):
                    if hash_ not in hashes2:
                        hashes2.append(hash_)

        # Get installed charm revision
        self._installed_charm_revision_raw = _RawCharmRevision.from_file()
        """Contents of this unit's .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-602")"""

        # Get versions from refresh_versions.toml
        refresh_versions = _RefreshVersions()
        self._installed_charm_version = refresh_versions.charm
        """This unit's charm version"""
        self._pinned_workload_version = refresh_versions.workload
        """Upstream workload version (e.g. "16.8") pinned by this unit's charm code

        Used for compatibility check & displayed to user
        """

        # Get installed & pinned workload container digest
        metadata_yaml = yaml.safe_load(pathlib.Path("metadata.yaml").read_text())
        upstream_source = (
            metadata_yaml.get("resources", {})
            .get(self._charm_specific.oci_resource_name, {})
            .get("upstream-source")
        )
        if not isinstance(upstream_source, str):
            raise ValueError(
                f"Unable to find `upstream-source` for {self._charm_specific.oci_resource_name=} "
                "resource in metadata.yaml `resources`"
            )
        try:
            _, digest = upstream_source.split("@")
            if not digest.startswith("sha256:"):
                raise ValueError
        except ValueError:
            raise ValueError(
                f"OCI image in `upstream-source` must be pinned to a digest (e.g. ends with "
                "'@sha256:e53eb99abd799526bb5a5e6c58180ee47e2790c95d433a1352836aa27d0914a4'): "
                f"{repr(upstream_source)}"
            )
        else:
            self._pinned_workload_container_version = digest
            """Workload image digest pinned by this unit's charm code

            (e.g. "sha256:e53eb99abd799526bb5a5e6c58180ee47e2790c95d433a1352836aa27d0914a4")
            """
        workload_containers: typing.List[str] = [
            key
            for key, value in metadata_yaml.get("containers", {}).items()
            if value.get("resource") == self._charm_specific.oci_resource_name
        ]
        if len(workload_containers) == 0:
            raise ValueError(
                "Unable to find workload container with "
                f"{self._charm_specific.oci_resource_name=} in metadata.yaml `containers`"
            )
        elif len(workload_containers) > 1:
            raise ValueError(
                f"Expected 1 container. Found {len(workload_containers)} workload containers with "
                f"{self._charm_specific.oci_resource_name=} in metadata.yaml `containers`: "
                f"{repr(workload_containers)}"
            )
        else:
            workload_container = workload_containers[0]

        class _InstalledWorkloadContainerDigestNotAvailable(Exception):
            """This unit's workload container digest is not available from the Kubernetes API

            If a refresh is not in progress, this is likely a temporary issue that will be resolved
            in a few seconds (probably in the next 1-2 Juju events).

            If a refresh is in progress, it's possible that the user refreshed to a workload
            container digest that doesn't exist. In that case, this issue will not be resolved
            unless the user runs `juju refresh` again.
            """

        try:
            workload_container_statuses = [
                status
                for status in this_pod.status.containerStatuses
                if status.name == workload_container
            ]
            if len(workload_container_statuses) == 0:
                raise _InstalledWorkloadContainerDigestNotAvailable
            if len(workload_container_statuses) > 1:
                raise ValueError(
                    f"Found multiple {workload_container} containers for this unit's pod. "
                    "Expected 1 container"
                )
            # Example: "registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:e53eb99abd799526bb5a5e6c58180ee47e2790c95d433a1352836aa27d0914a4"
            image_id = workload_container_statuses[0].imageID
            if not image_id:
                raise _InstalledWorkloadContainerDigestNotAvailable
            image_name, image_digest = image_id.split("@")
        except _InstalledWorkloadContainerDigestNotAvailable:
            # Fall back to image pinned in metadata.yaml
            image_name, _ = upstream_source.split("@")

            image_digest = None
        self._installed_workload_image_name: str = image_name
        """This unit's workload image name

        Includes registry and path

        (e.g. "registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image")
        """
        self._installed_workload_container_version: typing.Optional[str] = image_digest
        """This unit's workload image digest

        (e.g. "sha256:e53eb99abd799526bb5a5e6c58180ee47e2790c95d433a1352836aa27d0914a4")
        """

        if self._unit_controller_revision == self._app_controller_revision:
            if (
                self._installed_workload_container_version
                == self._pinned_workload_container_version
            ):
                charm.set_app_workload_version(self._pinned_workload_version)
            elif self._installed_workload_container_version is not None:
                charm.set_app_workload_version("")

        # Determine `self._pause_after`
        # Outdated units are not able to access the current config values
        # This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2084886
        # Workaround: each unit sets the config value it sees in its unit databag
        # To determine the current config value, look at the databag of up-to-date units & use the
        # most conservative value. (If a unit is raising an uncaught exception, its databag may be
        # outdated. Picking the most conservative value is the safest tradeoff—if the user wants to
        # configure pause after to a less conservative value, they need to fix the unit that is
        # raising an uncaught exception before that value will be propagated. Otherwise, they can
        # use the resume-refresh action with check-health-of-refreshed-units=false.)
        # It's possible that no units are up-to-date—if the first unit to refresh is stopping
        # before it's refreshed. In that case, units with the same controller revision as the first
        # unit to refresh are the closest to up-to-date.
        # Also, if the app is being scaled down, it's possible that the databags for all units with
        # the same controller revision as the first unit to refresh are not accessible. Therefore,
        # include units with the same controller revision as the first unit to refresh that's not
        # tearing down—to ensure that `len(pause_after_values) >= 1`.
        most_up_to_date_units = (
            unit
            for unit in self._units
            if unit.controller_revision == self._units[0].controller_revision
            or unit.controller_revision == self._units_not_tearing_down[0].controller_revision
        )
        pause_after_values = (
            # During scale up or initial install, `unit` or "pause_after_unit_refresh_config" key
            # may be missing from relation. During scale down, `unit` may be missing from relation.
            self._relation.get(unit, {}).get("pause_after_unit_refresh_config")
            for unit in most_up_to_date_units
        )
        # Exclude `None` values (for scale up/down or initial install) to avoid displaying app
        # status that says pause_after_unit_refresh is set to invalid value
        pause_after_values = (value for value in pause_after_values if value is not None)
        self._pause_after = max(_PauseAfter(value) for value in pause_after_values)

        if not self._in_progress:
            # Clean up state that is no longer in use
            self._relation.my_unit.pop("refresh_started_if_app_controller_revision_hash_in", None)
            self._refresh_started_local_state.unlink(missing_ok=True)
            self._relation.my_unit.pop("pod_uids_of_units_that_are_tearing_down", None)
            self._pod_uids_of_units_that_are_tearing_down_local_state.unlink(missing_ok=True)

            # Whether this unit is leader
            if self._relation.my_app_rw is not None:
                # Clean up state that is no longer in use
                self._relation.my_app_rw.pop(
                    "refresh_started_if_app_controller_revision_hash_in", None
                )

                if self._installed_workload_container_version:
                    # Save versions in app databag for next refresh
                    matches_pin = (
                        self._installed_workload_container_version
                        == self._pinned_workload_container_version
                    )
                    _OriginalVersions(
                        workload=self._pinned_workload_version if matches_pin else None,
                        workload_container=self._installed_workload_container_version,
                        installed_workload_container_matched_pinned_container=matches_pin,
                        charm=self._installed_charm_version,
                        charm_revision_raw=self._installed_charm_revision_raw,
                    ).write_to_app_databag(self._relation.my_app_rw)
                else:
                    logger.info(
                        "This unit's workload container digest is not available from the "
                        "Kubernetes API. Unable to save versions to app databag (for next "
                        "refresh). Will retry next Juju event"
                    )

        if self._in_progress or (charm.is_leader and self._installed_workload_container_version):
            original_versions = _OriginalVersions.from_app_databag(self._relation.my_app_ro)
            self._rollback_command = (
                f"juju refresh {charm.app} --revision "
                f"{original_versions.charm_revision_raw.charmhub_revision} --resource "
                f"{self._charm_specific.oci_resource_name}={self._installed_workload_image_name}@"
                f"{original_versions.workload_container}"
            )

        if self._in_progress:
            logger.info(f"Refresh in progress. To rollback, run `{self._rollback_command}`")

        # pre-refresh-check action
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "pre-refresh-check":
            if self._in_progress:
                charm.event.fail("Refresh already in progress")
            elif charm.is_leader:
                try:
                    # Check if we can get this unit's workload container digest from the Kubernetes
                    # API. If we can't, we should fail the pre-refresh-check action since, later,
                    # we won't be able to detect (or provide instructions for) rollback if we don't
                    # know what workload container digest we refreshed from.
                    if self._installed_workload_container_version:
                        assert self._rollback_command
                    else:
                        raise PrecheckFailed(
                            f"{self._charm_specific.workload_name} container is not running"
                        )

                    self._charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
                except PrecheckFailed as exception:
                    charm.event.fail(
                        "Charm is not ready for refresh. Pre-refresh check failed: "
                        f"{exception.message}"
                    )
                else:
                    charm.event.result = {
                        "result": (
                            "Charm is ready for refresh. For refresh instructions, see "
                            f"https://charmhub.io/{self._charm_specific.charm_name}/docs/refresh/{self._installed_charm_version}\n"
                            "After the refresh has started, use this command to rollback (copy "
                            "this down in case you need it later):\n"
                            f"`{self._rollback_command}`"
                        )
                    }
                    logger.info("Pre-refresh check succeeded")
            else:
                charm.event.fail(
                    f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader "
                    "pre-refresh-check`)"
                )

        self._start_refresh()

        self._set_partition_and_app_status(handle_action=True)


@dataclasses.dataclass(frozen=True)
class _HistoryEntry:
    """Charm code refresh that, at the time of refresh, was to the up-to-date charm code version

    The first charm code version on initial installation or on a new unit that is added during
    scale up counts as a "refresh"

    If a unit is raising an uncaught exception, it may get refreshed to a charm code version that
    is not up-to-date. That refresh should not be stored as a `_HistoryEntry`
    """

    charm_revision: _RawCharmRevision
    """Charm revision in .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-602")"""
    time_of_refresh: float
    """Modified time of .juju-charm file (e.g. 1727768259.4063382)"""


@dataclasses.dataclass
class _CharmCodeRefreshHistory:
    """History of this unit's charm code refreshes"""

    last_refresh_to_up_to_date_charm_code_version: _HistoryEntry
    """Last refresh that, at the time of refresh, was to the up-to-date charm code version"""

    second_to_last_refresh_to_up_to_date_charm_code_version: typing.Optional[_HistoryEntry]
    """Second to last refresh that, at the time of refresh, was to the up-to-date charm code version"""

    _PATH = _LOCAL_STATE / "machines_last_two_refreshes_to_up_to_date_charm_code_version.json"

    @classmethod
    def from_file(cls, *, installed_charm_version: CharmVersion):
        try:
            data: typing.Dict[str, typing.Optional[dict]] = json.loads(cls._PATH.read_text())
        except FileNotFoundError:
            # This is initial installation or this is a new unit that was added during scale up

            charm_revision = _RawCharmRevision.from_file()
            history = cls(
                last_refresh_to_up_to_date_charm_code_version=_HistoryEntry(
                    charm_revision=charm_revision,
                    time_of_refresh=_dot_juju_charm_modified_time(),
                ),
                second_to_last_refresh_to_up_to_date_charm_code_version=None,
            )
            history.save_to_file()

            if charm_revision.charmhub_revision:
                charm_version = (
                    f"revision {charm_revision.charmhub_revision} ({repr(installed_charm_version)})"
                )
            else:
                charm_version = f"{(repr(installed_charm_version))}"
            logger.info(f"Charm {charm_version} installed at {_dot_juju_charm_modified_time()}")

            return history
        data2 = {}
        for key, value in data.items():
            if value is not None:
                value = _HistoryEntry(**value)
            data2[key] = value
        return cls(**data2)

    def save_to_file(self):
        self._PATH.write_text(json.dumps(dataclasses.asdict(self), indent=4))


class _MachinesInProgress(enum.Enum):
    """Whether a refresh is currently in progress

    If any unit's snap revision does not match the snap revision pinned in this unit's charm code,
    a refresh is in progress.

    Otherwise, a refresh is not in progress.

    Sometimes, it is not possible to determine if a refresh is in progress.
    See `Machines._determine_in_progress()`
    """

    FALSE = 0
    TRUE = 1
    UNKNOWN = 2

    def __bool__(self):
        raise TypeError


class _MachinesDatabagUpToDate(enum.Enum):
    """Whether a unit's databag is up-to-date"""

    FALSE = 0
    TRUE = 1
    UNKNOWN = 2

    def __bool__(self):
        raise TypeError


class Machines(Common):
    """In-place rolling refreshes of stateful charmed applications on machines

    https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/instantiate/

    Raises:
        UnitTearingDown: https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/unit-tearing-down/
        PeerRelationNotReady: https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/peer-relation-not-ready/
    """

    @Common.in_progress.getter
    def in_progress(self) -> bool:
        # If the refresh completed during this Juju event, tell the charm code that the refresh is
        # still in progress.
        # This avoids a situation where, because the charm code sees that the refresh is no longer
        # in progress, the charm code runs an operation that raises an uncaught exception. If that
        # happens, changes to this unit's databag will not be persisted and—from the perspective of
        # other units—a refresh will still be in progress.
        # Wait until the changes to this unit's databag have been saved (i.e. wait until the next
        # Juju event) before telling the charm code that a refresh is no longer in progress.
        # When other units see that the refresh has completed, they will trigger a Juju event on
        # this unit.
        # For single unit deployments, there are no other units to ensure another Juju event is
        # triggered on this unit (and the aforementioned situation is not a concern).
        if self._refresh_completed_this_event and len(self._relation.all_units) > 1:
            return True

        if (
            self._in_progress is _MachinesInProgress.TRUE
            or self._in_progress is _MachinesInProgress.UNKNOWN
        ):
            return True
        elif self._in_progress is _MachinesInProgress.FALSE:
            return False
        else:
            raise TypeError

    @Common.next_unit_allowed_to_refresh.getter
    def next_unit_allowed_to_refresh(self) -> bool:
        return (
            self._relation.my_unit.get(
                "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date"
            )
            is True
        )

    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        if value is not True:
            raise ValueError("`next_unit_allowed_to_refresh` can only be set to `True`")
        if (
            self._relation.my_unit.get("installed_snap_revision")
            != self._get_installed_snap_revision()
        ):
            raise Exception(
                "Must call `update_snap_revision()` before setting "
                "`next_unit_allowed_to_refresh = True`"
            )
        if (
            self._relation.my_unit.get(
                "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date"
            )
            is not True
        ):
            logger.info(
                f"Allowed next unit to refresh if this unit's snap revision "
                f"({self._relation.my_unit['installed_snap_revision']}) & databag are up-to-date "
                "and if permitted by pause_after_unit_refresh config option or resume-refresh "
                "action"
            )
            self._relation.my_unit[
                "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date"
            ] = True

    @Common.workload_allowed_to_start.getter
    def workload_allowed_to_start(self) -> bool:
        return True

    def update_snap_revision(self) -> None:
        """Must be called immediately after the workload snap is refreshed

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/refresh-snap/
        """
        self._update_snap_revision(raise_if_not_installed=True)

    @property
    def pinned_snap_revision(self) -> str:
        """Workload snap revision pinned by this unit's current charm code

        Must only be used during the initial snap installation and must not be used to refresh the snap

        https://canonical-charm-refresh.readthedocs-hosted.com/latest/add-to-charm/remove-duplicate-hardcoded-snap/
        """
        return self._pinned_workload_container_version

    @Common.app_status_higher_priority.getter
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        return _convert_to_ops_status(self._app_status_higher_priority)

    @Common.unit_status_higher_priority.getter
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        return _convert_to_ops_status(self._unit_status_higher_priority)

    def unit_status_lower_priority(
        self, *, workload_is_running: bool = True
    ) -> typing.Optional[ops.StatusBase]:
        if self._in_progress is _MachinesInProgress.FALSE:
            return None
        if (
            self._history.last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            != _dot_juju_charm_modified_time()
        ):
            # This unit's charm code was refreshed without an upgrade-charm event
            # (https://bugs.launchpad.net/juju/+bug/2068500)
            return ops.MaintenanceStatus(
                "Waiting for Juju upgrade-charm or config-changed event. See `juju debug-log`"
            )
        message = f"{self._charm_specific.workload_name}"
        if self._installed_workload_version.exists():
            message += f" {self._installed_workload_version.read_text()}"
        if workload_is_running:
            message += " running"
        message += f"; Snap revision {self._get_installed_snap_revision()}"
        if self._get_installed_snap_revision() != self._pinned_workload_container_version:
            message += " (outdated)"
        if self._installed_charm_revision_raw.charmhub_revision:
            # Charm was deployed from Charmhub; use revision
            message += f"; Charm revision {self._installed_charm_revision_raw.charmhub_revision}"
        else:
            # Charmhub revision is not available; fall back to charm version
            message += f"; Charm version {self._installed_charm_version}"
        if workload_is_running:
            return ops.ActiveStatus(message)
        return ops.WaitingStatus(message)

    def _get_installed_snap_revision(self) -> typing.Optional[str]:
        # TODO docs: snap name cannot change on refresh
        # https://snapcraft.io/docs/using-the-api
        client = httpx.Client(transport=httpx.HTTPTransport(uds="/run/snapd.socket"))
        # https://snapcraft.io/docs/snapd-rest-api#heading--snaps
        response = client.get(
            "http://localhost/v2/snaps", params={"snaps": self._workload_snap_name}
        ).raise_for_status()
        data = response.json()
        assert data["type"] == "sync"
        snaps = data["result"]
        if not snaps:
            # Snap not installed
            return None
        assert len(snaps) == 1
        revision = snaps[0]["revision"]
        assert isinstance(revision, str)
        return revision

    def _update_snap_revision(self, *, raise_if_not_installed: bool):
        """Update snap revision in this unit's databag

        If the installed snap revision does not match the snap revision in this unit's databag:

        - reset
          "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date" in
          this unit's databag to `False`
        - update "installed_snap_revision" in this unit's databag
        - update `self._installed_workload_version`
        """
        snap_revision = self._get_installed_snap_revision()
        if snap_revision is None and raise_if_not_installed:
            raise ValueError(
                f"`update_snap_revision()` called but {repr(self._workload_snap_name)} snap is "
                "not installed"
            )
        if snap_revision != self._relation.my_unit.get("installed_snap_revision"):
            logger.info(f"Snap refreshed to (or installed at) revision {snap_revision}")
            self._relation.my_unit[
                "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date"
            ] = False
            if snap_revision:
                self._relation.my_unit["installed_snap_revision"] = snap_revision
                if snap_revision == self._pinned_workload_container_version:
                    self._installed_workload_version.write_text(self._pinned_workload_version)
                else:
                    # This can happen if the snap was refreshed on a previous version of the charm
                    # code and the charm code raised an uncaught exception on every Juju event
                    # until the charm code was refreshed (to this charm code version)
                    # This can also happen if a user manually refreshes the snap, which is not
                    # supported
                    logger.warning(
                        f"Unrecognized snap revision {snap_revision} installed. Expected snap "
                        f"revision {self._pinned_workload_container_version}. If this unit was "
                        "previously in error state and that error state was resolved with a `juju "
                        "refresh`, this situation is expected and this warning can usually be "
                        f"ignored. However, if the {repr(self._workload_snap_name)} snap was "
                        "manually refreshed or installed (i.e. not refreshed or installed by the "
                        "charm)—that is not supported and the charm might not automatically "
                        "recover."
                    )
                    self._installed_workload_version.unlink(missing_ok=True)
            else:
                del self._relation.my_unit["installed_snap_revision"]
                self._installed_workload_version.unlink(missing_ok=True)

    def _is_units_databag_up_to_date_unknown(self, unit: charm.Unit, /) -> _MachinesDatabagUpToDate:
        """Check if a unit's databag is up-to-date

        If `self._history.second_to_last_refresh_to_up_to_date_charm_code_version is None`, this
        method may return `_MachinesDatabagUpToDate.UNKNOWN`.
        Otherwise, this method will only return `_MachinesDatabagUpToDate.TRUE` or
        `_MachinesDatabagUpToDate.FALSE`

        This method assumes that "last_refresh_to_up_to_date_charm_code_version" is set in the
        unit's databag
        """
        if unit == charm.unit:
            return _MachinesDatabagUpToDate.TRUE
        other_unit_last_refresh = _HistoryEntry(
            **self._relation[unit]["last_refresh_to_up_to_date_charm_code_version"]
        )
        if other_unit_last_refresh.charm_revision != self._installed_charm_revision_raw:
            return _MachinesDatabagUpToDate.FALSE
        if self._history.second_to_last_refresh_to_up_to_date_charm_code_version is None:
            # It is not possible to determine if the databag is up-to-date
            # (This is initial installation or this unit is a new unit that was added during scale
            # up)
            return _MachinesDatabagUpToDate.UNKNOWN
        if (
            self._history.second_to_last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            < other_unit_last_refresh.time_of_refresh
        ):
            return _MachinesDatabagUpToDate.TRUE
        else:
            return _MachinesDatabagUpToDate.FALSE

    def _is_units_databag_up_to_date(self, unit: charm.Unit, /) -> bool:
        """Check if a unit's databag is up-to-date

        If `self._history.second_to_last_refresh_to_up_to_date_charm_code_version is None` and the
        charm code version in the unit's databag equals this unit's charm code version, it is not
        possible to determine if the unit's databag is up-to-date—but this method will assume it is
        and return `True`

        This method assumes that "last_refresh_to_up_to_date_charm_code_version" is set in the
        unit's databag
        """
        result = self._is_units_databag_up_to_date_unknown(unit)
        if result is _MachinesDatabagUpToDate.TRUE:
            return True
        elif result is _MachinesDatabagUpToDate.UNKNOWN:
            # Assume the databag is up-to-date
            return True
        elif result is _MachinesDatabagUpToDate.FALSE:
            return False
        else:
            raise TypeError

    def _determine_in_progress(self) -> _MachinesInProgress:
        """Determine whether a refresh is currently in progress

        If any unit's snap revision does not match the snap revision pinned in this unit's charm
        code, a refresh is in progress.

        Otherwise, a refresh is not in progress.

        Sometimes, it is not possible to determine if a refresh is in progress. If a unit has an
        outdated databag, it is not possible to confirm which snap revision is installed on that
        unit.

        For a charm-code-only refresh (i.e. the snap revision pinned in the charm code is identical
        for both charm code versions), a refresh is never in progress. However, for a few Juju
        events, it is not possible to determine if a refresh is in progress.

        If the user performs a charm-code-only refresh while a refresh is in progress (i.e. while
        any unit has a snap revision that does not match the pinned snap revision), the refresh
        will remain in progress.

        NOTE: If `self._refresh_completed_this_event is True`, `self.in_progress` may be `True`
        while this method returns `_MachinesInProgress.FALSE`
        """
        unknown = False
        for unit, databag in self._relation.all_units.items():
            installed_snap_revision = databag.get("installed_snap_revision")
            if installed_snap_revision is None:
                # This is initial installation or `unit` is a new unit that was added during scale
                # up
                # (For this unit's databag, `installed_snap_revision` could also be `None` if this
                # unit is tearing down. However, in that case, this code will not run since
                # `UnitTearingDown` will be raised earlier.)
                continue
            if installed_snap_revision != self._pinned_workload_container_version:
                return _MachinesInProgress.TRUE
            # Check if databag is up-to-date
            # If "installed_snap_revision" is set, "last_refresh_to_up_to_date_charm_code_version"
            # should also be set
            assert databag.get("last_refresh_to_up_to_date_charm_code_version") is not None
            if self._is_units_databag_up_to_date(unit):
                continue
            else:
                unknown = True
                continue

        if unknown:
            return _MachinesInProgress.UNKNOWN
        else:
            return _MachinesInProgress.FALSE

    def _start_refresh(self):
        """Run automatic checks after `juju refresh` on highest unit

        Automatic checks include:

        - workload container check
        - compatibility checks
        - pre-refresh checks

        Handles force-refresh-start action

        If this unit is the highest number unit, this unit's charm code is up-to-date, and the
        refresh to `self._pinned_workload_container_version` has not already started, this method
        will check for one of the following conditions:

        - this unit is rolling back
        - run all the automatic checks & check that all were successful
        - run the automatic checks (if any) that were not skipped by the force-refresh-start action
          and check that they were successful

        If one of those conditions is met, this method will set `self._refresh_started = True`, set
        "refresh_started_if_this_units_databag_is_up_to_date" to `True` in this unit's databag, and
        will touch `self._refresh_started_local_state`

        Sets `self._force_start`

        Sets `self._unit_status_higher_priority` & unit status. Unit status only set if
        `self._unit_status_higher_priority` (unit status is not cleared if
        `self._unit_status_higher_priority` is `None`—that is the responsibility of the charm)
        """

        class _InvalidForceEvent(ValueError):
            """Event is not valid force-refresh-start action event"""

        class _ForceRefreshStartAction(charm.ActionEvent):
            def __init__(
                self,
                event: charm.Event,
                /,
                *,
                first_unit_to_refresh: charm.Unit,
                in_progress: _MachinesInProgress,
            ):
                if not isinstance(event, charm.ActionEvent):
                    raise _InvalidForceEvent
                super().__init__()
                if event.action != "force-refresh-start":
                    raise _InvalidForceEvent
                if charm.unit != first_unit_to_refresh:
                    event.fail(f"Must run action on unit {first_unit_to_refresh.number}")
                    raise _InvalidForceEvent
                if in_progress is not _MachinesInProgress.TRUE:
                    if in_progress is _MachinesInProgress.FALSE:
                        message = "No refresh in progress"
                    elif in_progress is _MachinesInProgress.UNKNOWN:
                        message = (
                            "Determining if a refresh is in progress. Check `juju status` and "
                            "consider retrying this action"
                        )
                    else:
                        raise TypeError
                    event.fail(message)
                    raise _InvalidForceEvent
                self.check_workload_container: bool = event.parameters["check-workload-container"]
                self.check_compatibility: bool = event.parameters["check-compatibility"]
                self.run_pre_refresh_checks: bool = event.parameters["run-pre-refresh-checks"]
                for parameter in (
                    self.check_workload_container,
                    self.check_compatibility,
                    self.run_pre_refresh_checks,
                ):
                    if parameter is False:
                        break
                else:
                    event.fail(
                        "Must run with at least one of `check-compatibility`, "
                        "`run-pre-refresh-checks`, or `check-workload-container` parameters "
                        "`=false`"
                    )
                    raise _InvalidForceEvent

        self._force_start: typing.Optional[_ForceRefreshStartAction] = None
        """Used to log snap refresh to action output if snap refresh caused by force-refresh-start action"""
        force_start: typing.Optional[_ForceRefreshStartAction]
        try:
            force_start = _ForceRefreshStartAction(
                charm.event, first_unit_to_refresh=self._units[0], in_progress=self._in_progress
            )
        except _InvalidForceEvent:
            force_start = None
        self._unit_status_higher_priority: typing.Optional[charm.Status] = None
        if self._in_progress is not _MachinesInProgress.TRUE:
            return
        if charm.unit != self._units[0]:
            return
        if (
            self._history.last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            != _dot_juju_charm_modified_time()
        ):
            # This unit's charm code version is not up-to-date
            if force_start:
                force_start.fail(
                    "This unit is waiting for a Juju upgrade-charm or config-changed event. See "
                    "`juju debug-log`"
                )
            return

        original_versions = _OriginalVersions.from_app_databag(self._relation.my_app_ro)
        if not self._refresh_started:
            # Check if this unit is rolling back
            if original_versions.charm == self._installed_charm_version:
                # On machines, the user is not able to specify a snap revision that is different
                # from the snap revision pinned in the charm code.
                # In the future, if snap resources are added to Juju (similar to Kubernetes OCI
                # resources), this will change.
                assert (
                    original_versions.workload_container == self._pinned_workload_container_version
                )

                # Rollback to original charm code & workload container version; skip checks

                workload_version = (
                    f"{self._charm_specific.workload_name} {self._pinned_workload_version} (snap "
                    f"revision {self._pinned_workload_container_version})"
                )
                if self._installed_charm_revision_raw.charmhub_revision:
                    charm_version = (
                        f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                        f"({repr(self._installed_charm_version)})"
                    )
                else:
                    charm_version = f"{repr(self._installed_charm_version)}"
                logger.info(
                    "Rollback detected. Automatic refresh checks skipped. Refresh started. "
                    f"Rolling back to {workload_version} and charm {charm_version}"
                )

                self._refresh_started = True
                self._relation.my_unit["refresh_started_if_this_units_databag_is_up_to_date"] = True
                self._refresh_started_local_state.touch()
        if self._refresh_started:
            if force_start:
                force_start.fail(f"Unit {charm.unit.number} already refreshed")
            return

        # Run automatic checks

        # Log workload & charm versions we're refreshing from & to
        from_to_message = (
            f"from {self._charm_specific.workload_name} {original_versions.workload} (snap "
            f"revision {original_versions.workload_container}) and charm "
        )
        if original_versions.charm_revision_raw.charmhub_revision:
            from_to_message += (
                f"revision {original_versions.charm_revision_raw.charmhub_revision} "
                f"({repr(original_versions.charm)}) "
            )
        else:
            from_to_message += f"{repr(original_versions.charm)} "
        from_to_message += (
            f"to {self._charm_specific.workload_name} {self._pinned_workload_version} (snap "
            f"revision {self._pinned_workload_container_version}) and charm "
        )
        if self._installed_charm_revision_raw.charmhub_revision:
            from_to_message += (
                f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                f"({repr(self._installed_charm_version)})"
            )
        else:
            from_to_message += f"{repr(self._installed_charm_version)}"
        if force_start:
            false_values = []
            if not force_start.check_workload_container:
                false_values.append("check-workload-container")
            if not force_start.check_compatibility:
                false_values.append("check-compatibility")
            if not force_start.run_pre_refresh_checks:
                false_values.append("run-pre-refresh-checks")
            from_to_message += (
                ". force-refresh-start action ran with "
                f"{' '.join(f'{key}=false' for key in false_values)}"
            )
        logger.info(f"Attempting to start refresh {from_to_message}")

        if force_start and not force_start.check_workload_container:
            force_start.log(
                f"Skipping check that refresh is to {self._charm_specific.workload_name} "
                "container version that has been validated to work with the charm revision"
            )
        else:
            # Check workload container
            # On machines, the user is not able to specify a snap revision that is different from
            # the snap revision pinned in the charm code.
            # In the future, if snap resources are added to Juju (similar to Kubernetes OCI
            # resources), this will change.
            if True:
                if force_start:
                    force_start.log(
                        f"Checked that refresh is to {self._charm_specific.workload_name} "
                        "container version that has been validated to work with the charm revision"
                    )
            else:
                raise NotImplementedError
        if force_start and not force_start.check_compatibility:
            force_start.log(
                "Skipping check for compatibility with previous "
                f"{self._charm_specific.workload_name} version and charm revision"
            )
        else:
            # Check compatibility
            if self._charm_specific.is_compatible(
                old_charm_version=original_versions.charm,
                new_charm_version=self._installed_charm_version,
                # `original_versions.workload` is not `None` since
                # `original_versions.installed_workload_container_matched_pinned_container` is
                # always `True` on machines
                old_workload_version=original_versions.workload,
                new_workload_version=self._pinned_workload_version,
            ):
                if force_start:
                    force_start.log(
                        f"Checked that refresh from previous {self._charm_specific.workload_name} "
                        "version and charm revision to current versions is compatible"
                    )
            else:
                # Log reason why compatibility check failed
                logger.info(
                    "Refresh incompatible because new version of "
                    f"{self._charm_specific.workload_name} "
                    f"({repr(self._pinned_workload_version)}) and/or charm "
                    f"({repr(self._installed_charm_version)}) is not compatible with previous "
                    f"version of {self._charm_specific.workload_name} "
                    f"({repr(original_versions.workload)}) and/or charm "
                    f"({repr(original_versions.charm)})"
                )
                # The leader unit will set app status to show that refresh is incompatible

                if force_start:
                    force_start.fail("Refresh incompatible. Rollback with `juju refresh`")
                return
        if force_start and not force_start.run_pre_refresh_checks:
            force_start.log("Skipping pre-refresh checks")
        else:
            # Run pre-refresh checks
            if force_start:
                force_start.log("Running pre-refresh checks")
            try:
                self._charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
            except PrecheckFailed as exception:
                self._unit_status_higher_priority = charm.BlockedStatus(
                    f"Rollback with `juju refresh`. Pre-refresh check failed: {exception.message}"
                )
                charm.unit_status = self._unit_status_higher_priority
                logger.error(
                    f"Pre-refresh check failed: {exception.message}. Rollback with `juju refresh`. "
                    "Continuing this refresh may cause data loss and/or downtime. The refresh can "
                    "be forced to continue with the `force-refresh-start` action and the "
                    f"`run-pre-refresh-checks` parameter. Run `juju show-action {charm.app} "
                    "force-refresh-start` for more information"
                )
                if force_start:
                    force_start.fail(
                        f"Pre-refresh check failed: {exception.message}. Rollback with "
                        "`juju refresh`"
                    )
                return
            if force_start:
                force_start.log("Pre-refresh checks successful")
        # All checks that ran succeeded
        logger.info(
            f"Automatic checks succeeded{' or skipped' if force_start else ''}. Refresh started. "
            f"Refreshing {self._charm_specific.workload_name} on this unit. Refresh is "
            f"{from_to_message}"
        )
        self._refresh_started = True
        self._relation.my_unit["refresh_started_if_this_units_databag_is_up_to_date"] = True
        self._refresh_started_local_state.touch()
        self._force_start = force_start

    def _set_app_status(self):
        """Set `self._app_status_higher_priority` & app status

        App status only set if `self._app_status_higher_priority` (app status is not cleared if
        `self._app_status_higher_priority` is `None`—that is the responsibility of the charm)
        """
        self._app_status_higher_priority: typing.Optional[charm.Status] = None
        if not charm.is_leader:
            return

        if self._pause_after is _PauseAfter.UNKNOWN:
            self._app_status_higher_priority = charm.BlockedStatus(
                'pause_after_unit_refresh config must be set to "all", "first", or "none"'
            )
            charm.app_status = self._app_status_higher_priority
            return

        if self._in_progress is _MachinesInProgress.FALSE:
            return
        if self._in_progress is _MachinesInProgress.UNKNOWN:
            self._app_status_higher_priority = charm.MaintenanceStatus(
                "Determining if a refresh is in progress"
            )
            charm.app_status = self._app_status_higher_priority
            return
        assert self._in_progress is _MachinesInProgress.TRUE
        original_versions = _OriginalVersions.from_app_databag(self._relation.my_app_ro)
        if not self._refresh_started and not self._charm_specific.is_compatible(
            old_charm_version=original_versions.charm,
            new_charm_version=self._installed_charm_version,
            # `original_versions.workload` is not `None` since
            # `original_versions.installed_workload_container_matched_pinned_container` is always
            # `True` on machines
            old_workload_version=original_versions.workload,
            new_workload_version=self._pinned_workload_version,
        ):
            # Log reason why compatibility check failed
            logger.info(
                "Refresh incompatible because new version of "
                f"{self._charm_specific.workload_name} ({repr(self._pinned_workload_version)}) "
                f"and/or charm ({repr(self._installed_charm_version)}) is not compatible with "
                f"previous version of {self._charm_specific.workload_name} "
                f"({repr(original_versions.workload)}) and/or charm "
                f"({repr(original_versions.charm)})"
            )

            self._app_status_higher_priority = charm.BlockedStatus(
                "Refresh incompatible. Rollback with `juju refresh --revision "
                f"{original_versions.charm_revision_raw.charmhub_revision}`"
            )
            charm.app_status = self._app_status_higher_priority
            logger.info(
                "Refresh incompatible. Rollback with `juju refresh`. Continuing this refresh may "
                "cause data loss and/or downtime. The refresh can be forced to continue with the "
                "`force-refresh-start` action and the `check-compatibility` parameter. Run `juju "
                f"show-action {charm.app} force-refresh-start` for more information"
            )
            return

        if len(self._units) == 1:
            self._app_status_higher_priority = charm.MaintenanceStatus(
                "Refreshing. To rollback, `juju refresh --revision "
                f"{original_versions.charm_revision_raw.charmhub_revision}`"
            )
            charm.app_status = self._app_status_higher_priority
            return

        for index, unit in enumerate(self._units):
            databag = self._relation[unit]
            if databag.get("installed_snap_revision") != self._pinned_workload_container_version:
                break
            # If "installed_snap_revision" is set, "last_refresh_to_up_to_date_charm_code_version"
            # should also be set
            assert databag.get("last_refresh_to_up_to_date_charm_code_version") is not None
            if not self._is_units_databag_up_to_date(unit):
                break
        else:
            # This code should never run because `self._in_progress is _MachinesInProgress.TRUE`
            assert False
        next_unit_to_refresh = unit
        next_unit_to_refresh_index = index

        assert self._pause_after is not _PauseAfter.UNKNOWN
        if self._pause_after is _PauseAfter.ALL or (
            self._pause_after is _PauseAfter.FIRST and next_unit_to_refresh_index <= 1
        ):
            self._app_status_higher_priority = charm.BlockedStatus(
                "Refreshing. Check units "
                f">={self._units[max(next_unit_to_refresh_index - 1, 0)].number} are healthy & "
                f"run `resume-refresh` on unit {next_unit_to_refresh.number}. To rollback, `juju "
                f"refresh --revision {original_versions.charm_revision_raw.charmhub_revision}`"
            )
            charm.app_status = self._app_status_higher_priority
            return
        self._app_status_higher_priority = charm.MaintenanceStatus(
            f"Refreshing. To pause refresh, run `juju config {charm.app} "
            "pause_after_unit_refresh=all`"
        )
        charm.app_status = self._app_status_higher_priority

    def _refresh_unit(self):
        """Refresh this unit's snap, if allowed

        Handles resume-refresh action
        """

        class _ResumeRefreshAction(charm.ActionEvent):
            def __init__(self, event: charm.ActionEvent, /):
                super().__init__()
                assert event.action == "resume-refresh"
                self.check_health_of_refreshed_units: bool = event.parameters[
                    "check-health-of-refreshed-units"
                ]

        action: typing.Optional[_ResumeRefreshAction] = None
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "resume-refresh":
            action = _ResumeRefreshAction(charm.event)
        if self._in_progress is not _MachinesInProgress.TRUE:
            if action:
                if self._in_progress is _MachinesInProgress.FALSE:
                    message = "No refresh in progress"
                elif self._in_progress is _MachinesInProgress.UNKNOWN:
                    message = (
                        "Determining if a refresh is in progress. Check `juju status` and "
                        "consider retrying this action"
                    )
                else:
                    raise TypeError
                action.fail(message)
            return

        installed_snap_revision = self._get_installed_snap_revision()
        if self._installed_workload_version.exists():
            from_version = (
                f"{self._installed_workload_version.read_text()} (snap revision "
                f"{installed_snap_revision})"
            )
        else:
            from_version = f"snap revision {installed_snap_revision}"
        from_to_message = (
            f"{self._charm_specific.workload_name} on this unit from {from_version} to "
            f"{self._pinned_workload_version} (snap revision "
            f"{self._pinned_workload_container_version})"
        )

        if action and not action.check_health_of_refreshed_units:
            if installed_snap_revision == self._pinned_workload_container_version:
                action.fail("Unit already refreshed")
                return
            action.log("Ignoring health of refreshed units")
            action.log(f"Refreshing unit {charm.unit.number}")
            assert self._force_start is None
            logger.info(
                f"Refreshing {from_to_message} because resume-refresh action ran with "
                "check-health-of-refreshed-units=false"
            )
            self._charm_specific.refresh_snap(
                snap_name=self._workload_snap_name,
                snap_revision=self._pinned_workload_container_version,
                refresh=self,
            )
            if self._get_installed_snap_revision() == self._pinned_workload_container_version:
                logger.info(
                    f"Refreshed {from_to_message} because resume-refresh action ran with "
                    "check-health-of-refreshed-units=false"
                )
                action.result = {"result": f"Refreshed unit {charm.unit.number}"}
            else:
                # This code might not run since the charm code may intentionally raise an uncaught
                # exception in `self._charm_specific.refresh_snap()` if the snap is not refreshed
                logger.error(f"Failed to refresh {from_to_message}")
                action.fail(
                    "Failed to refresh snap. Check the error message in `juju debug-log` and then "
                    "consider retrying this action"
                )
            return

        for unit in self._units:
            databag = self._relation[unit]
            if databag.get("installed_snap_revision") != self._pinned_workload_container_version:
                break
            # If "installed_snap_revision" is set, "last_refresh_to_up_to_date_charm_code_version"
            # should also be set
            assert databag.get("last_refresh_to_up_to_date_charm_code_version") is not None
            if not self._is_units_databag_up_to_date(unit):
                break
        else:
            # This code should never run because `self._in_progress is _MachinesInProgress.TRUE`
            assert False
        next_unit_to_refresh = unit

        if next_unit_to_refresh != charm.unit:
            if action:
                assert action.check_health_of_refreshed_units
                action.fail(f"Must run action on unit {next_unit_to_refresh.number}")
            return

        if self._pause_after is _PauseAfter.NONE and action:
            action.fail(
                "`pause_after_unit_refresh` config is set to `none`. This action is not applicable."
            )
            # Do not log any additional information to action output
            action = None

        if not self._refresh_started:
            if action:
                action.fail(f"Unit {self._units[0].number} is unhealthy. Refresh will not resume.")
            return

        # Whether all units before this unit in the refresh order are up-to-date and have
        # allowed the next unit to refresh
        all_previous_units_have_allowed_this_unit_to_refresh = False
        for unit in self._units:
            if unit == charm.unit:
                all_previous_units_have_allowed_this_unit_to_refresh = True
                break
            databag = self._relation[unit]
            if (
                databag.get(
                    "next_unit_allowed_to_refresh_if_this_units_snap_revision_and_databag_are_up_to_date"
                )
                is not True
            ):
                break
            if databag.get("installed_snap_revision") != self._pinned_workload_container_version:
                break
            # If "installed_snap_revision" is set,
            # "last_refresh_to_up_to_date_charm_code_version" should also be set
            assert databag.get("last_refresh_to_up_to_date_charm_code_version") is not None
            if not self._is_units_databag_up_to_date(unit):
                break
        if not all_previous_units_have_allowed_this_unit_to_refresh:
            first_unit_that_has_not_allowed_this_unit_to_refresh = unit
            if action:
                action.fail(
                    f"Unit {first_unit_that_has_not_allowed_this_unit_to_refresh.number} is "
                    "unhealthy. Refresh will not resume."
                )
            return

        if (
            (self._pause_after is _PauseAfter.UNKNOWN or self._pause_after is _PauseAfter.ALL)
            and self._units.index(charm.unit) != 0
        ) or (self._pause_after is _PauseAfter.FIRST and self._units.index(charm.unit) == 1):
            # resume-refresh action required to refresh this unit

            if not action:
                return
            reason = "resume-refresh action ran"
        else:
            if action:
                # This unit would have refreshed even if the resume-refresh action was not run
                # Fail the action so that the user does not mistakenly believe that running the
                # action caused this unit to refresh—so that the user's mental model of how the
                # refresh works is more accurate.
                action.fail("Unit is currently refreshing")
                # Do not log any additional information to action output
                action = None

            if self._units.index(charm.unit) == 0:
                reason = "this unit is the first unit to refresh"
            else:
                reason = f"pause_after_unit_refresh config is {repr(self._pause_after.value)}"
                if self._pause_after is _PauseAfter.FIRST:
                    reason += " and second unit already refreshed"

        assert installed_snap_revision != self._pinned_workload_container_version
        if action:
            if self._pause_after is _PauseAfter.FIRST:
                action.log(f"Refresh resumed. Refreshing unit {charm.unit.number}")
            else:
                action.log(f"Refreshing unit {charm.unit.number}")
        if self._force_start is not None:
            self._force_start.log(f"Refreshing unit {charm.unit.number}")
        logger.info(f"Refreshing {from_to_message} because {reason}")
        self._charm_specific.refresh_snap(
            snap_name=self._workload_snap_name,
            snap_revision=self._pinned_workload_container_version,
            refresh=self,
        )
        if self._get_installed_snap_revision() == self._pinned_workload_container_version:
            logger.info(f"Refreshed {from_to_message} because {reason}")
            if action:
                if self._pause_after is _PauseAfter.FIRST:
                    action.result = {
                        "result": f"Refresh resumed. Unit {charm.unit.number} has refreshed"
                    }
                else:
                    action.result = {"result": f"Refreshed unit {charm.unit.number}"}
            if self._force_start is not None:
                self._force_start.result = {"result": f"Refreshed unit {charm.unit.number}"}
        else:
            # This code might not run since the charm code may intentionally raise an uncaught
            # exception in `self._charm_specific.refresh_snap()` if the snap is not refreshed
            logger.error(f"Failed to refresh {from_to_message}")
            if action:
                action.fail(
                    "Failed to refresh snap. Check the error message in `juju debug-log` and then "
                    "consider retrying this action"
                )
            if self._force_start is not None:
                self._force_start.result = {
                    "result": (
                        f"Refresh started. Failed to refresh unit {charm.unit.number}. Unit "
                        f"{charm.unit.number} will retry refresh on the next Juju event"
                    )
                }

    def __init__(self, charm_specific: CharmSpecificMachines, /):
        if not isinstance(charm_specific, CharmSpecificMachines):
            raise TypeError(
                f"expected type 'CharmSpecificMachines', got {repr(type(charm_specific).__name__)}"
            )
        self._charm_specific = charm_specific

        _LOCAL_STATE.mkdir(exist_ok=True)
        # Save state if this unit is tearing down.
        # Used in future Juju events
        tearing_down = _LOCAL_STATE / "machines_unit_tearing_down"
        if (
            # This check works for both principal & subordinate charms
            isinstance(charm.event, charm.RelationDepartedEvent)
            and charm.event.departing_unit == charm.unit
        ):
            # This unit is tearing down
            tearing_down.touch()

        # Check if this unit is tearing down
        if tearing_down.exists():
            if isinstance(charm.event, charm.ActionEvent) and charm.event.action in (
                "pre-refresh-check",
                "force-refresh-start",
                "resume-refresh",
            ):
                charm.event.fail("Unit tearing down")

            tearing_down_logged = _LOCAL_STATE / "machines_unit_tearing_down_logged"
            if not tearing_down_logged.exists():
                logger.info("Unit tearing down")
                tearing_down_logged.touch()

            raise UnitTearingDown

        # Get installed charm revision
        self._installed_charm_revision_raw = _RawCharmRevision.from_file()
        """Contents of this unit's .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-602")"""

        # Get versions from refresh_versions.toml
        refresh_versions = _MachinesRefreshVersions()
        self._installed_charm_version = refresh_versions.charm
        """This unit's charm version"""
        self._pinned_workload_version = refresh_versions.workload
        """Upstream workload version (e.g. "16.8") pinned by this unit's charm code

        Used for compatibility check & displayed to user
        """
        self._workload_snap_name = refresh_versions.snap_name
        self._pinned_workload_container_version = refresh_versions.snap_revision
        """Snap revision pinned by this unit's charm code for this unit's architecture

        (e.g. "182")
        """

        # Save state if this unit's charm code was refreshed to the up-to-date charm code version
        # On machines, we rely on data in the peer relation to determine if a refresh is in
        # progress. The relation databags visible from this unit may be outdated if a unit has many
        # events in queue or if a unit is raising an uncaught exception. If a unit is raising an
        # uncaught exception, it is not able to save changes to its relation databag.
        # Consider the following example:
        # - User refreshes from charm revision 602 to 613
        # - Highest unit's charm code refreshes snap from revision 182 to 183 and raises an
        #   uncaught exception in the same Juju event
        # - User refreshes (rollback) to charm revision 602
        # In this example, the highest unit was unable to update its databag while charm revision
        # 613 and snap revision 183 were installed. After the rollback starts, from the perspective
        # of the other units, the highest unit's databag says that snap revision 182 and charm
        # revision 602 are installed (and, therefore, a refresh is not in progress)— but, in
        # reality, snap revision 183 is installed.
        # To detect this, we need a mechanism to determine if a unit's databag is outdated.
        # If the charm revision in the databag is different from this unit's charm revision, we
        # know the databag is outdated.
        # If the charm revision is the same, then we do not know if the databag was last updated
        # after the latest refresh to that charm revision or if it was last updated after a
        # previous refresh to that charm revision.
        # If we store the timestamp of the last two times this unit was refreshed, and a unit's
        # databag was last updated before this unit's second to last refresh, then we know that
        # unit's databag is outdated.
        # (We cannot use the timestamp of the last refresh since this unit could get refreshed
        # before or after other units for the same `juju refresh`.)
        # There are a couple caveats:
        # 1. This relies on the unit machines' clocks to be roughly in sync. This is not ideal, but
        #    there is no better alternative to handle the refresh example above, which is one of
        #    the most likely rollback cases.
        #    If Juju were to expose "charm modified version", which is an integer that increments
        #    on every `juju refresh`, we could replace the timestamp with that—which would remove
        #    the requirement for clocks to be roughly in sync.
        # 2. If a unit is raising an uncaught exception, Juju may refresh the charm code without an
        #    upgrade-charm event (https://bugs.launchpad.net/juju/+bug/2068500). If this happens,
        #    Juju may also refresh that unit's charm code to a version that is not up-to-date. In
        #    contrast, if Juju emits an upgrade-charm event, it appears to be guaranteed that the
        #    charm code was the up-to-date version at the time of the refresh.
        #    Consider this example:
        #    - User deploys units 0, 1, and 2 with charm revision 5
        #    - Unit 2 is raising an uncaught exception
        #    - User refreshes app to charm revision 6
        #    - Units 0 and 1 refresh to revision 6 but unit 2 is still in error on revision 5
        #    - User refreshes (rollback) app to charm revision 5
        #    - Units 0 and 1 refresh to revision 5 but unit 2 is still in error
        #    - Unit 2 gets refreshed without upgrade-charm event to revision 6
        #    - Unit 2 gets refreshed to revision 5
        #    The issue here is that unit 2 now thinks that units 0 & 1 have outdated databags
        #    since its refresh to revision 6 happened after units 0 & 1 rolled back to revision 5.
        #    From testing, this example is not possible—units 0 and 1 cannot get refreshed to
        #    revision 5 before unit 2 refreshes to revision 6. However, in testing, it was possible
        #    for units 0 and 1 to refresh to revision 5 only 0.4 seconds after unit 2 was refreshed
        #    to revision 6. (And there's a decent possibility that the clocks for each unit could
        #    be out of sync by more than 0.4 seconds.)
        #    To mitigate this issue, we only store the timestamp on refresh if we know that the
        #    refresh was to the up-to-date charm code version.
        self._history = _CharmCodeRefreshHistory.from_file(
            installed_charm_version=self._installed_charm_version
        )
        self._refresh_started_local_state = _LOCAL_STATE / "machines_refresh_started"
        """NOTE: `self._refresh_started` and `self._refresh_started_local_state.exists()` can be
        out of sync if `self._history.second_to_last_refresh_to_up_to_date_charm_code_version` is
        `None`
        """
        if (
            self._history.last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            != _dot_juju_charm_modified_time()
        ):
            # Charm code has been refreshed

            if self._installed_charm_revision_raw.charmhub_revision:
                charm_version = (
                    f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                    f"({repr(self._installed_charm_version)})"
                )
            else:
                charm_version = f"{repr(self._installed_charm_version)}"
            self._refresh_started_local_state.unlink(missing_ok=True)
            # If Juju emits an upgrade-charm event, the charm code version is up-to-date
            # If Juju emits a config-changed event, because of this Juju bug
            # https://bugs.launchpad.net/juju/+bug/2084886, the charm code version is up-to-date
            # (If that bug is fixed, this code must be changed)
            # Juju does not always emit an upgrade-charm event when the charm code is refreshed
            # (https://bugs.launchpad.net/juju/+bug/2068500). If that bug happens, (from testing)
            # Juju will likely skip the upgrade-charm event but will correctly emit a
            # leader-settings-changed event (if unit is not leader) and then a config-changed event
            # So, in most cases, this unit will get a config-changed event after the charm code is
            # refreshed to the up-to-date version.
            if isinstance(charm.event, charm.UpgradeCharmEvent) or isinstance(
                charm.event, charm.ConfigChangedEvent
            ):
                # Charm code version is up-to-date

                self._history.second_to_last_refresh_to_up_to_date_charm_code_version = (
                    self._history.last_refresh_to_up_to_date_charm_code_version
                )
                self._history.last_refresh_to_up_to_date_charm_code_version = _HistoryEntry(
                    charm_revision=self._installed_charm_revision_raw,
                    time_of_refresh=_dot_juju_charm_modified_time(),
                )
                self._history.save_to_file()
                logger.info(
                    f"Charm refreshed to {charm_version} at {_dot_juju_charm_modified_time()}. "
                    "Charm is up-to-date. Second to last up-to-date charm refresh was at "
                    f"{self._history.second_to_last_refresh_to_up_to_date_charm_code_version.time_of_refresh}"
                )
            else:
                logger.info(
                    f"Charm refreshed to {charm_version} at {_dot_juju_charm_modified_time()}. "
                    "Charm may be outdated"
                )
                logger.warning(
                    "This unit's charm was refreshed without a Juju upgrade-charm event. This is "
                    "a Juju bug (https://bugs.launchpad.net/juju/+bug/2068500). Waiting for an "
                    "upgrade-charm or config-changed event to determine if this unit's charm is "
                    "up-to-date. If this unit is stuck and has repeatedly logged this message for "
                    "longer than 5 minutes, please contact the developers of this charm for "
                    f"support. If they are unreachable, consider running `juju config {charm.app} "
                    f"pause_after_unit_refresh=invalid` and then running `juju config {charm.app} "
                    "pause_after_unit_refresh=all`"
                )

        # Whether this unit's charm code version is up-to-date
        if (
            self._history.last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            == _dot_juju_charm_modified_time()
        ):
            # Set app workload version in `juju status` as soon as the charm code is refreshed so
            # that the workload version matches the charm revision shown for this app in
            # `juju status`
            charm.set_app_workload_version(self._pinned_workload_version)

        self._relation = charm_json.PeerRelation.from_endpoint("refresh-v-three")
        if not self._relation:
            raise _PeerRelationMissing

        self._installed_workload_version = _LOCAL_STATE / "machines_installed_workload_version"
        """File containing upstream workload version (e.g. "16.8") installed on this unit

        Used for compatibility check & displayed to user

        May be missing if the snap was refreshed on a previous version of the charm code and the
        charm code raised an uncaught exception on every Juju event until the charm code was
        refreshed (to this charm code version)
        May also be missing if a user manually refreshed the snap, which is not supported
        """

        self._refresh_started = self._refresh_started_local_state.exists()
        """Whether this app has started to refresh to the snap revision pinned in this unit's charm code

        `True` if this app is rolling back, if automatic checks have succeeded, or if the user
        successfully forced the refresh to start with the force-refresh-start action
        `False` otherwise

        Automatic checks include:

        - workload container check
        - compatibility checks
        - pre-refresh checks

        If the user runs `juju refresh` while a refresh is in progress, this will be reset to
        `False` unless the `juju refresh` is a rollback

        NOTE: `self._refresh_started` and `self._refresh_started_local_state.exists()` can be out
        of sync if `self._history.second_to_last_refresh_to_up_to_date_charm_code_version` is
        `None`
        """

        # Determine `self._refresh_started` and propagate
        # "refresh_started_if_this_units_databag_is_up_to_date" in other units' databags to this
        # unit's local state and databag, if the other unit's databag is up-to-date (from the
        # perspective of this unit).
        # The propagation preserves data if this app is scaled down (prevents workload container
        # check, compatibility checks, and pre-refresh checks from running again on scale down).
        # NOTE: `self._refresh_started` and `self._refresh_started_local_state.exists()` can be out
        # of sync if `self._history.second_to_last_refresh_to_up_to_date_charm_code_version` is
        # `None`
        presume_log: typing.Optional[str] = None  # Used to conditionally log message later
        if (
            not self._refresh_started
            # Whether this unit's charm code version is up-to-date
            and self._history.last_refresh_to_up_to_date_charm_code_version.time_of_refresh
            == _dot_juju_charm_modified_time()
        ):
            for unit, databag in self._relation.other_units.items():
                if databag.get("refresh_started_if_this_units_databag_is_up_to_date") is not True:
                    continue
                # If "refresh_started_if_this_units_databag_is_up_to_date" is set,
                # "last_refresh_to_up_to_date_charm_code_version" should also be set
                assert databag.get("last_refresh_to_up_to_date_charm_code_version") is not None
                # Check if other unit's databag is up-to-date
                up_to_date = self._is_units_databag_up_to_date_unknown(unit)
                if up_to_date is _MachinesDatabagUpToDate.TRUE:
                    # Other unit's databag is up-to-date

                    message = (
                        f"Learned from unit {unit.number} that refresh has started to "
                        f"{self._charm_specific.workload_name} {self._pinned_workload_version} "
                        f"(snap revision {self._pinned_workload_container_version}) and charm "
                    )
                    if self._installed_charm_revision_raw.charmhub_revision:
                        message += (
                            f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                            f"({repr(self._installed_charm_version)})"
                        )
                    else:
                        message += f"{repr(self._installed_charm_version)}"
                    logger.info(message)

                    self._refresh_started = True
                    # Propagate "refresh_started_if_this_units_databag_is_up_to_date" to this
                    # unit's local state & databag
                    self._refresh_started_local_state.touch()
                    self._relation.my_unit[
                        "refresh_started_if_this_units_databag_is_up_to_date"
                    ] = True
                    break
                elif up_to_date is _MachinesDatabagUpToDate.UNKNOWN:
                    # It is not possible to determine if the other unit's databag is up-to-date
                    # This is initial installation or this unit is a new unit that was added during
                    # scale up
                    # (Otherwise,
                    # `self._history.second_to_last_refresh_to_up_to_date_charm_code_version` would
                    # not be `None`—and, therefore, `up_to_date` would not be
                    # `_MachinesDatabagUpToDate.UNKNOWN`. On initial installation or scale up,
                    # `self._history.last_refresh_to_up_to_date_charm_code_version` is set. On the
                    # first refresh after initial installation or scale up,
                    # `self._history.second_to_last_refresh_to_up_to_date_charm_code_version` is
                    # set.)
                    # On scale up, there are two possible situations:
                    # 1. All units (that set refresh_started_... to `True`) have up-to-date
                    #    databags.
                    # 2. 1+ units (that set refresh_started_... to `True`) have outdated databags.
                    # Situation #1 is much more likely and it is important that, in situation #1,
                    # the automatic checks do not run again on scale up if they have already run.
                    # Therefore, we should assume that the other unit's databag is up-to-date.
                    # However, to minimize the damage if our assumption is incorrect, we should not
                    # propagate "refresh_started_if_this_units_databag_is_up_to_date" to this
                    # unit's databag.
                    # In situation #2, if we were to propagate
                    # "refresh_started_if_this_units_databag_is_up_to_date", other units would
                    # believe the refresh has started since this unit is up-to-date—and the
                    # automatic checks might never run for the refresh.
                    # Instead, by not propagating
                    # "refresh_started_if_this_units_databag_is_up_to_date", it is possible to
                    # recover—once the unit(s) with the outdated databags update their databags
                    # (e.g. because they stopped raising an uncaught exception), then all units
                    # will detect that the automatic checks have not been run for the current
                    # refresh & the refresh will pause until the highest unit runs the automated
                    # checks.
                    # Furthermore, even before the unit(s) with the outdated databags update their
                    # databags, units that were present before the scale up will detect that the
                    # automatic checks have not been run & will not refresh the workload snap on
                    # their unit.
                    # NOTE: Because we do not propagate
                    # "refresh_started_if_this_units_databag_is_up_to_date" to this unit's local
                    # state & databag, `self._refresh_started` and
                    # `self._refresh_started_local_state.exists()` will be out of sync

                    message = (
                        f"Presumed from unit {unit.number} that refresh has started to "
                        f"{self._charm_specific.workload_name} {self._pinned_workload_version} "
                        f"(snap revision {self._pinned_workload_container_version}) and charm "
                    )
                    if self._installed_charm_revision_raw.charmhub_revision:
                        message += (
                            f"revision {self._installed_charm_revision_raw.charmhub_revision} "
                            f"({repr(self._installed_charm_version)})"
                        )
                    else:
                        message += f"{repr(self._installed_charm_version)}"
                    # Save message to log it later if `self._in_progress` is not
                    # `_MachinesInProgress.FALSE`
                    # (To avoid logging the message on every Juju event if a refresh is not in
                    # progress)
                    presume_log = message

                    self._refresh_started = True
                    break

        # Propagate local state to this unit's databag.
        # Used to persist data to databag in case an uncaught exception was raised in the Juju
        # event where the data was originally set
        self._relation.my_unit["refresh_started_if_this_units_databag_is_up_to_date"] = (
            self._refresh_started_local_state.exists()
        )

        # Propagate local state to this unit's databag.
        # Used to persist data to databag in case an uncaught exception was raised in the Juju
        # event where the data was originally set
        # (Do not propagate `self._history.second_to_last_refresh_to_up_to_date_charm_code_version`
        # since other units do not need that information)
        self._relation.my_unit["last_refresh_to_up_to_date_charm_code_version"] = (
            dataclasses.asdict(self._history.last_refresh_to_up_to_date_charm_code_version)
        )

        # Propagate installed snap revision to this unit's databag
        # In case an uncaught exception was raised in the Juju event where `update_snap_revision()`
        # was called
        self._update_snap_revision(raise_if_not_installed=False)

        # Check if all units have joined the peer relation on initial install or scale up
        # During initial installation or when a new unit is added during scale up, the peer
        # relation will be available but will be missing units.
        # When a new unit is added during scale up, it should wait until it sees all other units in
        # the peer relation before it determines if a refresh is in progress.
        # After this unit sees all other units in the peer relation once, it should not check for
        # that condition again. If this unit is behind on events (e.g. because the charm code is
        # raising an uncaught exception), there may be a mismatch between the current units and the
        # units that this unit sees in the peer relation. If there is such a mismatch, that should
        # not prevent this unit from executing the code below this check (so that, among other
        # things, rollback is possible).
        peer_relation_initialized = _LOCAL_STATE / "machines_peer_relation_initialized"
        if not peer_relation_initialized.exists():
            # Whether this unit's app is subordinated to a principal app
            is_subordinate = os.environ["JUJU_PRINCIPAL_UNIT"] != ""

            result = json.loads(
                subprocess.run(
                    ["goal-state", "--format", "json"], capture_output=True, check=True, text=True
                ).stdout
            )
            if is_subordinate:
                # When a subordinate unit is tearing down, it is not possible to reliably determine
                # the number of planned units from `goal-state`
                assert not tearing_down.exists()

                # Example `result`:
                # {
                #     "units": {},
                #     "relations": {
                #         "backend-database": {
                #             "mysql": {"status": "joined", "since": "2025-03-10 10:51:40Z"},
                #             "mysql/0": {"status": "active", "since": "2025-03-10 10:51:40Z"},
                #         },
                #         "database": {
                #             "app1": {"status": "joined", "since": "2025-03-10 10:51:41Z"},
                #             "app1/0": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #             "app1/1": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #             "app1/2": {"status": "active", "since": "2025-03-10 10:52:52Z"},
                #         },
                #         "foobar": {
                #             "app1": {"status": "joined", "since": "2025-03-10 10:51:44Z"},
                #             "app1/0": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #             "app1/1": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #             "app1/2": {"status": "active", "since": "2025-03-10 10:52:52Z"},
                #             "app2": {"status": "joined", "since": "2025-03-10 10:51:46Z"},
                #             "app2/0": {"status": "waiting", "since": "2025-03-10 10:50:42Z"},
                #             "app2/1": {"status": "waiting", "since": "2025-03-10 10:50:38Z"},
                #         },
                #     },
                # }

                # Example: "app1"
                principal_app = charm.Unit(os.environ["JUJU_PRINCIPAL_UNIT"]).app
                principal_units_by_endpoint = {}
                for endpoint, endpoint_value in result["relations"].items():
                    principal_units = {}
                    for unit_or_app, value in endpoint_value.items():
                        if "/" not in unit_or_app:
                            # `unit_or_app` is app
                            continue
                        if charm.Unit(unit_or_app).app == principal_app:
                            principal_units[unit_or_app] = value
                    if principal_units:
                        principal_units_by_endpoint[endpoint] = principal_units
                # Example `principal_units_by_endpoint`:
                # {
                #     "database": {
                #         "app1/0": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #         "app1/1": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #         "app1/2": {"status": "active", "since": "2025-03-10 10:52:52Z"},
                #     },
                #     "foobar": {
                #         "app1/0": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #         "app1/1": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #         "app1/2": {"status": "active", "since": "2025-03-10 10:52:52Z"},
                #     },
                # }
                if not principal_units_by_endpoint:
                    raise ValueError(
                        f"Invalid `goal-state` output—no principal units found: {repr(result)}"
                    )
                elif len(principal_units_by_endpoint) > 1:
                    # Check that unit values are identical on each endpoint
                    values = iter(principal_units_by_endpoint.values())
                    first_value = next(values)
                    if not all(first_value == value for value in values):
                        raise ValueError(
                            f"Invalid `goal-state` output—different statuses for {principal_app=}"
                            f" across endpoints: {principal_units_by_endpoint=}"
                        )
                # Example `principal_units`:
                # {
                #     "app1/0": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #     "app1/1": {"status": "active", "since": "2025-03-10 10:54:12Z"},
                #     "app1/2": {"status": "active", "since": "2025-03-10 10:52:52Z"},
                # }
                _, principal_units = principal_units_by_endpoint.popitem()
                planned_units = sum(
                    1
                    for principal_unit in principal_units.values()
                    if principal_unit["status"] != "dying"
                )
            else:
                planned_units = sum(
                    1 for unit in result["units"].values() if unit["status"] != "dying"
                )

            if planned_units == len(self._relation.all_units):
                # All units have joined the peer relation
                peer_relation_initialized.touch()
                logger.info("Refresh peer relation ready")
            else:
                raise PeerRelationNotReady

        self._in_progress = self._determine_in_progress()

        if presume_log is not None and self._in_progress is not _MachinesInProgress.FALSE:
            logger.info(presume_log)

        # pre-refresh-check action
        if isinstance(charm.event, charm.ActionEvent) and charm.event.action == "pre-refresh-check":
            if self._in_progress is not _MachinesInProgress.FALSE:
                if self._in_progress is _MachinesInProgress.TRUE:
                    charm.event.fail("Refresh already in progress")
                elif self._in_progress is _MachinesInProgress.UNKNOWN:
                    charm.event.fail("Refresh already in progress")
                else:
                    raise TypeError
            elif charm.is_leader:
                try:
                    self._charm_specific.run_pre_refresh_checks_before_any_units_refreshed()
                except PrecheckFailed as exception:
                    charm.event.fail(
                        "Charm is not ready for refresh. Pre-refresh check failed: "
                        f"{exception.message}"
                    )
                else:
                    charm.event.result = {
                        "result": (
                            "Charm is ready for refresh. For refresh instructions, see "
                            f"https://charmhub.io/{self._charm_specific.charm_name}/docs/refresh/{self._installed_charm_version}\n"
                            "After the refresh has started, use this command to rollback:\n"
                            f"`juju refresh {charm.app} --revision "
                            f"{self._pinned_workload_container_version}`"
                        )
                    }
                    logger.info("Pre-refresh check succeeded")
            else:
                charm.event.fail(
                    f"Must run action on leader unit. (e.g. `juju run {charm.app}/leader "
                    "pre-refresh-check`)"
                )

        self._units = sorted(self._relation.all_units, reverse=True)
        """Sorted from highest to lowest unit number (refresh order)"""
        self._start_refresh()
        self._pause_after = _PauseAfter(charm.config["pause_after_unit_refresh"])

        # Set app status before potential snap refresh since snap refresh may take a long time
        # Ignore resume-refresh action when setting app status so that the app status UX is the
        # same when resume-refresh is run on the leader or a non-leader unit
        self._set_app_status()
        # Set in case `refresh.in_progress` accessed in `self._charm_specific.refresh_snap()`
        self._refresh_completed_this_event = False
        """Whether this app's refresh completed during this Juju event

        Only `True` if this unit's snap was refreshed during this Juju event and this app's refresh
        is now complete

        Used to avoid situation where, because the charm code sees that the refresh is no longer
        in progress, the charm code runs an operation that raises an uncaught exception. If that
        happens, changes to this unit's databag will not be persisted and—from the perspective of
        other units—a refresh will still be in progress.
        """

        self._refresh_unit()
        # Update `self._in_progress` and app status after possible snap refresh
        old_in_progress = self._in_progress
        self._in_progress = self._determine_in_progress()
        self._refresh_completed_this_event = (
            # If this unit's snap was refreshed during this Juju event, `old_in_progress` will be
            # `_MachinesInProgress.TRUE` and will not be `_MachinesInProgress.UNKNOWN`
            old_in_progress is _MachinesInProgress.TRUE
            and self._in_progress is _MachinesInProgress.FALSE
        )
        if self._refresh_completed_this_event:
            logger.info("Refresh was completed during this event")
        self._set_app_status()

        if self._in_progress is _MachinesInProgress.FALSE:
            # Unlike Kubernetes, do not clean up state
            # (Unlike Kubernetes, there are no lists that could grow indefinitely if not cleaned
            # up)
            # Keeping state avoids race conditions on scale up while the refresh is finishing

            # Trigger Juju event on the last unit to refresh after the refresh completes
            # (to ensure that unit has at least one Juju event after the refresh completes where
            # `self.in_progress` is `False`)
            self._relation.my_unit["_unused_last_completed_refresh"] = dataclasses.asdict(
                self._history.last_refresh_to_up_to_date_charm_code_version
            )
            # Whether this unit is leader
            if self._relation.my_app_rw is not None:
                # Save versions in app databag for next refresh
                _OriginalVersions(
                    workload=self._pinned_workload_version,
                    workload_container=self._pinned_workload_container_version,
                    installed_workload_container_matched_pinned_container=True,
                    charm=self._installed_charm_version,
                    charm_revision_raw=self._installed_charm_revision_raw,
                ).write_to_app_databag(self._relation.my_app_rw)
