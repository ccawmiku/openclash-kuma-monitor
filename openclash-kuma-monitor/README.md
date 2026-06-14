# OpenClash Kuma Monitor

Small Docker stack for monitoring selected OpenClash/Mihomo nodes in Uptime Kuma.

It runs:

- Uptime Kuma on host port `12301`
- A checker container that:
  - creates one Uptime Kuma Push monitor per configured node
  - every 5 minutes selects the node in a temporary OpenClash policy group
  - tests `https://cp.cloudflare.com/generate_204` through OpenClash mixed port
  - retries 2 more times before marking the node down
  - pushes measured latency to Uptime Kuma

## Router Deployment

Create `/root/openclash-kuma-monitor/.env` from `.env.example`, then:

```sh
docker compose up -d
```

Open:

```text
http://192.168.1.1:12301
```

The checker can initialize the first Uptime Kuma user using `KUMA_USERNAME` and
`KUMA_PASSWORD`. Change the default password before exposing the panel.

## Notes

- The checker temporarily changes `TEST_GROUP` while it tests each node, then
  restores the original group. Use one dedicated group, for example
  `🧪 节点监控测试`, and include all nodes that should be monitored.
- Keep `CHECK_INTERVAL_SECONDS` moderate. The default is 300 seconds.
- The OpenClash API secret is read from `OPENCLASH_SECRET`.
