import concurrent.futures
import contextlib
import fcntl
import http.client
import http.cookiejar
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

BASE = Path('/lotusemmc/campus-multiwan')
RUNTIME = Path('/var/run/campus_multiwan')
CONFIG = BASE / 'config.json'
PORTAL_SOURCE = '/lotusemmc/campus-portal/files/campus_portal.py'
TABLE = 'campus_multiwan'
COMMENT = 'campus-multiwan-managed'
MARKS = {1: 0x4101, 2: 0x4102, 3: 0x4103}
STOP = threading.Event()
PORTAL = None


def event(message, **fields):
    print(json.dumps(dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'), event=message, **fields)), flush=True)


def run(arguments, check=True, timeout=12):
    result = subprocess.run(arguments, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(arguments[:6]) + ': ' + result.stderr[:200])
    return result


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def load_config():
    config = json.loads(CONFIG.read_text())
    for interface in [config['parent'], *config['lan_interfaces']]:
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', interface):
            raise ValueError('Invalid interface')
    if sorted(line['id'] for line in config['lines']) != [2, 3]:
        raise ValueError('Exactly two additional identities are required')
    interfaces = set()
    addresses = set()
    for line in config['lines']:
        if not re.fullmatch(r'cwan[23]', line['interface']):
            raise ValueError('Unexpected secondary interface')
        if not re.fullmatch(r'02(?::[0-9a-f]{2}){5}', line['mac']):
            raise ValueError('Expected fixed local unicast MAC')
        interfaces.add(line['interface'])
        addresses.add(line['mac'])
    if len(interfaces) != 2 or len(addresses) != 2:
        raise ValueError('Duplicate device identity')
    for key in ['clients', 'destinations']:
        if not config[key]:
            raise ValueError('Empty scope')
        for value in config[key]:
            ipaddress.IPv4Network(value, strict=False)
    enabled = {1}
    for line in config['lines']:
        if not isinstance(line.get('enabled', True), bool):
            raise ValueError('Enabled must be a boolean')
        if line.get('enabled', True):
            enabled.add(line['id'])
    fixed = config.get('fixed_routes', {})
    if not isinstance(fixed, dict):
        raise ValueError('Fixed routes must map client networks to identities')
    if fixed:
        if set(fixed) != set(config['clients']):
            raise ValueError('Fixed routes must cover exactly the configured clients')
        networks = [ipaddress.IPv4Network(value, strict=False) for value in fixed]
        if any(first.overlaps(second) for index, first in enumerate(networks)
               for second in networks[index + 1:]):
            raise ValueError('Fixed client networks must not overlap')
        if any(type(identity) is not int or identity not in enabled for identity in fixed.values()):
            raise ValueError('Fixed route targets a disabled or unknown identity')
    if 'proxy_line' in config and (type(config['proxy_line']) is not int
                                   or config['proxy_line'] not in enabled):
        raise ValueError('Invalid fixed proxy identity')
    return config


def portal_module():
    global PORTAL
    if PORTAL is None:
        specification = importlib.util.spec_from_file_location('campus_portal_existing', PORTAL_SOURCE)
        PORTAL = importlib.util.module_from_spec(specification)
        sys.modules[specification.name] = PORTAL
        specification.loader.exec_module(PORTAL)
    return PORTAL


def interface_connector(interface, address):
    def connect(target, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        error = None
        for family, kind, protocol, canonical, endpoint in socket.getaddrinfo(target[0], target[1], socket.AF_INET, socket.SOCK_STREAM):
            stream = socket.socket(family, kind, protocol)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    stream.settimeout(timeout)
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
                stream.bind((address, 0))
                stream.connect(endpoint)
                return stream
            except OSError as problem:
                error = problem
                stream.close()
        raise error or OSError('No IPv4 address')
    return connect


class BoundHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, interface, address):
        super().__init__()
        self.connector = interface_connector(interface, address)

    def http_open(self, request):
        def factory(host, **options):
            connection = http.client.HTTPConnection(host, **options)
            connection._create_connection = self.connector
            return connection
        return self.do_open(factory, request)


class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, interface, address):
        super().__init__()
        self.connector = interface_connector(interface, address)

    def https_open(self, request):
        def factory(host, **options):
            connection = http.client.HTTPSConnection(host, **options)
            connection._create_connection = self.connector
            return connection
        return self.do_open(factory, request)


def bound_client(interface, address):
    module = portal_module()
    client = module.HttpClient(5)
    def opener(redirect):
        handlers = [urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(client.jar), BoundHTTPHandler(interface, address), BoundHTTPSHandler(interface, address)]
        if not redirect:
            handlers.append(module.NoRedirect())
        return urllib.request.build_opener(*handlers)
    client.follow = opener(True)
    client.no_redirect = opener(False)
    return client


def line_config(config, identity):
    return next(line for line in config['lines'] if line['id'] == identity)


def policy_arguments(config, identity):
    return ['priority', str(10000 + identity), 'fwmark', hex(MARKS[identity]), 'lookup', str(21000 + identity)]


def source_arguments(identity, address):
    return ['priority', str(10100 + identity), 'from', address + '/32', 'lookup', str(21000 + identity)]


def set_rule(arguments, enabled):
    run(['ip', '-4', 'rule', 'del', *arguments], check=False)
    if enabled:
        run(['ip', '-4', 'rule', 'add', *arguments])


def lease_event(identity, action):
    config = load_config()
    line = line_config(config, identity)
    interface = line['interface']
    if os.environ.get('interface') != interface:
        raise ValueError('DHCP interface mismatch')
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RUNTIME / ('lease-' + str(identity) + '.lock')).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state_path = RUNTIME / ('lease-' + str(identity) + '.json')
        previous = read_json(state_path, {})
        if action not in ('deconfig', 'bound', 'renew', 'nak', 'leasefail'):
            return
        if action in ('nak', 'leasefail'):
            return
        if action == 'deconfig':
            state_path.unlink(missing_ok=True)
            run(['ip', '-4', 'addr', 'flush', 'dev', interface], check=False)
            run(['ip', '-4', 'route', 'flush', 'table', str(21000 + identity)], check=False)
            run(['ip', '-4', 'route', 'add', 'unreachable', 'default', 'metric', '32767', 'table', str(21000 + identity)], check=False)
            if previous.get('address'):
                set_rule(source_arguments(identity, previous['address']), False)
            return
        address = str(ipaddress.IPv4Address(os.environ['ip']))
        prefix = ipaddress.IPv4Network('0.0.0.0/' + os.environ.get('subnet', '255.255.0.0')).prefixlen
        network = str(ipaddress.IPv4Network(address + '/' + str(prefix), strict=False))
        gateway = str(ipaddress.IPv4Address(os.environ['router'].split()[0]))
        duration = int(os.environ.get('lease', '3600'))
        if previous.get('address') and previous['address'] != address:
            set_rule(source_arguments(identity, previous['address']), False)
            run(['ip', '-4', 'addr', 'del', previous['address'] + '/' + str(previous['prefix']), 'dev', interface], check=False)
            run(['ip', '-4', 'route', 'flush', 'table', str(21000 + identity)], check=False)
        run(['ip', '-4', 'addr', 'replace', address + '/' + str(prefix), 'dev', interface, 'noprefixroute'])
        run(['ip', '-4', 'route', 'replace', network, 'dev', interface, 'src', address, 'table', str(21000 + identity)])
        run(['ip', '-4', 'route', 'replace', 'default', 'via', gateway, 'dev', interface, 'metric', '10', 'table', str(21000 + identity)])
        run(['ip', '-4', 'route', 'replace', 'unreachable', 'default', 'metric', '32767', 'table', str(21000 + identity)])
        if previous.get('address') != address:
            set_rule(source_arguments(identity, address), True)
        atomic_json(state_path, dict(address=address, prefix=prefix, gateway=gateway, renewed_at=time.time(), expires_at=time.time() + duration))
        event('dhcp_lease', line=identity, address=address, duration=duration)


