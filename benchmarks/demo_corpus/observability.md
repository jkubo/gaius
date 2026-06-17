# Observability — demo ops facts

- An OTel collector that hits its memory limit silently drops metrics, which shows up as "no data" gaps in Grafana dashboards downstream.
- Prometheus remote-write to a long-term store fails quietly when the scrape interval is shorter than the remote write flush; align the intervals to stop sample backlog.
- A Grafana panel showing stale data after a scrape change is usually caused by the collector caching the old metric relabel config until restart.
- Alert rules that never fire often have a label selector that does not match the metric's actual labels; check the series labels before tuning thresholds.
