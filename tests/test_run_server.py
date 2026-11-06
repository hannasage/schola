"""The server start-up function that the command line tool shares with `python3 server.py`.

`server.run_server` holds what the `__main__` block of `server.py` used to do. The old command must behave as before.
The tests that start a program use a free port.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import check_routes
import server


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class ServerCompatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def start(self, *arguments):
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.temp.name)
        self.addCleanup(self.finish, process)
        timer = threading.Timer(30, process.kill)
        timer.start()
        self.addCleanup(timer.cancel)
        return process, process.stdout.readline()

    @staticmethod
    def finish(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def stop(self, process):
        process.send_signal(signal.SIGINT)
        out, err = process.communicate(timeout=20)
        return process.returncode, out, err

    def test_the_old_command_still_starts_the_server_the_same_way(self):
        port = free_port()
        data = Path(self.temp.name) / 'data'
        process, line = self.start(sys.executable, str(ROOT / 'server.py'), '--port', str(port), '--data-dir', str(data))
        self.assertEqual(line, f'Schola is ready at http://127.0.0.1:{port}\n')
        connection = HTTPConnection('127.0.0.1', port, timeout=10)
        connection.request('GET', '/api/curricula')
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body[0]['id'], 'soccer-ml')
        code, out, err = self.stop(process)
        self.assertEqual((code, out, err), (0, '', ''))

    def test_the_old_command_keeps_its_defaults(self):
        probe = (
            'import http.server, json, runpy, sys\n'
            'class Fake:\n'
            '    def __init__(self, address, handler):\n'
            '        self.address, self.server_port = address, address[1]\n'
            '    def serve_forever(self):\n'
            '        print(json.dumps([list(self.address), str(self.data_dir)]))\n'
            '    def server_close(self):\n'
            '        pass\n'
            'http.server.HTTPServer = Fake\n'
            'sys.argv = ["server.py"]\n'
            'runpy.run_path("server.py", run_name="__main__")\n')
        done = subprocess.run([sys.executable, '-c', probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        address, data_dir = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual((address, data_dir), (['127.0.0.1', 8765], str(ROOT / 'data')))

    def test_the_old_command_still_refuses_a_bad_port_the_way_argparse_does(self):
        done = subprocess.run([sys.executable, str(ROOT / 'server.py'), '--port', 'abc'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)

    def test_run_server_announces_the_address_serves_and_returns_on_control_c(self):
        port = free_port()
        seen = []
        data = Path(self.temp.name) / 'data'

        def announce(address):
            seen.append(address)
            threading.Timer(0.5, __import__('_thread').interrupt_main).start()

        self.assertIsNone(server.run_server(port, data, announce))
        self.assertEqual(seen, [f'http://127.0.0.1:{port}'])
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=2)

    def test_run_server_makes_the_check_token_before_it_announces(self):
        created = []
        original = check_routes.session_for

        def spy(srv):
            created.append('session')
            return original(srv)

        def announce(address):
            created.append('announce')
            raise KeyboardInterrupt()

        with mock.patch.object(server.check_routes, 'session_for', spy):
            with self.assertRaises(KeyboardInterrupt):
                server.run_server(0, Path(self.temp.name), announce)
        self.assertEqual(created, ['session', 'announce'])

    def test_run_server_listens_on_the_local_address_only(self):
        seen = {}

        class Boom(Exception):
            pass

        class FakeServer:
            server_port = 1234

            def __init__(self, address, handler):
                seen['address'], seen['handler'] = address, handler

            def serve_forever(self):
                raise Boom()

            def server_close(self):
                seen['closed'] = True

        with mock.patch.object(server, 'HTTPServer', FakeServer):
            with self.assertRaises(Boom):
                server.run_server(1234, Path(self.temp.name), lambda address: None)
        self.assertEqual(seen['address'], ('127.0.0.1', 1234))
        self.assertIs(seen['handler'], server.Handler)


if __name__ == '__main__':
    unittest.main()
