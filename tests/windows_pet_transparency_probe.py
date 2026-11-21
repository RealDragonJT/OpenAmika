"""Opt-in Windows 11 native probe (not part of offline pytest).

Run with a Python that has pywebview/WebView2 installed:
    python -B tests/windows_pet_transparency_probe.py

Uses the actual installed BrowserForm in a hidden, disposable WebView2 window with inline HTML and a temporary
profile. No app server, audio, OS theme change, lock or suspend. SystemEvents notifications are replayed through
.NET Framework reflection *inside this process only*. This tests native recovery wiring, not real sleep/unlock.
"""
import ctypes
import importlib.metadata
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pet


def main():
    import webview

    list(webview.screens)  # load the same CLR/backend that the pet uses
    import clr
    from Microsoft.Win32 import (PowerModeChangedEventArgs, PowerModes, SessionSwitchEventArgs,
                                 SessionSwitchReason, SystemEvents, UserPreferenceCategory,
                                 UserPreferenceChangedEventArgs)
    from System import Array, EventArgs, Object
    from System.Reflection import BindingFlags
    from System.Windows.Forms import Control, Form, Timer

    Control.CheckForIllegalCrossThreadCalls = True
    flags = BindingFlags.NonPublic | BindingFlags.Static
    events_type = clr.GetClrType(SystemEvents)
    raise_event = next(m for m in events_type.GetMethods(flags)
                       if m.Name == 'RaiseEvent' and len(m.GetParameters()) == 2)

    def notify(name, args):
        key = events_type.GetField('On' + name + 'Event', flags).GetValue(None)
        raise_event.Invoke(None, Array[Object]([key, Array[Object]([None, args])]))

    dwm = ctypes.WinDLL('dwmapi')
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

    def backdrop(form, value=None):
        data = ctypes.c_int(value or 0)
        call = dwm.DwmGetWindowAttribute if value is None else dwm.DwmSetWindowAttribute
        hr = call(form.Handle.ToInt64(), 38, ctypes.byref(data), ctypes.sizeof(data))
        assert hr == 0, f'DWM attribute 38 unsupported/failed: {hr:#x}'
        return data.value

    results, failures = [], []
    original_apply = pet.see_through
    ui_thread, calls = [], []

    def measured_apply(form):
        assert threading.get_ident() == ui_thread[0], 'repair ran off the UI thread'
        original_apply(form)
        calls.append(form.Handle.ToInt64())

    pet.see_through = measured_apply
    window = webview.create_window('OpenAmika transparency probe (hidden)',
                                   html='<html><body style="background:transparent"></body></html>',
                                   hidden=True, frameless=True, transparent=True, width=160, height=240)
    api = pet.PetApi('http://127.0.0.1:1')  # no navigation or connection; permission origin only
    timer = Timer()
    timer.Interval = 60

    def check(label, condition):
        assert condition, label
        results.append(label)
        print('PASS:', label, flush=True)

    def scenarios(form, theme_calls):
        guard = api._transparency
        preference = UserPreferenceChangedEventArgs(UserPreferenceCategory.General)
        check('installed startup path sets backdrop NONE', backdrop(form) == 1)
        theme_calls.clear()
        notify('UserPreferenceChanged', preference)
        yield
        check('real SystemEvents subscription no longer calls pywebview Mica handler', not theme_calls)
        check('preference notification keeps backdrop NONE', backdrop(form) == 1)

        before = len(calls)
        backdrop(form, 2)
        worker = threading.Thread(target=lambda: [guard.request() for _ in range(50)])
        worker.start()
        worker.join(timeout=3)
        check('worker requests are nonblocking and deferred', not worker.is_alive() and len(calls) == before)
        yield
        check('50 worker requests coalesce into one UI repair', len(calls) == before + 1 and backdrop(form) == 1)

        triggers = [('PowerModeChanged', PowerModeChangedEventArgs(PowerModes.Resume)),
                    ('SessionSwitch', SessionSwitchEventArgs(SessionSwitchReason.SessionUnlock)),
                    ('SessionSwitch', SessionSwitchEventArgs(SessionSwitchReason.ConsoleConnect)),
                    ('SessionSwitch', SessionSwitchEventArgs(SessionSwitchReason.RemoteConnect)),
                    ('DisplaySettingsChanged', EventArgs.Empty)]
        for name, args in triggers:
            backdrop(form, 2)
            before = len(calls)
            notify(name, args)
            yield
            check(f'{name} {args} repairs injected Mica', backdrop(form) == 1 and len(calls) == before + 1)

        before = len(calls)
        notify('PowerModeChanged', PowerModeChangedEventArgs(PowerModes.Suspend))
        notify('PowerModeChanged', PowerModeChangedEventArgs(PowerModes.StatusChange))
        notify('SessionSwitch', SessionSwitchEventArgs(SessionSwitchReason.SessionLock))
        yield
        check('suspend/status/lock do not schedule repairs', len(calls) == before)

        guard.request()
        before = len(calls)
        recreate = clr.GetClrType(Control).GetMethod('RecreateHandle', BindingFlags.Instance | BindingFlags.NonPublic)
        recreate.Invoke(form, None)
        yield
        check('real HWND recreation repairs once and discards old queued callback',
              len(calls) == before + 1 and backdrop(form) == 1 and calls[-1] == form.Handle.ToInt64())

        for _ in range(3):
            backdrop(form, 2)
            notify('UserPreferenceChanged', preference)
            yield
            check('repeated preference recovery', backdrop(form) == 1 and not theme_calls)

        # Also exercise normal WinForms Close (the primary probe below tests direct Dispose).
        close_form = Form()
        close_guard = pet.PetTransparency(close_form)
        close_guard.request()
        close_form.Close()
        check('normal Close disposes the form and detaches recovery handlers',
              close_form.IsDisposed and close_guard._stopped and not close_guard._subscriptions)
        before = len(calls)
        close_guard.request()
        yield
        check('normal Close discards pending callbacks', len(calls) == before)

        guard.request()
        before = len(calls)
        form.Dispose()
        check('native disposal removes all owned subscriptions', guard._stopped and not guard._subscriptions)
        guard.dispose()
        for name, args in triggers:
            notify(name, args)
        notify('UserPreferenceChanged', preference)
        guard.request()
        yield
        check('queued and late events after disposal do no work', len(calls) == before and not theme_calls)

    def prepare(window):
        try:
            ui_thread.append(threading.get_ident())
            form = window.native
            check('probe stays hidden', not form.Visible)
            check('installed Windows theme is dark (unchanged)', form.is_dark_theme())
            theme_calls = []
            original_theme = form.update_title_bar_theme

            def measured_theme():
                theme_calls.append(True)
                original_theme()

            form.update_title_bar_theme = measured_theme
            pet.see_through(form)
            check('unpatched pet override sets backdrop NONE', backdrop(form) == 1)
            form.on_system_theme_changed(None, UserPreferenceChangedEventArgs(UserPreferenceCategory.General))
            check('actual installed pywebview handler overwrites NONE with Mica', backdrop(form) == 2)
            pet.prepare(window, api, 'http://127.0.0.1:1', (160, 240))
            steps = scenarios(form, theme_calls)

            def tick(*_):
                try:
                    next(steps)
                except StopIteration:
                    timer.Stop()
                    from System.Windows.Forms import Application
                    Application.ExitThread()
                except Exception:
                    failures.append(traceback.format_exc())
                    timer.Stop()
                    window.destroy()

            timer.Tick += tick
            timer.Start()
        except Exception:
            failures.append(traceback.format_exc())
            # before_show runs before Application.Run, so close after the loop starts.
            from System import Action
            window.native.BeginInvoke(Action(window.destroy))

    window.events.before_show += prepare
    print('Python', sys.version.split()[0], 'pywebview', importlib.metadata.version('pywebview'),
          'pythonnet', importlib.metadata.version('pythonnet'), flush=True)
    watchdog = threading.Timer(30, lambda: (failures.append('probe timed out'), window.destroy()))
    watchdog.daemon = True
    profile = tempfile.TemporaryDirectory(prefix='openamika-transparency-')
    try:
        watchdog.start()
        webview.start(private_mode=True, storage_path=profile.name)
    finally:
        watchdog.cancel()
        timer.Dispose()
        pet.see_through = original_apply
        # WebView2 releases its profile asynchronously after the form is disposed.
        for attempt in range(50):
            try:
                profile.cleanup()
                break
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.1)
    if failures:
        print('\n'.join(failures), file=sys.stderr)
        return 1
    print(f'{len(results)} native checks passed; actual lock/sleep acceptance still required.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
