#!/usr/bin/env python3
"""End-to-end acceptance run: the real server, a real socket, a real node.

This starts `serve.py` as a subprocess against a loopback Nano node stub,
so the HTTP layer, the node client and the store are all exercised as they
would be in production. Nothing leaves the loopback interface.

    python3 e2e_check.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "ledger"))
from money import format_xno  # noqa: E402

TOKEN = "e2e-token-generated-for-this-run-only"
SELLER = "nano_11131a3ia3a81w61k4id3i8iw5ri46b3871o4rdji8at5eg3t9izij86w3hz"
OTHER = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
XNO = 10 ** 30
CHECKS = []
BLOCKS = {}
NODE_ACTIONS = []


def block(prefix):
    return (prefix + "0123456789ABCDEF" * 4)[:64]


class NodeStub(BaseHTTPRequestHandler):
    """A Nano node that answers block_info and refuses to move money."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        NODE_ACTIONS.append(payload.get("action"))
        if payload.get("action") != "block_info":
            answer = {"error": "this stub refuses %r" % payload.get("action")}
        else:
            answer = BLOCKS.get(payload.get("hash"), {"error": "Block not found"})
        body = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def add_block(block_hash, destination, amount_raw, confirmed=True, subtype="send"):
    BLOCKS[block_hash] = {
        "amount": str(amount_raw),
        "confirmed": "true" if confirmed else "false",
        "subtype": subtype,
        "contents": {"link_as_account": destination},
    }
    return block_hash


def check(label, condition, detail=""):
    CHECKS.append(bool(condition))
    print("[%s] %s%s" % ("pass" if condition else "FAIL", label,
                         "" if condition else "\n       " + str(detail).strip()))


def call(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            return response.status, raw, response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8"), error.headers


def auth(body=None):
    return {"X-Ledger-Token": TOKEN}


def main():
    print("receipt ledger - end-to-end acceptance run\n")

    node = ThreadingHTTPServer(("127.0.0.1", 0), NodeStub)
    threading.Thread(target=node.serve_forever, daemon=True).start()
    node_url = "http://127.0.0.1:%d" % node.server_address[1]

    workdir = tempfile.mkdtemp()
    store_path = os.path.join(workdir, "ledger.json")
    port = _free_port()
    base = "http://127.0.0.1:%d" % port
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "serve.py"), "--port", str(port),
         "--store", store_path, "--node", node_url, "--base-url", base],
        env=dict(os.environ, RECEIPT_LEDGER_TOKEN=TOKEN),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(base)
        _run_checks(base, store_path, server)
    finally:
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        node.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for ok in CHECKS if ok)
    print("\n%d/%d checks pass" % (passed, len(CHECKS)))
    return 0 if passed == len(CHECKS) else 1


