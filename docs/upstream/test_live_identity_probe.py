"""Pure HTTP stubs only. No real localhost/API, token or account data."""
from pathlib import Path
import contextlib
import importlib.util
import io
import json
import unittest

script=Path(__file__).resolve().with_name("verify_live_identity_api.py")
spec=importlib.util.spec_from_file_location("live_probe",script)
live_probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_probe)

class ProbeTests(unittest.TestCase):
    def snapshot(self):
        return {"status":200,"etag":'"'+"a"*64+'"',"body":{"success":True,"version":1,"fresh":True,
            "capabilities":{"freshIdentitySnapshot":1},"revision":"a"*64,
            "contacts":[{"username":"wxid_private_test","displayName":"PRIVATE_NAME","remark":"PRIVATE_REMARK","nickname":"PRIVATE_NICK","alias":"PRIVATE_ALIAS","type":"friend"},
                        {"username":"private_group@chatroom","displayName":"PRIVATE_GROUP","remark":"","nickname":"PRIVATE_GROUP","alias":"","type":"group"}],
            "sessions":[{"username":"private_group@chatroom","displayName":"PRIVATE_GROUP","type":2}]}}

    def request_stub(self, snapshot=None, auth_status=401, conditional=None):
        calls=[]
        def request(address, endpoint, headers, timeout, max_body):
            calls.append((address,endpoint,headers,timeout,max_body))
            if endpoint=="/api/v1/health":return {"status":200,"body":{"status":"ok"}}
            if not headers:return {"status":auth_status,"body":{"error":"PRIVATE_SERVER_ERROR"}}
            if "If-None-Match" in headers:return conditional or {"status":304,"etag":'"'+"a"*64+'"'}
            return snapshot or self.snapshot()
        return request,calls

    def test_summary_only_and_no_history_or_sse_requests(self):
        request,calls=self.request_stub()
        report=live_probe.probe("http://localhost:5031","PRIVATE_TOKEN",request=request)
        encoded=json.dumps(report)
        self.assertTrue(report["ok"])
        for secret in ("PRIVATE_TOKEN","PRIVATE_NAME","PRIVATE_GROUP","PRIVATE_REMARK","PRIVATE_NICK","PRIVATE_ALIAS","wxid_private_test","private_group@chatroom","PRIVATE_SERVER_ERROR"):
            self.assertNotIn(secret,encoded)
        self.assertEqual(len(calls),4)
        self.assertEqual({entry[1] for entry in calls},{"/api/v1/health","/api/v1/identity-snapshot"})
        self.assertTrue(all(entry[0]==("127.0.0.1",5031) for entry in calls))
        self.assertFalse(report["sse"]["runtime_fields_verified"])
        self.assertEqual(report["identity"]["contacts_count"],2)

    def test_external_url_redirect_credentials_and_token_query_refused(self):
        for url in ("https://127.0.0.1:5031","http://example.com:5031","http://127.0.0.1:5031/?access_token=PRIVATE_TOKEN",
                    "http://PRIVATE_TOKEN@127.0.0.1:5031","http://127.0.0.1:5031/api/v1/messages","http://127.0.0.1:0"):
            with self.assertRaises(ValueError):live_probe.probe(url,"PRIVATE_TOKEN",request=lambda *a:self.fail("request forbidden"))

    def test_bad_contract_and_duplicate_ids_fail_without_printing_server_body(self):
        snapshot=self.snapshot();snapshot["body"]["contacts"].append(snapshot["body"]["contacts"][0])
        request,calls=self.request_stub(snapshot=snapshot)
        report=live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",request=request)
        self.assertFalse(report["ok"]);self.assertFalse(report["identity"]["actual_ids_valid_and_unique"])
        self.assertEqual(len(calls),3)
        self.assertNotIn("PRIVATE_NAME",json.dumps(report))
        snapshot=self.snapshot();snapshot["body"]["version"]=True
        self.assertFalse(live_probe._identity_summary(snapshot)["valid_contract"])

    def test_unauthenticated_success_is_failure(self):
        request,_=self.request_stub(auth_status=200)
        report=live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",request=request)
        self.assertFalse(report["ok"]);self.assertFalse(report["authentication"]["unauthenticated_rejected"])

    def test_legitimate_concurrent_change_200_is_accepted(self):
        changed=self.snapshot();changed["body"]["revision"]="b"*64;changed["etag"]='"'+"b"*64+'"'
        request,_=self.request_stub(conditional=changed)
        report=live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",request=request)
        self.assertTrue(report["ok"]);self.assertTrue(report["conditional"]["changed_valid_snapshot"])

    def test_arbitrary_etag_not_echoed_and_no_redirect_follow(self):
        snapshot=self.snapshot();snapshot["etag"]="PRIVATE_SERVER_HEADER"
        request,calls=self.request_stub(snapshot=snapshot)
        report=live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",request=request)
        self.assertFalse(report["ok"]);self.assertEqual(len(calls),3);self.assertNotIn("PRIVATE_SERVER_HEADER",json.dumps(report))
        request,_=self.request_stub(snapshot={"status":302,"body":{"error":"PRIVATE_REDIRECT"}})
        report=live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",request=request)
        self.assertFalse(report["ok"]);self.assertNotIn("PRIVATE_REDIRECT",json.dumps(report))

    def test_missing_token_bad_limits_and_header_injection_refused(self):
        for token in ("", "one\r\ntwo", "x"*4097):
            with self.assertRaises(ValueError):live_probe.probe(live_probe.DEFAULT_BASE,token,request=lambda *a:self.fail("request forbidden"))
        with self.assertRaises(ValueError):live_probe.probe(live_probe.DEFAULT_BASE,"PRIVATE_TOKEN",timeout=60,request=lambda *a:self.fail("request forbidden"))

    def test_argument_errors_do_not_echo_accidental_secrets(self):
        output=io.StringIO()
        with contextlib.redirect_stderr(output),self.assertRaises(SystemExit):
            live_probe.main(["--timeout","PRIVATE_TOKEN"])
        self.assertNotIn("PRIVATE_TOKEN",output.getvalue())
        self.assertIn("invalid_arguments",output.getvalue())

if __name__=="__main__":unittest.main()