def reconcile_lease(config, line):
    identity = line['id']
    lease_path = RUNTIME / ('lease-' + str(identity) + '.json')
    lease = read_json(lease_path, {})
    if not lease or lease.get('expires_at', 0) <= time.time():
        return False
    interface = line['interface']
    address = lease['address']
    prefix = int(lease['prefix'])
    network = str(ipaddress.IPv4Network(address + '/' + str(prefix), strict=False))
    gateway = lease['gateway']
    run(['ip', '-4', 'addr', 'replace', address + '/' + str(prefix), 'dev', interface, 'noprefixroute'])
    run(['ip', '-4', 'route', 'replace', network, 'dev', interface, 'src', address, 'table', str(21000 + identity)])
    run(['ip', '-4', 'route', 'replace', 'default', 'via', gateway, 'dev', interface, 'metric', '10', 'table', str(21000 + identity)])
    set_rule(source_arguments(identity, address), True)
    return True

def nft_apply(source):
    result = subprocess.run(['nft', '-f', '-'], input=source, text=True, capture_output=True, timeout=12)
    if result.returncode:
        raise RuntimeError('nft transaction failed: ' + result.stderr[:300])


def nft_set(values, quote=False):
    return '{ ' + ', '.join(json.dumps(value) if quote else str(value) for value in values) + ' }'


