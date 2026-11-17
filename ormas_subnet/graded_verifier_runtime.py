"""Bounded OCI verifier primitive, also embedded into prepared client commands.

Standard library only. No worker/model/queue authority. The caller binds the exact
source, image, locks and command into the packet. Candidate and Git stay outside
containers; a private tmpfs volume holds all installation and test output.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit


# --- graded plain-command mode: begin pytest-profiles ---
_PYTEST_PROFILE_IDS = frozenset({'linux-python-pytest-v1', 'linux-python-pytest-git-v1'})

# --- graded plain-command mode: end pytest-profiles ---
class RuntimeRefusal(Exception):
    pass


# Rootless daemons may start every container process with umask 0.
UMASK_ARGV = ['/bin/sh', '-c', 'umask 022 && exec "$@"', 'ormas-umask']


# These helpers live in the client-owned driver, never in the candidate. The
# socket is mounted only there. Requests can execute only in the named candidate
# container; neither host commands nor driver paths are accepted by the bridge.
PYTHON_ACCEPTANCE_HELPER = '''"""Ormas external acceptance: observe the isolated candidate."""
import base64, json, socket
def run(argv, timeout_s=30):
    request = json.dumps({"argv": argv, "timeout_s": timeout_s}).encode() + b"\\n"
    if len(request) > 65536:
        raise RuntimeError("candidate_request_limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(timeout_s + 5)
        channel.connect("/ormas/bridge/socket")
        channel.sendall(request)
        raw = bytearray()
        while not raw.endswith(b"\\n"):
            block = channel.recv(65536)
            if not block or len(raw) + len(block) > 1500000:
                raise RuntimeError("candidate_bridge_unavailable")
            raw.extend(block)
    result = json.loads(raw)
    if "error" in result:
        raise RuntimeError(result["error"])
    return {"returncode": result["returncode"], "output": base64.b64decode(result["output"]).decode("utf-8", "replace")}
'''

# --- graded plain-command mode: begin driver ---
# DRAFT Graded compiler, not the served Verified compiler.
CANDIDATE_PYTEST_DRIVER = '''import json, sys
sys.path.insert(0, "/ormas/helpers")
from ormas_acceptance import run
result = run(json.loads(sys.argv[1]), timeout_s=int(sys.argv[2]))
# JSON escaping can expand one non-BMP character to twelve bytes.
tail_chars = max(0, (int(sys.argv[3]) - 128) // 12)
result["output"] = result["output"][-tail_chars:] if tail_chars else ""
print(json.dumps(result, separators=(",", ":")))
raise SystemExit(0 if result["returncode"] == 0 else 1 if result["returncode"] == 1 else 2)
'''

# Card 8f2d6e06: the candidate run completed and its own well-formed report (or
# the bridge's output cap) shows errors, not assertions. Distinct from 86 so the
# seat can attribute it only after a green base run of the same command; the
# reason line is the one consumers already read for these outcomes.
CANDIDATE_ERROR_EXIT = 87


def candidate_error(reason):
    sys.stderr.write('ormas-oci-verifier-v1: ' + reason + '\n')
    sys.stderr.flush()
    return CANDIDATE_ERROR_EXIT


def candidate_error_report(raw, returncode):
    """Candidate evidence in a well-formed pytest JUnit report, read with pytest's own exit status.

    pytest 8.4.2 (the lock): 0 with every executed test skipped; 1 with
    non-assertion failures, setup/teardown errors or strict XPASS; 2 (interrupted)
    only with a recorded collection error; 5 (no tests collected) with no
    executed test. Interrupted collection writes the same zero-test report as
    "no tests" and an interrupted run leaves a nameless <testcase/>; neither is
    candidate evidence. A truncated, entity-bearing or foreign document is not one.
    """
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        return False
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return False
    suites = [root] if root.tag == 'testsuite' else list(root) if root.tag == 'testsuites' else []
    if not suites or any(suite.tag != 'testsuite' or any(
            not (suite.get(key) or '').isdigit() for key in ('tests', 'errors', 'failures', 'skipped'))
            for suite in suites):
        return False
    cases = list(root.iter('testcase'))
    if any(not case.get('name') for case in cases) or structured_result('junit-file', raw) != 'setup':
        return False
    skipped = all(case.find('skipped') is not None for case in cases)
    if returncode == 0:
        return bool(cases) and skipped
    if returncode == 1:
        return any(case.find('failure') is not None or case.find('error') is not None for case in cases)
    if returncode == 2:
        return any(case.find('error') is not None for case in cases)
    if returncode == 5:
        return skipped
    return False

# --- graded plain-command mode: end driver ---
NODE_ACCEPTANCE_HELPER = '''"use strict";
const net = require("node:net");
exports.run = (argv, timeout_s = 30) => new Promise((resolve, reject) => {
  const request = JSON.stringify({argv, timeout_s}) + "\\n";
  if (Buffer.byteLength(request) > 65536) return reject(new Error("candidate_request_limit"));
  const socket = net.createConnection("/ormas/bridge/socket");
  let raw = Buffer.alloc(0);
  socket.setTimeout((timeout_s + 5) * 1000, () => socket.destroy(new Error("candidate_bridge_timeout")));
  socket.on("error", reject);
  socket.on("connect", () => socket.write(request));
  socket.on("data", chunk => {
    raw = Buffer.concat([raw, chunk]);
    if (raw.length > 1500000) return socket.destroy(new Error("candidate_response_limit"));
    if (raw[raw.length - 1] !== 10) return;
    socket.end();
    try {
      const result = JSON.parse(raw.toString("utf8"));
      if (result.error) return reject(new Error(result.error));
      resolve({returncode: result.returncode, output: Buffer.from(result.output, "base64").toString("utf8")});
    } catch (error) { reject(error); }
  });
  socket.on("end", () => { if (!raw.length || raw[raw.length - 1] !== 10) reject(new Error("candidate_bridge_unavailable")); });
});
'''


NODE_HTTP_HELPER = NODE_ACCEPTANCE_HELPER + r'''
const {test} = require("/work/node_modules/@playwright/test");
const origin = "http://ormas-app.invalid";
const hop = new Set(["host","connection","content-length","transfer-encoding","upgrade",
  "proxy-authorization","proxy-authenticate","keep-alive","te","trailer"]);
let contexts = [], refusals = [], pageGuards = [];
const activeRoutes = new Set();
const guardedPages = new WeakMap();
function rpc(body) {
  return new Promise((resolve, reject) => {
    const request = JSON.stringify(body) + "\n";
    if (Buffer.byteLength(request) > 65536) return reject(new Error("candidate_request_limit"));
    const socket = net.createConnection("/ormas/bridge/socket");
    let raw = Buffer.alloc(0);
    socket.setTimeout(35000, () => socket.destroy(new Error("candidate_bridge_timeout")));
    socket.on("error", reject);
    socket.on("connect", () => socket.write(request));
    socket.on("data", chunk => {
      raw = Buffer.concat([raw, chunk]);
      if (raw.length > 1500000) return socket.destroy(new Error("candidate_response_limit"));
      if (raw[raw.length - 1] !== 10) return;
      socket.end();
      try {
        const result = JSON.parse(raw.toString("utf8"));
        if (result.error) reject(new Error(result.error));
        else resolve(result);
      } catch (error) { reject(error); }
    });
    socket.on("end", () => {
      if (!raw.length || raw[raw.length - 1] !== 10) reject(new Error("candidate_bridge_unavailable"));
    });
  });
}
function refuse(reason) {
  const report = rpc({refusal: reason}).catch(() => {});
  refusals.push(report);
  return report;
}
function supportedURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" && url.origin === origin;
  } catch (_) { return false; }
}
test.afterEach(async () => {
  await Promise.all(pageGuards);
  while (activeRoutes.size) await Promise.allSettled([...activeRoutes]);
  for (const context of contexts) {
    for (const page of context.pages()) {
      for (const frame of page.frames()) {
        if (!supportedURL(frame.url()))
          await refuse("unsupported_browser_navigation");
      }
    }
    await context.close();
  }
  await Promise.all(refusals);
  contexts = []; refusals = []; pageGuards = [];
});
function guardPage(context, page) {
  if (guardedPages.has(page)) return guardedPages.get(page);
  const setup = (async () => {
    const session = await context.newCDPSession(page);
    await session.send("Page.enable");
    for (const event of ["Page.frameScheduledNavigation", "Page.frameRequestedNavigation",
      "Page.frameStartedNavigating", "Page.windowOpen"]) {
      session.on(event, value => {
        if (value.url && !supportedURL(value.url))
          void refuse("unsupported_browser_navigation");
      });
    }
    page.on("framenavigated", frame => {
      if (!supportedURL(frame.url()))
        void refuse("unsupported_browser_navigation");
    });
    page.on("download", () => { void refuse("unsupported_browser_download"); });
  })().catch(async () => { await refuse("candidate_http_bridge_failed"); });
  guardedPages.set(page, setup); pageGuards.push(setup);
  return setup;
}
exports.openApp = async browser => {
  if (browser.contexts().length) {
    await refuse("unsupported_browser_navigation");
    throw new Error("openApp requires the browser fixture without an existing page/context");
  }
  const context = await browser.newContext({
    serviceWorkers: "block", acceptDownloads: false, baseURL: origin
  });
  contexts.push(context);
  context.setDefaultTimeout(10000);
  context.setDefaultNavigationTimeout(15000);
  context.on("page", page => { void guardPage(context, page); });
  await context.routeWebSocket(/.*/, async ws => {
    await refuse("unsupported_browser_websocket");
    ws.close({code: 1008, reason: "unsupported in this profile"});
  });
  async function observeRoute(route) {
    const request = route.request();
    if (!supportedURL(request.url())) {
      await refuse("unsupported_browser_origin");
      return route.abort();
    }
    try {
      const url = new URL(request.url()), headers = {};
      for (const [key, value] of Object.entries(await request.allHeaders())) {
        if (!hop.has(key) && key !== "accept-encoding") headers[key] = value;
      }
      const observed = await rpc({http: {
        method: request.method(), path: url.pathname + url.search, headers,
        body: (request.postDataBuffer() || Buffer.alloc(0)).toString("base64")
      }, timeout_s: 30});
      await route.fulfill({status: observed.http.status, headers: observed.http.headers,
        body: Buffer.from(observed.http.body, "base64")});
    } catch (_) {
      await refuse("candidate_http_bridge_failed");
      await route.abort().catch(() => {});
    }
  }
  await context.route(/.*/, route => {
    const observation = observeRoute(route);
    activeRoutes.add(observation);
    void observation.finally(() => activeRoutes.delete(observation)).catch(() => {});
    return observation;
  });
  const page = await context.newPage();
  await guardPage(context, page);
  await page.goto(origin);
  return page;
};
'''

# The candidate cannot replace this core-only program, executable, argv or pipe.
# It runs under a separate UID in the candidate's network namespace, with cwd=/.
NODE_HTTP_PROBE = r'''
"use strict";
const http = require("node:http");
const cfg = JSON.parse(Buffer.from(process.argv[1], "base64").toString("utf8"));
let finished = false;
function finish(value) {
  if (finished) return;
  finished = true;
  process.stdout.write(JSON.stringify(value) + "\n");
}
function fail(reason) { finish({error: reason}); }
const request = http.request({
  host: "127.0.0.1", port: cfg.port, path: cfg.path, method: cfg.method,
  maxHeaderSize: 16384, agent: false,
  headers: {...cfg.headers, host: "ormas-app.invalid", "accept-encoding": "identity",
    connection: "close", "content-length": Buffer.from(cfg.body, "base64").length}
}, response => {
  const chunks = [], headers = {};
  let size = 0;
  const hop = new Set(["host","connection","content-length","transfer-encoding","upgrade",
    "proxy-authorization","proxy-authenticate","keep-alive","te","trailer"]);
  for (let i = 0; i < response.rawHeaders.length; i += 2) {
    const key = response.rawHeaders[i].toLowerCase(), value = response.rawHeaders[i+1];
    if (hop.has(key)) continue;
    if (key in headers && key !== "set-cookie") {
      fail("candidate_http_duplicate_header"); response.destroy(); return;
    }
    headers[key] = key in headers ? headers[key] + "\n" + value : value;
  }
  response.on("data", chunk => {
    size += chunk.length;
    if (size > cfg.cap) {
      fail("candidate_http_response_limit"); response.destroy(); return;
    }
    chunks.push(chunk);
  });
  response.on("end", () => {
    if (!response.complete) return fail("candidate_http_response_incomplete");
    finish({status: response.statusCode, headers, body: Buffer.concat(chunks).toString("base64")});
  });
  response.on("error", () => fail("candidate_http_response_incomplete"));
  response.on("aborted", () => fail("candidate_http_response_incomplete"));
});
request.on("upgrade", (_, socket) => { fail("candidate_http_upgrade_unsupported"); socket.destroy(); });
request.on("error", error => fail(error.code === "ECONNREFUSED" ?
  "candidate_http_connection_refused" : "candidate_http_transport_failed"));
request.setTimeout(cfg.timeout_ms, () => { fail("candidate_http_timeout"); request.destroy(); });
const deadline = setTimeout(() => {
  fail("candidate_http_timeout"); request.destroy();
}, cfg.timeout_ms);
deadline.unref();
request.end(Buffer.from(cfg.body, "base64"));
'''

_CLEANUP_GUARD = r'''
import json,os,shutil,subprocess,sys,time
parent,seconds,scratch,containers,volumes=json.loads(sys.argv[1])
deadline=time.monotonic()+seconds
while time.monotonic()<deadline:
    try: os.kill(parent,0)
    except ProcessLookupError: break
    time.sleep(.2)
for argv in (['docker','rm','-f',*containers],['docker','volume','rm',*volumes]):
    try: subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    except (OSError,subprocess.TimeoutExpired): pass
if os.path.isdir(scratch):
    for root,dirs,files in os.walk(scratch,topdown=True,followlinks=False):
        try: os.chmod(root,0o700)
        except OSError: pass
    shutil.rmtree(scratch,ignore_errors=True)
'''

# A candidate call whose process spawn failed (a program the image does not
# provide, or an OS refusal) shows pytest's failing frame inside the standard
# library's subprocess.py raising the OSError. The error type is tied to that
# frame: in the long/auto/line tracebacks it follows the frame on the same
# line, in the short traceback its ``E`` line follows the frame's source. A
# test that itself raises FileNotFoundError on a data file, or a
# CalledProcessError beside another test's unrelated OSError, is a plain red.
_SPAWN_TYPES = rb'(?:FileNotFoundError|PermissionError|BlockingIOError|ProcessLookupError|OSError)\b'
_SPAWN = re.compile(
    rb'^\S*/python3\.\d+/subprocess\.py:\d+: (?:' + _SPAWN_TYPES
    + rb'|in \w+\n(?:[ \t].*\n|\n)*?E\s+' + _SPAWN_TYPES + rb')', re.M)


def candidate_spawn_failure(output):
    """True when a failed candidate call's output shows a process spawn that failed."""
    return bool(_SPAWN.search(output))


