"""Synthetic SSE transport and clock: no real services, credentials or desktop."""
import ast
import hashlib
import json
from pathlib import Path
import threading
import types
import unittest
from unittest.mock import Mock

SOURCE=Path(__file__).resolve().parents[1]/"wechat-weflow-bridge-ob11"


def source_function(filename,name,namespace):
    tree=ast.parse((SOURCE/filename).read_text(encoding="utf-8-sig"))
    function=next(node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name==name)
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(SOURCE/filename),"exec"),namespace)
    return namespace[name]


class Clock:
    def __init__(self):
        self.now=0.0
        self.sleeps=[]
    def monotonic(self):
        return self.now
    def sleep(self,seconds):
        self.sleeps.append(seconds)
        self.now+=seconds


class CancelWait:
    def __init__(self,clock):
        self.clock=clock
        self.waits=[]
        self.set=False
        self.on_wait=lambda:None
    def wait(self,seconds):
        self.waits.append(seconds)
        self.on_wait()
        if not self.set:
            self.clock.now+=seconds
        return self.set


class SseReconnectTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock()
        self.cancel=CancelWait(self.clock)
        self.state=types.SimpleNamespace(running=True,bridge_generation=7,
            send_cancel=self.cancel,bridge_lock=threading.Lock(),run_lock=threading.RLock(),bridge_instance=None)
        self.log=Mock()
        self.http=Mock()
        self.names=types.SimpleNamespace(observe_event=Mock(return_value=False))
        namespace={"state":self.state,"http_util":self.http,"log":self.log,
            "config":types.SimpleNamespace(WE_FLOW_BASE_URL="http://127.0.0.1:5031",ACCESS_TOKEN="synthetic-token"),
            "json":json,"hashlib":hashlib,"name_refresh":self.names}
        listen=source_function("bridge_core.py","listen_sse",namespace)
        self.bridge=types.SimpleNamespace(_sse_session=None,_sse_response=None,start_timestamp=100,
            _mark_processed=Mock(return_value=False),should_ignore=Mock(return_value=False),
            add_to_buffer=Mock(),close=Mock(),_sched=types.SimpleNamespace(stop=Mock()))
        self.bridge.listen_sse=types.MethodType(listen,self.bridge)
        self.loop=source_function("main.py","_bridge_loop",{"state":self.state,"log":self.log,
            "WeFlowBridge":lambda sender:self.bridge,"time":self.clock})
        self.sessions=[]
        self.responses=[]

    def attempts(self,*statuses):
        for status in statuses:
            session=Mock()
            response=None
            if isinstance(status,Exception):
                session.get.side_effect=status
            else:
                response=Mock(status_code=status,encoding=None)
                response.iter_lines.return_value=[]
                response.text="synthetic-provider-diagnostic-not-to-log"
                session.get.return_value=response
            self.sessions.append(session)
            self.responses.append(response)
        self.http.new_session.side_effect=self.sessions

    def stop_after_waits(self,count):
        def stop():
            if len(self.cancel.waits)>=count:
                self.state.running=False
        self.cancel.on_wait=stop

    def assert_closed(self):
        for session in self.sessions:
            session.close.assert_called_once()
        for response in self.responses:
            if response is not None:
                response.close.assert_called_once()
        self.assertIsNone(self.bridge._sse_session)
        self.assertIsNone(self.bridge._sse_response)

    def test_403_503_and_request_exception_back_off_to_five_seconds(self):
        self.attempts(403,503,ConnectionError("synthetic transport failed"),403,503,403)
        self.stop_after_waits(6)
        self.loop(7,object())
        self.assertEqual(self.cancel.waits,[0.5,1,2,4,5,5])
        self.assertEqual(self.http.new_session.call_count,6)
        self.assert_closed()
        self.bridge.add_to_buffer.assert_not_called()
        self.bridge.close.assert_called_once()
        self.assertIsNone(self.state.bridge_instance)
        self.assertNotIn("synthetic-provider-diagnostic",str(self.log.mock_calls))
        self.assertNotIn("synthetic-token",str(self.log.mock_calls))

    def test_real_200_then_read_disconnect_resets_retry_to_first_step(self):
        self.attempts(403,503,200,503,403)
        self.responses[2].iter_lines.side_effect=ConnectionError("synthetic reader closed")
        self.stop_after_waits(5)
        self.loop(7,object())
        self.assertEqual(self.cancel.waits,[0.5,1,0.5,1,2])
        self.assert_closed()

    def test_uncaught_listener_exceptions_also_keep_backoff(self):
        self.bridge.listen_sse=Mock(side_effect=RuntimeError("synthetic listener failure"))
        self.stop_after_waits(3)
        self.loop(7,object())
        self.assertEqual(self.cancel.waits,[0.5,1,2])
        self.assertEqual(self.bridge.listen_sse.call_count,3)
        self.assertEqual(self.log.warning.call_count,3)
        self.http.new_session.assert_not_called()
        self.bridge.close.assert_called_once()

    def test_set_cancel_event_while_running_preserves_real_backoff(self):
        self.attempts(403,403)
        self.cancel.set=True
        response=self.responses[1]
        def stop_on_second_request(*args,**kwargs):
            self.state.running=False
            return response
        self.sessions[1].get.side_effect=stop_on_second_request
        self.loop(7,object())
        self.assertEqual(self.cancel.waits,[0.5])
        self.assertAlmostEqual(sum(self.clock.sleeps),0.5)
        self.assertTrue(all(0<seconds<=0.1 for seconds in self.clock.sleeps))
        self.assertEqual(self.http.new_session.call_count,2)
        self.assert_closed()

    def test_stop_or_generation_change_during_retry_prevents_another_connection(self):
        for stop in ("running","generation"):
            with self.subTest(stop=stop):
                self.setUp()
                self.attempts(403)
                if stop=="running":
                    self.cancel.on_wait=lambda:setattr(self.state,"running",False)
                else:
                    self.cancel.on_wait=lambda:setattr(self.state,"bridge_generation",8)
                self.loop(7,object())
                self.assertEqual(self.http.new_session.call_count,1)
                self.assert_closed()

    def test_generation_change_during_set_event_wait_exits_promptly(self):
        self.attempts(403)
        self.cancel.set=True
        sleep=self.clock.sleep
        def change_during_sleep(seconds):
            sleep(seconds)
            self.state.bridge_generation=8
        self.clock.sleep=change_during_sleep
        self.loop(7,object())
        self.assertEqual(self.cancel.waits,[0.5])
        self.assertEqual(self.clock.sleeps,[0.1])
        self.assertEqual(self.http.new_session.call_count,1)
        self.assert_closed()

    def test_old_generation_never_replaces_new_bridge_or_connects(self):
        self.state.bridge_generation=8
        current=object()
        self.state.bridge_instance=current
        self.loop(7,object())
        self.http.new_session.assert_not_called()
        self.assertIs(self.state.bridge_instance,current)
        self.bridge.close.assert_called_once()

    def test_listener_returns_false_for_http_failures_and_request_exception(self):
        for status in (403,503,ConnectionError("synthetic transport failed")):
            with self.subTest(status=type(status).__name__):
                self.setUp()
                self.attempts(status)
                self.assertIs(self.bridge.listen_sse(7),False)
                if self.responses[0] is not None:
                    self.responses[0].iter_lines.assert_not_called()
                self.assert_closed()

    def test_listener_returns_true_only_after_200_and_still_filters_events(self):
        self.attempts(200)
        event={"id":"synthetic-current","timestamp":101,"content":"synthetic event"}
        self.responses[0].iter_lines.return_value=["event: ready","", "data: not-json", "data: []",
            "data: "+json.dumps({"event":"message.revoke"}),
            "data: "+json.dumps({"id":"synthetic-old","timestamp":90}),"data: "+json.dumps(event)]
        self.assertIs(self.bridge.listen_sse(7),True)
        self.bridge.add_to_buffer.assert_called_once_with(event)
        self.bridge._mark_processed.assert_called_once()
        self.assert_closed()

    def test_stop_or_generation_change_before_headers_never_becomes_connected(self):
        for stop in ("running","generation"):
            with self.subTest(stop=stop):
                self.setUp()
                self.attempts(200)
                def change_during_get(*args,**kwargs):
                    if stop=="running":self.state.running=False
                    else:self.state.bridge_generation=8
                    return self.responses[0]
                self.sessions[0].get.side_effect=change_during_get
                self.assertIs(self.bridge.listen_sse(7),False)
                self.responses[0].iter_lines.assert_not_called()
                self.assert_closed()

    def test_listener_stop_before_request_creates_no_session(self):
        self.state.running=False
        self.assertIs(self.bridge.listen_sse(7),False)
        self.http.new_session.assert_not_called()

    def test_status_read_stop_or_generation_change_prevents_stream_publication(self):
        for stop in ("running","generation"):
            with self.subTest(stop=stop):
                self.setUp()
                self.attempts(200)
                state=self.state
                class SwitchingResponse:
                    encoding=None
                    close=Mock()
                    iter_lines=Mock(return_value=[])
                    @property
                    def status_code(self):
                        with state.run_lock:
                            if stop=="running":state.running=False
                            else:state.bridge_generation=8
                        return 200
                response=SwitchingResponse()
                self.responses[0]=response
                self.sessions[0].get.return_value=response
                self.assertIs(self.bridge.listen_sse(7),False)
                response.iter_lines.assert_not_called()
                self.assert_closed()

    def test_exception_text_never_logs_an_old_token_or_provider_body(self):
        secret="synthetic-old-token-canary"
        diagnostic="synthetic-provider-body-canary"
        text="http://127.0.0.1:5031/push?access_token="+secret+" "+diagnostic
        for stage in ("request","read","loop"):
            with self.subTest(stage=stage):
                self.setUp()
                if stage=="request":
                    self.attempts(ValueError(text))
                    self.assertIs(self.bridge.listen_sse(7),False)
                    self.assert_closed()
                elif stage=="read":
                    self.attempts(200)
                    self.responses[0].iter_lines.side_effect=ValueError(text)
                    self.assertIs(self.bridge.listen_sse(7),True)
                    self.assert_closed()
                else:
                    self.bridge.listen_sse=Mock(side_effect=ValueError(text))
                    self.stop_after_waits(1)
                    self.loop(7,object())
                logged=str(self.log.mock_calls)
                self.assertIn("ValueError",logged)
                self.assertNotIn(secret,logged)
                self.assertNotIn(diagnostic,logged)

    def test_cancelled_read_after_200_returns_true_closes_and_logs_no_error(self):
        self.attempts(200)
        def cancelled_read(**kwargs):
            self.state.bridge_generation=8
            raise AttributeError("synthetic stopped reader")
        self.responses[0].iter_lines.side_effect=cancelled_read
        self.assertIs(self.bridge.listen_sse(7),True)
        self.log.error.assert_not_called()
        self.assert_closed()


if __name__=="__main__":unittest.main()