def render_policy(config, active, exists):
    statements = ['delete table ip ' + TABLE] if exists else []
    statements.extend(['table ip ' + TABLE + ' {', ' chain classify {', ' type filter hook prerouting priority -90; policy accept;', ' iifname != ' + nft_set(config['lan_interfaces'], True) + ' return', ' ip saddr != ' + nft_set(config['clients']) + ' return', ' ip daddr != ' + nft_set(config['destinations']) + ' return', ' fib daddr type local return', ' ip daddr { 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } return', ' meta mark != 0 return', ' ct mark ' + nft_set([hex(mark) for mark in MARKS.values()]) + ' meta mark set ct mark return', ' ct mark != 0 return', ' ct state != new return', ' meta l4proto != { tcp, udp } return'])
    if config.get('diagnostic_ports'):
        for identity in active:
            start = 41000 + identity * 1000
            statements.append(' ip saddr 10.0.0.123 ip daddr 218.26.162.114 tcp sport ' + str(start) + '-' + str(start + 999) + ' tcp dport 51090 ct mark set ' + hex(MARKS[identity]) + ' meta mark set ct mark counter return')
    if config.get('fixed_routes'):
        for network, identity in sorted(config['fixed_routes'].items()):
            statements.append(' ip saddr ' + network + ' ct mark set ' + hex(MARKS[identity]) + ' meta mark set ct mark counter return')
    elif active:
        mapping = '{ ' + ', '.join(str(index) + ' : ' + hex(MARKS[identity]) for index, identity in enumerate(active)) + ' }'
        statements.append(' ct mark set numgen inc mod ' + str(len(active)) + ' map ' + mapping + ' meta mark set ct mark counter')
    statements.extend([' }', ' chain srcnat {', ' type nat hook postrouting priority 99; policy accept;'])
    for line in config['lines']:
        statements.append(' oifname ' + json.dumps(line['interface']) + ' ip saddr ' + nft_set(config['clients']) + ' counter masquerade')
    statements.extend([' }', '}'])
    return '\n'.join(statements) + '\n'


def firewall_handles(chain):
    result = run(['nft', '-j', '-a', 'list', 'chain', 'inet', 'fw4', chain], check=False)
    if result.returncode:
        return None
    return [entry['rule']['handle'] for entry in json.loads(result.stdout)['nftables'] if entry.get('rule', {}).get('comment') == COMMENT]


def ensure_forward(config):
    handles = firewall_handles('forward')
    if handles is None:
        raise RuntimeError('Main firewall unavailable')
    if not handles:
        rule = 'insert rule inet fw4 forward iifname ' + nft_set(config['lan_interfaces'], True) + ' oifname ' + nft_set([line['interface'] for line in config['lines']], True) + ' ip saddr ' + nft_set(config['clients']) + ' ct state { new, established, related } counter accept comment "' + COMMENT + '"\n'
        nft_apply(rule)


def cleanup_line(config, line):
    identity = line['id']
    pid_path = RUNTIME / ('dhcp-' + str(identity) + '.pid')
    if pid_path.exists():
        with contextlib.suppress(ValueError, FileNotFoundError, ProcessLookupError):
            pid = int(pid_path.read_text())
            command = Path('/proc/' + str(pid) + '/cmdline').read_bytes()
            if b'udhcpc' in command and line['interface'].encode() in command:
                os.kill(pid, signal.SIGTERM)
        pid_path.unlink(missing_ok=True)
    set_rule(policy_arguments(config, identity), False)
    lease = read_json(RUNTIME / ('lease-' + str(identity) + '.json'), {})
    if lease.get('address'):
        set_rule(source_arguments(identity, lease['address']), False)
    run(['ip', '-4', 'route', 'flush', 'table', str(21000 + identity)], check=False)
    run(['ip', 'link', 'del', line['interface']], check=False)
    (RUNTIME / ('lease-' + str(identity) + '.json')).unlink(missing_ok=True)