def _free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(base, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            call(base, "GET", "/v1/custody")
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("the server never came up")


def _create(base, **over):
    payload = {"kind": "work_settlement", "counterparty": "arion",
               "counterparty_address": SELLER, "amount_xno": "0.050000",
               "reason": "delivered the job"}
    payload.update(over)
    status, body, _ = call(base, "POST", "/v1/receipts", payload, auth())
    return status, json.loads(body)


def _run_checks(base, store_path, server):
    # 9 - public reads need no auth at all, including before anything exists
    ok = True
    for path in ("/v1/receipts", "/v1/corrections", "/v1/custody"):
        status, _, _ = call(base, "GET", path)
        ok = ok and status == 200
    check("9  public reads need no auth", ok)

    # 10 - every write refuses without a valid token
    refused = []
    for token in (None, {"X-Ledger-Token": "wrong"}, {"X-Ledger-Token": ""}):
        status, _, _ = call(base, "POST", "/v1/receipts", {}, token)
        refused.append(status)
    check("10 writes need the token", refused == [401, 401, 401], refused)

    # 7 - a bad address creates nothing
    _, before, _ = call(base, "GET", "/v1/receipts")
    altered = SELLER[:-1] + ("a" if SELLER[-1] != "a" else "b")
    status, body, _ = call(base, "POST", "/v1/receipts", {
        "kind": "tip", "counterparty": "eddie_researcher",
        "counterparty_address": altered, "amount_xno": "0.010000",
        "reason": "must never be stored"}, auth())
    _, after, _ = call(base, "GET", "/v1/receipts")
    parsed = json.loads(body)
    check("7  a bad-checksum address is refused and creates nothing",
          status == 400 and parsed["error"] == "invalid_address"
          and parsed["detail"]["reason"] == "bad_checksum"
          and json.loads(before)["totals"] == json.loads(after)["totals"], body)

    status, receipt = _create(base)
    check("0  a receipt is created pending, with no block hash",
          status == 201 and receipt["block_hash"] is None
          and receipt["confirmed"] is False, receipt)

    # 3 - an unconfirmed block is not attached
    unconfirmed = add_block(block("0001"), SELLER, receipt["amount_raw"], confirmed=False)
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                           {"block_hash": unconfirmed}, auth())
    _, detail, _ = call(base, "GET", "/v1/receipts/%s" % receipt["id"])
    check("3  an unconfirmed block is not attached",
          status == 409 and json.loads(body)["error"] == "block_unconfirmed"
          and json.loads(detail)["block_hash"] is None, body)

    # 2 - amount and destination must match
    off_by_one = add_block(block("0002"), SELLER, int(receipt["amount_raw"]) + 1)
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                           {"block_hash": off_by_one}, auth())
    parsed = json.loads(body)
    check("2a a block one raw off is refused, and the detail names 'amount'",
          status == 409 and parsed["error"] == "block_mismatch"
          and "amount" in parsed["detail"], body)

    wrong_payee = add_block(block("0003"), OTHER, receipt["amount_raw"])
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                           {"block_hash": wrong_payee}, auth())
    parsed = json.loads(body)
    check("2b a block paying somebody else is refused, detail names 'destination'",
          status == 409 and "destination" in parsed["detail"], body)

    a_receive = add_block(block("0004"), SELLER, receipt["amount_raw"], subtype="receive")
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                           {"block_hash": a_receive}, auth())
    check("2c a confirmed receive pays nobody and is refused",
          status == 409 and "subtype" in json.loads(body)["detail"], body)

    _, detail, _ = call(base, "GET", "/v1/receipts/%s" % receipt["id"])
    check("2d after four refusals the receipt still carries no block",
          json.loads(detail)["block_hash"] is None, detail)

    # the good path
    good = add_block(block("9F2C"), SELLER, receipt["amount_raw"])
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % receipt["id"],
                           {"block_hash": good}, auth())
    settled = json.loads(body)
    check("0b a matching confirmed send attaches and confirms the receipt",
          status == 200 and settled["block_hash"] == good and settled["confirmed"], body)

    # 4 - a block cannot be reused
    _, second = _create(base, counterparty="bart")
    status, body, _ = call(base, "POST", "/v1/receipts/%s/attach-block" % second["id"],
                           {"block_hash": good}, auth())
    check("4  the same block cannot settle a second receipt",
          status == 409 and json.loads(body)["error"] == "block_reused"
          and json.loads(body)["detail"]["receipt_id"] == receipt["id"], body)

    # 5 - totals count confirmed only
    _, index, _ = call(base, "GET", "/v1/receipts")
    totals = json.loads(index)["totals"]
    check("5  totals count confirmed receipts only",
          totals["paid_xno_total"] == "0.050000" and totals["pending"] == 1
          and totals["confirmed"] == 1, totals)

    # 6 - exact decimal arithmetic
    for index_, amount in enumerate(("0.1", "0.2", "0.000001")):
        _, extra = _create(base, amount_xno=amount, counterparty="seller-%d" % index_)
        hash_ = add_block(block("1%03X" % index_), SELLER, extra["amount_raw"])
        call(base, "POST", "/v1/receipts/%s/attach-block" % extra["id"],
             {"block_hash": hash_}, auth())
    _, index, _ = call(base, "GET", "/v1/receipts")
    totals = json.loads(index)["totals"]
    check("6  0.05 + 0.1 + 0.2 + 0.000001 == 0.350001 exactly",
          totals["paid_xno_total"] == "0.350001", totals)

    # 1 - no mutation endpoints
    statuses = {}
    for method in ("PUT", "PATCH", "DELETE"):
        status, body, _ = call(base, method, "/v1/receipts/%s" % receipt["id"], {}, auth())
        statuses[method] = (status, json.loads(body).get("error"))
    check("1  PUT, PATCH and DELETE on a receipt all answer 405 append_only",
          all(v == (405, "append_only") for v in statuses.values()), statuses)

    # 8 - supersede appends, and never erases
    _, replacement = _create(base, reason="the corrected settlement")
    fixed = add_block(block("BEEF"), SELLER, replacement["amount_raw"])
    call(base, "POST", "/v1/receipts/%s/attach-block" % replacement["id"],
         {"block_hash": fixed}, auth())
    status, body, _ = call(base, "POST", "/v1/receipts/%s/supersede" % receipt["id"],
                           {"replacement_id": replacement["id"],
                            "reason": "the first settlement named the wrong job"}, auth())
    corrected = json.loads(body)
    _, corrections, _ = call(base, "GET", "/v1/corrections")
    entries = json.loads(corrections)["corrections"]
    check("8  a correction is an addition: the old block hash is untouched and "
          "the reason is public",
          status == 200 and corrected["superseded_by"] == replacement["id"]
          and corrected["block_hash"] == good and corrected["confirmed"]
          and len(entries) == 1 and entries[0]["receipt_id"] == receipt["id"]
          and "wrong job" in entries[0]["reason"], body + corrections)

    # 12 - the plaintext representation
    status, text, headers = call(base, "GET", "/v1/receipts/%s" % receipt["id"],
                                 headers={"Accept": "text/plain"})
    lines = text.rstrip("\n").split("\n")
    check("12 text/plain is <= 20 lines and carries the block, amount, address and verify",
          status == 200 and headers["Content-Type"].startswith("text/plain")
          and len(lines) <= 20 and good in text and "0.050000" in text
          and SELLER in text and "verify" in text, text)

    # 13 - the custody document is honest
    _, custody, _ = call(base, "GET", "/v1/custody")
    document = json.loads(custody)
    check("13 /v1/custody states what we do hold and at least four things we cannot do",
          "Our own funding account's key" in document["what_we_do_hold"]
          and len(document["what_we_cannot_do"]) >= 4, custody)

    # 14 - the published verify instructions actually work
    _, detail, _ = call(base, "GET", "/v1/receipts/%s" % receipt["id"])
    document = json.loads(detail)
    example = document["verify"]["node_rpc_example"]
    payload = json.loads(example[example.index("{"):example.rindex("}") + 1])
    node_url = example.rsplit(" ", 1)[-1]
    request = urllib.request.Request(
        node_url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        answer = json.loads(response.read().decode("utf-8"))
    expect = document["verify"]["expect"]
    check("14 the curl line we publish, run verbatim against the node, returns what "
          "the receipt says",
          answer["contents"]["link_as_account"] == expect["destination"]
          and format_xno(int(answer["amount"])) == expect["amount_xno"]
          and answer["subtype"] == expect["subtype"]
          and answer["confirmed"] == expect["confirmed"],
          json.dumps(answer) + json.dumps(expect))

    # 11 - the service never asked the node to do anything but read
    check("11 the node was only ever asked for block_info",
          set(NODE_ACTIONS) == {"block_info"}, sorted(set(NODE_ACTIONS)))

    # the store holds no token, and survives a restart
    with open(store_path, encoding="utf-8") as handle:
        stored = handle.read()
    check("15 the token is nowhere in the store on disk", TOKEN not in stored)
    check("16 the store on disk carries the settled receipt",
          good in stored and json.loads(stored)["corrections"], store_path)


if __name__ == "__main__":
    sys.exit(main())
