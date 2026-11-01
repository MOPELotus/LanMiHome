# Scoped Steam CDN DNS health pool

This optional router-side control-plane helper changes only one exact hostname's IPv4 DNS records. It does not force a Steam content provider, proxy download data, change WAN assignments, alter hosts files, or disable hardware offload. Other Steam CDN providers keep their normal fallback behavior.

Private `config.json` under `/lotusemmc/steam-cdn-health` specifies `domain`, `seed_ips`, an HTTPS DNS JSON `discovery_url`, and an optional `interval` (default 60 seconds). Do not commit actual device configuration or signed Steam download URLs. Candidate discovery is refreshed every five minutes. Public IPv4 addresses only; checks are limited to eight concurrent HEAD requests with a two-second timeout.

Two consecutive successful HTTP handshakes admit an address; one failure removes it. A 401/403 on the unsigned root URL only proves the frontend responded, not depot authorization or download correctness. Real signed manifests and multiple chunks must be validated separately through the native client. In particular, HTTP 403 for an actual authorized chunk is still a download failure.

Healthy candidates become exact records in a dedicated additional-hosts file. Its dnsmasq include is syntax-checked before replacement. The include's initial creation or removal requires a DNS service restart because SIGHUP does not reread dnsmasq configuration; later pool changes only reload the hosts file, without restarting DNS. Unchanged pools do nothing. If no candidate qualifies, the helper empties its hosts file so the existing resolver answers normally; it does not publish an authoritative empty address pool. The explicit restore action also removes the helper's include. Default local hosts TTL is retained; other DNS settings are unchanged. It does not guarantee throughput or solve every CDN error.

Deploy with a private backup and an independent rollback watchdog. Run `health.py once` twice to seed health, verify DNS answers contain all expected healthy candidates, then install/enable `steam-cdn-health.init`. On rollback, stop and disable the service and run `health.py restore`. No global DNS resolver changes are needed.

After deployment refresh the affected client's resolver cache. Steam may retain its own addresses or unavailable-source state; use a graceful restart only when required and authorized. Keep all partial downloads. Require at least ten minutes of native-client progress and no repeated batch failure before calling the change stable.

Tests: `python -m unittest discover -s tools/steam_cdn_health/tests -v`