class CandidateBridge:
    """A bounded, serial, candidate-only command channel for the trusted driver."""
    def __init__(self, path, execute, *, deadline, cap):
        self.execute, self.deadline, self.cap = execute, deadline, cap
        self.calls = self.used = 0
        self.http_observations = 0
        self.http_execute = None
        self.spawn_failure = False
        self.error = None
        self.stopping = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        os.chmod(path, 0o600)
        self.socket.listen(1)
        self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopping.set()
        self.socket.close()
        self.thread.join(timeout=max(1, self.deadline - time.monotonic() + 2))
        if self.thread.is_alive():
            raise RuntimeRefusal('candidate_bridge_cleanup_failed')

    def _serve(self):
        while not self.stopping.is_set() and time.monotonic() < self.deadline:
            try:
                channel, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with channel:
                try:
                    channel.settimeout(min(2, max(.01, self.deadline - time.monotonic())))
                    raw = bytearray()
                    while not raw.endswith(b'\n'):
                        block = channel.recv(65536)
                        if not block or len(raw) + len(block) > 65536:
                            raise RuntimeRefusal('candidate_request_limit')
                        raw.extend(block)
                    request = json.loads(raw)
                    if isinstance(request, dict) and set(request) == {'refusal'}:
                        if request['refusal'] not in {'unsupported_browser_origin', 'unsupported_browser_websocket',
                                                     'unsupported_browser_navigation', 'unsupported_browser_download',
                                                     'candidate_http_bridge_failed'}:
                            raise RuntimeRefusal('invalid_candidate_request')
                        raise RuntimeRefusal(request['refusal'])
                    if not isinstance(request, dict) or set(request) not in ({'argv', 'timeout_s'}, {'http', 'timeout_s'}):
                        raise RuntimeRefusal('invalid_candidate_request')
                    timeout = request['timeout_s']
                    # --- graded plain-command mode: begin candidate-timeout ---
                    # served: if type(timeout) is not int or not 1 <= timeout <= 60:
                    if type(timeout) is not int or not 1 <= timeout <= getattr(self, 'max_timeout_s', 60):
                    # --- graded plain-command mode: end candidate-timeout ---
                        raise RuntimeRefusal('invalid_candidate_request')
                    if 'http' in request:
                        observation = _http_request(request['http'])
                        if self.http_execute is None:
                            raise RuntimeRefusal('invalid_candidate_request')
                    else:
                        argv = request['argv']
                        if (not isinstance(argv, list) or not argv or len(argv) > 128
                                or any(not isinstance(a, str) or '\0' in a or len(a) > 16384 for a in argv)
                                or not argv[0]):
                            raise RuntimeRefusal('invalid_candidate_request')
                    if self.calls >= 128 or self.used >= self.cap:
                        raise RuntimeRefusal('candidate_observation_limit')
                    self.calls += 1
                    call_deadline = min(self.deadline, time.monotonic() + timeout)
                    if 'http' in request:
                        response = self.http_execute(observation, call_deadline, self.cap - self.used)
                        result, size = _http_response(response, self.cap - self.used)
                        self.used += size
                        self.http_observations += 1
                    else:
                        code, output = self.execute(argv, call_deadline, self.cap - self.used)
                        self.used += len(output)
                        if code and candidate_spawn_failure(output):
                            self.spawn_failure = True
                        result = {'returncode': code, 'output': base64.b64encode(output).decode()}
                except Exception as exc:
                    self.error = self.error or (str(exc) if isinstance(exc, RuntimeRefusal) else 'candidate_bridge_failed')
                    result = {'error': self.error}
                try:
                    channel.sendall(json.dumps(result).encode() + b'\n')
                except OSError:
                    self.error = self.error or 'candidate_bridge_disconnected'


_HTTP_HOP_HEADERS = {'host', 'connection', 'content-length', 'transfer-encoding', 'upgrade',
                     'proxy-authorization', 'proxy-authenticate', 'keep-alive', 'te', 'trailer'}


