if (system('ucode -S /etc/homeproxy/scripts/generate_client.lotus-multiwan-original.uc') !== 0)
    exit(1);
if (system('/usr/bin/python3 /lotusemmc/homeproxy-multiwan/integration.py finalize') !== 0)
    exit(1);
