"""The tests, and the three mocks they run against.

    pip install mock-sap mock-edi mock-bank
    python3 -m unittest discover -s tests -t . -v

Importing this package starts mock-sap, mock-edi and mock-bank, each on a port
the operating system chose, and stops them when the interpreter exits. So the
command above is the whole arrangement: nothing to start first, and no port to
keep free. In the mocks' own repositories the same tests needed three servers
started by hand on 8000, 8080 and 8090, and mock-edi and mock-bank both default
to 8080.

`SAP_URL`, `EDI_URL` and `BANK_URL` point the tests at mocks that are already
running. A mock whose URL is set is not started, which is how a test is run
against a mock's `main` rather than its release:

    python3 -m mocksap --port 8000 &          # from a mock-sap checkout
    SAP_URL=http://127.0.0.1:8000 python3 -m unittest discover -s tests -t .

**The bank starts at 16:00 on Friday 2 October 2026**, after its 15:00 cutoff,
and a reset returns it there. `test_payment_run` needs exactly that moment for
one test, and every other test moves the bank's clock itself. A bank started
by hand needs `--clock 2026-10-02T16:00`.

The test modules read the three URLs when they are imported, which is after
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
)

STARTUP_SECONDS = 20

_started = []


def _free_port():
    """A port nothing is listening on. Another process could take it before the
    mock binds, and then the mock fails to start and `_wait` says so."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthy(url):
    try:
        with urllib.request.urlopen(url + "/_mock/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait(module, process, url):
    deadline = time.monotonic() + STARTUP_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "%s exited with %s before it answered. Is it installed? "
                "(pip install mock-sap mock-edi mock-bank)" % (module, process.returncode))
        if _healthy(url):
            return
        time.sleep(0.1)
    raise RuntimeError("%s did not answer %s/_mock/health within %d seconds"
                       % (module, url, STARTUP_SECONDS))


def _stop():
    for process in _started:
        process.terminate()
    for process in _started:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    del _started[:]


def _start():
    atexit.register(_stop)
    waiting = []
    for variable, module, arguments in MOCKS:
        if os.environ.get(variable):
            continue
        port = _free_port()
        process = subprocess.Popen(
            [sys.executable, "-m", module, "--port", str(port)] + arguments,
            stdout=subprocess.DEVNULL)
        _started.append(process)
        url = "http://127.0.0.1:%d" % port
        os.environ[variable] = url
        waiting.append((module, process, url))
    try:
        for module, process, url in waiting:
            _wait(module, process, url)
    except Exception:
        _stop()
        raise


_start()