def _http_headers(headers, *, response=False):
    if not isinstance(headers, dict) or len(headers) > 64:
        raise RuntimeRefusal('invalid_candidate_request')
    total = 0
    for name, value in headers.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]{1,128}", name)
                or name in _HTTP_HOP_HEADERS or not isinstance(value, str)):
            raise RuntimeRefusal('invalid_candidate_request')
        values = value.split('\n') if response and name == 'set-cookie' else [value]
        if any(any(ord(c) < 32 or ord(c) > 126 for c in part) for part in values):
            raise RuntimeRefusal('invalid_candidate_request')
        total += len(name) + len(value)
    if total > 16384:
        raise RuntimeRefusal('candidate_request_limit')
    return total


def _http_request(value):
    if not isinstance(value, dict) or set(value) != {'method', 'path', 'headers', 'body'}:
        raise RuntimeRefusal('invalid_candidate_request')
    path = value['path']
    if (value['method'] not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
            or not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or len(path) > 8192 or any(ord(c) <= 32 or ord(c) > 126 or c in '\\#' for c in path)):
        raise RuntimeRefusal('invalid_candidate_request')
    _http_headers(value['headers'])
    try:
        body = base64.b64decode(value['body'], validate=True)
        if base64.b64encode(body).decode() != value['body']:
            raise ValueError()
    except (ValueError, TypeError):
        raise RuntimeRefusal('invalid_candidate_request') from None
    if len(body) > 32768:
        raise RuntimeRefusal('candidate_request_limit')
    return {**value, 'body': body}


def _http_response(value, cap):
    if (not isinstance(value, dict) or set(value) != {'status', 'headers', 'body'}
            or type(value['status']) is not int or not 200 <= value['status'] <= 599
            or not isinstance(value['body'], bytes)):
        raise RuntimeRefusal('candidate_http_response_invalid')
    size = len(value['body']) + _http_headers(value['headers'], response=True)
    if size > cap:
        raise RuntimeRefusal('candidate_observation_limit')
    if value['headers'].get('content-encoding', 'identity').lower() != 'identity':
        raise RuntimeRefusal('candidate_http_encoding_unsupported')
    return {'http': {**value, 'body': base64.b64encode(value['body']).decode()}}, size


def acceptance_projection(candidate, destination, cfg):
    """Only exact frozen client files enter the driver's import namespace."""
    files = cfg['acceptance_files']
    for name, expected in {**cfg['lock_files'], **files}.items():
        path = candidate / name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeRefusal('acceptance_material_changed')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o400)


def structured_test_argv(cfg, candidate, report_path):
    """Keep the client's named tests; force a supported harness and its reporter."""
    argv = list(cfg['argv'])
    profile = cfg['profile_id']
    if argv[:2] in (['npm', 'test'], ['npm', 'run']) or argv[0] == 'npx':
        if argv[:2] == ['npm', 'test']:
            extra = argv[2:]
        elif argv[:3] == ['npm', 'run', 'test']:
            extra = argv[3:]
        elif argv[:3] == ['npx', 'playwright', 'test']:
            extra = argv[3:]
            argv = ['playwright', 'test']
        else:
            raise RuntimeRefusal('unsupported_profile_command')
        if cfg['argv'][0] == 'npm':
            manifest = json.loads((candidate / 'package.json').read_bytes())
            script = manifest.get('scripts', {}).get('test')
            if not isinstance(script, str):
                raise RuntimeRefusal('test_script_required')
            argv = shlex.split(script)
        argv += extra[1:] if extra[:1] == ['--'] else extra
    # --- graded plain-command mode: begin pytest-argv ---
    # served: if profile in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'} and argv[:3] == ['/work/.venv/bin/python', '-m', 'pytest']:
    if profile in _PYTEST_PROFILE_IDS and argv[:3] == ['/work/.venv/bin/python', '-m', 'pytest']:
    # --- graded plain-command mode: end pytest-argv ---
        args = argv[3:]
        selectors = []
        i = 0
        while i < len(args):
            arg = args[i]
            if args[i:i + 2] == ['-p', 'no:cacheprovider']:
                i += 2
                continue
            if arg in {'-q', '-v', '-x', '--disable-warnings', '--tb=short', '--tb=long', '--tb=line'}:
                i += 1
                continue
            if arg.startswith('-'):
                raise RuntimeRefusal('unsupported_pytest_option')
            selectors.append(arg)
            i += 1
        return ['/work/.venv/bin/python', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                '--junitxml=' + report_path, '-o', 'junit_family=xunit2'] + _test_paths(selectors), 'junit-file'
    if profile == 'linux-node-test-v1' and argv[:2] == ['node', '--test']:
        return ['node', '--test', '--test-reporter=junit'] + _test_paths(argv[2:]), 'node-junit'
    if profile in {'linux-node-browser-v1', 'linux-node-browser-http-v1'}:
        if argv[:2] == ['playwright', 'test']:
            args = argv[2:]
        elif argv[:3] == ['node', 'node_modules/@playwright/test/cli.js', 'test']:
            args = argv[3:]
        else:
            raise RuntimeRefusal('unsupported_profile_command')
        fixed = ['node', 'node_modules/@playwright/test/cli.js', 'test', '--reporter=json', '--retries=0']
        if profile == 'linux-node-browser-http-v1':
            fixed += ['--workers=1']
        return fixed + _test_paths(args), 'playwright-json'
    raise RuntimeRefusal('structured_test_harness_required')


def _test_paths(args):
    if not args or any(not isinstance(a, str) or a.startswith(('-', '/', '~'))
                       or '..' in a.split('/') or a in {';', '&&', '|', '&'} for a in args):
        raise RuntimeRefusal('explicit_test_paths_required')
    return args


def structured_result(kind, raw):
    """Return pass/assertion/setup from actual case records; ignore captured output."""
    if kind in {'junit-file', 'node-junit'}:
        if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
            return 'setup'
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return 'setup'
        cases = list(root.iter('testcase'))
        # --- graded plain-command mode: begin junit-skips ---
        # served: if not cases or list(root.iter('error')) or list(root.iter('skipped')):
            # served: return 'setup'
        if not cases or list(root.iter('error')):
            return 'setup'
        if kind == 'node-junit' and list(root.iter('skipped')):
            return 'setup'
        executed = [c for c in cases if c.find('skipped') is None]
        if not executed:
            return 'setup'
        # --- graded plain-command mode: end junit-skips ---
        failures = list(root.iter('failure'))
        if failures:
            if kind == 'junit-file':
                # --- graded plain-command mode: begin junit-failures ---
                # served: genuine = all(f.get('message', '').startswith(('assert ', 'AssertionError')) for f in failures)
                genuine = all(f.get('message', '').startswith(('assert ', 'AssertionError', 'Failed: '))
                              or f.get('message') == 'Failed' for f in failures)
                # --- graded plain-command mode: end junit-failures ---
            else:
                genuine = all('ERR_ASSERTION' in ''.join(f.itertext()) for f in failures)
            return 'assertion' if genuine else 'setup'
        return 'pass' if any(c.find('skipped') is None for c in cases) else 'setup'
    if kind == 'playwright-json':
        try:
            report = json.loads(raw)
            if not isinstance(report, dict) or report.get('errors'):
                return 'setup'
            tests = []
            def visit(suite):
                for spec in suite.get('specs', []):
                    tests.extend(spec.get('tests', []))
                for child in suite.get('suites', []):
                    visit(child)
            for suite in report.get('suites', []):
                visit(suite)
            results = [r for t in tests for r in t.get('results', [])]
            if not results or any(r.get('status') not in {'passed', 'failed'} for r in results):
                return 'setup'
            failed = [r for r in results if r.get('status') != 'passed' and r.get('status') != 'skipped']
            if not failed:
                return 'pass'
            for result in failed:
                message = result.get('error', {}).get('message', '')
                message = re.sub(r'\x1b\[[0-9;]*m', '', message)
                if result.get('status') != 'failed' or not message.startswith('Error: expect(') or not result.get('error', {}).get('location'):
                    return 'setup'
            return 'assertion'
        except (ValueError, AttributeError, TypeError):
            return 'setup'
    return 'setup'


def dependency_spec(profile_id, locks):
    """Closed artifact declarations. No resolver, scripts, URLs or config from a project."""
    if profile_id == 'linux-python-stdlib-v1':
        if locks:
            raise RuntimeRefusal('unexpected_dependency_locks')
        return [], None
    # --- graded plain-command mode: begin pytest-dependencies ---
    # served: if profile_id in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
    if profile_id in _PYTEST_PROFILE_IDS:
    # --- graded plain-command mode: end pytest-dependencies ---
        pins = []
        for line in locks['requirements.lock'].decode('utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*) ((?:--hash=sha256:[0-9a-f]{64})(?: --hash=sha256:[0-9a-f]{64})*)', line)
            if not match:
                raise RuntimeRefusal('unsupported_python_lock')
            name, version, hashes = match.groups()
            pins.append({'name': name, 'version': version, 'hashes': [h.split(':')[1] for h in hashes.split()]})
        if not pins or len(pins) > 512 or not any(p['name'].lower() == 'pytest' for p in pins):
            raise RuntimeRefusal('pytest_lock_required')
        return pins, None
    if profile_id not in {'linux-node-test-v1', 'linux-node-browser-v1', 'linux-node-browser-http-v1'}:
        raise RuntimeRefusal('unsupported_runtime_profile')
    lock, manifest = json.loads(locks['package-lock.json']), json.loads(locks['package.json'])
    if (not isinstance(lock, dict) or type(lock.get('lockfileVersion')) is not int
            or lock['lockfileVersion'] != 3 or not isinstance(lock.get('packages'), dict)
            or '' not in lock['packages'] or len(lock['packages']) > 4096 or not isinstance(manifest, dict)):
        raise RuntimeRefusal('unsupported_node_lock')
    clean_manifest = {k: manifest[k] for k in ('name', 'version', 'private') if k in manifest}
    for group in ('dependencies', 'devDependencies', 'optionalDependencies'):
        deps = manifest.get(group, {})
        if not isinstance(deps, dict) or any(not isinstance(version, str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) for version in deps.values()):
            raise RuntimeRefusal('node_direct_dependencies_must_be_pinned')
        if deps != lock['packages'][''].get(group, {}):
            raise RuntimeRefusal('node_manifest_lock_mismatch')
        if deps:
            clean_manifest[group] = deps
    artifacts = []
    for name, package in lock['packages'].items():
        if not isinstance(package, dict) or package.get('link'):
            raise RuntimeRefusal('unsupported_node_lock')
        if not name:
            continue
        url = urlsplit(str(package.get('resolved', '')))
        integrity = str(package.get('integrity', ''))
        if (url.scheme != 'https' or url.hostname != 'registry.npmjs.org' or url.username or url.password
                or url.port is not None or url.fragment or url.query
                or not re.fullmatch(r'sha(?:256|512)-[A-Za-z0-9+/]+={0,2}', integrity)):
            raise RuntimeRefusal('unsupported_node_registry_or_integrity')
        algo, encoded = integrity.split('-', 1)
        expected = base64.b64decode(encoded, validate=True)
        if len(expected) != hashlib.new(algo).digest_size:
            raise RuntimeRefusal('invalid_dependency_integrity')
        artifacts.append({'url': package['resolved'], 'algorithm': algo, 'digest': expected.hex()})
    return artifacts, clean_manifest


