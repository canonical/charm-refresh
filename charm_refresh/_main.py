import abc
import collections.abc
import dataclasses
import enum
import functools
import json
import logging
import pathlib
import typing

import charm
import charm_json
import lightkube
import lightkube.models.authorization_v1
import lightkube.resources.apps_v1
import lightkube.resources.authorization_v1
import lightkube.resources.core_v1
import ops
import packaging.version
import tomli
import yaml

logger = logging.getLogger(__name__)


class Cloud(enum.Enum):
    """Cloud that a charm is deployed in

    https://juju.is/docs/juju/cloud#heading--machine-clouds-vs--kubernetes-clouds
    """

    KUBERNETES = enum.auto()
    MACHINES = enum.auto()


@functools.total_ordering
class CharmVersion:
    """Charm code version

    Stored as a git tag on charm repositories

    TODO: link to docs about versioning spec
    """

    def __init__(self, version: str, /):
        # Example 1: "14/1.12.0"
        # Example 2: "14/1.12.0.post1.dev0+71201f4.dirty"
        self._version = version
        track, pep440_version = self._version.split("/")
        # Example 1: "14"
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
                f"track; got {len(self._pep440_version.release)} components instead: "
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
    """Pre-refresh health check or preparation failed"""

    def __init__(self, message: str, /):
        """Pre-refresh health check or preparation failed

        Include a short, descriptive message that explains to the user which health check or
        preparation failed. For example: "Backup in progress"

        The message will be shown to the user in the output of `juju status`, refresh actions, and
        `juju debug-log`.

        Messages longer than 64 characters will be truncated in the output of `juju status`.
        It is recommended that messages are <= 64 characters.

        Do not mention "pre-refresh check" or prompt the user to rollback in the message—that
        information will already be included alongside the message.
        """
        if len(message) == 0:
            raise ValueError(f"{type(self).__name__} message must be longer than 0 characters")
        self.message = message
        super().__init__(message)


