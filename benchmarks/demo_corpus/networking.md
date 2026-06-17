# Networking — demo ops facts

- A VXLAN overlay MTU set too high causes cross-site packet timeouts; lower the overlay MTU below the path MTU to fix fragmentation drops.
- Restarting a CNI DaemonSet on every node at once triggers a cascade outage by dropping pod networking cluster-wide; delete one pod at a time instead.
- Cloudflare Tunnel falls back from QUIC to HTTP2 over WireGuard because UDP-in-UDP encapsulation breaks QUIC; force the HTTP2 protocol.
- A WireGuard overlay mesh assigns each node an IP from a fixed subnet; a join failure usually means the subnet range is exhausted or the route is missing.
- Pinning the ingress controller to a single low-latency node avoids cross-site latency for user-facing traffic; verify the pod lands on the pinned node after any change.
- DNS resolution inside the cluster breaks when CoreDNS loses its upstream; check the forward plugin and the upstream resolver reachability first.