# Dependency download resilience. Measured on a Phala TDX CVM (2026-10-11,
# coordination/protected_u6_live_gates_2026_10_11.md): ~38 downloads, each on a fresh
# TLS connection, succeeded; the next handshake hung to the 20 s operation timeout,
# once per run. One kept-alive connection per host removes the ~120 fresh handshakes a
# 61-pin lock used to open. A request that still fails is retried on a new connection:
# 3 attempts = the original, a reconnect for the one measured hang, and one more for an
# independent transient. Every attempt keeps the existing min(20 s, remaining)
# operation timeout; backoff 1 s then 2 s adds 3 s at most, under one 20 s timeout, and
# the verification deadline still bounds the whole fetch.
_DOWNLOAD_TIMEOUT_S = 20
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_DOWNLOAD_TRANSIENT = (OSError, http.client.HTTPException)  # includes timeouts and ssl.SSLError


class _TransientDownload(Exception):
    pass


class _DeadlineSocket:
    """Arm every send and receive with min(20 s, time left) so the verification
    deadline bounds header and body reads absolutely, not per inactivity gap."""

    def __init__(self, sock):
        self.sock, self.deadline = sock, None
        self.readers, self.closing = 0, False

    def arm(self):
        self.sock.settimeout(min(_DOWNLOAD_TIMEOUT_S, _remaining(self.deadline)))

    def sendall(self, data):
        self.arm()
        return self.sock.sendall(data)

    def makefile(self, mode='rb', *args, **kwargs):
        if mode != 'rb':
            raise ValueError('unsupported_socket_mode')
        self.readers += 1
        return io.BufferedReader(_DeadlineReader(self))

    def close(self):
        # Like socket.makefile: an open response body keeps the socket readable.
        self.closing = True
        if not self.readers:
            self.sock.close()

    def release(self):
        self.readers -= 1
        if self.closing and not self.readers:
            self.sock.close()


class _DeadlineReader(io.RawIOBase):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def readable(self):
        return True

    def readinto(self, buffer):
        self.owner.arm()
        return self.owner.sock.recv_into(buffer)

    def close(self):
        if not self.closed:
            self.owner.release()
        super().close()


class _DownloadSession:
    """At most one open connection per host, pinned to an address validated at connect."""

    def __init__(self):
        self.connections = {}

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()

    def discard(self, host):
        connection = self.connections.pop(host, None)
        if connection is not None:
            connection.close()

    def connection(self, host, deadline):
        connection = self.connections.get(host)
        if connection is not None:
            return connection
        timeout = min(_DOWNLOAD_TIMEOUT_S, _remaining(deadline))
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [row[4][0] for row in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise RuntimeRefusal('dependency_download_private_address')
        raw_socket = socket.create_connection((ips[0], 443), timeout=timeout)
        try:
            tls_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
        except BaseException:
            raw_socket.close()
            raise
        connection = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
        connection.sock = _DeadlineSocket(tls_socket)
        self.connections[host] = connection
        return connection


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeRefusal('verification_timeout')
    return remaining


def _download(url, destination, *, hosts, deadline, budget, redirects=3, session=None):
    """HTTPS only; pin validated public IPs to the actual TLS connection.

    Ignore proxy/environment configuration. Redirects must satisfy the same host,
    port and address rules. A DNS rebinding cannot change the connected address.
    Connect, TLS, read and transient HTTP failures retry on a fresh connection and
    finally refuse as ``dependency_download_unavailable``.
    """
    if session is None:
        session = _DownloadSession()
        try:
            return _download(url, destination, hosts=hosts, deadline=deadline, budget=budget,
                             redirects=redirects, session=session)
        finally:
            session.close()
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise RuntimeRefusal('dependency_download_destination_refused')
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        _remaining(deadline)
        try:
            location = _download_once(parsed, destination, session=session, deadline=deadline, budget=budget)
        except (_TransientDownload, *_DOWNLOAD_TRANSIENT):
            session.discard(parsed.hostname)
            destination.unlink(missing_ok=True)
            if attempt == _DOWNLOAD_ATTEMPTS:
                raise RuntimeRefusal('dependency_download_unavailable') from None
            time.sleep(min(2 ** (attempt - 1), _remaining(deadline)))
            continue
        if isinstance(location, int):
            return location
        if redirects <= 0 or not location:
            raise RuntimeRefusal('dependency_download_redirect_refused')
        return _download(urljoin(url, location), destination, hosts=hosts, deadline=deadline,
                         budget=budget, redirects=redirects - 1, session=session)


def _download_once(parsed, destination, *, session, deadline, budget):
    """Byte count on 200, the Location string on a redirect; raise otherwise."""
    connection = session.connection(parsed.hostname, deadline)
    connection.sock.deadline = deadline
    connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''),
                       headers={'User-Agent': 'Ormas-public-runtime/1'})
    response = connection.getresponse()
    if response.status in (301, 302, 303, 307, 308):
        session.discard(parsed.hostname)
        return response.getheader('Location') or ''
    if response.status in _DOWNLOAD_RETRY_STATUS:
        raise _TransientDownload()
    if response.status != 200:
        session.discard(parsed.hostname)
        raise RuntimeRefusal('dependency_download_unavailable')
    count = 0
    with destination.open('xb') as stream:
        while True:
            _remaining(deadline)
            chunk = response.read(65536)
            if not chunk:
                break
            count += len(chunk)
            if count > budget:
                raise RuntimeRefusal('dependency_download_byte_limit')
            stream.write(chunk)
    if response.length:
        # EOF before the declared Content-Length: a truncated transfer, not a file.
        raise _TransientDownload()
    if response.will_close:
        session.discard(parsed.hostname)
    return count


# Cross-job public-wheel cache. Hosts normally point this at
# $HOME/.cache/ormas/verifier-packages. Unset or empty keeps the old network path.
_PACKAGE_CACHE_ENV = 'ORMAS_VERIFIER_PACKAGE_CACHE'
_PACKAGE_CACHE_MANIFEST = 'wheels.json'
_PACKAGE_CACHE_UNSET = object()


class _CacheInvalid(Exception):
    pass


def _cache_directory(path, *, create):
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return False
        if create:
            path.chmod(0o700)
            info = path.lstat()
        return stat.S_IMODE(info.st_mode) == 0o700
    except OSError:
        return False


def _package_cache_dir(candidate, artifacts, mounted=()):
    configured = os.environ.get(_PACKAGE_CACHE_ENV)
    if not configured:
        return None
    try:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            return None
        root = Path(os.path.abspath(str(root)))
        current = Path(root.anchor)
        for part in root.parts[1:]:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                return None
        root = Path(os.path.realpath(str(root)))
        for job_path in (candidate, artifacts, *mounted):
            if job_path is None:
                continue
            job = Path(os.path.realpath(str(job_path)))
            if root == job or root in job.parents or job in root.parents:
                return None
        return root if _cache_directory(root, create=True) else None
    except (OSError, RuntimeError, ValueError):
        return None


def _cache_pin_dir(root, pin):
    key = hashlib.sha256((pin['name'] + '\0' + pin['version']).encode('utf-8')).hexdigest()
    return root / key


def _open_cache_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _CacheInvalid()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise _CacheInvalid()
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    return stream


def _discard_cache_file(path):
    try:
        path.unlink()
    except OSError:
        pass


def _read_cache_manifest(pin_dir, pin):
    with _open_cache_file(pin_dir / _PACKAGE_CACHE_MANIFEST) as stream:
        document = json.load(stream)
    if (not isinstance(document, dict) or document.get('name') != pin['name']
            or document.get('version') != pin['version']
            or not isinstance(document.get('wheels'), list) or not document['wheels']):
        raise _CacheInvalid()
    records = []
    for entry in document['wheels']:
        if not isinstance(entry, dict):
            raise _CacheInvalid()
        name, digest = entry.get('filename'), entry.get('sha256')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', name)
                or digest not in pin['hashes'] or name in {record[0] for record in records}):
            raise _CacheInvalid()
        records.append((name, digest))
    return records