def cleanup(config):
    run(['nft', 'delete', 'table', 'ip', TABLE], check=False)
    for handle in firewall_handles('forward') or []:
        run(['nft', 'delete', 'rule', 'inet', 'fw4', 'forward', 'handle', str(handle)], check=False)
    for line in config['lines']:
        cleanup_line(config, line)
    event('network_cleanup_complete')


def setup_line(config, line):
    identity = line['id']
    interface = line['interface']
    existing = run(['ip', '-j', 'link', 'show', interface], check=False)
    if existing.returncode:
        run(['ip', 'link', 'add', 'link', config['parent'], 'name', interface, 'address', line['mac'], 'type', 'macvlan', 'mode', 'bridge'])
    elif json.loads(existing.stdout)[0]['address'] != line['mac']:
        raise RuntimeError('Unexpected interface owner')
    for setting, value in [('net.ipv6.conf.' + interface + '.disable_ipv6', '1'), ('net.ipv4.conf.' + interface + '.rp_filter', '0'), ('net.ipv4.conf.' + interface + '.arp_ignore', '1')]:
        run(['sysctl', '-w', setting + '=' + value])
    run(['ip', 'link', 'set', interface, 'up'])
    set_rule(policy_arguments(config, identity), True)
    run(['ip', '-4', 'route', 'replace', 'unreachable', 'default', 'metric', '32767', 'table', str(21000 + identity)])
    log = (RUNTIME / ('dhcp-' + str(identity) + '.log')).open('a')
    process = subprocess.Popen(['udhcpc', '-f', '-t', '3', '-T', '3', '-A', '15', '-i', interface, '-p', str(RUNTIME / ('dhcp-' + str(identity) + '.pid')), '-s', str(BASE / 'dhcp.sh')], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    log.close()
    return process


def probe_primary(config, credentials):
    data = json.loads(run(['ubus', 'call', 'network.interface.wan', 'status']).stdout)
    addresses = data.get('ipv4-address', [])
    if not data.get('up') or not addresses:
        return dict(id=1, online=False, error='WAN_DOWN')
    address = addresses[0]['address']
    active = portal_module().is_online(bound_client(config['parent'], address), credentials)
    return dict(id=1, online=active, address=address, interface=config['parent'])


def check_secondary(line, credentials, previous):
    identity = line['id']
    lease = read_json(RUNTIME / ('lease-' + str(identity) + '.json'), {})
    state = dict(id=identity, interface=line['interface'], mac=line['mac'], online=False, checked_at=time.time())
    if not lease or lease['expires_at'] <= time.time():
        return dict(state, error='WAITING_DHCP')
    address = lease['address']
    state['address'] = address
    module = portal_module()
    client = bound_client(line['interface'], address)
    if module.is_online(client, credentials):
        return dict(state, online=True, error=None, next_auth=0)
    if STOP.is_set():
        return state
    if previous.get('next_auth', 0) > time.time():
        return dict(state, error=previous.get('error'), next_auth=previous['next_auth'])
    settings = dict(credentials)
    settings['runtime_dir'] = str(RUNTIME / ('auth-' + str(identity)))
    for field in ['portal_base', 'wlan_user_ip', 'wlan_ac_name', 'wlan_ac_ip', 'ssid']:
        settings[field] = ''
    runtime = Path(settings['runtime_dir'])
    runtime.mkdir(mode=0o700, exist_ok=True)
    code = 'AUTH_FAILED'
    for attempt in range(3):
        if STOP.is_set():
            break
        try:
            context = module.discover(client, settings)
            if context.wlan_user_ip != address:
                raise RuntimeError('Portal address mismatch')
            with module.process_lock(runtime):
                module.authenticate(client, settings, context, runtime)
            active = module.is_online(client, settings)
            if active:
                event('line_authenticated', line=identity, address=address)
                return dict(state, online=True, error=None, next_auth=0)
        except module.PortalError as problem:
            code = problem.code
            if code not in ('E_OCR_UNCERTAIN', 'E_CAPTCHA_REJECTED', 'E_CAPTCHA_EXPIRED'):
                break
        except Exception as problem:
            code = type(problem).__name__
            break
    delay = 900 if code in ('E_PASSWORD_REJECTED', 'E_ACCOUNT_REJECTED') else 45
    event('line_auth_deferred', line=identity, code=code)
    return dict(state, error=code, next_auth=time.time() + delay)


def daemon():
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = load_config()
    module = portal_module()
    children = {}
    states = {}
    last_policy = None
    last_scope = None
    with (RUNTIME / 'daemon.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(RUNTIME / 'process.json', dict(pid=os.getpid(), started=time.time()))
        cleanup(config)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                while not STOP.is_set():
                    try:
                        config = load_config()
                        credentials = module.load_uci()
                        module.validate_config(credentials)
                        for line in config['lines']:
                            if not line.get('enabled', True):
                                child = children.pop(line['id'], None)
                                if child is not None and child.poll() is None:
                                    child.terminate()
                                    child.wait(timeout=5)
                                if Path('/sys/class/net', line['interface']).exists() or (RUNTIME / ('lease-' + str(line['id']) + '.json')).exists():
                                    cleanup_line(config, line)
                                states[line['id']] = dict(id=line['id'], interface=line['interface'], enabled=False, online=False)
                                continue
                            reconcile_lease(config, line)
                            child = children.get(line['id'])
                            if child is None or child.poll() is not None:
                                children[line['id']] = setup_line(config, line)
                        jobs = {1: pool.submit(probe_primary, config, credentials)}
                        for line in config['lines']:
                            if not line.get('enabled', True):
                                continue
                            jobs[line['id']] = pool.submit(check_secondary, line, credentials, states.get(line['id'], {}))
                        for identity, future in jobs.items():
                            try:
                                states[identity] = future.result(timeout=100)
                            except Exception as problem:
                                states[identity] = dict(id=identity, online=False, error=type(problem).__name__)
                        active = sorted(identity for identity, state in states.items() if state.get('online'))
                        scope = json.dumps([config['clients'], config['destinations'], config['lan_interfaces']])
                        if scope != last_scope:
                            for handle in firewall_handles('forward') or []:
                                run(['nft', 'delete', 'rule', 'inet', 'fw4', 'forward', 'handle', str(handle)], check=False)
                            last_scope = scope
                        ensure_forward(config)
                        exists = run(['nft', 'list', 'table', 'ip', TABLE], check=False).returncode == 0
                        signature = json.dumps([active, config])
                        if signature != last_policy or not exists:
                            nft_apply(render_policy(config, active, exists))
                            event('policy_updated', active=active, clients=config['clients'])
                            last_policy = signature
                        atomic_json(RUNTIME / 'status.json', dict(updated_at=time.time(), active=active, lines=states, clients=config['clients'], destinations=config['destinations'], diagnostic_ports=config.get('diagnostic_ports', False), fixed_routes=config.get('fixed_routes', {}), proxy_active=([config['proxy_line']] if config['proxy_line'] in active else []) if 'proxy_line' in config else active))
                    except Exception as problem:
                        event('supervisor_error', code=type(problem).__name__, detail=str(problem)[:240])
                        run(['nft', 'delete', 'table', 'ip', TABLE], check=False)
                        last_policy = None
                    STOP.wait(config.get('check_interval', 15))
        finally:
            STOP.set()
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            for child in children.values():
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=5)
            cleanup(config)
            atomic_json(RUNTIME / 'status.json', dict(updated_at=time.time(), active=[], stopped=True))


def main():
    os.umask(0o077)
    action = sys.argv[1]
    if action == 'daemon':
        for value in (signal.SIGTERM, signal.SIGINT):
            signal.signal(value, lambda signum, frame: STOP.set())
        daemon()
    elif action == 'dhcp':
        lease_event(int(sys.argv[2]), sys.argv[3])
    elif action == 'status':
        print(json.dumps(read_json(RUNTIME / 'status.json', {}), indent=2))
    elif action == 'cleanup':
        cleanup(load_config())
    elif action == 'check-config':
        print(json.dumps(load_config(), indent=2))


if __name__ == '__main__':
    main()
