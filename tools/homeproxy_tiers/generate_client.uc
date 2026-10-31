if (system('/usr/bin/python3 /lotusemmc/homeproxy-tiers/tiered_proxy.py prepare') !== 0)
    exit(1);
if (system('ucode -S /etc/homeproxy/scripts/generate_client.lotus-original.uc') !== 0)
    exit(1);
if (system('/usr/bin/python3 /lotusemmc/homeproxy-tiers/tiered_proxy.py finalize') !== 0)
    exit(1);
