import argparse
import concurrent.futures
from decimal import Decimal
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import ssl
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
    commands = prepare_commands(sections['config'], nodes)
    if not commands:
        return
    commands.append('commit homeproxy')
    subprocess.run(['uci', '-q', 'batch'], input='\n'.join(commands) + '\n',
                   text=True, check=True, capture_output=True)


def prepare_commands(settings, nodes):
    commands = []
    for key, value in [('main_node', 'urltest'), ('main_udp_node', 'same')]:
        if settings.get(key) != value:
            commands.append('set homeproxy.config.' + key + '=' + value)
    previous = settings.get('main_urltest_nodes', [])
    if isinstance(previous, str):
        previous = [previous]
    desired = [node['identity'] for node in nodes]
    if previous != desired:
        if previous:
            commands.append('delete homeproxy.config.main_urltest_nodes')
        commands.extend('add_list homeproxy.config.main_urltest_nodes=' + identity for identity in desired)
    return commands


def patch_config(config, nodes, secret, port, initial_selected=None, probe_base_port=34000):
    if not nodes:
        raise ValueError('Empty proxy pool')
    nodes = sorted(nodes, key=lambda node: (Decimal(node['cost']), node['label']))
    if not isinstance(probe_base_port, int) or not 1024 <= probe_base_port <= 65536 - len(nodes):
        raise ValueError('Invalid probe port range')
    existing = {outbound['tag'] for outbound in config['outbounds']}
    if 'main-out' not in existing or any(node['tag'] not in existing for node in nodes):
        raise ValueError('Generated proxy pool does not match subscription')
    groups = {}
    initial = next((node for node in nodes if node['tag'] == initial_selected), nodes[0])
    for node in nodes:
        groups.setdefault(tier_tag(node['cost']), []).append(node['tag'])
    replacements = [dict(type='selector', tag=tag, outbounds=members,
                         default=initial['tag'] if initial['tag'] in members else members[0],
                         interrupt_exist_connections=False)
                    for tag, members in groups.items()]
    main = dict(type='selector', tag='main-out', outbounds=list(groups),
                default=tier_tag(initial['cost']), interrupt_exist_connections=False)
    config['outbounds'] = [outbound for outbound in config['outbounds']
                           if outbound['tag'] != 'main-out'] + replacements + [main]
    probe_rules = []
    for index, node in enumerate(nodes):
        node['probe_port'] = probe_base_port + index
        inbound = 'lotus-probe-' + str(index)
        config.setdefault('inbounds', []).append(dict(type='http', tag=inbound,
                                                     listen='127.0.0.1', listen_port=node['probe_port']))
        probe_rules.append(dict(inbound=[inbound], action='route', outbound=node['tag']))
    route = config.setdefault('route', {})
    route['rules'] = probe_rules + route.get('rules', [])
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
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    config = patch_config(json.loads(GENERATED.read_text()), nodes,
                          settings['secret'], settings['port'],
                          previous.get('selected', settings.get('bootstrap_tag')),
                          settings.get('probe_base_port', 34000))
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


def chatgpt_response_reason(status, headers, body):
    text = body.decode('utf-8', errors='replace').lower()
    if any(marker in text for marker in ['unsupported_country_region_territory',
                                        'unsupported country', 'not available in your country',
                                        'country is not supported', 'ip has been blocked']):
        return 'REGION_OR_ACCESS_DENIED'
    if status >= 500:
        return 'HTTP_' + str(status)
    if (headers.get('cf-mitigated', '').lower() == 'challenge'
            or re.search(r'<title[^>]*>\s*(just a moment|attention required)', text)
            or 'id="challenge-form"' in text):
        return None
    if status in (204, 401, 403):
        return None
    if status != 200:
        return 'HTTP_' + str(status)
    if 'application/json' in headers.get('content-type', '').lower():
        try:
            json.loads(body)
            return None
        except (ValueError, UnicodeDecodeError):
            return 'NOT_JSON'
    if 'text/html' not in headers.get('content-type', '').lower():
        return 'NOT_HTML'
    if '<html' not in text or 'chatgpt' not in text or not any(marker in text for marker in
            ['chatgpt.com', '__next', '__reactrouter', 'openai']):
        return 'NOT_CHATGPT_PAGE'
    return None


