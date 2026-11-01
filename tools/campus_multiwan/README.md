# Campus identity routing

This service supports the existing three-identity balance mode and an opt-in fixed-subnet mode.

Private device configuration stays on the router. Keep both identity records, set the unused record's `enabled` to `false`, map every client CIDR in `fixed_routes` to an enabled identity, and set `proxy_line` to the identity used by router-owned proxy sockets. Main and shared subnets must not overlap.

In fixed mode each subnet keeps its own identity; it does not borrow another line when authentication is unavailable. The service still renews DHCP and authenticates enabled secondary identities. Disabled identities have their DHCP process, routing rules and interface removed and are not recreated after reboot. Identity 1 uses the existing native WAN and authentication service.

HomeProxy integration must also honor `proxy_active`. Restrict HomeProxy interception to the main bridge when the shared bridge should remain direct on its own identity. Preserve existing hardware QoS and offload configuration.

Deploy with a private backup and a detached rollback watchdog. Refresh only affected Internet connection tracking after changing identity assignments; do not flush SSH, router management or unrelated connections. No global account logout is performed, so the upstream may retain a disabled identity's old session until it expires.

Tests: `python -m unittest discover -s tools/campus_multiwan/tests -v`

