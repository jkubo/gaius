# Storage — demo ops facts

- DRBD quorum is lost when a node reboots without a tiebreaker; restore by bringing the surviving replica up first, then rejoining the rebooted node.
- A DRBD split-brain after a node reboot requires discarding the outdated replica's data and forcing a resync from the up-to-date peer.
- When an S3 object store volume hits max capacity, writes are blocked and return errors until a new volume is allocated or old data is reclaimed.
- A CSI satellite pod stuck in CrashLoopBackOff is almost always caused by missing kernel headers on the host that match the running kernel version.
- PVC expansion only takes effect after the pod is restarted; the filesystem resize is online for the block device but the mount must be remounted.
- Postgres replica failover stalls when the replica node carries a taint that the postgres pod does not tolerate, leaving it unschedulable.
