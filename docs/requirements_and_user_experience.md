# In-place charm upgrades
This document specifies the product requirements and user experience for in-place, rolling upgrades of stateful charmed applications—particularly for charmed databases maintained by the Data Platform team.

This document will be used as reference for & versioned alongside a Python package containing shared upgrade code for Data Platform charmed databases.

Overview:
- The [Glossary](#glossary) defines terms used in this document
- [What happens after a Juju application is refreshed](#what-happens-after-a-juju-application-is-refreshed) describes the behavior of Juju and Kubernetes after the user runs `juju refresh`. These are the constraints in which the product requirements are implemented.
- [Product requirements](#product-requirements) describes the functionality & behavior that Data Platform charmed databases need to support for in-place upgrades.
- [User experience](#user-experience) is a full description—excluding user documentation—of how the user interacts with and experiences an in-place upgrade of a single Juju application. The user experience satisfies the product requirements.

# Glossary
[Application](https://juju.is/docs/juju/application), [unit](https://juju.is/docs/juju/unit), [leader](https://juju.is/docs/juju/leader), [charm](https://juju.is/docs/juju/charmed-operator), [revision](https://juju.is/docs/sdk/revision), and [relation/integration](https://juju.is/docs/juju/relation) have the same meaning as in the Juju documentation.

User: User of Juju (e.g. user of juju CLI). Same meaning as "user" in diagram [in the Juju documentation](https://juju.is/docs/juju)

Event: Same meaning as "[Juju event](https://juju.is/docs/juju/hook)" or "hook" in the Juju documentation. Does not refer to an "ops event"

Workload: A software component that the charm operates (e.g. PostgreSQL)
- Note: a charm can have 0, 1, or multiple workloads

Charm code: Contents of *.charm file or `charm` directory (e.g. `/var/lib/juju/agents/unit-postgresql-k8s-0/charm/`) on a unit. Contains charm source code and (specific versions of) Python dependencies

Charm code version: Same meaning as charm [revision](https://juju.is/docs/sdk/revision)

Outdated version:
- Charm code version on a unit that **does not** match the application's charm code version (revision) and/or
- Workload version on a unit that **does not** match the workload version pinned in the application's charm code version (revision)

Up-to-date version:
- Charm code version on a unit that **does** match the application's charm code version (revision) and/or
- Workload version on a unit that **does** match the workload version pinned in the application's charm code version (revision)

Original version: workload and/or charm code version of all units immediately after the last completed upgrade—or, if no completed upgrades, immediately after `juju deploy` and (on machines) initial installation

## For an application (or if not specified)
Upgrade: `juju refresh` to a different workload and/or charm code version
- Note: "rollback" and "downgrade" are specific types of "upgrade"

In-progress upgrade: 1+ units have an outdated workload and/or charm code version

Completed upgrade: All units have the up-to-date workload and charm code version

Rollback: While an upgrade is in-progress and 1+ units have the **original** workload (and, on Kubernetes, charm code) version, `juju refresh` to the original workload and charm code version
 - Note: If all units have already upgraded, then it would be a downgrade, not a rollback
 - Note: If `juju refresh` is not to the original workload and charm code version, then it is not a rollback

Downgrade: Upgrade to older (lower) workload and/or charm code version

## For a unit
Charm code upgrade: Contents of `charm` directory are replaced with up-to-date charm code version

Workload upgrade: Workload is stopped (if running) and updated to up-to-date workload version

Upgrade:
- For Kubernetes: charm code and workload are upgraded
- For machines: workload is upgraded

Rollback:
- For Kubernetes: charm code and workload are upgraded to original versions
- For machines: workload is upgraded to original version

Downgrade:
- For Kubernetes: charm code and/or workload are upgraded to older (lower) version
- For machines: workload is upgraded to older (lower) version

# What happens after a Juju application is refreshed
This section describes the behavior of Juju and Kubernetes after the user runs `juju refresh`. These are the constraints in which the [product requirements](#product-requirements) are implemented.

## Kubernetes
On Kubernetes, each Juju application is a [StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/) configured with the [`RollingUpdate` update strategy](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#rolling-updates). Each Juju unit is a [Pod](https://kubernetes.io/docs/concepts/workloads/pods/).

When the user runs `juju refresh`, Juju updates the application's StatefulSet.

Then:
1. Kubernetes [sends a SIGTERM signal](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination) to the pod with the highest [ordinal](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#ordinal-index) (unit number)
1. Juju emits a [stop event](https://juju.is/docs/sdk/stop-event) on the unit
1. After the unit processes the stop event **or** after the pod's `terminationGracePeriodSeconds` have elapsed, whichever comes first, Kubernetes deletes the pod
    - `terminationGracePeriodSeconds` is set to 30 seconds as of Juju 3.3 (300 seconds in Juju <=3.2). It is [not recommended](https://chat.charmhub.io/charmhub/pl/i4czczen7f8i9cecdzpfmazs6a) for charms to patch this value. Details: https://bugs.launchpad.net/juju/+bug/2035102

1. Kubernetes re-creates the pod using the updated StatefulSet
    - This upgrades the unit's charm code and container image(s) (i.e. workload(s))
1. Juju emits an [upgrade-charm event](https://juju.is/docs/sdk/upgrade-charm-event) on the unit
    - Note: Receiving an upgrade-charm event does not guarantee that a unit has upgraded. If, at any time, a pod is deleted and re-created, Juju may emit an upgrade-charm event on that unit. Details: https://bugs.launchpad.net/juju/+bug/2021891
1. After the pod's [readiness probe](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes) succeeds, the previous steps are repeated for the pod with the next highest ordinal
    - For a Juju unit, [pebble's health endpoint](https://github.com/canonical/pebble?tab=readme-ov-file#health-endpoint) is used for the readiness probe. By default, pebble will always succeed the probe

Charms can interrupt this process by setting the [`RollingUpdate` partition](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/#partitions).
> If a partition is specified, all Pods with an ordinal that is greater than or equal to the partition will be updated when the StatefulSet's `.spec.template` is updated. All Pods with an ordinal that is less than the partition will not be updated, and, even if they are deleted, they will be recreated at the previous version.

For example, in a 3-unit Juju application (unit numbers: 0, 1, 2), as unit 2's pod is being deleted, the charm can set the partition to 2. Unit 2 will upgrade but units 1 and 0 will not. Then, after the charm verifies that all units are healthy, it can set the partition to 1 and unit 1 will upgrade.

Note: after the user runs `juju refresh`, the charm cannot prevent upgrade of the highest unit number.

> [!WARNING]
> Charms should not set the partition greater than the highest unit number. If they do, `juju refresh` will not trigger any [Juju events](https://juju.is/docs/juju/hook).

> [!IMPORTANT]
> During rollback, all pods—even those that have not upgraded—will be deleted (workload will restart). This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2036246

> [!CAUTION]
> If a pod (unit) with an outdated (workload or charm code) version is deleted and re-created on the same version (e.g. because the pod is [evicted](https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/)), it will not start. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073506

## Machines
After the user runs `juju refresh`, for each unit of the Juju application:
> [!NOTE]
> If the unit failed to execute the last event (raised uncaught exception), Juju may retry that event. Then, Juju will upgrade the unit's charm code without emitting an upgrade-charm event on that unit. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2068500

1. If the unit is currently executing another event, Juju [waits for the unit to finish executing that event](https://matrix.to/#/!xzmWHtGpPfVCXKivIh:ubuntu.com/$firps4AV5YInSDQh4izbPTZ0B0e0QwAbQVMaURT0T3o?via=ubuntu.com&via=matrix.org&via=fsfe.org)
1. Juju upgrades the unit's charm code
1. Juju emits an [upgrade-charm event](https://juju.is/docs/sdk/upgrade-charm-event) on that unit

This process happens concurrently and independently for each unit. For example, if one unit is executing another event, that will not prevent Juju from upgrading other units' charm code.

Upgrading the workload(s) (e.g. snap or apt packages) is left to the charm.

## Key differences between Kubernetes and machines
On Kubernetes, the charm code and workload are upgraded at the same time (for a unit). On machines, they are upgraded at different times.

On Kubernetes, while an upgrade is in progress, units will have different charm code versions. The leader unit may have the old or new charm code version.

On machines, while an upgrade is in progress, the charm code version may be out of sync with the workload version. (For example, if the charm code is written for workload version B, it may not know how to operate workload version A [e.g. to maintain high availability].)

After `juju refresh`, on machines, the charm can prevent workload upgrade (e.g. if the new version is incompatible) for all units. On Kubernetes, the charm cannot prevent workload upgrade of the highest unit number.

# Product requirements
This section describes the functionality & behavior that Data Platform charmed databases need to support for in-place upgrades.

Top-level bullet points are requirements. Sub-level bullet points are the rationale for a requirement.
- Upgrade units in place
  - To avoid replicating large amounts of data
  - To avoid downtime
  - To avoid additional hardware costs
  - To keep existing configuration & integrations with other Juju applications
- Upgrade units one at a time
  - To serve read & write traffic to database during upgrade
  - To test new version with subset of traffic (e.g. on one unit) before switching all traffic to new version
- Rollback upgraded units (one at a time) at any time during upgrade
  - If there are any issues with new version of charm code or workload
- Maintain high availability while upgrade is in progress (for up to multiple weeks)
  - To allow user to monitor new version with subset of traffic for extended period of time before switching all traffic to new version
  - For large databases (terabytes, petabytes)
- Pause upgrade to allow user to perform manual checks after upgrade of units: all, first, or none
  - Automated checks within the charm are not sufficient—for example, if a database client is outdated & incompatible with the new database version
  - Needs to be configurable for different user risk levels
- Allow user to change which units (all, first, or none) the upgrade pauses after while an upgrade is in progress
  - To allow user to pause after each of the first few units and then proceed with the remaining units
  - To allow user to interrupt an upgrade (e.g. to rollback) when a pause was not originally planned
- Warn the user if an upgrade is incompatible. Allow them to proceed if they accept potential data loss and downtime
- Automatically check the health of the application and all units after each unit upgrades. If anything is unhealthy, pause the upgrade and notify the user. Allow them to proceed if they accept potential data loss and downtime
- Provide pre-upgrade health checks (e.g. backup created) & preparations (e.g. switch primary) that the user can run before `juju refresh` and, when possible, that are automatically run after `juju refresh`
- Provide accurate, up-to-date information about the current upgrade status, workload status for each unit, workload and charm code versions for each unit, which units' workloads will restart, and what action, if any, the user should take next
- If a unit (e.g. the leader) is in error state (charm raises uncaught exception), allow rollback on other units
  - In case there is a bug in the new charm code version
  - In case the user accidentally upgraded to a different charm code version than they intended
- If a unit (e.g. the leader) is in error state (charm raises uncaught exception), allow upgrade on other units with manual user confirmation
  - For an application with several units upgraded, it may be safer to ignore one unhealthy unit and complete the upgrade then to rollback all upgraded units

# User experience
This section is a full description—excluding user documentation—of how the user interacts with and experiences an in-place upgrade of a single Juju application. The user experience satisfies the [product requirements](#product-requirements).

## `pause_after_unit_upgrade` config option
```yaml
# config.yaml
options:
  # [...]
  pause_after_unit_upgrade:
    description: |
      Wait for manual confirmation to resume upgrade after these units upgrade

      Allowed values: "all", "first", "none"
    type: string
    default: first
```
If an upgrade is not in progress, changing this value will have no effect until the next upgrade.

If an upgrade is in progress, changes to this value will take effect before the next unit upgrades. (Any units that are upgrading when the value is changed will finish upgrading.)

Example 1:
- 4-unit Juju application
  - Unit 0: v1
  - Unit 1: v1
  - Unit 2: v2
  - Unit 3: v2
- `pause_after_unit_upgrade` changed from `all` to `first`
- Unit 1 will immediately upgrade. If it is healthy after upgrading, unit 0 will upgrade

Example 2:
- 4-unit Juju application
  - Unit 0: v1
  - Unit 1: upgrading from v1 to v2
  - Unit 2: v2
  - Unit 3: v2
- `pause_after_unit_upgrade` changed from `none` to `all`
- Unit 1 will finish upgrading to v2. After that, no units will upgrade until the user runs the `resume-upgrade` action or runs `juju refresh` (e.g. to rollback)

### App status if `pause_after_unit_upgrade` set to invalid value
If `pause_after_unit_upgrade` is not set to `all`, `first`, or `none`, this app status will be displayed—regardless of whether an upgrade is in progress.

This status will have higher priority than any other app status in a charm.

```
$ juju status
[...]
App             [...]  Status   [...]  Message
postgresql-k8s         blocked         pause_after_unit_upgrade config must be set to "all", "first", or "none"
[...]
```

## `pre-upgrade-check` action (optional)
Before the user runs `juju refresh`, they should run the `pre-upgrade-check` action on the leader unit. The leader unit will run pre-upgrade health checks (e.g. backup created) & preparations (e.g. switch primary).

Optional: In the user documentation, this step will not be marked as optional (since it improves the safety of the upgrade—especially on Kubernetes). However, since forgetting to run the action is a common mistake (it has already happened on a production PostgreSQL charm), it is not required.

This action should not be run before a rollback.

```yaml
# actions.yaml
pre-upgrade-check:
  description: Check if charm is ready to upgrade
```

### If pre-upgrade health checks & preparations are successful
#### Kubernetes
```
$ juju run postgresql-k8s/leader pre-upgrade-check
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-0

Waiting for task 2...
result: |-
  Charm is ready for upgrade. For upgrade instructions, see https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro
  After the upgrade has started, use this command to rollback (copy this down in case you need it later):
  `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `https://charmhub.io/postgresql-k8s/docs/h-upgrade-intro` is replaced with the link to the charm's upgrade documentation, `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original (current) charm code revision, and `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original (current) charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources)

#### Machines
```
result: |-
  Charm is ready for upgrade. For upgrade instructions, see https://charmhub.io/postgresql/docs/h-upgrade-intro
  After the upgrade has started, use this command to rollback:
  `juju refresh postgresql --revision 10007`
```
where `https://charmhub.io/postgresql/docs/h-upgrade-intro` is replaced with the link to the charm's upgrade documentation, `postgresql` is replaced with the Juju application name, and `10007` is replaced with the original (current) charm code revision

### If pre-upgrade health checks & preparations are not successful
```
$ juju run postgresql-k8s/leader pre-upgrade-check
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-0

Waiting for task 2...
Action id 2 failed: Charm is *not* ready for upgrade. Pre-upgrade check failed: Backup in progress
```
where `Backup in progress` is replaced with a message that is specific to the pre-upgrade health check or preparation that failed
### If action ran while upgrade is in progress
```
Action id 2 failed: Upgrade already in progress
```

### If action ran on non-leader unit
```
Action id 2 failed: Must run action on leader unit. (e.g. `juju run postgresql-k8s/leader pre-upgrade-check`)
```
where `postgresql-k8s` is replaced with the Juju application name

## Status messages while upgrade in progress
After the user runs `juju refresh`, these status messages will be displayed until the upgrade is complete.

On machines, if the charm code is upgraded and the workload version is identical (i.e. same snap revision) the upgrade will immediately complete. The leader unit will log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-0: 11:34:35 INFO unit.postgresql/0.juju-log Charm upgraded. PostgreSQL version unchanged
```
where `PostgreSQL` is replaced with the name of the workload(s)

> [!NOTE]
> Status messages over 120 characters are truncated in `juju status` (tested on Juju 3.1.6 and 2.9.45)

### App status
All of these app statuses will have higher priority than any other app status in a charm—except for [App status if `pause_after_unit_upgrade` set to invalid value](#app-status-if-pause_after_unit_upgrade-set-to-invalid-value).
#### If upgrade will pause for manual confirmation
(`pause_after_unit_upgrade` is set to `all` or set to `first` and second unit has not started to upgrade)

##### Kubernetes
```
$ juju status
[...]
App             Version  Status   [...]    Rev  [...]  Message
postgresql-k8s  14.12    blocked         10008         Upgrading. Verify units >=11 are healthy & run `resume-upgrade` on leader. To rollback, see docs or `juju debug-log`
[...]
```
where `>=11` is replaced with the units that have upgraded or are currently upgrading
<!-- TODO: version field? -->

During every Juju event, the leader unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-0: 11:34:35 INFO unit.postgresql/0.juju-log Upgrade in progress. To rollback, run `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, and `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources)

##### Machines
```
App         Version  Status     Rev  Message
postgresql  14.12    blocked  10008  Upgrading. Verify units >=11 are healthy & run `resume-upgrade` on unit 10. To rollback, `juju refresh --revision 10007`
```
where `>=11` is replaced with the units that have upgraded or are currently upgrading, `10` is replaced with the next unit to upgrade, and `10007` is replaced with the original charm code revision
<!-- TODO: version field? -->

#### If upgrade will not pause for manual confirmation
(`pause_after_unit_upgrade` is set to `none` or set to `first` and second unit has upgraded or started to upgrade)

```
App             Status       Message
postgresql-k8s  maintenance  Upgrading. To pause upgrade, run `juju config postgresql-k8s pause_after_unit_upgrade=all`
```
where `postgresql-k8s` is replaced with the Juju application name

#### (Machines only) If upgrade is incompatible
On machines, after the user runs `juju refresh` and before any workload is upgraded, the new charm code checks if it supports upgrading from the previous workload & charm code version.

If the upgrade is not supported, no workload will be upgraded and the app status will be
```
App         Status     Rev  Message
postgresql  blocked  10008  Upgrade incompatible. Rollback with `juju refresh --revision 10007`
```
where `10007` is replaced with the original charm code revision

This status will only show if an incompatible upgrade has not been forced on the first unit to upgrade with the `force-upgrade-start` action.

The leader unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-0: 11:34:35 INFO unit.postgresql/0.juju-log Upgrade incompatible. If you accept potential *data loss* and *downtime*, you can continue by running `force-upgrade-start ignore-compatibility-checks=true` on unit 2
```
where `2` is replaced with the first unit to upgrade

### Unit status
#### Higher priority statuses
These statuses will have higher priority than any other unit status in a charm.

##### (Kubernetes only) If workload version does not match charm code version
If the user runs `juju refresh` with `--revision` and without `--resource`, the workload(s) will not be upgraded. This is not supported—Data Platform charms pin a specific workload version for each charm code version.
```
Unit              Workload  [...]  Message
postgresql-k8s/2  blocked          `juju refresh` ran with missing or incorrect OCI resource. Rollback with instructions in docs or `juju debug-log`
```

The unit will also log an ERROR level message to `juju debug-log`. For example:
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log `juju refresh` ran with missing or incorrect OCI resource. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, and `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources)

##### (Kubernetes only) If upgrade is incompatible
On Kubernetes, after the first unit upgrades and before that unit starts its workload, that unit (new charm code) checks if it supports upgrading from the previous workload & charm code version.

If the upgrade is not supported, that unit will not start its workload and its status will be
```
Unit              Workload  [...]  Message
postgresql-k8s/2  blocked          Upgrade incompatible. Rollback with instructions in Charmhub docs or `juju debug-log`
```

This status will only show on the first unit to upgrade and only if the workload has not been forced to (attempt to) start with the `force-upgrade-start` action.

The unit will also log an INFO level message to `juju debug-log`. For example:
```
unit-postgresql-k8s-2: 11:34:35 INFO unit.postgresql-k8s/2.juju-log Upgrade incompatible. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. If you accept potential *data loss* and *downtime*, you can force upgrade to continue by running `force-upgrade-start ignore-compatibility-checks=true` on unit 2
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `2` is replaced with that unit (the first unit to upgrade)

##### If automatic pre-upgrade health checks & preparations fail
Regardless of whether the user runs the `pre-upgrade-check` action before `juju refresh`, the charm will run pre-upgrade health checks & preparations after `juju refresh`—unless it is a rollback.

On machines, the checks & preparations run before any workload is upgraded. These checks & preparations are identical to those in the `pre-upgrade-check` action—except that they are from the new charm code version.

On Kubernetes, the checks & preparations run after the first unit has upgraded. These checks & preparations are a subset of those in the `pre-upgrade-check` action (since some checks & preparations may require that all units have the same workload version). These checks & preparations run on the upgraded unit (i.e. on the new charm code version).

```
Unit              Workload  Message
postgresql-k8s/2  blocked   Rollback with `juju refresh`. Pre-upgrade check failed: Backup in progress
```
where `Backup in progress` is replaced with a message that is specific to the pre-upgrade health check or preparation that failed

This status will only show on the first unit to upgrade and only if the workload has not been forced to upgrade (machines) or to attempt to start (Kubernetes) with the `force-upgrade-start` action.

The unit will also log an ERROR level message to `juju debug-log`. For example:

Kubernetes
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Pre-upgrade check failed: Backup in progress. If you accept potential *data loss* and *downtime*, you can force the upgrade to continue by running `force-upgrade-start ignore-pre-upgrade-checks=true` on unit 2
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), `Backup in progress` is replaced with a message that is specific to the pre-upgrade health check or preparation that failed, and `2` is replaced with that unit (the first unit to upgrade)

Machines
```
unit-postgresql-k8s-2: 11:34:35 ERROR unit.postgresql-k8s/2.juju-log Rollback with `juju refresh`. Pre-upgrade check failed: Backup in progress. If you accept potential *data loss* and *downtime*, you can continue by running `force-upgrade-start ignore-pre-upgrade-checks=true` on unit 2
```
where `Backup in progress` is replaced with a message that is specific to the pre-upgrade health check or preparation that failed and `2` is replaced with that unit (the first unit to upgrade)

#### Lower priority statuses
These statuses will have lower priority than any other unit status with a message in a charm.

In all the following examples, all units are healthy. If a unit was unhealthy, that unit's status would take priority.

##### Kubernetes
###### Example: Normal upgrade
Unit 2 has upgraded. Units 1 and 0 have not upgraded.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.11 running (restart pending); Charmed operator revision 10007
postgresql-k8s/1   active    PostgreSQL 14.11 running (restart pending); Charmed operator revision 10007
postgresql-k8s/2   active    PostgreSQL 14.12 running; Charmed operator revision 10008
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

###### Example: Rollback
Units 2 and 1 upgraded from revision 10007 & OCI resource 76ef26 to revision 10008 & OCI resource 6be83f. Then, the user ran `juju refresh` to revision 10007 & OCI resource 76ef26. Unit 2 has rolled back.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.11 running (restart pending); Charmed operator revision 10007
postgresql-k8s/1   active    PostgreSQL 14.12 running (restart pending); Charmed operator revision 10008
postgresql-k8s/2   active    PostgreSQL 14.11 running; Charmed operator revision 10007
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

Unit 0 will restart even though the workload & charm code version will not change. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2036246

###### Example: Charm code upgrade without workload upgrade
If the charm code is upgraded and the workload version is unchanged, all units will restart. This happens because `juju refresh` updates the Kubernetes StatefulSet.

Unit 2 has upgraded. Units 1 and 0 have not upgraded.
```
Unit               Workload  Message
postgresql-k8s/0*  active    PostgreSQL 14.12 running (restart pending); Charmed operator revision 10008
postgresql-k8s/1   active    PostgreSQL 14.12 running (restart pending); Charmed operator revision 10008
postgresql-k8s/2   active    PostgreSQL 14.12 running; Charmed operator revision 10009
```
where `PostgreSQL 14.12` is replaced with the name & version of the workload(s) installed on that unit and `10009` and `10008` are replaced with the revision of the charm code on that unit

###### Example: Workload is not running before & during upgrade
These statuses are only applicable if the workload would also not be running if there was no upgrade in progress.

For example, MySQL Router will only run if its charm is related to a MySQL charm. If a MySQL Router charm—that is not related to a MySQL charm—is upgraded, these statuses would be shown.

Unit 2 has upgraded. Units 1 and 0 have not upgraded.
```
Unit                 Workload  Message
mysql-router-k8s/0*  waiting   Router 8.0.36; Charmed operator revision 10007 (restart pending)
mysql-router-k8s/1   waiting   Router 8.0.36; Charmed operator revision 10007 (restart pending)
mysql-router-k8s/2   waiting   Router 8.0.37; Charmed operator revision 10008
```
where `Router 8.0.37` and `Router 8.0.36` are replaced with the name & version of the workload(s) installed on that unit and `10008` and `10007` are replaced with the revision of the charm code on that unit

##### Machines
###### Example: Normal upgrade
Unit 2 has upgraded. Units 1 and 0 have not upgraded.
```
Unit           Workload  Message
postgresql/0*  active    PostgreSQL 14.11 running; Snap revision 20001 (outdated); Charmed operator revision 10008
postgresql/1   active    PostgreSQL 14.11 running; Snap revision 20001 (outdated); Charmed operator revision 10008
postgresql/2   active    PostgreSQL 14.12 running; Snap revision 20002; Charmed operator revision 10008
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10008` is replaced with the revision of the charm code on that unit

###### Example: Rollback
The user ran `juju refresh` to revision 10008. Units 2 and 1 upgraded from snap revision 20001 to 20002. Then, the user ran `juju refresh` to revision 10007. Unit 2 has rolled back to snap revision 20001.
```
Unit           Workload  Message
postgresql/0*  active    PostgreSQL 14.11 running; Snap revision 20001; Charmed operator revision 10007
postgresql/1   active    PostgreSQL 14.12 running; Snap revision 20002 (outdated); Charmed operator revision 10007
postgresql/2   active    PostgreSQL 14.11 running; Snap revision 20001; Charmed operator revision 10007
```
where `PostgreSQL 14.12` and `PostgreSQL 14.11` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10007` is replaced with the revision of the charm code on that unit

###### Example: Workload is not running before & during upgrade
These statuses are only applicable if the workload would also not be running if there was no upgrade in progress.

For example, MySQL Router will only run if its charm is related to a MySQL charm. If a MySQL Router charm—that is not related to a MySQL charm—is upgraded, these statuses would be shown.

Unit 2 has upgraded. Units 1 and 0 have not upgraded.
```
Unit             Workload  Message
mysql-router/0*  waiting   Router 8.0.36; Snap revision 20001 (outdated); Charmed operator revision 10008
mysql-router/1   waiting   Router 8.0.36; Snap revision 20001 (outdated); Charmed operator revision 10008
mysql-router/2   waiting   Router 8.0.37; Snap revision 20002; Charmed operator revision 10008
```
where `Router 8.0.37` and `Router 8.0.36` are replaced with the name & version of the workload(s) installed on that unit, `20002` and `20001` are replaced with the revision of the snap(s) installed on that unit, and `10008` is replaced with the revision of the charm code on that unit

## `force-upgrade-start` action
If the upgrade is incompatible or the automatic pre-upgrade health checks & preparations fail, the user will be prompted to rollback. If they accept potential data loss & downtime and want to proceed anyways (e.g. to force a downgrade), the user can run the `force-upgrade-start` action on the first unit to upgrade.

After `force-upgrade-start` is run and the first unit's workload upgrades (machines) or attempts to start (Kubernetes), the compatibility and pre-upgrade checks will not run again (unless the user runs `juju refresh` [and if `juju refresh` is a rollback, the pre-upgrade checks will still not run again]).

```yaml
# actions.yaml
force-upgrade-start:
  description: |
    Potential of *data loss* and *downtime*
    
    Force upgrade of first unit
    
    Must run with at least one of the ignore parameters `=true`
  params:
    ignore-compatibility-checks:
      type: boolean
      default: false
      description: |
        Potential of *data loss* and *downtime*
        
        Force upgrade if new version of PostgreSQL and/or charm is not compatible with previous version
    ignore-pre-upgrade-checks:
      type: boolean
      default: false
      description: |
        Potential of *data loss* and *downtime*
        
        Force upgrade if app is unhealthy or not ready to upgrade (and unit status shows "Pre-upgrade check failed")
  required: []
```
where `PostgreSQL` is replaced with the name of the workload(s)

### If action ran while upgrade not in progress
```
Action id 2 failed: No upgrade in progress
```

### If action ran on unit other than first unit to upgrade
```
Action id 2 failed: Must run action on unit 2
```
where `2` is replaced with the first unit to upgrade

### If action ran without ignore parameters
```
$ juju run postgresql-k8s/2 force-upgrade-start
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
Action id 2 failed: Must run with at least one of `ignore-compatibility-checks` or `ignore-pre-upgrade-checks` parameters `=true`
```

### If action ran with `ignore-compatibility-checks=true` and `ignore-pre-upgrade-checks=false`
#### If pre-upgrade health checks & preparations are successful
```
$ juju run postgresql-k8s/2 force-upgrade-start ignore-compatibility-checks=true
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:34 Skipping check for compatibility with previous PostgreSQL version and charm revision
12:15:34 Running pre-upgrade checks
12:15:39 Pre-upgrade checks successful
```
Kubernetes
```
12:15:39 PostgreSQL upgraded. Attempting to start PostgreSQL

result: Upgraded unit 2
```
Machines
```
12:15:39 Upgrading unit 2

result: Upgraded unit 2
```
where `PostgreSQL` is replaced with the name of the workload(s) and `2` is replaced with that unit (the first unit to upgrade)

#### If pre-upgrade health checks & preparations are not successful
```
$ juju run postgresql-k8s/2 force-upgrade-start ignore-compatibility-checks=true
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:34 Skipping check for compatibility with previous PostgreSQL version and charm revision
12:15:34 Running pre-upgrade checks

```
Kubernetes
```
Action id 2 failed: Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`. Pre-upgrade check failed: Backup in progress
```
Machines
```
Action id 2 failed: Rollback with `juju refresh`. Pre-upgrade check failed: Backup in progress
```

where `PostgreSQL` is replaced with the name of the workload(s), `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources), and `Backup in progress` is replaced with a message that is specific to the pre-upgrade health check or preparation that failed

### If action ran with `ignore-compatibility-checks=false` and `ignore-pre-upgrade-checks=true`
#### If compatibility checks were successful
```
$ juju run postgresql-k8s/2 force-upgrade-start ignore-pre-upgrade-checks=true
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:39 Skipping pre-upgrade checks
```
Kubernetes
```
12:15:39 PostgreSQL upgraded. Attempting to start PostgreSQL

result: Upgraded unit 2
```
Machines
```
12:15:39 Upgrading unit 2

result: Upgraded unit 2
```
where `2` is replaced with that unit (the first unit to upgrade)

#### If compatibility checks were not successful
compatibility checks run before pre-upgrade checks. It would be unusual for a user to run this command if compatibility checks were failing (and indicated as failing in unit status).
```
$ juju run postgresql-k8s/2 force-upgrade-start ignore-pre-upgrade-checks=true
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
```
Kubernetes
```
Action id 2 failed: Upgrade incompatible. Rollback by running `juju refresh postgresql-k8s --revision 10007 --resource postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6`
```
Machines
```
Action id 2 failed: Upgrade incompatible. Rollback with `juju refresh`
```
where `postgresql-k8s` is replaced with the Juju application name, `10007` is replaced with the original charm code revision, and `postgresql-image=ghcr.io/canonical/charmed-postgresql@sha256:76ef26c7d11a524bcac206d5cb042ebc3c8c8ead73fa0cd69d21921552db03b6` is replaced with the OCI image(s) in the original charm code [resources](https://juju.is/docs/sdk/charmcraft-yaml#heading--resources)

### If action ran with `ignore-compatibility-checks=true` and `ignore-pre-upgrade-checks=true`
```
$ juju run postgresql-k8s/2 force-upgrade-start ignore-compatibility-checks=true ignore-pre-upgrade-checks=true
Running operation 1 with 1 task
  - task 2 on unit-postgresql-k8s-2

Waiting for task 2...
12:15:39 Skipping check for compatibility with previous PostgreSQL version and charm revision
12:15:39 Skipping pre-upgrade checks
```
Kubernetes
```
12:15:39 PostgreSQL upgraded. Attempting to start PostgreSQL

result: Upgraded unit 2
```
Machines
```
12:15:39 Upgrading unit 2

result: Upgraded unit 2
```
where `PostgreSQL` is replaced with the name of the workload(s) and `2` is replaced with that unit (the first unit to upgrade)


## `resume-upgrade` action
After the user runs `juju refresh`, if `pause_after_unit_upgrade` is set to `all` or `first`, the upgrade will pause.

The user is expected to manually check that upgraded units are healthy and that clients connected to the upgraded units are healthy. For example, the user could check that the transactions per second, over a period of several days, are similar on upgraded and non-upgraded units. These manual checks supplement the automatic checks in the charm. (If the automatic checks fail, the charm will pause the upgrade regardless of the value of `pause_after_unit_upgrade`.)

When the user is ready to continue the upgrade, they should run the `resume-upgrade` action.

```yaml
# actions.yaml
resume-upgrade:
  description: |
    Upgrade next unit(s) (after you have manually verified that upgraded units are healthy)
    
    If the `pause_after_unit_upgrade` config is set to `all`, this action will upgrade the next unit.
    
    If `pause_after_unit_upgrade` is set to `first`, this action will upgrade all remaining units.
    Exception: if automatic health checks fail after a unit has upgraded, the upgrade will pause.
    
    If `pause_after_unit_upgrade` is set to `none`, this action will have no effect unless it is called with `ignore-health-of-upgraded-units` as `true`.
  params:
    ignore-health-of-upgraded-units:
      type: boolean
      default: false
      description: |
        Potential of *data loss* and *downtime*
        
        Force upgrade (of next unit) if 1 or more upgraded units are unhealthy
        
        WARNING: if first unit to upgrade is unhealthy, consider running `force-upgrade-start` action on that unit instead of using this parameter.
        If first unit to upgrade is unhealthy because compatibility checks or pre-upgrade checks are failing, this parameter is more destructive than the `force-upgrade-start` action.
  required: []
```

The user can also change the value of the `pause_after_unit_upgrade` config (e.g. from `all` to `none`) to resume the upgrade.

### Which unit the action is run on
#### Kubernetes
On Kubernetes, the user should run `resume-upgrade` on the leader unit.

If the StatefulSet partition is lowered and then quickly raised, the Juju agent may hang. This is a Juju bug: https://bugs.launchpad.net/juju/+bug/2073473. To avoid a race condition, only the leader unit lowers the partition. (If that bug were resolved, the `resume-upgrade` action could be run on any unit.)

To improve the robustness of rollbacks, `resume-upgrade` runs on the leader unit instead of the next unit to upgrade. If a unit is upgraded to an incorrect or buggy charm code version, its charm code may raise an uncaught exception and may not be able to process the `resume-upgrade` action to rollback its unit. (The improvement in robustness comes from `resume-upgrade` running on a unit that is different from the unit that needs to rollback.) This is different from machines, where the charm code is rolled back separately from the workload and the charm code on a unit needs to run to rollback the workload (i.e. snap) for that unit.

If the charm code on the leader unit raises an uncaught exception, the user can manually patch (e.g. using kubectl) the StatefulSet partition to rollback the leader unit (after `juju refresh` has been run to start the rollback). From the perspective of the upgrade design, if the user is instructed properly, this is safe (since it uses the same mechanism as a normal rollback). However, any rollback has risk and there may be additional risk if the leader unit did something (e.g. modified a relation databag in a previous Juju event) before it raised an uncaught exception.

#### Machines
On machines, the user should run `resume-upgrade` on the next unit to upgrade. This unit is shown in the app status.

This improves the robustness of rollbacks by requiring only the charm code on the unit that is rolling back to be healthy (i.e. not raising an uncaught exception). (If the action was run on the leader unit, rolling back a unit would require the charm code on both the leader unit & the unit rolling back to be healthy.)

If `ignore-health-of-upgraded-units=false` (default), a unit rolling back will also check that units that have already rolled back are healthy.

In case an upgraded unit is unhealthy and the user wants to force the upgrade to continue, `ignore-health-of-upgraded-units=true` allows the user to run this action on any unit that is not up-to-date—so that they can skip over the unhealthy unit. However, the user should be instructed to follow the upgrade order (usually highest to lowest unit number) even though they have the power to upgrade any unit that is not up-to-date.

### If action ran while upgrade not in progress
```
Action id 2 failed: No upgrade in progress
```

### If action ran on incorrect unit
#### Kubernetes
```
Action id 2 failed: Must run action on leader unit. (e.g. `juju run postgresql-k8s/leader resume-upgrade`)
```
where `postgresql-k8s` is replaced with the Juju application name

#### Machines
##### If action ran with `ignore-health-of-upgraded-units=false`
```
Action id 2 failed: Must run action on unit 1
```
where `1` is replaced with the next unit to upgrade

##### If action ran with `ignore-health-of-upgraded-units=true` and unit already up-to-date
```
Action id 2 failed: Unit already upgraded
```

### If action ran with `ignore-health-of-upgraded-units=false`
#### If `pause_after_unit_upgrade` is `none`
```
Action id 2 failed: `pause_after_unit_upgrade` config is set to `none`. This action is not applicable.
```

#### (Machines only) If first unit has not upgraded
(Upgrade is incompatible or automatic pre-upgrade health checks & preparations failed)
```
Action id 2 failed: Unit 2 is unhealthy. Upgrade will not resume.
```
where `2` is replaced with the first unit to upgrade

#### If 1 or more upgraded units are unhealthy
```
Action id 2 failed: Unit 2 is unhealthy. Upgrade will not resume.
```
where `2` is replaced with the first upgraded unit that is unhealthy

#### If upgrade is successfully resumed
##### If `pause_after_unit_upgrade` is `first`
###### Kubernetes
```
result: Upgrade resumed. Unit 1 is upgrading next
```
where `1` is replaced with the unit that is upgrading
###### Machines
```
12:15:39 Upgrade resumed. Upgrading unit 1

result: Upgrade resumed. Unit 1 has upgraded
```
where `1` is replaced with the unit that is upgrading (the unit the action ran on)

##### If `pause_after_unit_upgrade` is `all`
###### Kubernetes
```
result: Unit 1 is upgrading next
```
where `1` is replaced with the unit that is upgrading
###### Machines
```
12:15:39 Upgrading unit 1

result: Upgraded unit 1
```
where `1` is replaced with the unit that is upgrading (the unit the action ran on)

### If action ran with `ignore-health-of-upgraded-units=true` and upgrade is successfully resumed
#### Kubernetes
```
12:15:39 Ignoring health of upgraded units

result: Attempting to upgrade unit 1
```
where `1` is replaced with the unit that is upgrading

"Attempting to" is included because on Kubernetes we only control the partition, not which units upgrade. Kubernetes may not upgrade a unit even if the partition allows it (e.g. if the charm container of a higher unit is not ready).
#### Machines
```
12:15:39 Ignoring health of upgraded units
12:15:39 Upgrading unit 1

result: Upgraded unit 1
```
where `1` is replaced with the unit that is upgrading (the unit the action ran on)
