import argparse
import concurrent.futures
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


BASE = Path('/lotusemmc/homeproxy-tiers')
GENERATED = Path('/var/run/homeproxy/sing-box-c.json')
STATE = Path('/tmp/homeproxy-tiers-state.json')
EXCLUDED = re.compile(
    r'香港|澳门|澳門|台湾|臺灣|🇭🇰|🇲🇴|🇹🇼|'
    r'\b(?:HK|HKG|MO|MAC|TW|TWN|Taiwan|Macao|Macau|Hong\s*Kong)\b', re.I
)
METADATA = re.compile(r'剩余|流量|到期|过期|订阅|套餐|traffic|expire', re.I)


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def read_uci():
    result = subprocess.run(['uci', '-q', 'show', 'homeproxy'], check=True,
                            capture_output=True, text=True)
    sections = {}
    for row in result.stdout.splitlines():
        key, separator, value = row.partition('=')
        if not separator:
            continue
        parts = key.split('.')
        if len(parts) == 2:
            sections.setdefault(parts[1], {})['section_type'] = value
        elif len(parts) == 3:
            values = shlex.split(value)
            sections.setdefault(parts[1], {})[parts[2]] = values[0] if len(values) == 1 else values
    return sections


def multiplier(label):
    match = re.search(r'[×✕xX]\s*(\d+(?:\.\d+)?)\s*\)?\s*(?:\(NEW\))?\s*$', label, re.I)
    if match is None:
        match = re.search(r'\+\s*(\d+(?:\.\d+)?)\s*$', label)
    value = Decimal(match.group(1)) if match else Decimal('1')
    if value <= 0:
        raise ValueError('Nonpositive multiplier')
    return value


def eligible_nodes(sections):
    nodes = []
    for identity, node in sections.items():
        label = node.get('label', identity)
        if node.get('section_type') != 'node' or EXCLUDED.search(label) or METADATA.search(label):
            continue
        if node.get('type') not in ('vless', 'vmess', 'anytls', 'trojan', 'shadowsocks', 'hysteria2', 'tuic'):
            continue
        if str(node.get('port')) == '9999':
            continue
        nodes.append(dict(tag='cfg-' + identity + '-out', identity=identity,
                          label=label, cost=str(multiplier(label))))
    return sorted(nodes, key=lambda node: (Decimal(node['cost']), node['label']))


def tier_tag(cost):
    return 'lotus-rate-' + str(Decimal(cost).normalize()).replace('.', '_')


def prepare():
    sections = read_uci()
    if sections.get('config', {}).get('lotus_tiers_enabled') != '1':
        return
    nodes = eligible_nodes(sections)
    if not nodes:
        raise ValueError('No eligible nodes; refusing empty proxy pool')
    commands = ['set homeproxy.config.main_node=urltest',
                'set homeproxy.config.main_udp_node=same',
                'delete homeproxy.config.main_urltest_nodes']
    commands.extend('add_list homeproxy.config.main_urltest_nodes=' + node['identity'] for node in nodes)
    commands.append('commit homeproxy')
    subprocess.run(['uci', '-q', 'batch'], input='\n'.join(commands) + '\n',
                   text=True, check=True, capture_output=True)


def patch_config(config, nodes, secret, port):
    existing = {outbound['tag'] for outbound in config['outbounds']}
    if 'main-out' not in existing or any(node['tag'] not in existing for node in nodes):
        raise ValueError('Generated proxy pool does not match subscription')
    groups = {}
    for node in nodes:
        groups.setdefault(tier_tag(node['cost']), []).append(node['tag'])
    replacements = [dict(type='selector', tag=tag, outbounds=members,
                         default=members[0], interrupt_exist_connections=False)
                    for tag, members in groups.items()]
    main = dict(type='selector', tag='main-out', outbounds=list(groups),
                default=next(iter(groups)), interrupt_exist_connections=False)
    config['outbounds'] = [outbound for outbound in config['outbounds']
                           if outbound['tag'] != 'main-out'] + replacements + [main]
    experimental = config.setdefault('experimental', {})
    if experimental.get('clash_api'):
        raise ValueError('Existing Clash controller must be preserved explicitly')
    experimental['clash_api'] = dict(external_controller='127.0.0.1:' + str(port), secret=secret)
    return config


