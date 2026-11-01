import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time


BASE = Path('/lotusemmc/homeproxy-multiwan')
RUNTIME = Path('/var/run/campus_multiwan/status.json')
TABLE = 'lotus_proxy_multiwan'
SOCKET_MARK = 0x4200
PRIVATE = '0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3'
NETWORK_TYPES = {'direct', 'anytls', 'vless', 'vmess', 'trojan', 'shadowsocks',
                 'hysteria', 'hysteria2', 'tuic', 'socks', 'http', 'ssh', 'shadowtls'}


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, timeout=20, **kwargs)


def atomic_text(path, text):
    temporary = path.with_name(path.name + '.lotus-new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(text)
    os.replace(temporary, path)


def patch_config(config):
    count = 0
    for outbound in config.get('outbounds', []):
        if outbound.get('type') in NETWORK_TYPES:
            if outbound.get('bind_interface') or outbound.get('inet4_bind_address'):
                raise ValueError('Explicit outbound binding requires a manual migration')
            outbound['routing_mark'] = SOCKET_MARK
            count += 1
    if not count:
        raise ValueError('No network outbounds')
    for server in config.get('dns', {}).get('servers', []):
        if not server.get('detour') and server.get('type') in {'udp', 'tcp', 'tls', 'https', 'quic', 'h3'}:
            server['routing_mark'] = SOCKET_MARK
    return config


def direct_domains(text):
    domains = []
    for row in text.splitlines():
        domain = row.strip().lower().rstrip('.')
        if not domain or domain.startswith('#'):
            continue
        domain = domain.removeprefix('*.').removeprefix('.')
        if len(domain) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', domain):
            raise ValueError('Direct entry must be a DNS suffix, not a keyword or expression')
        if '.' not in domain or any(not label or len(label) > 63 or label.startswith('-')
                                    or label.endswith('-') for label in domain.split('.')):
            raise ValueError('Invalid direct DNS suffix')
        domains.append(domain)
    return sorted(set(domains))


def dnsmasq_text(domains):
    return ''.join('nftset=/' + domain + '/4#inet#fw4#lotus_direct4,6#inet#fw4#lotus_direct6\n'
                   for domain in domains)


def sync_dns():
    domains = direct_domains(Path('/etc/homeproxy/resources/direct_list.txt').read_text())
    directories = set()
    for config in Path('/tmp/etc').glob('dnsmasq.conf.*'):
        for row in config.read_text().splitlines():
            if row.startswith('conf-dir='):
                directories.add(Path(row.split('=', 1)[1].split(',', 1)[0]))
    if not directories:
        raise ValueError('Cannot locate active dnsmasq configuration')
    changed = False
    for directory in directories:
        path = directory / 'lotus-direct.conf'
        content = dnsmasq_text(domains)
        if not path.exists() or path.read_text() != content:
            directory.mkdir(parents=True, exist_ok=True)
            atomic_text(path, content)
            changed = True
    return changed


def finalize():
    path = Path('/var/run/homeproxy/sing-box-c.json')
    candidate = path.with_name('sing-box-c.lotus-candidate')
    try:
        config = patch_config(json.loads(path.read_text()))
        atomic_text(candidate, json.dumps(config, indent=2))
        run(['sing-box', 'check', '-c', str(candidate)])
        os.replace(candidate, path)
        sync_dns()
    finally:
        candidate.unlink(missing_ok=True)


def patch_template(source):
    marker = 'chain homeproxy_mangle_tun {\n'
    if 'set lotus_direct4 {' in source:
        return source
    if source.count(marker) != 1:
        raise ValueError('Unsupported HomeProxy firewall template')
    rules = ('\tmeta mark 0x4200 counter return\n'
             '\tip daddr @lotus_direct4 counter return\n'
             '\tip6 daddr @lotus_direct6 counter return\n')
    sets = ('\nset lotus_direct4 {\n type ipv4_addr\n flags timeout\n timeout 1h\n}\n'
            '\nset lotus_direct6 {\n type ipv6_addr\n flags timeout\n timeout 1h\n}\n')
    return source.replace(marker, marker + rules) + sets


def line_interfaces(status):
    interfaces = {}
    for identity, line in status.get('lines', {}).items():
        identity = int(identity)
        interface = line.get('interface', '')
        if identity not in (1, 2, 3) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', interface):
            raise ValueError('Unexpected campus interface')
        if line.get('address'):
            ipaddress.IPv4Address(line['address'])
        interfaces[identity] = interface
    if set(interfaces) != {1, 2, 3}:
        raise ValueError('Incomplete campus interface state')
    return interfaces


def render_policy(status, exists=False):
    interfaces = line_interfaces(status)
    active = sorted(set(status.get('active', [])))
    if any(identity not in interfaces for identity in active):
        raise ValueError('Unknown active identity')
    statements = ['delete table ip ' + TABLE] if exists else []
    statements.extend(['table ip ' + TABLE + ' {', ' chain output {',
                       ' type route hook output priority -140; policy accept;',
                       ' meta mark != 0x4200 return', ' fib daddr type local return',
                       ' ip daddr { ' + PRIVATE + ' } return',
                       ' meta l4proto != { tcp, udp } return'])
    for identity in (1, 2, 3):
        statements.append(f' ct mark {hex(0x4300 + identity)} meta mark set {hex(0x4100 + identity)} counter return')
    statements.extend([' ct mark != 0 return', ' ct state != new return'])
    if active:
        mapping = '{ ' + ', '.join(f'{index} : {hex(0x4300 + identity)}'
                                  for index, identity in enumerate(active)) + ' }'
        statements.append(f' ct mark set numgen inc mod {len(active)} map {mapping} counter')
        for identity in active:
            statements.append(f' ct mark {hex(0x4300 + identity)} meta mark set {hex(0x4100 + identity)} return')
    statements.extend([' }', ' chain srcnat {',
                       ' type nat hook postrouting priority 98; policy accept;'])
    for identity, interface in sorted(interfaces.items()):
        statements.append(f' oifname "{interface}" ct mark {hex(0x4300 + identity)} counter masquerade')
    statements.extend([' }', '}'])
    return '\n'.join(statements) + '\n'


def reconcile():
    status = json.loads(RUNTIME.read_text())
    if time.time() - status['updated_at'] > 120:
        raise ValueError('Campus health state is stale')
    signature = json.dumps([status['active'], line_interfaces(status)], sort_keys=True)
    exists = subprocess.run(['nft', 'list', 'table', 'ip', TABLE], capture_output=True).returncode == 0
    statefile = Path('/tmp/homeproxy-multiwan-state.json')
    previous = json.loads(statefile.read_text()) if statefile.exists() else {}
    if not exists or previous.get('signature') != signature:
        source = render_policy(status, exists)
        run(['nft', '-c', '-f', '-'], input=source, text=True)
        run(['nft', '-f', '-'], input=source, text=True)
        atomic_text(statefile, json.dumps(dict(signature=signature, active=status['active'], updated_at=time.time())))
        print(json.dumps(dict(event='router_output_policy', active=status['active'])), flush=True)


def daemon():
    last_dns_check = 0
    while True:
        try:
            reconcile()
            if time.monotonic() - last_dns_check > 30:
                if sync_dns():
                    run(['/etc/init.d/dnsmasq', 'reload'])
                last_dns_check = time.monotonic()
        except Exception as error:
            print(json.dumps(dict(event='multiwan_error', error=type(error).__name__)), flush=True)
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['finalize', 'daemon', 'once'])
    action = parser.parse_args().action
    {'finalize': finalize, 'daemon': daemon, 'once': reconcile}[action]()


if __name__ == '__main__':
    main()
