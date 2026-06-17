# Security — demo ops facts

- An eBPF tracing policy can capture process-exec and network events from sandboxed pods; load the policy before the workload starts or it misses early events.
- A BPF LSM hook on the control plane can deadlock the API server during the gRPC TLS handshake if the policy blocks a syscall the apiserver needs at startup.
- Vault secret rotation propagates to workloads only after the ExternalSecret refreshes and the consuming pod re-reads the mounted secret template.
- An OAuth2 proxy ForwardAuth middleware loops forever on a host that is missing its companion auth callback ingress; add the callback route to break the loop.
- RBAC denials on a service account usually trace to a missing RoleBinding in the workload's own namespace, not the cluster role itself.
- TLS certificate SAN mismatches block etcd peers from joining; the cert must list every peer address the node is reached by.
