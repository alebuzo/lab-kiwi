#!/usr/bin/env python3
"""Minimal stdlib JSON-RPC mock of Kiwi TCMS for testing the k6 scripts.

Env: MOCK_PORT (8443), MOCK_ERROR_RATE (0..1, default 0), MOCK_LATENCY_MS (default 5,
applied to every call except the big listings), MOCK_SEED (default 1).
"""
import json
import os
import random
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "8443"))
ERROR_RATE = float(os.environ.get("MOCK_ERROR_RATE", "0"))
LATENCY = float(os.environ.get("MOCK_LATENCY_MS", "5")) / 1000
rnd = random.Random(int(os.environ.get("MOCK_SEED", "1")))

PLAN_ID = 16
CONFIRMED_ID = 2
U3 = [
    (7355, "test_guild_tcp_fsm_eviction_timers"),
    (11499, "test_guild_tcp_fsm_eviction_timers"),
    (11500, "test_gibson_mdio_cmd_list"),
    (11501, "test_tele_tunnel_rep_double_service_entry_config"),
]
N_GIBSON = 1380
N_TOTAL = 11500
N_PLAN = 9710

lock = threading.Lock()
sessions = set()
counters = {"run": 1000, "exec": 100000}
plan_added = set()
priorities = {}
WORDS = ["gibson", "guild", "tele", "mdio", "tcp", "fsm", "tunnel", "cmd", "pcie", "dma", "serdes", "fabric"]


def make_cases():
    cases = []
    for i in range(1, N_TOTAL + 1):
        if i in (7355, 11499, 11500):
            continue
        gib = i <= N_GIBSON
        w = "gibson" if gib else rnd.choice(WORDS[1:])
        cases.append({
            "id": i, "summary": f"test_{w}_{rnd.choice(WORDS[1:])}_{rnd.choice(WORDS[1:])}_{i}",
            "gib": gib, "in_plan": i <= N_PLAN or gib, "case_status": 1 if i % 25 == 0 else CONFIRMED_ID,
            "category": 1 if gib or i % 3 else 2,
        })
    for cid, name in U3:
        cases.append({"id": cid, "summary": name, "gib": "gibson" in name, "in_plan": True,
                      "case_status": CONFIRMED_ID, "category": 1})
    return cases


CASES = make_cases()
BY_ID = {c["id"]: c for c in CASES}
BY_SUMMARY = {}
for c in CASES:
    BY_SUMMARY.setdefault(c["summary"], []).append(c)


def serialize(c):
    cid = c["id"]
    return {
        "id": cid, "create_date": "2024-03-11 10:22:31", "is_automated": True,
        "script": "", "arguments": "", "extra_link": None, "summary": c["summary"],
        "requirement": None, "notes": "", "text": "Setup:\n- configure device\nAction:\n- run test\n" * 3,
        "case_status": c["case_status"], "case_status__name": "CONFIRMED" if c["case_status"] == CONFIRMED_ID else "PROPOSED",
        "category": c["category"], "category__name": "Functional" if c["category"] == 1 else "Performance",
        "priority": priorities.get(cid, 16), "priority__value": "Medium",
        "author": 3, "author__username": "makise", "default_tester": None, "reviewer": None,
        "setup_duration": None, "testing_duration": None, "expected_duration": 0.0,
    }


def filter_cases(f):
    if "pk" in f:
        c = BY_ID.get(f["pk"])
        res = [c] if c else []
        if "plan" in f and res and (c["id"] % 97 == 0) and c["id"] not in plan_added:
            return []
        return res
    if "summary" in f:
        res = BY_SUMMARY.get(f["summary"], [])
        pn = f.get("category__product__name")
        return res if pn in (None, "PVSDK") else []
    res = CASES
    if "plan" in f:
        res = [c for c in res if c["in_plan"]]
    if "summary__contains" in f:
        res = [c for c in res if f["summary__contains"] in c["summary"]]
    if f.get("category__name") == "Functional":
        res = [c for c in res if c["category"] == 1]
    if "category__product__name" in f and f["category__product__name"] != "PVSDK":
        res = []
    return res


