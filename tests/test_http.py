"""Exercise the five routes over a real loopback socket with disposable files."""
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest

import httpx
import uvicorn
from api_server import create_app


class HttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.file = self.root / "lecture.txt"
        self.file.write_bytes(b"lesson")
        token = secrets.token_urlsafe(32)
        app = create_app(self.root, token)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error", proxy_headers=False))
        thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        def stop():
            self.server.should_exit = True
            thread.join(5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                self.fail("HTTP server failed to start")
            time.sleep(0.02)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False)
        self.addCleanup(self.client.close)
        self.auth = {"Authorization": f"Bearer {token}"}

    def plan(self):
        r = self.client.post("/plans", headers=self.auth, json={"directory": str(self.root)})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_round_trip(self):
        self.assertEqual(self.client.get("/health").json(), {"status":"ok"})
        plan = self.plan()
        path = "/plans/" + plan["plan_id"]
        self.assertEqual(self.client.get(path, headers=self.auth).json(), plan)
        self.assertEqual(list(self.root.iterdir()), [self.file])
        self.assertEqual(self.client.post(path + "/execute", headers=self.auth, json={"approved":False}).status_code, 409)
        r = self.client.post(path + "/execute", headers=self.auth, json={"approved":True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["moves"][0]["state"], "moved")
        self.assertFalse(self.file.exists())
        undo = "/operations/" + r.json()["operation_id"] + "/rollback"
        self.assertEqual(self.client.post(undo, headers=self.auth, json={}).status_code, 422)
        self.assertEqual(self.client.post(undo, headers=self.auth, json={"approved":True}).json()["moves"][0]["state"], "restored")
        self.assertEqual(self.file.read_bytes(), b"lesson")

    def test_access_and_validation(self):
        for method, path in [("POST","/plans"),("GET","/plans/x"),("POST","/plans/x/execute"),("POST","/operations/x/rollback")]:
            self.assertEqual(self.client.request(method,path,json={}).status_code,401)
        self.assertEqual(self.client.post("/plans",headers=self.auth,json={"directory":str(self.root.parent)}).status_code,403)
        self.assertEqual(self.client.get("/plans/unknown",headers=self.auth).status_code,404)
        self.assertEqual(self.client.post("/operations/unknown/rollback",headers=self.auth,json={"approved":True}).status_code,404)
        self.assertIn("/plans",self.client.get("/openapi.json").json()["paths"])

    def test_stale_and_strict_approval(self):
        path = "/plans/" + self.plan()["plan_id"] + "/execute"
        self.assertEqual(self.client.post(path,headers=self.auth,json={"approved":"true"}).status_code,422)
        self.file.write_bytes(b"modified")
        self.assertEqual(self.client.post(path,headers=self.auth,json={"approved":True}).status_code,409)
        self.assertEqual(list(self.root.iterdir()),[self.file])