def finalize():
    sections = read_uci()
    if sections.get('config', {}).get('lotus_tiers_enabled') != '1':
        return
    nodes = eligible_nodes(sections)
    settings = json.loads((BASE / 'private.json').read_text())
    config = patch_config(json.loads(GENERATED.read_text()), nodes,
                          settings['secret'], settings['port'])
    candidate = GENERATED.with_suffix('.tiers-candidate')
    try:
        atomic_json(candidate, config)
        checked = subprocess.run(['sing-box', 'check', '-c', str(candidate)], capture_output=True)
        if checked.returncode:
            raise ValueError('Tiered sing-box configuration failed validation')
        os.replace(candidate, GENERATED)
        atomic_json(BASE / 'pool.json', dict(nodes=nodes))
    finally:
        candidate.unlink(missing_ok=True)


def api(settings, path, method='GET', payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request('http://127.0.0.1:' + str(settings['port']) + path,
                                     data=body, method=method,
                                     headers={'Authorization': 'Bearer ' + settings['secret'],
                                              'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=7) as response:
        content = response.read(1048576)
        return json.loads(content) if content else {}


def select_node(nodes, delays, previous):
    available = [node for node in nodes if node['tag'] in delays]
    if not available:
        return None
    candidate = min(available, key=lambda node: (Decimal(node['cost']), delays[node['tag']]))
    current = next((node for node in available if node['tag'] == previous.get('selected')), None)
    if current is None:
        return candidate
    if candidate['tag'] == current['tag']:
        return current
    if previous.get('streaks', {}).get(candidate['tag'], 0) < 2:
        return current
    if Decimal(candidate['cost']) < Decimal(current['cost']):
        return candidate
    if delays[candidate['tag']] + 50 < delays[current['tag']]:
        return candidate
    return current


def reconcile():
    sections = read_uci()
    if (sections.get('config', {}).get('lotus_tiers_enabled') != '1'
            or sections['config'].get('main_node') != 'urltest'):
        return
    settings = json.loads((BASE / 'private.json').read_text())
    nodes = json.loads((BASE / 'pool.json').read_text())['nodes']
    proxies = api(settings, '/proxies')['proxies']
    if proxies.get('main-out', {}).get('type', '').lower() != 'selector':
        return
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}

    def probe(node):
        query = urllib.parse.urlencode({'url': settings['probe_url'], 'timeout': 4000})
        try:
            result = api(settings, '/proxies/' + urllib.parse.quote(node['tag'], safe='') + '/delay?' + query)
            delay = result.get('delay')
            return node['tag'], delay if isinstance(delay, int) and delay > 0 else None
        except (OSError, ValueError, urllib.error.URLError):
            return node['tag'], None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        measurements = dict(pool.map(probe, nodes))
    delays = {tag: delay for tag, delay in measurements.items() if delay is not None}
    streaks = {node['tag']: min(previous.get('streaks', {}).get(node['tag'], 0) + 1, 100)
               if node['tag'] in delays else 0 for node in nodes}
    previous['streaks'] = streaks
    selected = select_node(nodes, delays, previous)
    if selected is not None:
        group = tier_tag(selected['cost'])
        api(settings, '/proxies/' + group, 'PUT', {'name': selected['tag']})
        api(settings, '/proxies/main-out', 'PUT', {'name': group})
    selection = selected['tag'] if selected else previous.get('selected')
    state = dict(updated_at=time.time(), selected=selection, healthy=bool(selected),
                 label=selected['label'] if selected else previous.get('label'),
                 cost=selected['cost'] if selected else previous.get('cost'),
                 streaks=streaks, delays=delays, candidates=len(nodes))
    atomic_json(STATE, state)
    if selection != previous.get('selected') or state['healthy'] != previous.get('healthy'):
        print(json.dumps(dict(event='tier_selection', label=state['label'],
                              cost=state['cost'], healthy=state['healthy']), ensure_ascii=False), flush=True)


def daemon():
    while True:
        started = time.monotonic()
        try:
            reconcile()
        except Exception as error:
            print(json.dumps(dict(event='tier_probe_error', error=type(error).__name__)), flush=True)
        time.sleep(max(5, 60 - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'finalize', 'daemon', 'once'])
    action = parser.parse_args().action
    {'prepare': prepare, 'finalize': finalize, 'daemon': daemon, 'once': reconcile}[action]()


if __name__ == '__main__':
    main()
