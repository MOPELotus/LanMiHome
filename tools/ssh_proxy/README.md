# Router SSH HTTP-proxy tunnel

Reuses an existing remote loopback HTTP proxy through a persistent SSH local
forward. No public proxy listener or remote-server configuration is created.
The tunnel carries TCP only; it is not a UDP VPN or a hardware-offloaded path.

## Private deployment

- Install the OpenSSH client, not just the Dropbear `ssh` compatibility symlink.
- Install `tunnel.py` under `/lotusemmc/ssh-proxy`, directory mode 700.
- Create private `config.json` there, mode 600, with `alias`, `local_port`, and
  `remote_port`. The alias names an existing `/root/.ssh/config` entry. Both
  ends of the HTTP forward bind to loopback only.
- Migrate only the explicitly approved SSH identity, aliases, and matching
  verified `known_hosts` entries. Directory mode 700; config, host keys, and
  private identity mode 600. Never copy the router administration identity
  merely because it exists on the workstation. Never commit actual settings,
  private keys, subscription URLs, or server credentials.
- Host verification remains strict. A changed or unknown host key requires
  independent verification; do not use `StrictHostKeyChecking=no`.
- Install `ssh-proxy.init` as `/etc/init.d/ssh-proxy`. Procd restarts failed
  tunnels indefinitely with a ten-second retry delay, including while campus
  authentication is unavailable after boot; SSH keepalives detect dead peers. Initial establishment is bounded
  by the SSH connection timeout.

The SSH transport uses a bounded stream relay with socket mark `0x4200` before
connect. Existing HomeProxy bypass and campus proxy-output policy recognize
this mark, avoiding interception of the SSH connection by its own proxy.
Resolve the SSH hostname through direct/bootstrap DNS, not through the tunnel
itself; add only the necessary SSH hostname to the direct-domain list.

## HomeProxy integration

Back up the live UCI settings and direct-domain list privately first. Create a
dedicated `http` node pointing at the router's loopback forward. Select it as
the main node and set UDP to the same node; this does not add UDP support.
Disable the price-tier selector while this manual node is active so that it
cannot overwrite the selection. Retain the existing fixed main/shared WAN
assignments, main-only interception, direct-domain bypass, and hardware
acceleration. Start the tunnel before re-enabling HomeProxy.

Validate the tunnel from the router first, then validate the generated
HomeProxy configuration and its actual selected outbound. Confirm domestic
direct traffic, protected SSH transport, shared-LAN bypass, and reboot state.
Never describe a ChatGPT verification challenge as successful account use.
Keep the workstation's existing tunnel intact until the user elects to stop it.

## Rollback

Stop HomeProxy if it was previously disabled, stop and disable `ssh-proxy`,
and restore only this deployment's saved HomeProxy/direct-domain settings.
Do not restore old three-WAN load balancing or overwrite unrelated changes.
Remove only the imported SSH files after confirming they are not used by
another router task. Leave remote-server proxy settings unchanged.

## Tests

`python -m unittest discover -s tools/ssh_proxy/tests -v`
