"""The tests, and the four mocks they run against.

    pip install mock-sap mock-edi mock-bank mock-einvoice
    python3 -m unittest discover -s tests -t . -v

Importing this package starts mock-sap, mock-edi, mock-bank and mock-einvoice,
each on a port the operating system chose, and stops them when the interpreter
exits. So the command above is the whole arrangement: nothing to start first,
and no port to keep free. In the mocks' own repositories the same tests needed
three servers started by hand on 8000, 8080 and 8090, and mock-edi and
mock-bank both default to 8080.

`SAP_URL`, `EDI_URL`, `BANK_URL` and `EINVOICE_URL` point the tests at mocks
that are already running. A mock whose URL is set is not started, which is how
a test is run against a mock's `main` rather than its release:

    python3 -m mocksap --port 8000 &          # from a mock-sap checkout
    SAP_URL=http://127.0.0.1:8000 python3 -m unittest discover -s tests -t .

**The bank starts at 16:00 on Friday 2 October 2026**, after its 15:00 cutoff,
and a reset returns it there. `test_payment_run` needs exactly that moment for
one test, and every other test moves the bank's clock itself. A bank started
by hand needs `--clock 2026-10-02T16:00`.

The test modules read the URLs when they are imported, which is after
this file has run, so setting `os.environ` here is enough.
"""
import atexit
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BANK_CLOCK = "2026-10-02T16:00"

# variable, module to run, and what to pass besides --port
MOCKS = (
    ("SAP_URL", "mocksap", ["-q"]),
    ("EDI_URL", "mockedi", ["-q"]),
    ("BANK_URL", "mockbank", ["-q", "--clock", BANK_CLOCK]),
    ("EINVOICE_URL", "mockeinvoice", ["-q"]),
)

# Long, because of mock-sap alone. `HTTPServer.server_bind` reverse-resolves the
# address it bound to, and on a host whose resolver is slow to say no - GitHub's
# macOS runners are - that lookup took more than 20 seconds before mock-sap
# 0.16.0 answered its first request. mock-edi and mock-bank skip the lookup and
# are up in well under a second. A mock that has exited is noticed at once, so
# the wait is only ever spent on one that is still coming.
STARTUP_SECONDS = 120

# How many times a mock is started again on a fresh port when the one it was
# given turned out to be taken.
PORT_ATTEMPTS = 5

# What each mock's `Server` header starts with. Every mock answers
# `/_mock/health`, so a 200 alone does not say which one answered; the bodies
# do not reliably either.
SERVERS = {
    "mocksap": "SAP NetWeaver Application Server (mock)",
    "mockedi": "mock-edi",
    "mockbank": "mock-bank",
    "mockeinvoice": "mock-einvoice",
}

_started = []
_handed_out = set()


def _free_port():
    """A port nothing is listening on, and not one already handed out in this
    run. Nothing listens on it until the mock binds, so the operating system can
    give the same port to the next call, and another process can take it in the
    meantime. Then the mock fails to start, and `_wait` notices."""
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _handed_out:
            _handed_out.add(port)
            return port


free_port = _free_port


class PortTaken(RuntimeError):
    """The mock exited, and something else is listening on its port."""


def _server(url):
    """The `Server` header of the health answer, or None if there was no 200."""
    try:
        with urllib.request.urlopen(url + "/_mock/health", timeout=2) as response:
            if response.status == 200:
                return response.headers.get("Server", "")
    except (urllib.error.URLError, OSError):
        pass
    return None


def _taken(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _wait(module, process, url, port):
    """Until the mock started as `process` answers. A 200 from something else on
    its port does not count: the process has to be the one answering, and still
    running afterwards."""
    expected = SERVERS.get(module, "")
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            if _taken(port):
                raise PortTaken("%s exited with %s because something else is listening on %d"
                                % (module, process.returncode, port))
            raise RuntimeError(
                "%s exited with %s before it answered. Is it installed? "
                "(pip install mock-sap mock-edi mock-bank mock-einvoice)" % (module, process.returncode))
        server = _server(url)
        if server is not None and server.startswith(expected) and process.poll() is None:
            return
        time.sleep(0.1)
    raise RuntimeError("%s did not answer %s/_mock/health within %d seconds"
                       % (module, url, STARTUP_SECONDS))


def _launch(module, arguments, port):
    process = subprocess.Popen(
        [sys.executable, "-m", module, "--port", str(port)] + list(arguments),
        stdout=subprocess.DEVNULL)
    _started.append(process)
    return process


def _up(module, arguments, port, process, chosen=False):
    """Waits for `process`, and returns the mock's URL. If its port was taken,
    starts it again on a fresh one - unless the port was `chosen` by the caller,
    who has told another mock of it."""
    for attempt in range(PORT_ATTEMPTS):
        url = "http://127.0.0.1:%d" % port
        try:
            _wait(module, process, url, port)
            return url
        except PortTaken:
            if chosen or attempt == PORT_ATTEMPTS - 1:
                raise
        port = _free_port()
        process = _launch(module, arguments, port)


def _stop():
    for process in _started:
        process.terminate()
    for process in _started:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    del _started[:]


def another(module, arguments, port=None):
    """One more mock, started differently from the shared one. Returns its URL.

    For a test that needs a mock configured at start-up - mock-edi with
    `--tax-rate`, say - which a running mock cannot be told afterwards. It is
    stopped with the rest when the interpreter exits.

    `port` is for two mocks that have to be told of each other when they
    start: take two from `free_port`, and start each naming the other's.
    """
    chosen = port is not None
    port = port or _free_port()
    arguments = ["-q"] + list(arguments)
    return _up(module, arguments, port, _launch(module, arguments, port), chosen)


def _start():
    atexit.register(_stop)
    waiting = []
    for variable, module, arguments in MOCKS:
        if os.environ.get(variable):
            continue
        port = _free_port()
        waiting.append((variable, module, arguments, port, _launch(module, arguments, port)))
    try:
        for variable, module, arguments, port, process in waiting:
            os.environ[variable] = _up(module, arguments, port, process)
    except Exception:
        _stop()
        raise


_start()
