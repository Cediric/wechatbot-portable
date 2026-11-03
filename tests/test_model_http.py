"""Loopback HTTP only: keep-alive, parallel credentials, cancellation and timeout."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import types
import unittest
from unittest.mock import patch

from test_model_client import load


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.1"

    def log_message(self,*args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError,ConnectionAbortedError):
            # Cancelling/closing a keep-alive connection is expected here.
            pass

    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.record_lock:
            self.server.records.append((self.client_address,self.headers.get("Authorization"),payload))
        self.server.received.set()
        status=401 if self.path.startswith("/unauthorized/") else 200
        body=json.dumps({"choices":[{"message":{"content":payload["model"]+" 最终正文"}}]},ensure_ascii=False).encode()
        if self.path.startswith("/parallel/"):
            try:
                self.server.parallel.wait(timeout=3)
            except threading.BrokenBarrierError:
                status=500
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        if self.path.startswith("/held/"):
            self.server.release.wait(timeout=3)
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server=ThreadingHTTPServer(("127.0.0.1",0),ModelHandler)
        self.server.daemon_threads=True
        self.server.record_lock=threading.Lock()
        self.server.records=[]
        self.server.received=threading.Event()
        self.server.release=threading.Event()
        self.server.parallel=threading.Barrier(2)
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.server_thread.start()
        self.base="http://127.0.0.1:"+str(self.server.server_port)
        catalog=load("local_http_catalog","model_catalog.py")
        self.config=types.SimpleNamespace()
        for profile,(prefix,*_) in catalog.PROFILES.items():
            for suffix,value in [("api_base",self.base+"/v1"),("api_key","synthetic-"+profile),("model",profile+"-model"),("context_tokens",8192),("max_tokens",512),("thinking","auto")]:
                setattr(self.config,(prefix+"_"+suffix).upper(),value)
        self.http=load("local_http_util","http_util.py")
        modules=patch.dict(sys.modules,{"config":self.config,"http_util":self.http,"model_catalog":catalog})
        modules.start();self.addCleanup(modules.stop)
        self.client=load("local_http_client","model_client.py")

    def tearDown(self):
        self.server.release.set()
        self.http._close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def request(self,profile="main",**kwargs):
        return self.client.request(profile,[{"role":"user","content":"合成本地输入 "+profile}],**kwargs)

    def set_path(self,path):
        self.config.MAIN_LLM_API_BASE=self.base+path
        self.config.MEMORY_LLM_API_BASE=self.base+path

    def test_keepalive_reuses_connection_and_headers_remain_per_request(self):
        self.assertEqual(self.request(),"main-model 最终正文")
        self.assertEqual(self.request("memory"),"memory-model 最终正文")
        self.assertEqual(len(self.server.records),2)
        first,second=self.server.records
        self.assertEqual(first[0],second[0],"sequential calls must reuse one TCP connection")
        self.assertEqual([first[1],second[1]],["Bearer synthetic-main","Bearer synthetic-memory"])
        self.assertNotIn("Authorization",self.http.session().headers)
        self.assertEqual(first[2]["model"],"main-model")
        self.assertEqual(second[2]["model"],"memory-model")

    def test_parallel_workers_do_not_lock_other_sessions_or_share_credentials(self):
        self.set_path("/parallel/v1")
        sessions=[]
        def call(profile):
            current=self.http.session()
            sessions.append(current)
            try:
                return self.request(profile)
            finally:
                current.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(call,profile) for profile in ("main","memory")]
            results=[future.result(timeout=5) for future in futures]
        self.assertEqual(results,["main-model 最终正文","memory-model 最终正文"])
        self.assertIsNot(sessions[0],sessions[1])
        self.assertEqual(len({item[0] for item in self.server.records}),2)
        self.assertEqual({(auth,body["model"]) for _,auth,body in self.server.records},
                         {("Bearer synthetic-main","main-model"),("Bearer synthetic-memory","memory-model")})

    def test_cancel_during_blocked_read_discards_body_without_another_paid_request(self):
        self.set_path("/held/v1")
        cancelled=threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.request,cancelled=cancelled)
            try:
                self.assertTrue(self.server.received.wait(timeout=2))
                cancelled.set()
            finally:
                self.server.release.set()
            with self.assertRaisesRegex(self.client.ModelError,"已取消"):
                future.result(timeout=3)
        self.assertEqual(len(self.server.records),1)

    def test_read_timeout_and_http_error_each_issue_only_one_request(self):
        self.set_path("/held/v1")
        with self.assertRaisesRegex(self.client.ModelError,"调用失败"):
            self.request(timeout=0.1)
        self.assertEqual(len(self.server.records),1)
        self.server.release.set()
        self.set_path("/unauthorized/v1")
        with self.assertRaisesRegex(self.client.ModelError,"HTTP 401"):
            self.request()
        self.assertEqual(len(self.server.records),2)
        self.assertEqual(self.http.session().get_adapter(self.base).max_retries.total,0)


if __name__=="__main__":unittest.main()
