"""The server start-up function that the command line tool shares with `python3 server.py`.

`server.run_server` holds what the `__main__` block of `server.py` used to do. The old command must behave as before.
The tests that start a program use port 0 and read the address that the server announces.
"""
import json
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

import server


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
        data = Path(self.temp.name) / 'data'
        process, line = self.start(sys.executable, str(ROOT / 'server.py'), '--port', '0', '--data-dir', str(data))
        self.assertRegex(line, r'^Schola is ready at http://127\.0\.0\.1:[1-9][0-9]*\n$')
        port = int(line.rsplit(':', 1)[1])
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

    def test_run_server_announces_the_real_port_serves_and_returns_on_control_c(self):
        seen = []
        data = Path(self.temp.name) / 'data'

        def announce(address):
            seen.append(address)
            threading.Timer(0.5, __import__('_thread').interrupt_main).start()

        self.assertIsNone(server.run_server(0, data, announce))
        self.assertRegex(seen[0], r'^http://127\.0\.0\.1:[1-9][0-9]*$')
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', int(seen[0].rsplit(':', 1)[1])), timeout=2)

    def test_the_default_announcement_prints_the_old_line(self):
        with mock.patch('builtins.print') as printed:
            server.announce_ready('http://127.0.0.1:8765')
        printed.assert_called_once_with('Schola is ready at http://127.0.0.1:8765', flush=True)

    def test_run_server_makes_the_check_token_before_it_announces(self):
        created = []
        original = server.check_routes.session_for

        def spy(srv):
            created.append('session')
            return original(srv)

        def announce(address):
            created.append('announce')
            raise KeyboardInterrupt()

        made = []

        class Recording(server.HTTPServer):
            def __init__(self, *args):
                super().__init__(*args)
                made.append(self)

        with mock.patch.object(server.check_routes, 'session_for', spy), mock.patch.object(server, 'HTTPServer', Recording):
            server.run_server(0, Path(self.temp.name), announce)
        self.assertEqual(created, ['session', 'announce'])
        self.assertEqual(len(made), 1)

    def test_a_fault_in_announce_closes_the_socket_and_passes_the_fault_on(self):
        made = []

        class Recording(server.HTTPServer):
            def __init__(self, *args):
                super().__init__(*args)
                made.append(self)

        def announce(address):
            raise RuntimeError('announce failed')

        with mock.patch.object(server, 'HTTPServer', Recording):
            with self.assertRaises(RuntimeError):
                server.run_server(0, Path(self.temp.name), announce)
        self.assertEqual(made[0].socket.fileno(), -1)

    def test_a_fault_in_the_session_setup_closes_the_socket(self):
        made = []

        class Recording(server.HTTPServer):
            def __init__(self, *args):
                super().__init__(*args)
                made.append(self)

        with mock.patch.object(server, 'HTTPServer', Recording), mock.patch.object(server.check_routes, 'session_for', side_effect=RuntimeError('no session')):
            with self.assertRaises(RuntimeError):
                server.run_server(0, Path(self.temp.name), lambda address: None)
        self.assertEqual(made[0].socket.fileno(), -1)

    def test_a_busy_port_raises_an_os_error_marked_as_a_bind_fault(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen(1)
            with self.assertRaises(OSError) as raised:
                server.run_server(busy.getsockname()[1], Path(self.temp.name), lambda address: None)
        self.assertEqual(raised.exception.hub_phase, 'bind')

    def test_an_os_error_from_announce_is_not_marked_as_a_bind_fault(self):
        def announce(address):
            raise BrokenPipeError()

        with self.assertRaises(OSError) as raised:
            server.run_server(0, Path(self.temp.name), announce)
        self.assertFalse(hasattr(raised.exception, 'hub_phase'))

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
        self.assertIs(seen['closed'], True)


if __name__ == '__main__':
    unittest.main()
