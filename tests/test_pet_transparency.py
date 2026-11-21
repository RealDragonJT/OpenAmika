"""Offline lifecycle checks; native Windows coverage is in windows_pet_transparency_probe.py."""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from test_pet import pet


class Event:
    def __init__(self):
        self.handlers = []
        self.fail = False

    def __iadd__(self, handler):
        if self.fail:
            raise RuntimeError('subscription failed')
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        if handler in self.handlers:
            self.handlers.remove(handler)
        return self

    def fire(self, args=None):
        for handler in self.handlers[:]:
            handler(None, args)


class Form:
    def __init__(self):
        self.Disposed, self.HandleCreated, self.HandleDestroyed = Event(), Event(), Event()
        self.IsDisposed = self.Disposing = False
        self.IsHandleCreated = True
        self.queue = []
        self.fail_invoke = False
        self.Invalidate = mock.Mock()
        self.on_system_theme_changed = mock.Mock()

    def BeginInvoke(self, callback):
        if self.fail_invoke:
            raise RuntimeError('handle went away')
        self.queue.append(callback)

    def drain(self):
        queue, self.queue = self.queue, []
        for callback in queue:
            callback()


class TransparencyTests(unittest.TestCase):
    def setUp(self):
        self.events = SimpleNamespace(**{name: Event() for name in (
            'UserPreferenceChanged', 'DisplaySettingsChanged', 'PowerModeChanged', 'SessionSwitch')})
        self.sessions = SimpleNamespace(SessionUnlock=1, ConsoleConnect=2, RemoteConnect=3, SessionLock=4)
        modules = {
            'Microsoft.Win32': SimpleNamespace(SystemEvents=self.events, PowerModes=SimpleNamespace(Resume=1),
                                               SessionSwitchReason=self.sessions),
            'System': SimpleNamespace(Action=lambda fn: fn, InvalidOperationException=RuntimeError),
        }
        patch = mock.patch.dict('sys.modules', modules)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(pet, 'see_through')
        self.apply = patch.start()
        self.addCleanup(patch.stop)
        self.form = Form()
        self.events.UserPreferenceChanged += self.form.on_system_theme_changed

    def install(self):
        guard = pet.PetTransparency(self.form)
        self.addCleanup(guard.dispose)
        self.apply.reset_mock()
        return guard

    def test_startup_replaces_only_this_forms_theme_handler(self):
        other = mock.Mock()
        self.events.UserPreferenceChanged += other
        guard = pet.PetTransparency(self.form)
        self.addCleanup(guard.dispose)
        self.apply.assert_called_once_with(self.form)
        self.events.UserPreferenceChanged.fire()
        self.form.drain()
        self.form.on_system_theme_changed.assert_not_called()
        other.assert_called_once()
        self.assertEqual(self.apply.call_count, 2)

    def test_worker_event_burst_runs_one_repair_on_ui_thread(self):
        self.install()
        threads = [threading.Thread(target=self.events.UserPreferenceChanged.fire) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.apply.assert_not_called()
        self.assertEqual(len(self.form.queue), 1)
        thread_ids = []
        self.apply.side_effect = lambda form: thread_ids.append(threading.get_ident())
        self.form.drain()
        self.assertEqual(thread_ids, [threading.get_ident()])
        self.form.Invalidate.assert_called_once()
        self.events.UserPreferenceChanged.fire()
        self.form.drain()
        self.assertEqual(len(thread_ids), 2)

    def test_resume_unlock_reconnect_and_display_repair_but_suspend_and_lock_do_not(self):
        self.install()
        self.events.PowerModeChanged.fire(SimpleNamespace(Mode=3))
        self.events.PowerModeChanged.fire(SimpleNamespace(Mode=2))
        self.events.SessionSwitch.fire(SimpleNamespace(Reason=self.sessions.SessionLock))
        self.assertEqual(self.form.queue, [])
        triggers = [(self.events.PowerModeChanged, SimpleNamespace(Mode=1)),
                    (self.events.DisplaySettingsChanged, None)]
        triggers += [(self.events.SessionSwitch, SimpleNamespace(Reason=value)) for value in (1, 2, 3)]
        for event, args in triggers:
            event.fire(args)
            self.form.drain()
        self.assertEqual(self.apply.call_count, len(triggers))

    def test_handle_recreation_invalidates_stale_work_and_uses_new_handle(self):
        self.install()
        self.events.UserPreferenceChanged.fire()
        self.form.IsHandleCreated = False
        self.form.HandleDestroyed.fire()
        self.events.UserPreferenceChanged.fire()
        self.assertEqual(len(self.form.queue), 1)
        self.form.IsHandleCreated = True
        self.form.HandleCreated.fire()
        self.assertEqual(len(self.form.queue), 2)
        self.form.drain()
        self.apply.assert_called_once_with(self.form)

    def test_dispose_cancels_queued_work_detaches_events_and_is_idempotent(self):
        guard = self.install()
        self.events.UserPreferenceChanged.fire()
        self.form.IsDisposed = True
        self.form.Disposed.fire()
        guard.dispose()
        self.form.drain()
        guard.request()                        # an event already in flight at disposal
        self.apply.assert_not_called()
        for event in (*vars(self.events).values(), self.form.Disposed, self.form.HandleCreated,
                      self.form.HandleDestroyed):
            self.assertEqual(event.handlers, [])

    def test_disposing_window_is_ignored(self):
        self.install()
        self.form.Disposing = True
        self.events.UserPreferenceChanged.fire()
        self.assertEqual(self.form.queue, [])

    def test_begininvoke_failure_does_not_block_next_event(self):
        self.install()
        self.form.fail_invoke = True
        self.events.UserPreferenceChanged.fire()
        self.form.fail_invoke = False
        self.events.UserPreferenceChanged.fire()
        self.form.drain()
        self.apply.assert_called_once()

    def test_failed_repair_is_reported_and_next_event_retries(self):
        self.install()
        self.apply.side_effect = [OSError('DWM unavailable'), None]
        with mock.patch('sys.stderr') as stderr:
            self.events.DisplaySettingsChanged.fire()
            self.form.drain()
        self.assertIn('could not restore transparency', str(stderr.write.call_args_list))
        self.events.DisplaySettingsChanged.fire()
        self.form.drain()
        self.assertEqual(self.apply.call_count, 2)
        self.form.Invalidate.assert_called_once()

    def test_failed_install_rolls_back_subscriptions_and_preserves_dependency_handler(self):
        self.events.SessionSwitch.fail = True
        with self.assertRaisesRegex(RuntimeError, 'subscription failed'):
            pet.PetTransparency(self.form)
        self.apply.assert_called_once_with(self.form)  # startup remains transparent without recovery events
        self.assertEqual(self.events.UserPreferenceChanged.handlers, [self.form.on_system_theme_changed])
        for event in (self.events.DisplaySettingsChanged, self.events.PowerModeChanged, self.form.Disposed,
                      self.form.HandleCreated, self.form.HandleDestroyed):
            self.assertEqual(event.handlers, [])

    def test_failed_initial_repair_preserves_dependency_handler(self):
        self.apply.side_effect = OSError('DWM unavailable')
        with self.assertRaises(OSError):
            pet.PetTransparency(self.form)
        self.assertEqual(self.events.UserPreferenceChanged.handlers, [self.form.on_system_theme_changed])
        self.assertEqual(self.events.SessionSwitch.handlers, [])


if __name__ == '__main__':
    unittest.main()
