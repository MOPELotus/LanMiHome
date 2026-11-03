import argparse
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading


BASE = Path('/lotusemmc/ssh-proxy')
SOCKET_MARK = 0x4200


def ssh_arguments(settings):
    alias = settings.get('alias', '')
    if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', alias):
        raise ValueError('Invalid SSH alias')
    local_port = settings.get('local_port')
    remote_port = settings.get('remote_port')
    if type(local_port) is not int or not 1024 <= local_port <= 65535:
        raise ValueError('Invalid local port')
    if type(remote_port) is not int or not 1 <= remote_port <= 65535:
        raise ValueError('Invalid remote port')
    return ['/usr/bin/ssh', '-F', '/root/.ssh/config', '-N', '-T',
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=20',
            '-o', 'ServerAliveCountMax=3', '-o',
            'ProxyCommand=/usr/bin/python3 /lotusemmc/ssh-proxy/tunnel.py relay %h %p',
            '-L', f'127.0.0.1:{local_port}:127.0.0.1:{remote_port}', alias]


def connect_marked(host, port):
    if not 1 <= port <= 65535:
        raise ValueError('Invalid destination port')
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last_error = None
    for family, kind, protocol, _, address in addresses:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_MARK', 36), SOCKET_MARK)
            connection.settimeout(10)
            connection.connect(address)
            connection.settimeout(None)
            return connection
        except OSError as error:
            last_error = error
            connection.close()
    if last_error is not None:
        raise last_error
    raise OSError('No SSH destination addresses')


def relay_stream(connection, input_fd=0, output_fd=1):
    def upload():
        try:
            while True:
                chunk = os.read(input_fd, 65536)
                if not chunk:
                    connection.shutdown(socket.SHUT_WR)
                    return
                connection.sendall(chunk)
        except OSError:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    worker = threading.Thread(target=upload, daemon=True)
    worker.start()
    while True:
        chunk = connection.recv(65536)
        if not chunk:
            return
        remaining = memoryview(chunk)
        while remaining:
            written = os.write(output_fd, remaining)
            if written <= 0:
                raise OSError('SSH output closed')
            remaining = remaining[written:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['daemon', 'relay'])
    parser.add_argument('host', nargs='?')
    parser.add_argument('port', nargs='?', type=int)
    arguments = parser.parse_args()
    if arguments.action == 'daemon':
        settings = json.loads((BASE / 'config.json').read_text())
        command = ssh_arguments(settings)
        os.execv(command[0], command)
    else:
        if not arguments.host or arguments.port is None:
            parser.error('relay requires host and port')
        with connect_marked(arguments.host, arguments.port) as connection:
            relay_stream(connection)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('ssh_proxy_error: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