def dispatch(method, p):
    f = p[0] if p and isinstance(p[0], dict) else {}
    if method == "TestCase.filter":
        res = filter_cases(f)
        if not f:
            return "[" + ",".join(json.dumps(serialize(c)) for c in res) + "]"
        return [serialize(c) for c in res]
    if method == "TestCase.update":
        with lock:
            if "priority" in p[1]:
                priorities[p[0]] = p[1]["priority"]
            if "case_status" in p[1] and p[0] in BY_ID:
                BY_ID[p[0]]["case_status"] = p[1]["case_status"]
        return {"id": p[0]}
    if method == "TestPlan.filter":
        return [{"id": PLAN_ID, "name": "Regressions"}] if f.get("name") == "Regressions" else []
    if method == "TestPlan.add_case":
        plan_added.add(p[1])
        return {"id": p[1]}
    if method == "Priority.filter":
        allp = [{"id": 15, "value": "High"}, {"id": 16, "value": "Medium"}, {"id": 17, "value": "Low"}]
        v = f.get("value__iexact") or f.get("value")
        return [x for x in allp if v is None or x["value"].lower() == str(v).lower()]
    if method == "TestCaseStatus.filter":
        allp = [{"id": 1, "name": "PROPOSED"}, {"id": CONFIRMED_ID, "name": "CONFIRMED"}]
        return [x for x in allp if f.get("name") in (None, x["name"])]
    if method == "Build.filter":
        return [{"id": 7, "name": "ASIC-mock-1"}, {"id": 9, "name": "ASIC-mock-2"}]
    if method == "TestRun.filter":
        return []
    if method == "TestRun.create":
        with lock:
            counters["run"] += 1
            return {"id": counters["run"], "summary": f.get("summary")}
    if method == "TestRun.add_case":
        with lock:
            counters["exec"] += 1
            return [{"id": counters["exec"], "case": p[1], "run": p[0], "status": 1}]
    if method in ("TestRun.update", "TestExecution.update"):
        return {"id": p[0]}
    if method == "TestExecution.add_comment":
        return {"id": 1, "comment": p[1]}
    if method.endswith(".filter"):
        return [{"id": 1, "name": method.split(".")[0]}]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, cookie=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", f"sessionid={cookie}; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n)
        if self.path.rstrip("/") != "/json-rpc":
            return self.send(404, "{}")
        try:
            req = json.loads(raw)
            method, params, rid = req["method"], req.get("params", []), req.get("id")
        except Exception:
            return self.send(400, '{"jsonrpc":"2.0","error":{"code":-32700,"message":"parse"},"id":null}')

        if method == "Auth.login":
            sid = uuid.uuid4().hex
            sessions.add(sid)
            return self.send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "result": sid}), cookie=sid)

        sid = (self.headers.get("Cookie") or "").split("sessionid=")[-1].split(";")[0]
        if sid not in sessions:
            if os.environ.get('MOCK_DEBUG'):
                print('auth fail', method, repr(self.headers.get('Cookie')), flush=True)
            return self.send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Authentication required"}}))

        if ERROR_RATE and rnd.random() < ERROR_RATE:
            kind = rnd.choice(["500", "502", "rpc", "garbage"])
            if kind in ("500", "502"):
                return self.send(int(kind), "oops")
            if kind == "rpc":
                return self.send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error"}}))
            return self.send(200, "<html>not json")

        if params and isinstance(params[0], dict) and params[0] == {} and method == "TestCase.filter":
            pass
        elif LATENCY:
            import time
            time.sleep(LATENCY)
        try:
            result = dispatch(method, params)
        except Exception as e:
            if os.environ.get('MOCK_DEBUG'):
                print('dispatch error', method, params, repr(e), flush=True)
            return self.send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}))
        if isinstance(result, str):
            return self.send(200, '{"jsonrpc":"2.0","id":%s,"result":%s}' % (json.dumps(rid), result))
        return self.send(200, json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"mock Kiwi on :{PORT} cases={len(CASES)} error_rate={ERROR_RATE}", flush=True)
    srv.serve_forever()
