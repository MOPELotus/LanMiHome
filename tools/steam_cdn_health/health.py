import argparse
import concurrent.futures
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request


BASE = Path('/lotusemmc/steam-cdn-health')
RUNTIME = Path('/var/run/steam-cdn-health')
FRAGMENT = 'lotus-steam-health.conf'


def public_address(value):
    address = ipaddress.IPv4Address(value)
    if not address.is_global:
        raise ValueError('Candidate must be a public IPv4 address')
    return str(address)


def validate_config(config):
    domain = config['domain']
    if len(domain) > 253 or '.' not in domain or not all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in domain.split('.')):
        raise ValueError('Invalid exact CDN hostname')
    config['seed_ips'] = sorted({public_address(value) for value in config['seed_ips']})
    if not 1 <= len(config['seed_ips']) <= 32:
        raise ValueError('Expected 1 to 32 seed addresses')
    upstream = urllib.parse.urlsplit(config['discovery_url'])
    if upstream.scheme != 'https' or not upstream.hostname or upstream.username or upstream.password:
        raise ValueError('Discovery must use an HTTPS DNS JSON endpoint')
    if not 30 <= config.get('interval', 60) <= 600:
        raise ValueError('Invalid check interval')
    return config


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def discover(config):
    separator = '&' if '?' in config['discovery_url'] else '?'
    url = config['discovery_url'] + separator + urllib.parse.urlencode(
        {'name': config['domain'], 'type': 'A'})
    request = urllib.request.Request(url, headers={'Accept': 'application/dns-json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = json.loads(response.read(65536))
    addresses = set()
    for answer in data.get('Answer', []):
        if answer.get('type') == 1:
            try:
                addresses.add(public_address(answer['data']))
            except (ValueError, KeyError):
                continue
    return sorted(addresses)[:32]


def probe(domain, address):
    connection = http.client.HTTPConnection(address, 80, timeout=2)
    started = time.monotonic()
    try:
        connection.request('HEAD', '/', headers={'Host': domain, 'Connection': 'close'})
        response = connection.getresponse()
        return dict(ok=200 <= response.status < 500, status=response.status,
                    seconds=round(time.monotonic() - started, 3))
    except (OSError, http.client.HTTPException) as error:
        return dict(ok=False, error=type(error).__name__)
    finally:
        connection.close()


def update_health(previous, results):
    records = {}
    for address, result in results.items():
        prior = previous.get(address, {})
        successes = min(prior.get('successes', 0) + 1, 2) if result['ok'] else 0
        records[address] = dict(result, successes=successes, healthy=successes >= 2)
    return records


def dns_text(domain, addresses):
    return ''.join(f'host-record={domain},{public_address(address)},60\n'
                   for address in sorted(set(addresses)))


def config_directories():
    directories = set()
    for path in Path('/tmp/etc').glob('dnsmasq.conf.*'):
        for row in path.read_text().splitlines():
            if row.startswith('conf-dir='):
                directory = Path(row.split('=', 1)[1].split(',', 1)[0])
                if not directory.is_absolute():
                    raise ValueError('DNS directory must be absolute')
                directories.add(directory)
    if not directories:
        raise ValueError('No active dnsmasq directory')
    return sorted(directories)


def publish(domain, addresses, directories):
    content = dns_text(domain, addresses)
    changed = False
    for directory in directories:
        path = directory / FRAGMENT
        if not content:
            if path.exists():
                path.unlink()
                changed = True
            continue
        if path.exists() and path.read_text() == content:
            continue
        temporary = path.with_name(path.name + '.new')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(content)
            subprocess.run(['dnsmasq', '--test', '--conf-file=' + str(temporary)],
                           check=True, capture_output=True, timeout=10)
            os.replace(temporary, path)
            changed = True
        finally:
            temporary.unlink(missing_ok=True)
    if changed:
        subprocess.run(['/etc/init.d/dnsmasq', 'reload'], check=True,
                       capture_output=True, timeout=20)
    return changed


def once():
    config = validate_config(json.loads((BASE / 'config.json').read_text()))
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RUNTIME / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = RUNTIME / 'state.json'
        try:
            state = json.loads(state_path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        if state.get('domain') != config['domain'] or time.time() - state.get('updated_at', 0) > 180:
            state = {}
        candidates = set(config['seed_ips']) | set(state.get('discovered', []))
        if time.time() - state.get('discovered_at', 0) > 300:
            try:
                state['discovered'] = discover(config)
                state['discovered_at'] = time.time()
                candidates.update(state['discovered'])
            except Exception as error:
                state['discovery_error'] = type(error).__name__
        candidates = sorted({public_address(value) for value in candidates})[:64]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(candidates, pool.map(lambda address: probe(config['domain'], address), candidates)))
        records = update_health(state.get('records', {}), results)
        healthy = sorted(address for address, result in records.items() if result['healthy'])
        changed = publish(config['domain'], healthy, config_directories())
        state.update(domain=config['domain'], records=records, healthy=healthy, updated_at=time.time())
        atomic_json(state_path, state)
        print(json.dumps(dict(event='cdn_health', healthy=healthy, dns_changed=changed)), flush=True)
        return config.get('interval', 60)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['once', 'daemon', 'restore'])
    action = parser.parse_args().action
    if action == 'restore':
        publish('', [], config_directories())
        return
    while True:
        try:
            interval = once()
        except Exception as error:
            print(json.dumps(dict(event='cdn_health_error', error=type(error).__name__)), flush=True)
            if action == 'once':
                raise
            publish('', [], config_directories())
            interval = 60
        if action == 'once':
            return
        time.sleep(interval)


if __name__ == '__main__':
    main()