@dataclasses.dataclass(eq=False)
class CharmSpecific(abc.ABC):
    """Charm-specific callbacks & configuration for in-place refreshes"""

    cloud: Cloud
    """Cloud that the charm is deployed in

    https://juju.is/docs/juju/cloud#heading--machine-clouds-vs--kubernetes-clouds
    """

    workload_name: str
    """Human readable workload name (e.g. PostgreSQL)"""

    refresh_user_docs_url: str
    """Link to charm's in-place refresh user documentation

    (e.g. https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro)

    Displayed to user in output of `pre-refresh-check` action
    """
    # TODO: add note about link in old version of charm & keeping evergreen

    oci_resource_name: typing.Optional[str] = None
    """Resource name for workload OCI image in metadata.yaml `resources`

    (e.g. postgresql-image)

    Required if `cloud` is `Cloud.KUBERNETES`

    https://juju.is/docs/sdk/metadata-yaml#heading--resources
    """

    # TODO: add note about upstream-source for pinning?
    # TODO: add note about `containers` assumed in metadata.yaml (to find container name)

    def __post_init__(self):
        """Validate values of dataclass fields

        Subclasses should not override these validations
        """
        # TODO: validate length of workload_name?
        if self.cloud is Cloud.KUBERNETES:
            if self.oci_resource_name is None:
                raise ValueError(
                    "`oci_resource_name` argument is required if `cloud` is `Cloud.KUBERNETES`"
                )
        elif self.oci_resource_name is not None:
            raise ValueError(
                "`oci_resource_name` argument is only allowed if `cloud` is `Cloud.KUBERNETES`"
            )

    @staticmethod
    @abc.abstractmethod
    def run_pre_refresh_checks_after_1_unit_refreshed() -> None:
        """Run pre-refresh health checks & preparations after the first unit has already refreshed.

        There are three situations in which the pre-refresh health checks & preparations run:

        1. When the user runs the `pre-refresh-check` action on the leader unit before the refresh
           starts
        2. On machines, after `juju refresh` and before any unit is refreshed, the highest number
           unit automatically runs the checks & preparations
        3. On Kubernetes; after `juju refresh`, after the highest number unit refreshes, and before
           the highest number unit starts its workload; the highest number unit automatically runs
           the checks & preparations

        Note that:

        - In situation #1 the checks & preparations run on the old charm code and in situations #2
          and #3 they run on the new charm code
        - In situations #2 and #3, the checks & preparations run on a unit that may or may not be
          the leader unit
        - In situation #3, the highest number unit's workload is offline
        - Before the refresh starts, situation #1 is not guaranteed to happen
        - Situation #2 or #3 (depending on machines or Kubernetes) will happen regardless of
          whether the user ran the `pre-refresh-check` action
        - In situations #2 and #3, if the user scales up or down the application before all checks
          & preparations are successful, the checks & preparations will run on the new highest
          number unit.
          If the user scaled up the application:
              - In situation #3, multiple units' workloads will be offline
              - In situation #2, the new units may install the new snap version before the checks &
                preparations succeed
        - In situations #2 and #3, if the user scales up the application after all checks &
          preparations succeeded, the checks & preparations will not run again. If they scale down
          the application, the checks & preparations will most likely not run again

        This method is called in situation #3.

        If possible, pre-refresh checks & preparations should be written to support all 3
        situations.

        If a pre-refresh check/preparation supports all 3 situations, it should be placed in this
        method and called by the `run_pre_refresh_checks_before_any_units_refreshed` method.

        Otherwise, if it does not support situation #3 but does support situations #1 and #2, it
        should be placed in the `run_pre_refresh_checks_before_any_units_refreshed` method.

        By default, all checks & preparations in this method will also be run in the
        `run_pre_refresh_checks_before_any_units_refreshed` method.

        Checks & preparations are run sequentially. Therefore, it is recommended that:

        - Checks (e.g. backup created) should be run before preparations (e.g. switch primary)
        - More critical checks should be run before less critical checks
        - Less impactful preparations should be run before more impactful preparations

        However, if any checks or preparations fail and the user runs the `force-refresh-start`
        action with `run-pre-refresh-checks=false`, the remaining checks & preparations will be
        skipped—this may impact how you decide to order the checks & preparations.

        If a check or preparation fails, raise the `PrecheckFailed` exception. All of the checks &
        preparations may be run again on the next Juju event.

        If all checks & preparations are successful, they will not run again unless the user runs
        `juju refresh`. Exception: in rare cases, they may run again if the user scales down the
        application.

        Checks & preparations will not run during a rollback.

        Raises:
            PrecheckFailed: If a pre-refresh health check or preparation fails
        """

    def run_pre_refresh_checks_before_any_units_refreshed(self) -> None:
        """Run pre-refresh health checks & preparations before any unit is refreshed.

        There are three situations in which the pre-refresh health checks & preparations run:

        1. When the user runs the `pre-refresh-check` action on the leader unit before the refresh
           starts
        2. On machines, after `juju refresh` and before any unit is refreshed, the highest number
           unit automatically runs the checks & preparations
        3. On Kubernetes; after `juju refresh`, after the highest number unit refreshes, and before
           the highest number unit starts its workload; the highest number unit automatically runs
           the checks & preparations

        Note that:

        - In situation #1 the checks & preparations run on the old charm code and in situations #2
          and #3 they run on the new charm code
        - In situations #2 and #3, the checks & preparations run on a unit that may or may not be
          the leader unit
        - In situation #3, the highest number unit's workload is offline
        - Before the refresh starts, situation #1 is not guaranteed to happen
        - Situation #2 or #3 (depending on machines or Kubernetes) will happen regardless of
          whether the user ran the `pre-refresh-check` action
        - In situations #2 and #3, if the user scales up or down the application before all checks
          & preparations are successful, the checks & preparations will run on the new highest
          number unit.
          If the user scaled up the application:
              - In situation #3, multiple units' workloads will be offline
              - In situation #2, the new units may install the new snap version before the checks &
                preparations succeed
        - In situations #2 and #3, if the user scales up the application after all checks &
          preparations succeeded, the checks & preparations will not run again. If they scale down
          the application, the checks & preparations will most likely not run again

        This method is called in situations #1 and #2.

        If possible, pre-refresh checks & preparations should be written to support all 3
        situations.

        If a pre-refresh check/preparation supports all 3 situations, it should be placed in the
        `run_pre_refresh_checks_after_1_unit_refreshed` method and called by this method.

        Otherwise, if it does not support situation #3 but does support situations #1 and #2, it
        should be placed in this method.

        By default, all checks & preparations in the
        `run_pre_refresh_checks_after_1_unit_refreshed` method will also be run in this method.

        Checks & preparations are run sequentially. Therefore, it is recommended that:

        - Checks (e.g. backup created) should be run before preparations (e.g. switch primary)
        - More critical checks should be run before less critical checks
        - Less impactful preparations should be run before more impactful preparations

        However, if any checks or preparations fail and the user runs the `force-refresh-start`
        action with `run-pre-refresh-checks=false`, the remaining checks & preparations will be
        skipped—this may impact how you decide to order the checks & preparations.

        If a check or preparation fails, raise the `PrecheckFailed` exception. All of the checks &
        preparations may be run again on the next Juju event.

        If all checks & preparations are successful, they will not run again unless the user runs
        `juju refresh`. Exception: in rare cases, they may run again if the user scales down the
        application.

        Checks & preparations will not run during a rollback.

        Raises:
            PrecheckFailed: If a pre-refresh health check or preparation fails
        """
        self.run_pre_refresh_checks_after_1_unit_refreshed()

    def refresh_snap(self, *, snap_revision: str, refresh: "Refresh") -> None:
        """Refresh workload snap

        `refresh.update_snap_revision()` must be called immediately after the snap is refreshed.

        This method should:

        1. Gracefully stop the workload, if it is running
        2. Refresh the snap
        3. Immediately call `refresh.update_snap_revision()`

        Then, this method should attempt to:

        4. Start the workload
        5. Check if the application and this unit are healthy
        6. If they are both healthy, set `refresh.next_unit_allowed_to_refresh = True`

        If the snap is not refreshed, this method will be called again on the next Juju event—if
        this unit is still supposed to be refreshed.

        Note: if this method was run because the user ran the `resume-refresh` action, this method
        will not be called again even if the snap is not refreshed unless the user runs the action
        again.

        If the workload is successfully stopped (step #1) but refreshing the snap (step #2) fails
        (i.e. the snap revision has not changed), consider starting the workload (in the same Juju
        event). If refreshing the snap fails, retrying in a future Juju event is not recommended
        since the user may decide to rollback. If the user does not decide to rollback, this method
        will be called again on the next Juju event—except in the `resume-refresh` action case
        mentioned above.

        If the snap is successfully refreshed (step #2), this method will not be called again
        (unless the user runs `juju refresh` to a different snap revision).

        Therefore, if `refresh.next_unit_allowed_to_refresh` is not set to `True` (step #6)
        (because starting the workload [step #4] failed, checking if the application and this unit
        were healthy [step #5] failed, either the application or unit was unhealthy in step #5, or
        the charm code raised an uncaught exception later in the same Juju event), then the charm
        code should retry steps #4-#6, as applicable, in future Juju events until
        `refresh.next_unit_allowed_to_refresh` is set to `True` and an uncaught exception is not
        raised by the charm code later in the same Juju event.

        Also, if step #5 fails or if either the application or this unit is unhealthy, the charm
        code should set a unit status to indicate what is unhealthy.

        Implementation of this method is required in subclass if `cloud` is `Cloud.MACHINES`
        """
        if self.cloud is not Cloud.MACHINES:
            raise ValueError("`refresh_snap` can only be called if `cloud` is `Cloud.MACHINES`")

    @staticmethod
    def _is_charm_version_compatible(*, old: CharmVersion, new: CharmVersion):
        """Check that new charm version is higher than old and that major versions are identical

        TODO talk about intermediate charms

        TODO talk about recommendation to not support charm code downgrade
        """
        # TODO log: add logging
        if not (old.released and new.released):
            # Unreleased charms contain changes that do not affect the version number
            # Those changes could affect compatability
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

        This method is called using the new charm code version.

        On Kubernetes, this method runs before the highest number unit starts the new workload
        version.
        On machines, this method runs before any unit is refreshed.

        If this method returns `False`, the refresh will be blocked and the user will be prompted
        to rollback.

        The user can override that block using the `force-refresh-start` action with
        `check-compatibility=false`.

        In order to support rollbacks, this method should always return `True` if the old and new
        charm code versions are identical and the old and new workload versions are identical.

        This method should not use any information beyond its parameters to determine if the
        refresh is compatible.
        """
        if not cls._is_charm_version_compatible(old=old_charm_version, new=new_charm_version):
            return False
        return True


class PeerRelationMissing(Exception):
    """Refresh peer relation is not yet available"""


class UnitTearingDown(Exception):
    """This unit is being removed"""


def _convert_to_ops_status(status: charm.Status) -> ops.StatusBase:
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


class Refresh:
    # TODO: add note about putting at end of charm __init__

    @property
    def in_progress(self) -> bool:
        """Whether a refresh is currently in progress"""
        return self._refresh.in_progress

    @property
    def next_unit_allowed_to_refresh(self) -> bool:
        """Whether the next unit is allowed to refresh

        After this unit refreshes, the charm code should check if the application and this unit are
        healthy. If they are healthy, this attribute should be set to `True` to allow the refresh
        to proceed on the next unit.

        Otherwise (if either is unhealthy or if it is not possible to determine that both are
        healthy), the charm code should (in future Juju events) continue to retry the health checks
        and set this attribute to `True` when both are healthy. In this Juju event, the charm code
        should also set a unit status to indicate what is unhealthy.

        If the charm code raises an uncaught exception in the same Juju event where this attribute
        is set to `True`, it will not be saved. In the next Juju events, the charm code should
        retry the health checks until this attribute is set to `True` in a Juju event where an
        uncaught exception is not raised by the charm code.

        This attribute can only be set to `True`. When the unit is refreshed, this attribute will
        automatically be reset to `False`.

        This attribute should only be read to determine if the health checks need to be run again
        so that this attribute can be set to `True`.

        Note: this has no connection to the `pause_after_unit_refresh` user configuration option.
        That user configuration option corresponds to manual checks performed by the user after the
        automatic checks are successful. This attribute is set to `True` when the automatic checks
        succeed. For example:

        - If this attribute is set to `True` and `pause_after_unit_refresh` is set to "all", the
          next unit will not refresh until the user runs the `resume-refresh` action.
        - If `pause_after_unit_refresh` is set to "none" and this attribute is not set to `True`,
          the next unit will not refresh until this attribute is set to `True`.

        The user can override failing automatic health checks by running the `resume-refresh`
        action with `check-health-of-refreshed-units=false`.
        """
        return self._refresh.next_unit_allowed_to_refresh

    @next_unit_allowed_to_refresh.setter
    def next_unit_allowed_to_refresh(self, value: typing.Literal[True]):
        self._refresh.next_unit_allowed_to_refresh = value

    def update_snap_revision(self):
        """Must be called immediately after the workload snap is refreshed

        Only applicable if cloud is `Cloud.MACHINES`

        If the charm code raises an uncaught exception in the same Juju event where this method is
        called, this method does not need to be called again. (That situation will be automatically
        handled.)

        Resets `next_unit_allowed_to_refresh` to `False`.
        """
        raise NotImplementedError

    @property
    def pinned_snap_revision(self) -> str:
        # TODO: move to CharmSpecific so it can be accessed during install event where refresh peer relation might be missing?
        """Workload snap revision pinned by this unit's current charm code

        This attribute should only be read during initial snap installation and should not be read
        during a refresh.

        During a refresh, the snap revision should be read from the `refresh_snap` method's
        `snap_revision` parameter.
        """
        raise NotImplementedError

    @property
    def workload_allowed_to_start(self) -> bool:
        """Whether this unit's workload is allowed to start

        Only applicable if cloud is `Cloud.KUBERNETES`

        On Kubernetes, the automatic checks (

        - that OCI image hash matches pin in charm code
        - that refresh is compatible from old to new workload and charm code versions
        - pre-refresh health checks & preparations

        ) run after the highest number unit is refreshed but before the highest number unit starts
        its workload.

        After a unit is refreshed, the charm code must check the value of this attribute to
        determine if the workload can be started.

        Note: the charm code should check this attribute for all units (not just the highest unit
        number) in case the user scales up or down the application during the refresh.

        After a unit is refreshed, the charm code should:

        1. Check the value of this attribute. If it is `True`, continue to step #2
        2. Start the workload
        3. Check if the application and this unit are healthy
        4. If they are both healthy, set `next_unit_allowed_to_refresh = True`

        If `next_unit_allowed_to_refresh` is not set to `True` (because the value of this attribute
        [step #1] was `False`, starting the workload [step #2] failed, checking if the application
        and this unit were healthy [step #3] failed, either the application or unit was unhealthy
        in step #3, or the charm code raised an uncaught exception later in the same Juju event),
        then the charm code should retry these steps, as applicable, in future Juju events until
        `next_unit_allowed_to_refresh` is set to `True` and an uncaught exception is not raised by
        the charm code later in the same Juju event.

        Also, if step #3 fails or if either the application or this unit is unhealthy, the charm
        code should set a unit status to indicate what is unhealthy.

        If the user skips the automatic checks by running the `force-refresh-start` action, the
        value of this attribute will be `True`.
        """
        return self._refresh.workload_allowed_to_start

    @property
    def app_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """App status with higher priority than any other app status in the charm

        Charm code should ensure that this status is not overridden
        """
        status = self._refresh.app_status_higher_priority
        if status:
            status = _convert_to_ops_status(status)
        return status

    @property
    def unit_status_higher_priority(self) -> typing.Optional[ops.StatusBase]:
        """Unit status with higher priority than any other unit status in the charm

        Charm code should ensure that this status is not overridden
        """
        status = self._refresh.unit_status_higher_priority
        if status:
            status = _convert_to_ops_status(status)
        return status

    def unit_status_lower_priority(
        self, *, workload_is_running: bool = True
    ) -> typing.Optional[ops.StatusBase]:
        """Unit status with lower priority than any other unit status with a message in the charm

        This status will not be automatically set. It should be set by the charm code if there is
        no other unit status with a message to display.
        """
        # TODO: note about set status on every Juju event? or up to charm?
        status = self._refresh.unit_status_lower_priority(workload_is_running=workload_is_running)
        if status:
            status = _convert_to_ops_status(status)
        return status

    def __init__(self, charm_specific: CharmSpecific, /):
        if charm_specific.cloud is Cloud.KUBERNETES:
            self._refresh = _Kubernetes(charm_specific)
        elif charm_specific.cloud is Cloud.MACHINES:
            raise NotImplementedError
        else:
            raise TypeError


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
        if not isinstance(other, type(self)):
            # Raise instead of `return NotImplemented` since this class inherits from `str`
            raise TypeError
        priorities = {self.NONE: 0, self.FIRST: 1, self.ALL: 2, self.UNKNOWN: 3}
        return priorities[self] > priorities[other]


@dataclasses.dataclass(frozen=True)
class _RefreshVersions:
    """Versions pinned in this unit's refresh_versions.toml"""

    # TODO add note on machines that workload versions pinned are not necc installed
    # TODO add machines subclass with snap
    charm: CharmVersion
    workload: str

    @classmethod
    def from_file(cls):
        with pathlib.Path("refresh_versions.toml").open("rb") as file:
            versions = tomli.load(file)
        try:
            return cls(charm=CharmVersion(versions["charm"]), workload=versions["workload"])
        except KeyError:
            # TODO link to docs with format?
            raise KeyError("Required key missing from refresh_versions.toml")
        except ValueError:
            raise ValueError("Invalid charm version in refresh_versions.toml")


class _RawCharmRevision(str):
    """Charm revision in .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""

    @classmethod
    def from_file(cls):
        """Charm revision in this unit's .juju-charm file"""
        return cls(pathlib.Path(".juju-charm").read_text().strip())

    @property
    def charmhub_revision(self) -> typing.Optional[str]:
        if self.startswith("ch:"):
            return self.split("-")[-1]


@dataclasses.dataclass(frozen=True)
class _OriginalVersions:
    """Versions (of all units) immediately after the last completed refresh

    Or, if no completed refreshes, immediately after juju deploy and (on machines) initial
    installation
    """

    workload: typing.Optional[str]
    """Original upstream workload version (e.g. "14.11")
    
    Always a str if `installed_workload_container_matched_pinned_container` is `True`
    `None` if `installed_workload_container_matched_pinned_container` is `False`
    """
    workload_container: str
    """Original workload image digest
        
    (e.g. "sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6")
    """
    installed_workload_container_matched_pinned_container: bool
    """Whether original workload container matched container pinned in original charm code"""
    charm: CharmVersion
    """Original charm version"""
    charm_revision_raw: _RawCharmRevision
    """Original charm revision in .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""

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
            logger.info("Saved versions to app databag for next refresh")


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
        *app_name, unit_number = pod_name.split("-")
        # Example: "postgresql-k8s/0"
        unit_name = f"{'-'.join(app_name)}/{unit_number}"
        return cls(
            unit_name,
            controller_revision=pod.metadata.labels["controller-revision-hash"],
            pod_uid=pod.metadata.uid,
        )


class KubernetesJujuAppNotTrusted(Exception):
    """Juju app is not trusted (needed to patch StatefulSet partition)

    User must run `juju trust` with `--scope=cluster`
    or re-deploy using `juju deploy` with `--trust`
    """


class _Kubernetes:
    @property
    def in_progress(self) -> bool:
        return self._in_progress

    @property
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

    @property
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

    @property
    def app_status_higher_priority(self) -> typing.Optional[charm.Status]:
        return self._app_status_higher_priority

    @property
    def unit_status_higher_priority(self) -> typing.Optional[charm.Status]:
        return self._unit_status_higher_priority

    def unit_status_lower_priority(
        self, *, workload_is_running: bool
    ) -> typing.Optional[charm.Status]:
        if not self._in_progress:
            return
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
        if workload_is_running:
            message += " running"
        if self._unit_controller_revision != self._app_controller_revision:
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
                    f"{self._installed_workload_container_version.removeprefix('sha256:')[:6]}"
                )
            else:
                # This message is unlikely to be displayed—the status will probably be overridden
                # by a Kubernetes ImagePullBackOff error
                message += "; Unable to check container"
        if workload_is_running:
            return charm.ActiveStatus(message)
        return charm.WaitingStatus(message)

    @staticmethod
    def _get_partition() -> int:
        """Kubernetes StatefulSet rollingUpdate partition

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
        """Kubernetes StatefulSet rollingUpdate partition

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
                charm.event, first_unit_to_refresh=self._units[0], in_progress=self.in_progress
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

        if not charm.unit == self._units[0]:
            return
        if self._unit_controller_revision != self._app_controller_revision:
            if force_start:
                force_start.fail(
                    f"Unit {charm.unit.number} has not yet refreshed to latest app revision"
                )  # TODO UX
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
                force_start.fail("refresh already started")  # TODO UX
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
                        f"Checked that refresh is to {self._charm_specific.workload_name}"
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
                # TODO UX: change message to refresh not started? (for scale up case)
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

    def __init__(self, charm_specific: CharmSpecific, /):
        assert charm_specific.cloud is Cloud.KUBERNETES
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
            # TODO comment: is this true when scaling to 0 units? do we care for this case?
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
            # TODO improve docstring with exceptions that can be raised
            # TODO is this k8s only?
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

        self._relation = charm_json.PeerRelation.from_endpoint("refresh-v-three")
        if not self._relation:
            raise PeerRelationMissing

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

        if isinstance(charm.event, charm.StopEvent) and self._in_progress:
            # Trigger Juju event on other units so that they quickly update app & unit status after
            # a refresh starts
            self._relation.my_unit["_unused_controller_revision_during_last_stop_event"] = (
                self._unit_controller_revision
            )

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
        # databag. Preserves data if app is scaled down (prevents workload container check,
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
        """Contents of this unit's .juju-charm file (e.g. "ch:amd64/jammy/postgresql-k8s-381")"""

        # Get versions from refresh_versions.toml
        refresh_versions = _RefreshVersions.from_file()
        self._installed_charm_version = refresh_versions.charm
        """This unit's charm version"""
        self._pinned_workload_version = refresh_versions.workload
        """Upstream workload version (e.g. "14.11") pinned by this unit's charm code
        
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
                "'@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6'): "
                f"{repr(upstream_source)}"
            )
        else:
            self._pinned_workload_container_version = digest
            """Workload image digest pinned by this unit's charm code

            (e.g. "sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6)"
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
            # Example: "registry.jujucharms.com/charm/kotcfrohea62xreenq1q75n1lyspke0qkurhk/postgresql-image@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6"
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
        
        (e.g. "sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6")
        """

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
                    self._original_versions = _OriginalVersions(
                        workload=self._pinned_workload_version if matches_pin else None,
                        workload_container=self._installed_workload_container_version,
                        installed_workload_container_matched_pinned_container=matches_pin,
                        charm=self._installed_charm_version,
                        charm_revision_raw=self._installed_charm_revision_raw,
                    )
                    self._original_versions.write_to_app_databag(self._relation.my_app_rw)
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
                            f"{self._charm_specific.refresh_user_docs_url}\n"
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
