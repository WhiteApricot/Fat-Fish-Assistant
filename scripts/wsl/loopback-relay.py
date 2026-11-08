"""Forward selected Windows loopback ports to WSL loopback using binary pipes."""
import argparse
import os
import socket
import socketserver
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path


def peer(port):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as target:
        target.settimeout(None)

        def upload():
            try:
                while data := os.read(0, 65536):
                    target.sendall(data)
                target.shutdown(socket.SHUT_WR)
            except OSError:
                pass

        threading.Thread(target=upload, daemon=True).start()
        while data := target.recv(65536):
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with subprocess.Popen(self.server.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as process:
            def upload():
                try:
                    while data := self.request.recv(65536):
                        process.stdin.write(data)
                        process.stdin.flush()
                except (OSError, ValueError):
                    pass
                finally:
                    process.stdin.close()

            threading.Thread(target=upload, daemon=True).start()
            try:
                while data := os.read(process.stdout.fileno(), 65536):
                    self.request.sendall(data)
            except OSError:
                pass
            finally:
                if process.poll() is None:
                    process.terminate()


def mapping(value):
    try:
        host, guest = (int(port) for port in value.split(':'))
        if 1024 <= host <= 65535 and 1024 <= guest <= 65535:
            return host, guest
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Use HOST_PORT:WSL_PORT with unprivileged ports')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--distro', default='Ubuntu')
    parser.add_argument('--map', type=mapping, action='append', required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Run the relay from Windows')
    wsl = ['wsl.exe', '-d', args.distro, '--exec']
    path = subprocess.check_output([*wsl, 'wslpath', '-a', Path(__file__).resolve().as_posix()],
                                   text=True, encoding='utf-8').strip()
    with ExitStack() as stack:
        servers = []
        for host, guest in args.map:
            server = stack.enter_context(socketserver.ThreadingTCPServer(('127.0.0.1', host), Relay))
            server.daemon_threads = True
            server.command = [*wsl, 'python3.12', '-B', path, '--peer', str(guest)]
            servers.append(server)
            print(f'Windows 127.0.0.1:{host} → WSL 127.0.0.1:{guest}', flush=True)
        try:
            for server in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
            threading.Event().wait()
        except KeyboardInterrupt:
            for server in servers:
                server.shutdown()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--peer':
        peer(int(sys.argv[2]))
    else:
        main()
