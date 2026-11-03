"""Native controls must work independently of optional recovery build profiles."""
import unittest

import test_uia_sender as fixtures


class UiaVersionIndependentTests(unittest.TestCase):
    setUp = fixtures.UiaTests.setUp
    tearDown = fixtures.UiaTests.tearDown
    load_moments = fixtures.MomentsTests.load_moments

    def test_new_qt_main_window_with_complete_controls_needs_no_recovery(self):
        gate = fixtures.FakeGate(False)
        self.sender._gate_manager = gate
        self.auto.main.ClassName = "Qt69999QWindowIcon"
        self.auto.main.Name = "Weixin"
        self.assertTrue(self.sender.send_text("Alice", "你好"))
        self.assertEqual(self.auto.delivered, [("Alice", "你好")])
        self.assertEqual(gate.ensures, [])
        self.assertNotIn("{Tab}", self.auto.keys)
        self.assertEqual(self.auto.main.NativeWindowHandle, 100)

    def test_moments_with_complete_controls_needs_no_recovery_profile(self):
        fixtures.MomentsTests.embedded_fixture(self)
        gate = fixtures.FakeGate(False)
        self.moment._gate_manager = gate
        self.auto.main.ClassName = "Qt69999QWindowIcon"
        self.auto.main.Name = "Weixin"
        self.assertTrue(self.moment.send_moment("今天挺好。"))
        self.assertEqual(self.moment.last_send_result()["status"], "accepted_by_ui")
        self.assertEqual(gate.ensures, [])
        self.assertNotIn("{Tab}", self.auto.keys)
        self.assertEqual(self.auto.main.NativeWindowHandle, 100)


if __name__ == "__main__":
    unittest.main()