def probe_node(node, settings, context):
    target = urllib.parse.urlsplit(settings['probe_url'])
    if target.scheme != 'https' or target.hostname != 'chatgpt.com' or target.port not in (None, 443):
        raise ValueError('Availability probe must target ChatGPT HTTPS')
    started = time.monotonic()
    connection = http.client.HTTPConnection('127.0.0.1', node['probe_port'], timeout=8)
    stream = None
    try:
        connection.set_tunnel(target.hostname, 443)
        connection.connect()
        stream = context.wrap_socket(connection.sock, server_hostname=target.hostname)
        connection.sock = None
        path = urllib.parse.urlunsplit(('', '', target.path or '/', target.query, ''))
        request = ('GET ' + path + ' HTTP/1.1\r\nHost: chatgpt.com\r\n'
                   'User-Agent: Mozilla/5.0\r\nAccept: text/html\r\n'
                   'Accept-Encoding: identity\r\nConnection: close\r\n\r\n')
        stream.sendall(request.encode())
        response = http.client.HTTPResponse(stream)
        response.begin()
        headers = {key.lower(): value for key, value in response.getheaders()}
        body = bytearray(response.read1(16384))
        reason = chatgpt_response_reason(response.status, headers, body)
        deadline = started + 15
        while (reason in ('NOT_CHATGPT_PAGE', 'NOT_JSON') or (
                response.status in (401, 403) and 'application/json' in headers.get('content-type', '').lower()
                and not json_body_complete(body))) and len(body) < 131072:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('ChatGPT response deadline')
            stream.settimeout(min(8, remaining))
            chunk = response.read1(min(65536, 131072 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            reason = chatgpt_response_reason(response.status, headers, body)
        delay = max(1, round((time.monotonic() - started) * 1000)) if reason is None else None
        observation = ('CF_CHALLENGE_REACHABLE' if headers.get('cf-mitigated') == 'challenge'
                       else 'HTTP_' + str(response.status) + '_REACHABLE') if reason is None else None
        return node['tag'], dict(delay=delay, reason=reason, observation=observation)
    except (OSError, http.client.HTTPException) as error:
        return node['tag'], dict(delay=None, reason=type(error).__name__)
    finally:
        if stream is not None:
            stream.close()
        connection.close()


def json_body_complete(body):
    try:
        json.loads(body)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


def locked_reconcile():
    import fcntl
    with Path('/var/lock/homeproxy-tiers.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        reconcile()


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
    current_group = proxies['main-out'].get('now')
    actual_selected = proxies.get(current_group, {}).get('now')
    actual_node = next((node for node in nodes if node['tag'] == actual_selected), None)
    if actual_node is not None:
        previous.update(selected=actual_selected, label=actual_node['label'], cost=actual_node['cost'])

    context = ssl.create_default_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        measurements = dict(pool.map(lambda node: probe_node(node, settings, context), nodes))
    delays = {tag: result['delay'] for tag, result in measurements.items() if result['delay'] is not None}
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
                 streaks=streaks, delays=delays, candidates=len(nodes),
                 failures={tag: result['reason'] for tag, result in measurements.items() if result['reason']},
                 responses={tag: result.get('observation') for tag, result in measurements.items()
                            if result['delay'] is not None}, criterion='chatgpt_backend_reachable')
    atomic_json(STATE, state)
    if selection != previous.get('selected') or state['healthy'] != previous.get('healthy'):
        print(json.dumps(dict(event='tier_selection', label=state['label'],
                              cost=state['cost'], healthy=state['healthy']), ensure_ascii=False), flush=True)


def daemon():
    while True:
        started = time.monotonic()
        try:
            locked_reconcile()
        except Exception as error:
            print(json.dumps(dict(event='tier_probe_error', error=type(error).__name__)), flush=True)
        time.sleep(max(5, 120 - (time.monotonic() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'finalize', 'daemon', 'once'])
    action = parser.parse_args().action
    {'prepare': prepare, 'finalize': finalize, 'daemon': daemon, 'once': locked_reconcile}[action]()


if __name__ == '__main__':
    main()