def _copy_cached_wheel(source, target, digest, budget):
    temporary = None
    try:
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise _CacheInvalid()
        with _open_cache_file(source) as stream:
            if target.exists():
                output = None
            else:
                fd, temporary = tempfile.mkstemp(prefix='.ormas-cache-', dir=target.parent)
                output = os.fdopen(fd, 'wb')
            try:
                hasher, count = hashlib.sha256(), 0
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > budget:
                        raise _CacheInvalid()
                    hasher.update(chunk)
                    if output is not None:
                        output.write(chunk)
            finally:
                if output is not None:
                    output.close()
        if hasher.hexdigest() != digest:
            raise _CacheInvalid()
        if temporary is not None:
            os.replace(temporary, target)
            temporary = None
        return count
    except BaseException:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _restore_cached_wheels(root, pin, artifacts, budget):
    if root is None:
        return None
    pin_dir = _cache_pin_dir(root, pin)
    if not _cache_directory(pin_dir, create=False):
        return None
    manifest = pin_dir / _PACKAGE_CACHE_MANIFEST
    try:
        records = _read_cache_manifest(pin_dir, pin)
    except (OSError, ValueError, RecursionError, MemoryError, _CacheInvalid):
        _discard_cache_file(manifest)
        return None
    created, restored = [], []
    for name, digest in records:
        target = artifacts / name
        was_present = target.exists()
        try:
            size = _copy_cached_wheel(pin_dir / name, target, digest, budget)
        except (OSError, _CacheInvalid):
            _discard_cache_file(pin_dir / name)
            _discard_cache_file(manifest)
            for path in created:
                path.unlink(missing_ok=True)
            return None
        if not was_present:
            created.append(target)
        restored.append((name, digest, size))
        budget -= size
    return restored


def _sync_cache_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
                     | getattr(os, 'O_NOFOLLOW', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_cache_write(target, *, source=None, data=None, digest=None):
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=target.parent)
        with os.fdopen(fd, 'wb') as output:
            stream = source.open('rb') if source is not None else io.BytesIO(data)
            try:
                hasher = hashlib.sha256()
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    output.write(chunk)
            finally:
                if source is not None:
                    stream.close()
            if digest is not None and hasher.hexdigest() != digest:
                raise _CacheInvalid()
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
        _sync_cache_directory(target.parent)
    except BaseException:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _store_cached_wheels(root, pin, artifacts, records):
    if root is None:
        return
    pin_dir = _cache_pin_dir(root, pin)
    if not _cache_directory(pin_dir, create=True):
        return
    manifest = pin_dir / _PACKAGE_CACHE_MANIFEST
    try:
        unique = {}
        for name, digest in records:
            unique.setdefault(name, digest)
        for name, digest in unique.items():
            _atomic_cache_write(pin_dir / name, source=artifacts / name, digest=digest)
        document = {'name': pin['name'], 'version': pin['version'], 'wheels': [
            {'filename': name, 'sha256': digest} for name, digest in unique.items()]}
        _atomic_cache_write(manifest, data=json.dumps(document, sort_keys=True).encode('utf-8'))
    except (OSError, _CacheInvalid):
        _discard_cache_file(manifest)


def fetch_dependencies(cfg, candidate, artifacts, deadline, mounted=()):
    raw_locks = {name: (candidate / name).read_bytes() for name in cfg['lock_files']}
    spec, clean_manifest = dependency_spec(cfg['profile_id'], raw_locks)
    budget = min(1024 * 1024 * 1024, cfg['limits']['disk_mib'] * 1024 * 1024 // 4)
    cache_dir = _package_cache_dir(candidate, artifacts, mounted)
    session = _DownloadSession()
    try:
        return _fetch_dependencies(cfg, spec, clean_manifest, raw_locks, artifacts, deadline, budget, session,
                                   cache_dir)
    finally:
        session.close()


def _fetch_dependencies(cfg, spec, clean_manifest, raw_locks, artifacts, deadline, budget, session,
                        cache_dir=_PACKAGE_CACHE_UNSET):
    used = 0
    if cache_dir is _PACKAGE_CACHE_UNSET:
        cache_dir = _package_cache_dir(None, artifacts)
    # --- graded plain-command mode: begin pytest-downloads ---
    # served: if cfg['profile_id'] in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
    if cfg['profile_id'] in _PYTEST_PROFILE_IDS:
    # --- graded plain-command mode: end pytest-downloads ---
        for index, pin in enumerate(spec):
            cached = _restore_cached_wheels(cache_dir, pin, artifacts, budget - used)
            if cached is not None:
                # A complete, validated manifest replaces the metadata request.
                used += sum(size for _, _, size in cached)
                continue
            metadata = artifacts / ('metadata-' + str(index) + '.json')
            used += _download('https://pypi.org/pypi/' + quote(pin['name'], safe='') + '/' + quote(pin['version'], safe='') + '/json',
                              metadata, hosts={'pypi.org'}, deadline=deadline, budget=min(4 * 1024 * 1024, budget - used), session=session)
            document = json.loads(metadata.read_bytes())
            wheels = [entry for entry in document.get('urls', []) if isinstance(entry, dict)
                      and entry.get('packagetype') == 'bdist_wheel' and entry.get('digests', {}).get('sha256') in pin['hashes']]
            if not wheels:
                raise RuntimeRefusal('locked_wheel_unavailable')
            records = []
            for entry in wheels:
                name = entry.get('filename', '')
                if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.+!-]+\.whl', name):
                    raise RuntimeRefusal('invalid_wheel_name')
                target = artifacts / name
                digest = entry['digests']['sha256']
                records.append((name, digest))
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                        raise RuntimeRefusal('dependency_integrity_failed')
                    continue
                used += _download(entry['url'], target, hosts={'files.pythonhosted.org'}, deadline=deadline, budget=budget - used, session=session)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise RuntimeRefusal('dependency_integrity_failed')
            metadata.unlink()
            # The manifest is renamed last: readers never mistake a partial fill for a hit.
            _store_cached_wheels(cache_dir, pin, artifacts, records)
    elif clean_manifest is not None:
        for index, artifact in enumerate(spec):
            target = artifacts / (str(index) + '.tgz')
            used += _download(artifact['url'], target, hosts={'registry.npmjs.org'}, deadline=deadline, budget=budget - used, session=session)
            if hashlib.new(artifact['algorithm'], target.read_bytes()).hexdigest() != artifact['digest']:
                raise RuntimeRefusal('dependency_integrity_failed')
        (artifacts / 'npmrc-user').touch()
        (artifacts / 'npmrc-global').touch()
        (artifacts / 'package.json').write_text(json.dumps(clean_manifest))
        (artifacts / 'package-lock.json').write_bytes(raw_locks['package-lock.json'])
    return used


def installer_argv(profile_id):
    if profile_id == 'linux-python-stdlib-v1':
        return []
    # --- graded plain-command mode: begin pytest-installer ---
    # served: if profile_id in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
    if profile_id in _PYTEST_PROFILE_IDS:
    # --- graded plain-command mode: end pytest-installer ---
        return ['/bin/sh', '-c', 'python3 -m venv /work/.venv && /work/.venv/bin/python -m pip --isolated install --disable-pip-version-check --no-index --no-deps --find-links=/artifacts --require-hashes --only-binary=:all: -r requirements.lock']
    if profile_id in {'linux-node-test-v1', 'linux-node-browser-v1', 'linux-node-browser-http-v1'}:
        return ['/bin/sh', '-c', 'mkdir /work/.ormas-install && cp /artifacts/package*.json /work/.ormas-install/ && cd /work/.ormas-install && for artifact in /artifacts/*.tgz; do [ ! -f "$artifact" ] || npm cache add "$artifact" --cache /work/.ormas-cache --offline --ignore-scripts --userconfig=/artifacts/npmrc-user --globalconfig=/artifacts/npmrc-global; done && npm ci --offline --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org --cache=/work/.ormas-cache --userconfig=/artifacts/npmrc-user --globalconfig=/artifacts/npmrc-global && if [ -d node_modules ]; then mv node_modules /work/node_modules; fi']
    raise RuntimeRefusal('unsupported_runtime_profile')


