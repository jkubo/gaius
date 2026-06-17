# Services and gitops — demo ops facts

- A CI runner that cannot acquire a PVC for its workspace stays pending; give it a storage class with a local-path provisioner for ephemeral build volumes.
- A Helm chart change must bump the chart version or the gitops reconciler caches the old manifests and never applies the update.
- etcd quorum loss on the control plane requires restoring from the latest snapshot on a single member, then re-adding the other members one at a time.
- A node reboot under a CI agent must drain and uncordon the node so in-flight jobs reschedule cleanly instead of being killed mid-build.
- A gitops kustomization stuck in a drift loop usually points at a field a controller mutates at runtime; mark that field as ignored in the sync config.
