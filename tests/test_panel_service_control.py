"""Panel service lifecycle uses mocked processes, models and desktop workers."""
import os
from pathlib import Path
import types
import unittest
from unittest.mock import Mock, patch

import test_lifecycle as fixture


class PanelServiceControlTests(unittest.TestCase):
    setUp = fixture.LifecycleTests.setUp

    def launcher(self):
        lifecycle = types.SimpleNamespace(stop_owned=Mock(return_value=True))
        launcher = types.SimpleNamespace(launch_astrbot=Mock(return_value=(None, 'http://127.0.0.1:6185')),
                                         _astrbot_lifecycle=lambda: lifecycle)
        return launcher, lifecycle

    def test_panel_callbacks_start_astrbot_then_bridge_and_stop_both_keep_memory(self):
        launcher, lifecycle = self.launcher()
        store = self.state.memory_store
        self.assertTrue(store.extraction_paused)
        self.assertIs(self.state.start_bridge, self.main._start_panel_services)
        self.assertIs(self.state.stop_bridge, self.main._stop_panel_services)
        def astrbot_start():
            self.assertFalse(self.state.running)
            return None, 'http://127.0.0.1:6185'
        launcher.launch_astrbot.side_effect = astrbot_start
        with patch.object(self.main, '_package_service_launcher', return_value=launcher):
            result = self.state.start_bridge()
            self.assertTrue(result['ok'])
            self.assertTrue(result['running'])
            self.assertFalse(store.extraction_paused)
            stopped = self.state.stop_bridge()
        lifecycle.stop_owned.assert_called_once_with(timeout=8)
        self.assertTrue(stopped['ok'])
        self.assertEqual(stopped['service_control']['state'], 'stopped')
        self.assertFalse(stopped['running'])
        self.assertIs(self.state.memory_store, store)
        self.assertTrue(store.enabled)
        self.assertTrue(store.extraction_paused)
        self.assertEqual(self.main.get_status()['memory_count'], 0)
        self.assertEqual(self.state.moments_interactions.stopped, 0)
        self.assertTrue(self.state.moments_interactions._cancel.is_set())
        self.assertTrue(self.state.autopilot._cancel.is_set())

    def test_stop_failure_remains_retryable_without_restart(self):
        launcher, lifecycle = self.launcher()
        lifecycle.stop_owned.side_effect = [RuntimeError('本包进程仍未退出'), True]
        self.main._start_bridge()
        with patch.object(self.main, '_package_service_launcher', return_value=launcher):
            failed = self.state.stop_bridge()
            self.assertFalse(failed['ok'])
            self.assertFalse(failed['running'])
            self.assertEqual(failed['service_control']['state'], 'failed')
            self.assertEqual(failed['service_control']['action'], 'stop')
            self.assertTrue(self.state.stop_bridge()['ok'])
        self.assertEqual(lifecycle.stop_owned.call_count, 2)
        launcher.launch_astrbot.assert_not_called()

    def test_complete_exit_after_stop_closes_only_owned_services_and_rejects_restart(self):
        launcher, lifecycle = self.launcher()
        with patch.object(self.main, '_package_service_launcher', return_value=launcher):
            self.state.start_bridge()
            self.state.stop_bridge()
            with patch('desktop_coordinator.restore_external_states', return_value=True):
                self.assertTrue(self.state.exit_panel_services()['ok'])
            self.assertFalse(self.state.start_bridge()['ok'])
            self.assertFalse(self.state.stop_bridge()['ok'])
            self.assertEqual(self.main.get_status()['service_control']['state'], 'exiting')
        self.assertEqual(lifecycle.stop_owned.call_count, 2)
        launcher.launch_astrbot.assert_called_once()

    def test_complete_exit_from_pause_can_retry_failed_restoration(self):
        self.main._start_bridge()
        self.main._pause_bridge()
        with patch.object(self.main, '_package_service_launcher', return_value=None), \
                patch('desktop_coordinator.restore_external_states', side_effect=[False, True]):
            failed = self.state.exit_panel_services()
            self.assertFalse(failed['ok'])
            self.assertIn('还原', failed['error'])
            self.assertEqual(self.main.get_status()['service_control']['state'], 'failed')
            self.assertTrue(self.state.exit_panel_services()['ok'])

    def test_start_failure_does_not_start_chat_or_resume_memory(self):
        launcher, _ = self.launcher()
        launcher.launch_astrbot.side_effect = RuntimeError('本包AstrBot启动失败')
        with patch.object(self.main, '_package_service_launcher', return_value=launcher):
            result = self.state.start_bridge()
        self.assertFalse(result['ok'])
        self.assertFalse(self.state.running)
        self.assertTrue(self.state.memory_store.extraction_paused)
        self.assertFalse(self.created_senders)

    def test_internal_reconnect_does_not_stop_astrbot(self):
        with patch.object(self.main, '_package_service_launcher') as launcher:
            self.main._start_bridge()
            self.main.update_config({'access_token': 'changed'})
            self.main._stop_bridge(user_requested=False)
        launcher.assert_not_called()
        self.assertTrue(self.state.memory_store.extraction_paused)

    def test_stop_then_start_resumes_owned_services(self):
        launcher, lifecycle = self.launcher()
        with patch.object(self.main, '_package_service_launcher', return_value=launcher):
            self.state.start_bridge()
            self.state.stop_bridge()
            result = self.state.start_bridge()
        self.assertTrue(result['ok'])
        self.assertEqual(launcher.launch_astrbot.call_count, 2)
        lifecycle.stop_owned.assert_called_once()
        self.assertFalse(self.state.memory_store.extraction_paused)

    def test_busy_control_has_no_second_service_effect(self):
        self.main._service_control_lock.acquire()
        self.addCleanup(self.main._service_control_lock.release)
        with patch.object(self.main, '_package_service_launcher') as launcher:
            result = self.state.stop_bridge()
        self.assertFalse(result['ok'])
        launcher.assert_not_called()

    def test_stopped_config_save_does_not_start_scheduler_or_resume_memory(self):
        self.state.autopilot.start = Mock()
        self.main.update_config({'memory_enabled': True})
        self.state.autopilot.start.assert_not_called()
        self.assertTrue(self.state.memory_store.extraction_paused)

    def test_external_source_mode_does_not_manage_unowned_astrbot(self):
        with patch.dict(os.environ, {'WECHATBOT_PORTABLE_ROOT': ''}), patch.dict('sys.modules', {'startup_launcher': Mock()}) as modules:
            result = self.state.stop_bridge()
            modules['startup_launcher'].launch_astrbot.assert_not_called()
        self.assertTrue(result['ok'])
        self.assertIn('外部 AstrBot', result['message'])

    def test_wrong_package_environment_cannot_manage_another_instance(self):
        with patch.dict(os.environ, {'WECHATBOT_PORTABLE_ROOT': str(Path(self.root) / 'other')}):
            result = self.state.stop_bridge()
        self.assertFalse(result['ok'])
        self.assertIn('路径不一致', result['error'])


if __name__ == '__main__':
    unittest.main()