def _bounded(argv, *, cwd, deadline, cap, env=None):
    """Drain combined output without allowing a pipe or memory deadlock."""
    if time.monotonic() >= deadline:
        raise RuntimeRefusal("verification_timeout")
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeRefusal("verification_timeout")
                for key, _ in selector.select(min(remaining, 0.1)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(block) > cap:
                        raise RuntimeRefusal("verification_output_limit")
                    output.extend(block)
        return proc.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()


def _source_file(root_fd, name, directory_modes):
    parts = name.split("/")
    if not name or any(p in ("", ".", "..", ".git") for p in parts) or "\\" in name:
        raise RuntimeRefusal("unsupported_source_path")
    fd = os.dup(root_fd)
    try:
        for index, part in enumerate(parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            directory_modes["/".join(parts[:index + 1])] = stat.S_IMODE(os.fstat(child).st_mode) & 0o777
            os.close(fd)
            fd = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def safe_git_argv(*args):
    # Command-line settings override repository config, including includes. Git's
    # fsmonitor is executable code even for an otherwise read-only ls-files/status.
    return ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.excludesFile=/dev/null', *args]


def validate_repository_gitlinks(value):
    if not isinstance(value, dict) or len(value) > 1000 or any(
            not isinstance(path, str) or not path or path.startswith(('/', '~'))
            or '\\' in path or any(part in ('', '.', '..', '.git') for part in path.split('/'))
            or not isinstance(oid, str) or not re.fullmatch('[0-9a-f]{40}', oid)
            for path, oid in value.items()):
        raise RuntimeRefusal('invalid_repository_gitlinks')
    return dict(value)


def _indexed_gitlinks(source, deadline, env):
    code, raw = _bounded(safe_git_argv('ls-files', '--stage', '-z'),
                        cwd=source, deadline=deadline, cap=4 * 1024 * 1024, env=env)
    if code:
        raise RuntimeRefusal('source_inventory_unavailable')
    links = {}
    for entry in raw.split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        mode, oid, stage = metadata.split()
        if mode == b'160000':
            if stage != b'0':
                raise RuntimeRefusal('repository_gitlink_changed')
            links[os.fsdecode(path)] = oid.decode('ascii')
    return validate_repository_gitlinks(links)


def _empty_gitlink(root_fd, name, directory_modes):
    try:
        fd = _source_file(root_fd, name, directory_modes)
    except FileNotFoundError:
        return
    except OSError:
        raise RuntimeRefusal('repository_gitlink_materialized') from None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeRefusal('repository_gitlink_materialized')
        with os.scandir(fd) as entries:
            if next(entries, None) is not None:
                raise RuntimeRefusal('repository_gitlink_materialized')
    finally:
        os.close(fd)


def _snapshot(source, target, cfg, deadline, env):
    # Unversioned exclusions are not transferable to a fresh validator clone.
    # Refuse them instead of silently hiding material or exposing ignored files.
    code, local_excludes = _bounded(safe_git_argv('config', '--local', '--includes', '--get', 'core.excludesFile'),
                                    cwd=source, deadline=deadline, cap=4096, env=env)
    if code == 0 and local_excludes.strip():
        raise RuntimeRefusal('repository_exclusion_override_unsupported')
    code, exclude_path = _bounded(safe_git_argv('rev-parse', '--git-path', 'info/exclude'),
                                  cwd=source, deadline=deadline, cap=4096, env=env)
    if code:
        raise RuntimeRefusal('source_inventory_unavailable')
    path = Path(os.fsdecode(exclude_path.strip()))
    if not path.is_absolute():
        path = source / path
    if path.exists():
        if path.is_symlink() or path.stat().st_size > 65536:
            raise RuntimeRefusal('repository_exclusion_override_unsupported')
        if any(line.strip() and not line.lstrip().startswith('#') for line in path.read_text().splitlines()):
            raise RuntimeRefusal('repository_exclusion_override_unsupported')
    code, raw = _bounded(safe_git_argv("ls-files", "-z", "--cached", "--others", "--exclude-standard"),
                         cwd=source, deadline=deadline, cap=4 * 1024 * 1024, env=env)
    if code:
        raise RuntimeRefusal("source_inventory_unavailable")
    names = sorted(set(os.fsdecode(n) for n in raw.split(b"\0") if n))
    gitlinks = validate_repository_gitlinks(cfg.get('repository_gitlinks', {}))
    if _indexed_gitlinks(source, deadline, env) != gitlinks:
        raise RuntimeRefusal('repository_gitlink_changed')
    if len(names) > cfg["limits"]["max_files"]:
        raise RuntimeRefusal("source_file_limit")
    fingerprints = {}
    directory_modes = {}
    total = 0
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in names:
            if name.split('/')[0] in {'.venv', 'venv', 'node_modules', '.ormas-install', '.ormas-cache'}:
                raise RuntimeRefusal('source_installed_artifacts_refused')
            if time.monotonic() >= deadline:
                raise RuntimeRefusal("verification_timeout")
            if name in gitlinks:
                _empty_gitlink(root_fd, name, directory_modes)
                (target / name).mkdir(parents=True, exist_ok=False)
                fingerprints[name] = 'gitlink:' + gitlinks[name]
                continue
            try:
                fd = _source_file(root_fd, name, directory_modes)
            except FileNotFoundError:
                continue  # A deleted candidate file is absent in the copy too.
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeRefusal("unsupported_source_file")
                total += before.st_size
                if total > cfg["limits"].get("source_max_bytes", cfg["limits"]["max_bytes"]):
                    raise RuntimeRefusal("source_byte_limit")
                dest = target / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                count = 0
                with dest.open("xb") as output:
                    while True:
                        block = stream.read(min(65536, before.st_size - count + 1))
                        if not block:
                            break
                        count += len(block)
                        if count > before.st_size:
                            raise RuntimeRefusal("source_changed_during_copy")
                        output.write(block)
                        digest.update(block)
                after = os.fstat(stream.fileno())
                if (count != before.st_size or before.st_mtime_ns != after.st_mtime_ns
                        or before.st_ctime_ns != after.st_ctime_ns):
                    raise RuntimeRefusal("source_changed_during_copy")
                dest.chmod(stat.S_IMODE(before.st_mode) & 0o777)
                fingerprints[name] = digest.hexdigest()
        # Recheck opaque boundaries after the copy; never recurse into a submodule.
        for name in gitlinks:
            _empty_gitlink(root_fd, name, directory_modes)
        if _indexed_gitlinks(source, deadline, env) != gitlinks:
            raise RuntimeRefusal('repository_gitlink_changed')
    finally:
        os.close(root_fd)
    for name, expected in cfg["lock_files"].items():
        if fingerprints.get(name) != expected:
            raise RuntimeRefusal("dependency_lock_changed")
    for name, mode in sorted(directory_modes.items(), key=lambda item: item[0].count("/"), reverse=True):
        (target / name).chmod(mode)
    target.chmod(stat.S_IMODE(source.stat().st_mode) & 0o777)
    return fingerprints


def validate_service(value):
    if not isinstance(value, dict) or set(value) != {'argv', 'port'}:
        raise RuntimeRefusal('invalid_candidate_service')
    argv, port = value['argv'], value['port']
    if (not isinstance(argv, list) or not 2 <= len(argv) <= 32 or argv[0] != 'node'
            or any(not isinstance(a, str) or not a or len(a) > 1024 or '\0' in a for a in argv)
            or argv[1].startswith(('-', '/', '~')) or '\\' in argv[1]
            or any(p in ('', '.', '..', '.git', 'node_modules') for p in argv[1].split('/'))
            or not argv[1].endswith(('.js', '.cjs', '.mjs'))
            or type(port) is not int or not 1024 <= port <= 65535):
        raise RuntimeRefusal('invalid_candidate_service')
    return {'argv': list(argv), 'port': port}


class CandidateService:
    """One foreground service; bounded drained logs never enter the test report."""
    def __init__(self, argv, *, cwd, env, deadline, cap=65536):
        self.error = None
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.thread = threading.Thread(target=self._drain, args=(deadline, cap), daemon=True)
        self.thread.start()

    def _drain(self, deadline, cap):
        size = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise RuntimeRefusal('candidate_service_timeout')
                    for key, _ in selector.select(.1):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        size += len(chunk)
                        if size > cap:
                            raise RuntimeRefusal('candidate_service_output_limit')
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, RuntimeRefusal) else 'candidate_service_failed'

    def check(self):
        if self.error or self.proc.poll() is not None:
            raise RuntimeRefusal(self.error or 'candidate_service_exited')

    def close(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=5)
        self.thread.join(timeout=5)
        self.proc.stdout.close()
        if self.thread.is_alive():
            raise RuntimeRefusal('candidate_service_cleanup_failed')


def _validate_config(cfg):
    keys = {"schema_version", "image", "platform", "profile_id", "argv", "env",
            "lock_files", "limits", "install_argv", "acceptance_files"}
    # --- graded plain-command mode: begin config-key ---
    if isinstance(cfg, dict) and 'candidate_argv' in cfg:
        keys.add('candidate_argv')
    # --- graded plain-command mode: end config-key ---
    if isinstance(cfg, dict) and cfg.get('profile_id') == 'linux-node-browser-http-v1':
        keys.add('service')
    if isinstance(cfg, dict) and 'repository_gitlinks' in cfg:
        # --- graded plain-command mode: begin pytest-gitlinks ---
        # served: if (cfg.get('profile_id') not in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}
        # served:         or not isinstance(cfg.get('limits'), dict)
        # served:         or 'source_max_bytes' not in cfg['limits']):
        if (cfg.get('profile_id') not in _PYTEST_PROFILE_IDS
                or not isinstance(cfg.get('limits'), dict)
                or 'source_max_bytes' not in cfg['limits']):
        # --- graded plain-command mode: end pytest-gitlinks ---
            raise RuntimeRefusal('repository_gitlinks_unsupported')
        keys.add('repository_gitlinks')
        validate_repository_gitlinks(cfg['repository_gitlinks'])
    if not isinstance(cfg, dict) or set(cfg) != keys or cfg["schema_version"] != "ormas.oci-verifier.v2":
        raise RuntimeRefusal("invalid_runtime_config")
    if 'service' in cfg:
        validate_service(cfg['service'])
    if not isinstance(cfg["image"], str) or not re.fullmatch(
            r"(?:python|node|mcr\.microsoft\.com/playwright)@sha256:[0-9a-f]{64}", cfg["image"]):
        raise RuntimeRefusal("invalid_runtime_image")
    if cfg["platform"] != "linux/amd64":
        raise RuntimeRefusal("unsupported_runtime_platform")
    limits = cfg["limits"]
    bounds = {"timeout_s": 600, "cpus": 2, "memory_mib": 4096, "disk_mib": 4096,
              "output_bytes": 1048576, "max_files": 10000, "max_bytes": 67108864}
    if isinstance(limits, dict) and 'source_max_bytes' in limits:
        # --- graded plain-command mode: begin pytest-source-limit ---
        # served: if cfg.get('profile_id') not in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
        if cfg.get('profile_id') not in _PYTEST_PROFILE_IDS:
        # --- graded plain-command mode: end pytest-source-limit ---
            raise RuntimeRefusal('invalid_runtime_limits')
        bounds['source_max_bytes'] = 134217728
    if not isinstance(limits, dict) or set(limits) != set(bounds):
        raise RuntimeRefusal("invalid_runtime_limits")
    if any(type(limits[k]) is not int or not 1 <= limits[k] <= maximum for k, maximum in bounds.items()):
        raise RuntimeRefusal("invalid_runtime_limits")
    for key in ("argv", "install_argv"):
        if not isinstance(cfg[key], list) or not all(isinstance(x, str) and x and "\0" not in x for x in cfg[key]):
            raise RuntimeRefusal("invalid_runtime_argv")
    if not isinstance(cfg['profile_id'], str):
        raise RuntimeRefusal('unsupported_runtime_profile')
    if cfg['install_argv'] != installer_argv(cfg['profile_id']):
        raise RuntimeRefusal('unapproved_installer_command')
    # --- graded plain-command mode: begin candidate-validation ---
    if 'candidate_argv' in cfg:
        argv = cfg['candidate_argv']
        if (cfg['profile_id'] not in _PYTEST_PROFILE_IDS or argv != cfg['argv']
                or argv[:3] != ['/work/.venv/bin/python', '-m', 'pytest']
                or len(argv) > 128
                or any(len(a) > 16384 or a.startswith(('/', '~')) or '..' in a.split('/')
                       for a in argv[3:])):
            raise RuntimeRefusal('invalid_candidate_argv')
    # --- graded plain-command mode: end candidate-validation ---
    files = cfg['acceptance_files']
    if not isinstance(files, dict) or not files or len(files) > 1000 or any(
            not isinstance(name, str) or name.startswith(('/', '~')) or '\\' in name
            or any(p in ('', '.', '..', '.git', '.venv', 'node_modules') for p in name.split('/'))
            or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha) for name, sha in files.items()):
        raise RuntimeRefusal('invalid_acceptance_files')
    if not cfg["argv"]:
        raise RuntimeRefusal("invalid_runtime_argv")
    if not isinstance(cfg["env"], dict) or set(cfg["env"]) - {"LANG", "LC_ALL", "TZ", "PYTHONDONTWRITEBYTECODE"}:
        raise RuntimeRefusal("unsupported_runtime_environment")
    if any(not isinstance(v, str) or "\0" in v for v in cfg["env"].values()):
        raise RuntimeRefusal("unsupported_runtime_environment")
    if not isinstance(cfg["lock_files"], dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
            for k, v in cfg["lock_files"].items()):
        raise RuntimeRefusal("invalid_dependency_locks")


