# -*- coding: utf-8 -*-
"""
가짜 VCF Operations (시험용). suite-api 가운데 tcrun 이 쓰는 호출만 흉내 낸다: 토큰 발급, 객체 조회, 속성 기록.
받은 요청은 메모리에 남기고(self.calls, self.props), 시험이 그 내용을 확인한다. 표준 라이브러리만 쓴다.

    srv = FakeOps(user="admin", password="pw", vms=["esx01ev01"], hosts=["esx01"])
    srv.start()          # 127.0.0.1 의 빈 포트. srv.base 는 http://127.0.0.1:<포트>
    ...
    srv.stop()
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class FakeOps(object):
    def __init__(self, user="admin", password="pw", vms=(), hosts=(), scheme="OpsToken", fail_props=()):
        self.user, self.password, self.scheme = user, password, scheme
        self.objects = dict((("VirtualMachine", n), "vm-%s" % n) for n in vms)
        self.objects.update((("HostSystem", n), "host-%s" % n) for n in hosts)
        self.fail_props = set(fail_props)        # 객체 id: 속성 기록을 500 으로 거절
        self.calls, self.props, self.logins = [], {}, []
        self.token = "tok-%d" % id(self)
        self.httpd, self.thread, self.base = None, None, ""

    def start(self):
        ops = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authed(self):
                return self.headers.get("Authorization", "") == "%s %s" % (ops.scheme, ops.token)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace")
                path = urlparse(self.path).path
                ops.calls.append(("POST", path))
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    return self.send(400, {"message": "bad json"})
                if path == "/suite-api/api/auth/token/acquire":
                    ops.logins.append((body.get("username"), body.get("authSource")))
                    if body.get("username") == ops.user and body.get("password") == ops.password:
                        return self.send(200, {"token": ops.token, "validity": 0})
                    return self.send(401, {"message": "The provided username/password or token is not valid."})
                if not self.authed():
                    return self.send(401, {"message": "no token"})
                parts = path.split("/")
                if len(parts) >= 6 and parts[1:4] == ["suite-api", "api", "resources"] and parts[5] == "properties":
                    rid = parts[4]
                    if rid in ops.fail_props:
                        return self.send(500, {"message": "internal error"})
                    if rid not in ops.objects.values():
                        return self.send(404, {"message": "no such object"})
                    for item in body.get("property-content") or []:
                        ops.props.setdefault(rid, {})[item.get("statKey")] = (item.get("values") or [None])[0]
                    return self.send(200, {})
                return self.send(404, {"message": "not found"})

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                ops.calls.append(("GET", u.path + ("?" + u.query if u.query else "")))
                if u.path != "/suite-api/api/resources":
                    return self.send(404, {"message": "not found"})
                if not self.authed():
                    return self.send(401, {"message": "no token"})
                kind = (q.get("resourceKind") or [""])[0]
                name = (q.get("name") or [""])[0]
                rows = [{"identifier": rid, "resourceKey": {"name": n, "adapterKindKey": "VMWARE", "resourceKindKey": k}}
                        for (k, n), rid in sorted(ops.objects.items())
                        if (not kind or k == kind) and (not name or n.lower() == name.lower())]
                return self.send(200, {"resourceList": rows[:int((q.get("pageSize") or ["1000"])[0])]})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