def _share_rootless_projection(root):
    """Expose scratch owner read/execute to its mapped group, never to others.

    The containing scratch directory remains 0700. Only explicit readonly bind
    mounts enter containers; the original checkout and credentials are untouched.
    """
    uid, gid = os.getuid(), os.getgid()
    for path in [root, *root.rglob('*')]:
        item = path.lstat()
        if (item.st_uid != uid
                or not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode))):
            raise RuntimeRefusal('rootless_projection_owner_mismatch')
        mode = stat.S_IMODE(item.st_mode) & 0o700
        if item.st_gid != gid:
            os.chown(path, -1, gid, follow_symlinks=False)
        path.chmod(mode | ((mode >> 3) & 0o050))


def _check_rootless_map(raw, host_id):
    try:
        rows = [tuple(map(int, line.split())) for line in raw.decode('ascii').splitlines()]
    except (ValueError, UnicodeError):
        raise RuntimeRefusal('rootless_identity_map_mismatch') from None
    if (not rows or any(len(row) != 3 for row in rows)
            or rows[0] != (0, host_id, 1)):
        raise RuntimeRefusal('rootless_identity_map_mismatch')


def run_verifier(cfg, *, cwd=None):
    _validate_config(cfg)
    source = Path(cwd or os.getcwd()).resolve()
    deadline = time.monotonic() + cfg["limits"]["timeout_s"]
    cap = cfg["limits"]["output_bytes"]
    host_env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(Path.home()),
                "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for exe in ("docker", "git"):
        path = shutil.which(exe, path=host_env["PATH"])
        if path is None or Path(path).resolve().is_relative_to(source):
            raise RuntimeRefusal("runtime_executable_unavailable")
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise RuntimeRefusal("runtime_nonroot_required")
    token = "ormas-verify-" + uuid.uuid4().hex
    verifier, installer, volume = token + "-check", token + "-install", token + "-data"
    candidate_container, driver_volume = token + '-candidate', token + '-driver'
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if temporary_root == source or source in temporary_root.parents:
        raise RuntimeRefusal("scratch_root_inside_candidate")
    scratch = Path(tempfile.mkdtemp(prefix="ormas-oci-", dir=temporary_root))
    candidate = scratch / "candidate"
    candidate.mkdir()
    old_handlers = {}
    bridge = None
    service = None
    # This small, credential-free cleanup process survives a SIGKILL of the
    # verifier. It has only this invocation's exact container/volume/temp names.
    guard = subprocess.Popen([sys.executable, '-I', '-S', '-c', _CLEANUP_GUARD,
                              json.dumps([os.getpid(), cfg['limits']['timeout_s'] + 40, str(scratch),
                                          [installer, verifier, candidate_container], [volume, driver_volume]])],
                             cwd='/', env=host_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)

    def cancel(signum, frame):
        raise RuntimeRefusal("verification_cancelled")

    def command(argv, *, required=True, limit=cap):
        code, output = _bounded(argv, cwd=scratch, deadline=deadline, cap=limit, env=host_env)
        if required and code:
            raise RuntimeRefusal("runtime_setup_failed")
        return code, output

    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, cancel)
        _snapshot(source, candidate, cfg, deadline, host_env)
        driver = scratch / 'driver'
        driver.mkdir()
        acceptance_projection(candidate, driver, cfg)
        artifacts = scratch / 'artifacts'
        artifacts.mkdir()
        report_path = '/tmp/' + uuid.uuid4().hex + '.xml'
        test_argv, report_kind = structured_test_argv(cfg, driver, report_path)
        fetch_dependencies(cfg, candidate, artifacts, deadline, (source, scratch))
        code, info = command(["docker", "info", "--format", "{{json .}}"], required=False)
        try:
            runtime_info = json.loads(info)
        except (ValueError, UnicodeError):
            raise RuntimeRefusal("runtime_platform_unavailable") from None
        if (code or not isinstance(runtime_info, dict) or runtime_info.get('OSType') != 'linux'
                or runtime_info.get('Architecture') not in ('x86_64', 'amd64')):
            raise RuntimeRefusal("runtime_platform_unavailable")
        rootless = bool({'rootless', 'name=rootless'}.intersection(runtime_info.get('SecurityOptions') or []))
        # Guest group 0 maps to the checker's ordinary host group in rootless
        # Docker. Only projection readers need it. Candidate processes retain
        # their nonzero UID and original group, distinct from the keeper/probe.
        reader_gid = 0 if rootless else gid
        code, _ = command(["docker", "image", "inspect", cfg["image"]], required=False)
        if code:
            command(["docker", "pull", "--platform", cfg["platform"], cfg["image"]])
        # Reap only expired volumes from this primitive. Docker refuses a volume
        # still in use, so concurrent current executions cannot lose their data.
        _, old_volumes = command(["docker", "volume", "ls", "-q", "--filter", "label=ormas.verifier=v1"])
        for old_volume in old_volumes.decode().splitlines():
            code, metadata = command(["docker", "volume", "inspect", old_volume], required=False)
            # Another verifier can remove its finished volume after the list.
            # Reaping is opportunistic; never delete an uninspected volume.
            if code:
                continue
            expiry = json.loads(metadata)[0].get("Labels", {}).get("ormas.expires", "")
            if expiry.isdigit() and int(expiry) < int(time.time()):
                command(["docker", "volume", "rm", old_volume], required=False)
        for name in (volume, driver_volume):
            command(["docker", "volume", "create", "--driver", "local", "--label", "ormas.verifier=v1",
                     "--label", f"ormas.expires={int(time.time()) + cfg['limits']['timeout_s'] + 60}",
                     "--opt", "type=tmpfs", "--opt", "device=tmpfs", "--opt",
                     f"o=size={max(1, min(cfg['limits']['disk_mib'] // 4, cfg['limits']['memory_mib'] // 4))}m,uid={uid},gid={gid},mode=0700", name])
        common = ["docker", "run", "--rm", "--pull", "never", "--platform", cfg["platform"],
                  "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                  "--tmpfs", "/tmp:rw,exec,size=256m", "--memory", f"{max(1, cfg['limits']['memory_mib'] // 4)}m",
                  "--cpus", str(cfg["limits"]["cpus"] / 2), "--pids-limit", "128", "--shm-size", "128m",
                  "-w", "/work", "-e", "HOME=/tmp", "-e", "CI=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
                  "-e", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"]
        for key, value in sorted(cfg["env"].items()):
            common += ["-e", key + "=" + value]
        # --- graded plain-command mode: begin pytest-path ---
        # served: if cfg['profile_id'] in {'linux-python-pytest-v1', 'linux-python-pytest-git-v1'}:
        if cfg['profile_id'] in _PYTEST_PROFILE_IDS:
        # --- graded plain-command mode: end pytest-path ---
            common += ['-e', 'PATH=/work/.venv/bin:/usr/local/bin:/usr/bin:/bin']
        bridge_root = scratch / 'bridge'
        bridge_root.mkdir(mode=0o700)
        helpers = scratch / 'helpers'
        helpers.mkdir()
        (helpers / 'ormas_acceptance.py').write_text(PYTHON_ACCEPTANCE_HELPER)
        (helpers / 'ormas_acceptance.js').write_text(NODE_HTTP_HELPER if 'service' in cfg else NODE_ACCEPTANCE_HELPER)
        def observe(argv, call_deadline, call_cap):
            # CLI options end at the fixed container id. Client argv never reaches
            # the host shell or controls Docker options/container selection.
            return _bounded(['docker', 'exec', '--user', f'{uid}:{gid}', candidate_container, *UMASK_ARGV,
                             '/usr/bin/timeout', '--signal=KILL', str(max(1, int(call_deadline - time.monotonic())))] + argv,
                            cwd=scratch, deadline=call_deadline, cap=call_cap, env=host_env)
        bridge = CandidateBridge(bridge_root / 'socket', observe, deadline=deadline, cap=cap)
        # --- graded plain-command mode: begin bridge-driver ---
        bridge.max_timeout_s = cfg['limits']['timeout_s'] if 'candidate_argv' in cfg else 60
        (helpers / 'ormas_candidate.py').write_text(CANDIDATE_PYTEST_DRIVER)
        report_container = verifier
        if 'candidate_argv' in cfg:
            report_container = candidate_container
            test_argv = ['/work/.venv/bin/python', '-I', '-S', '/ormas/helpers/ormas_candidate.py',
                         json.dumps(test_argv), str(cfg['limits']['timeout_s']), str(cap)]
            # Keep pytest's own exit status from the trusted bridge; the driver
            # folds 2 (interrupted) and 5 (no tests collected) into one exit.
            bridge.candidate_returncode = None
            observe_candidate = bridge.execute

            def record_returncode(argv, call_deadline, call_cap):
                returncode, observed = observe_candidate(argv, call_deadline, call_cap)
                bridge.candidate_returncode = returncode
                return returncode, observed
            bridge.execute = record_returncode
        # --- graded plain-command mode: end bridge-driver ---
        bridge.start()
        for name, data_volume, projection in ((candidate_container, volume, candidate), (verifier, driver_volume, driver)):
            options = common + ['--mount', f'type=volume,source={data_volume},target=/work,volume-nocopy']
            driver_options = []
            if name == verifier:
                driver_options = ['--mount', f'type=bind,source={bridge_root},target=/ormas/bridge,readonly',
                                  '--mount', f'type=bind,source={helpers},target=/ormas/helpers,readonly',
                                  '-e', 'PYTHONPATH=/ormas/helpers', '-e', 'NODE_PATH=/ormas/helpers',
                                  '-e', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1']
            # Keep each tmpfs mounted across install/test phases, with no network.
            command(options + driver_options + ['--name', name, '--network', 'none', '--detach',
                                                '--user', '0:0', '--entrypoint', '/bin/sleep',
                                                '--label', 'ormas.verifier=v2',
                                                '--label', f'ormas.parentpid={os.getpid()}',
                                                '--label', f'ormas.expires={int(time.time()) + cfg["limits"]["timeout_s"] + 10}',
                                                cfg['image'], str(cfg['limits']['timeout_s'] + 10)])
            if rootless and name == candidate_container:
                # Prove the selected daemon's actual namespace mapping before
                # granting group access or running anything from the checkout.
                for kind, host_id in (('uid', uid), ('gid', gid)):
                    _, mapping = command(['docker', 'exec', '--user', f'{uid}:{gid}', name,
                                          '/bin/cat', f'/proc/self/{kind}_map'], limit=4096)
                    _check_rootless_map(mapping, host_id)
                for projection_root in (candidate, driver, artifacts, helpers):
                    _share_rootless_projection(projection_root)
                os.chown(bridge_root, -1, gid)
                os.chown(bridge_root / 'socket', -1, gid)
                bridge_root.chmod(0o710)
                (bridge_root / 'socket').chmod(0o660)
            command(options + ['--name', installer, '--network', 'none', '--user', f'{uid}:{reader_gid}', '--mount',
                               f'type=bind,source={projection},target=/source,readonly', cfg['image'], *UMASK_ARGV,
                               'cp', '-a', '/source/.', '/work/'])
            if cfg['install_argv']:
                command(options + ['--name', installer, '--network', 'none', '--user', f'{uid}:{reader_gid}', '--mount',
                                   f'type=bind,source={artifacts},target=/artifacts,readonly', cfg['image'], *UMASK_ARGV,
                                   '/usr/bin/timeout', '--signal=KILL', str(max(1, int(deadline - time.monotonic())))]
                        + cfg['install_argv'])
        if 'service' in cfg:
            frozen = cfg['service']
            # Both containers remain network=none. Only this fixed, distinct-UID
            # core probe can observe the app's loopback, through a direct pipe.
            probe_uid = 65534 if uid != 65534 else 65533
            def http_observe(request, call_deadline, call_cap):
                service.check()
                spec = {**request, 'body': base64.b64encode(request['body']).decode(),
                        'port': frozen['port'], 'cap': call_cap,
                        'timeout_ms': max(1, int((call_deadline - time.monotonic()) * 1000) - 250)}
                argument = base64.b64encode(json.dumps(spec, separators=(',', ':')).encode()).decode()
                code, raw = _bounded(
                    ['docker', 'exec', '--user', f'{probe_uid}:{probe_uid}', '--workdir', '/',
                     '-e', 'NODE_OPTIONS=', '-e', 'NODE_PATH=', candidate_container, *UMASK_ARGV,
                     '/usr/bin/node', '-e', NODE_HTTP_PROBE, argument],
                    cwd=scratch, env=host_env, deadline=call_deadline,
                    cap=min(1500000, call_cap * 2 + 32768))
                if code:
                    raise RuntimeRefusal('candidate_http_probe_failed')
                try:
                    observed = json.loads(raw)
                    if isinstance(observed, dict) and isinstance(observed.get('error'), str):
                        raise RuntimeRefusal(observed['error'])
                    observed['body'] = base64.b64decode(observed['body'], validate=True)
                    _http_response(observed, call_cap)
                    return observed
                except (ValueError, TypeError, KeyError):
                    raise RuntimeRefusal('candidate_http_response_invalid') from None
            service = CandidateService(
                ['docker', 'exec', '--user', f'{uid}:{gid}', '--workdir', '/work',
                 '-e', 'PORT=' + str(frozen['port']), '-e', 'NODE_OPTIONS=', '-e', 'NODE_PATH=',
                 candidate_container, *UMASK_ARGV, '/usr/bin/node', *frozen['argv'][1:]],
                cwd=scratch, env=host_env, deadline=deadline)
            startup_deadline = min(deadline, time.monotonic() + 20)
            while True:
                service.check()
                try:
                    http_observe({'method': 'GET', 'path': '/', 'headers': {}, 'body': b''},
                                 min(startup_deadline, time.monotonic() + 2), cap)
                    break
                except RuntimeRefusal as exc:
                    if str(exc) != 'candidate_http_connection_refused' or time.monotonic() >= startup_deadline:
                        raise
                    time.sleep(.1)
            bridge.http_execute = http_observe
        code, output = command(["docker", "exec", '--user', f'{uid}:{reader_gid}', verifier, *UMASK_ARGV] + test_argv, required=False)
        # Drain any observation already accepted by the host before deciding.
        # A late transport/refusal result must not be hidden by a test report.
        bridge.close()
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        report = output
        if report_kind == 'junit-file':
            # --- graded plain-command mode: begin candidate-report ---
            # served: report_code, report = command(['docker', 'exec', verifier, *UMASK_ARGV, 'cat', report_path], required=False)
            if 'candidate_argv' in cfg and bridge.calls == 1 and bridge.error == 'verification_output_limit':
                # The one candidate pytest call exceeded the trusted cap; any report is unread.
                return candidate_error('verification_output_limit')
            report_gid = gid if 'candidate_argv' in cfg else reader_gid
            report_code, report = command(['docker', 'exec', '--user', f'{uid}:{report_gid}',
                                           report_container, *UMASK_ARGV, 'cat', report_path], required=False)
            if 'candidate_argv' in cfg:
                bridge.spawn_failure = False
                # Driver exits 0/1/2 only after relaying a bridge result; a refused
                # bridge call, missing or malformed report stays a refusal below.
                if (not bridge.error and bridge.calls == 1 and not report_code and code in (0, 1, 2)
                        and candidate_error_report(report, bridge.candidate_returncode)):
                    return candidate_error('verifier_did_not_complete_assertions')
            # --- graded plain-command mode: end candidate-report ---
            if report_code:
                raise RuntimeRefusal('structured_test_report_missing')
        outcome = structured_result(report_kind, report)
        if bridge.error or not bridge.calls:
            raise RuntimeRefusal(bridge.error or 'external_candidate_observation_required')
        if outcome == 'assertion' and bridge.spawn_failure:
            # The red came from a program the candidate could not start, not
            # from the behaviour under test; on the base this refuses before
            # any miner spend.
            raise RuntimeRefusal('candidate_subprocess_unavailable')
        if service is not None:
            service.check()
            if not bridge.http_observations:
                raise RuntimeRefusal('external_http_observation_required')
        if (code, outcome) not in {(0, 'pass'), (1, 'assertion')}:
            raise RuntimeRefusal('verifier_did_not_complete_assertions')
        return 86 if outcome == 'assertion' else 0
    finally:
        # Cleanup has its own small deadline even after the test exhausts its budget.
        for argv in (["docker", "rm", "-f", installer, verifier, candidate_container], ["docker", "volume", "rm", volume, driver_volume]):
            try:
                subprocess.run(argv, env=host_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if bridge is not None:
            bridge.close()
        if service is not None:
            service.close()
        for root, dirs, files in os.walk(scratch, topdown=True, followlinks=False):
            os.chmod(root, 0o700)
        shutil.rmtree(scratch)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        guard.terminate()
        guard.wait(timeout=5)


if __name__ == "__main__":
    import base64
    try:
        configuration = json.loads(base64.b64decode(sys.argv[1]))
        raise SystemExit(run_verifier(configuration))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, RuntimeRefusal) else "runtime_unavailable"
        sys.stderr.write("ormas-oci-verifier-v1: " + reason + "\n")
        raise SystemExit(124 if reason == "verification_timeout" else 98)
