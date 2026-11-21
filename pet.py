#!/usr/bin/env python3
# Copyright (c) 2026 MekaHime Inc.
# SPDX-License-Identifier: MIT
"""OpenAmika's desktop pet: her Live2D stage alone, in a transparent, always-on-top window on your desktop (Windows).

    envs\\brain\\Scripts\\python.exe pet.py           her window, for the OpenAmika that runs from this folder
    ... --url http://localhost:53000                another address (default: settings.json "chat_port")
    python launch.py --pet, Start_OpenAmika_Pet.bat start everything, then this window instead of the browser page
    the Use page's "Desktop pet" button             the same window, opened from the web UI (she moves there and back)

The window is the Use page with ?pet=1 (her stage alone, on a transparent page: ui/use.js "desktop pet") in Microsoft
Edge WebView2, through pywebview, which is optional:

    envs\\brain\\Scripts\\python.exe -m pip install -r brain\\requirements-pet.txt

Drag her to move the window; the mouse wheel over her makes it bigger or smaller; hover over her for the bar (power,
microphone, full page, close). Her voice and your microphone use the page's dialog socket, one session at a time: the
"full page" button closes this window before it opens the page in your browser. The window may use the microphone for
its own page only. Its position and size are kept in logs\\pet-window\\window.json, next to its own WebView2 profile
(logs\\pet-window\\webview). Exit code 0 when the window was closed; 1 when OpenAmika does not answer at the
address (or the window failed); 2 without pywebview, WebView2 or Windows.

The web UI opens and closes this window too: the Use page's "Desktop pet" button and its "Bring her back here"
(speech/pet_window.py). While the window lives it holds a named Windows event (instance_name()). The event existing
says "her window is open": a second start finds it and leaves at once, with exit code 0. Setting the event closes
the window. A second event, "<name>-ready", appears when her page in the window has asked for its hand-over: --power
and --mic are what the page that sent her here had on, and her page in the window switches the same on, once (nothing
of it travels in the address). --close-with PID closes the window when that process ends: the server that opened it.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / 'logs' / 'pet-window'
GEOMETRY = STATE_DIR / 'window.json'
PROFILE = STATE_DIR / 'webview'
REQUIREMENTS = os.path.join('brain', 'requirements-pet.txt')
TITLE = 'OpenAmika'
DEFAULT_SIZE = (320, 560)        # logical px: she stands at 85 % of the window's height (her banks.json "scale")
MIN_SIZE = (160, 240)
MARGIN = 24                      # first start: this far from the bottom-right corner of the primary screen's work area
DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE = 38, 1
SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS = 5, 183
READY = '-ready'                 # the second event's name: instance_name() + READY (speech/pet_window.py READY)


# ── plain helpers (no pywebview, no Windows; tests/test_pet.py) ─────────────────────────────────────────────────
def chat_url(settings_path: Path) -> str:
    """The web UI's address, as launch.py opens it: http://localhost:<settings.json chat_port> (default 53000)."""
    port = 53000
    try:
        port = int(json.loads(Path(settings_path).read_text(encoding='utf-8-sig')).get('chat_port') or port)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return f'http://localhost:{port}'


def pet_url(base: str) -> str:
    return base.rstrip('/') + '/use?pet=1'


def instance_name(root=ROOT) -> str:
    """The name of the Windows event this window holds while it is open (speech/pet_window.py event_name(): keep the
    two the same). The web UI asks "is her window open?" by looking for it, and closes the window by setting it. One
    per OpenAmika folder, so two installs on one PC do not see each other's window; "Local" keeps it inside this
    Windows session."""
    key = os.path.normcase(str(Path(root).resolve()))
    return r'Local\OpenAmika-pet-' + hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def same_origin(url: str, base: str) -> bool:
    """True when url is on base's scheme, host and port (the only page this window grants the microphone to)."""
    try:
        a, b = urlsplit(url), urlsplit(base)
        return (a.scheme, a.hostname, a.port or 0) == (b.scheme, b.hostname, b.port or 0) and bool(a.hostname)
    except ValueError:
        return False


def reachable(base: str, timeout: float = 3.0) -> bool:
    """True when the speech gateway (the web UI) answers its /health at base.

    "localhost" is asked as 127.0.0.1 first: Windows tries ::1 before it, and she listens on IPv4 only, so the name
    costs two seconds of every start. The name itself is still asked when the number does not answer."""
    urls = [base]
    try:
        parts = urlsplit(base)
        if parts.hostname == 'localhost':
            urls.insert(0, parts._replace(netloc='127.0.0.1' + (f':{parts.port}' if parts.port else '')).geturl())
    except ValueError:
        pass
    for url in urls:
        try:
            with urlopen(url.rstrip('/') + '/health', timeout=timeout) as response:
                if response.status == 200:
                    return True
        except (OSError, ValueError):
            continue
    return False


def load_geometry(path: Path) -> dict | None:
    """The saved {x, y, width, height} (pywebview's logical px), or None when there is none or it is unusable."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        geo = {key: int(data[key]) for key in ('x', 'y', 'width', 'height')}
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if geo['width'] < MIN_SIZE[0] or geo['height'] < MIN_SIZE[1] or geo['width'] > 20000 or geo['height'] > 20000:
        return None
    return geo


def save_geometry(path: Path, geo: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps({key: int(geo[key]) for key in ('x', 'y', 'width', 'height')}), encoding='utf-8')
    os.replace(tmp, path)


def zoomed(rect: tuple, factor: float, min_size: tuple, max_height: int) -> tuple:
    """rect (left, top, width, height) scaled by factor around its bottom centre (she keeps standing on the same
    spot), the aspect kept, no smaller than min_size and no taller than max_height."""
    left, top, width, height = rect
    factor = max(min_size[0] / width, min_size[1] / height, min(float(factor), max_height / height))
    new_w, new_h = round(width * factor), round(height * factor)
    return left + (width - new_w) // 2, top + height - new_h, new_w, new_h


class GeometrySaver:
    """Keeps the window's last position and size and writes them a moment after it stops moving (and when it closes)."""

    def __init__(self, path: Path, geo: dict, delay: float = 0.6):
        self.path, self.geo, self.delay = Path(path), dict(geo), delay
        self.lock = threading.Lock()
        self.timer = None
        self.dirty = False

    def moved(self, x, y):
        self._update(x=x, y=y)

    def resized(self, width, height):
        self._update(width=width, height=height)

    def _update(self, **values):
        with self.lock:
            self.geo.update({key: int(value) for key, value in values.items()})
            self.dirty = True
            if self.timer:
                self.timer.cancel()
            self.timer = threading.Timer(self.delay, self.flush)
            self.timer.daemon = True
            self.timer.start()

    def flush(self):
        with self.lock:
            if not self.dirty or None in (self.geo.get('x'), self.geo.get('y')):
                return
            try:
                save_geometry(self.path, self.geo)
                self.dirty = False
            except OSError as error:
                print(f'pet: could not save the window position ({error})', file=sys.stderr)


# ── Windows ──────────────────────────────────────────────────────────────────────────────────────────────────────
_DLLS = {}


def _dll(name):
    """A private handle on a Windows DLL: the argtypes set on it never reach pywebview's own calls, which go through
    the shared ctypes.windll (its window move passes None for a size, which a c_int prototype refuses)."""
    if name not in _DLLS:
        _DLLS[name] = ctypes.WinDLL(name)
    return _DLLS[name]


def _user32():
    from ctypes import wintypes
    user32 = _dll('user32')
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT]
    return user32


def _kernel32():
    """kernel32 with its own error slot (ctypes.get_last_error) and prototypes: a handle is a pointer."""
    if 'kernel32' not in _DLLS:
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                               wintypes.DWORD]
        k32.WaitForMultipleObjects.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        _DLLS['kernel32'] = k32
    return _DLLS['kernel32']


def claim_instance(name: str) -> tuple:
    """(handle, already). The handle is the named event that says "her window is open" for as long as this process
    holds it. already is True when another window of this OpenAmika holds it (the handle is None: leave). When
    Windows refuses the event for any other reason the window opens all the same, unseen by the web UI's button."""
    k32 = _kernel32()
    ctypes.set_last_error(0)
    handle = k32.CreateEventW(None, True, False, name)              # manual reset, not set
    error = ctypes.get_last_error()
    if handle and error == ERROR_ALREADY_EXISTS:
        k32.CloseHandle(handle)
        return None, True
    if not handle:
        return None, error == ERROR_ACCESS_DENIED                   # held by a window started as administrator
    return handle, False


def page_alive(name: str):
    """The second event, "<name>-ready": her page in this window is alive. Returns its handle (None: refused)."""
    return _kernel32().CreateEventW(None, True, True, name + READY) or None


def close_when(window, event, pid=None, closing=None) -> None:
    """Close the window when `event` is set (the web UI's "Bring her back here") or when the process `pid` ends (the
    server that opened this window has stopped: she goes with it). A daemon thread does the waiting; `closing`
    (a threading.Event) tells it that the window is on its way out by itself."""
    from ctypes import wintypes
    k32 = _kernel32()
    handles = [event] if event else []
    process = k32.OpenProcess(SYNCHRONIZE, False, int(pid)) if pid else None
    if process:
        handles.append(process)
    if not handles:
        return

    def watch():
        array = (wintypes.HANDLE * len(handles))(*handles)
        result = k32.WaitForMultipleObjects(len(handles), array, False, INFINITE)
        if result >= len(handles) or (closing is not None and closing.is_set()):
            return                               # the wait failed, or the window is closing already
        try:
            window.destroy()
        except Exception as error:
            print(f'pet: could not close the window ({type(error).__name__}: {error})', file=sys.stderr)

    threading.Thread(target=watch, name='pet-close', daemon=True).start()


def release(handles) -> None:
    """The window has gone: drop its events at once, so "her window is open" is false before anyone looks again (the
    browser page that "full page" opens asks right away). Setting each first wakes close_when's thread."""
    k32 = _kernel32()
    for handle in handles:
        if handle:
            k32.SetEvent(handle)
            k32.CloseHandle(handle)


def _system_scale() -> float:
    try:
        return _dll('user32').GetDpiForSystem() / 96
    except (AttributeError, OSError):
        return 1.0


def placed(geo: dict | None) -> dict:
    """The geometry to open with: the saved one while its centre is on a screen, else the default size at the bottom
    right of the primary screen's work area (pywebview's logical px, as it reports and takes them; the process is
    DPI-aware like pywebview makes it, so logical = physical / system scale)."""
    from ctypes import wintypes
    user32 = _user32()
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass
    scale = _system_scale()
    if geo:
        centre = wintypes.POINT(int((geo['x'] + geo['width'] / 2) * scale), int((geo['y'] + geo['height'] / 2) * scale))
        if user32.MonitorFromPoint(centre, 0):           # MONITOR_DEFAULTTONULL: None off every screen
            return geo
    width, height = DEFAULT_SIZE
    work = wintypes.RECT()
    if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):   # SPI_GETWORKAREA, physical px
        return {'x': int(work.right / scale) - width - MARGIN, 'y': int(work.bottom / scale) - height - MARGIN,
                'width': width, 'height': height}
    return {'x': None, 'y': None, 'width': width, 'height': height}


def see_through(form) -> None:
    """Make pywebview's 'transparent' window really transparent. pywebview gives the WebView2 a transparent background,
    but on Windows the WinForms window behind it stays opaque (grey). A black client area extended into DWM's frame
    (the 'sheet of glass') is drawn with per-pixel alpha, so the desktop shows wherever the page is transparent and
    her soft edges blend with it; Windows 11 must also be told not to draw a backdrop material there (Windows 10 has
    none). Always runs on the window's thread, including after Windows session/display changes."""
    from ctypes import wintypes
    from System.Drawing import Color

    class Margins(ctypes.Structure):
        _fields_ = [('left', ctypes.c_int), ('right', ctypes.c_int), ('top', ctypes.c_int), ('bottom', ctypes.c_int)]

    form.BackColor = Color.Black
    hwnd = wintypes.HWND(form.Handle.ToInt64())
    dwm = _dll('dwmapi')
    dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmExtendFrameIntoClientArea.argtypes = [wintypes.HWND, ctypes.c_void_p]
    backdrop = ctypes.c_int(DWMSBT_NONE)
    dwm.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
    dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(Margins(-1, -1, -1, -1)))


class PetTransparency:
    """Own this frameless form's backdrop for its lifetime (created by before_show on the UI thread).

    pywebview 6.2.1's preference handler enables Mica even for transparent windows in dark mode. Remove that
    handler on this form only; a second subscriber that merely undoes it would depend on callback ordering.
    Windows can also reset the frame on resume/reconnect or handle recreation. SystemEvents may arrive on a
    worker thread: queue one repair on the form's thread, never touch its Handle from the event thread.
    """

    def __init__(self, form):
        from Microsoft.Win32 import PowerModes, SessionSwitchReason, SystemEvents
        from System import Action, InvalidOperationException

        self.form = form
        self._action = Action
        self._invoke_error = InvalidOperationException
        self._resume = PowerModes.Resume
        self._sessions = (SessionSwitchReason.SessionUnlock, SessionSwitchReason.ConsoleConnect,
                          SessionSwitchReason.RemoteConnect)
        self._lock = threading.Lock()
        self._stopped = False
        self._pending = None
        self._subscriptions = []
        see_through(form)                    # preserve startup transparency even if event registration fails
        theme_handler = getattr(form, 'on_system_theme_changed', None)
        try:
            self._subscribe(form, 'Disposed', self.dispose)
            self._subscribe(form, 'HandleDestroyed', self._handle_destroyed)
            self._subscribe(form, 'HandleCreated', self._handle_created)
            self._subscribe(SystemEvents, 'UserPreferenceChanged', self.request)
            self._subscribe(SystemEvents, 'DisplaySettingsChanged', self.request)
            self._subscribe(SystemEvents, 'PowerModeChanged', self._power_changed)
            self._subscribe(SystemEvents, 'SessionSwitch', self._session_changed)
            if theme_handler is not None:
                SystemEvents.UserPreferenceChanged -= theme_handler
        except Exception:
            self.dispose()
            raise

    def _subscribe(self, owner, name, handler):
        event = getattr(owner, name)
        event += handler
        self._subscriptions.append((owner, name, handler))

    def _usable(self):
        return not (self._stopped or self.form.IsDisposed or self.form.Disposing) and self.form.IsHandleCreated

    def request(self, *_):
        with self._lock:
            if not self._usable() or self._pending is not None:
                return
            token = self._pending = object()
        try:
            # Even UI-thread notifications are deferred until the current Windows message has finished.
            self.form.BeginInvoke(self._action(lambda: self._repair(token)))
        except self._invoke_error:
            # The handle can disappear between the check and BeginInvoke. A new HandleCreated will repair it.
            with self._lock:
                if self._pending is token:
                    self._pending = None

    def _repair(self, token):
        with self._lock:
            if self._pending is not token:
                return
            self._pending = None
            if not self._usable():
                return
        try:
            see_through(self.form)
            self.form.Invalidate()
        except Exception as error:
            # Do not let an asynchronous callback escape into WinForms' unhandled-exception dialog.
            print(f'pet: could not restore transparency ({type(error).__name__}: {error})', file=sys.stderr)

    def _power_changed(self, _sender, args):
        if args.Mode == self._resume:
            self.request()

    def _session_changed(self, _sender, args):
        if args.Reason in self._sessions:
            self.request()

    def _handle_destroyed(self, *_):
        with self._lock:
            self._pending = None             # invalidate callbacks queued for the old HWND

    def _handle_created(self, *_):
        self.request()                      # fetch the new HWND only when the UI-thread callback runs

    def dispose(self, *_):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._pending = None
        for owner, name, handler in reversed(self._subscriptions):
            event = getattr(owner, name)
            event -= handler
        self._subscriptions.clear()          # static SystemEvents must not retain a closed window


class PetApi:
    """What her page may ask of its window (window.pywebview.api in ui/use.js): zoom, full_page, close, handover.
    pywebview exposes the public methods only; the window and its handle are filled in when it exists."""

    def __init__(self, base: str, power: bool = False, mic: bool = False, on_ready=None, audio=None, voice_output=None):
        self._base = base
        self._window = None
        self._hwnd = None
        self._open_page = False
        self._handover = {'power': bool(power), 'mic': bool(mic)}
        if voice_output is not None:
            self._handover['voice_output'] = voice_output
        if audio is not None:
            self._handover['audio'] = audio
        self._needs_audio = audio is not None
        self._audio_permission = None
        self._audio_error = None
        self._on_ready = on_ready
        self._lock = threading.Lock()

    def handover(self):
        """What the page that sent her here had on: {'power': bool, 'mic': bool}. Her page in this window asks when
        it is ready and switches the same on. Only the first call gets it: a reload of the page gets both False, so
        she does not come on by herself a second time. The first call also tells main() that her page is alive
        (on_ready). An audio-device transfer waits for handover_done() instead."""
        with self._lock:
            state, self._handover = self._handover, {'power': False, 'mic': False}
            if 'voice_output' in state:          # a reload must not silently unmute her when power is enabled later
                self._handover['voice_output'] = state['voice_output']
        if 'audio' in state:
            try:
                if self._audio_permission is not None:
                    self._audio_permission.GetAwaiter().GetResult()
            except Exception:
                self._audio_error = 'The desktop window could not access your audio device list.'
            if self._audio_error:
                state['audio_error'] = self._audio_error
        if not self._needs_audio:
            self._signal_ready()
        return state

    def _signal_ready(self):
        with self._lock:
            on_ready, self._on_ready = self._on_ready, None
        if on_ready is not None:
            try:
                on_ready()
            except Exception as error:
                print(f'pet: {type(error).__name__}: {error}', file=sys.stderr)

    def handover_done(self, error=None):
        """A device handoff is ready only after the page selects its audio devices; failures return to the browser."""
        if error:
            print('pet: ' + str(error)[:500], file=sys.stderr, flush=True)
            self.close()
            return
        self._needs_audio = False
        self._signal_ready()

    def zoom(self, factor):
        """Make the window bigger or smaller around her feet, within her screen's work area."""
        if not self._hwnd:
            return
        from ctypes import wintypes
        user32 = _user32()
        hwnd = wintypes.HWND(self._hwnd)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        class MonitorInfo(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT),
                        ('dwFlags', wintypes.DWORD)]
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        max_height = rect.bottom - rect.top
        if user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):   # MONITOR_DEFAULTTONEAREST
            max_height = info.rcWork.bottom - info.rcWork.top
        scale = (user32.GetDpiForWindow(hwnd) or 96) / 96
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return
        left, top, width, height = zoomed((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top), factor,
                                          (round(MIN_SIZE[0] * scale), round(MIN_SIZE[1] * scale)), max_height)
        user32.SetWindowPos(hwnd, None, left, top, width, height, SWP_NOZORDER | SWP_NOACTIVATE)

    def full_page(self):
        """Back to the normal page: close this window (that frees the dialog socket); main() opens the page in the
        browser once it is gone."""
        self._open_page = True
        self.close()

    def close(self):
        if self._window is not None:
            self._window.destroy()


def prepare(window, api: PetApi, base: str, size: tuple) -> None:
    """pywebview's before_show (on the window's thread): the window's real size, the see-through window, and the
    microphone for her own page (granted when the page asks, which it does when you click the microphone; any other
    permission is WebView2's usual prompt)."""
    from ctypes import wintypes
    from System.Drawing import Size
    form = window.native
    api._hwnd = int(form.Handle.ToInt64())
    # pywebview sizes the window with its frame, then drops the frame: it would open smaller than asked (and smaller
    # again on every start, since its saved size is what it opened with)
    scale = (_user32().GetDpiForWindow(wintypes.HWND(api._hwnd)) or 96) / 96
    form.Size = Size(round(size[0] * scale), round(size[1] * scale))
    api._transparency = PetTransparency(form)
    from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState

    def on_permission(_sender, args):
        if args.PermissionKind == CoreWebView2PermissionKind.Microphone and same_origin(str(args.Uri), base):
            args.State = CoreWebView2PermissionState.Allow

    def on_ready(sender, args):
        if args.IsSuccess:
            sender.CoreWebView2.PermissionRequested += on_permission
            if any(api._handover.get('audio', {}).values()):
                # Grant the same host-only microphone permission without opening a capture stream. Chromium then
                # exposes device labels, including outputs, even when the handed-over microphone is switched off.
                try:
                    api._audio_permission = sender.CoreWebView2.Profile.SetPermissionStateAsync(
                        CoreWebView2PermissionKind.Microphone, base, CoreWebView2PermissionState.Allow)
                except Exception:
                    api._audio_error = 'The desktop window could not access your audio device list.'

    form.webview.CoreWebView2InitializationCompleted += on_ready


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--url', help='the web UI address (default: http://localhost:<settings.json chat_port>)')
    ap.add_argument('--debug', action='store_true', help="pywebview's debug mode: developer tools, context menu")
    ap.add_argument('--power', action='store_true', help='she was on in the page that sent her here: switch her on')
    ap.add_argument('--mic', action='store_true', help='the microphone was on there too: switch it on as well')
    ap.add_argument('--voice-output', action=argparse.BooleanOptionalAction, default=None,
                    help='preserve whether spoken replies were enabled in the page')
    ap.add_argument('--close-with', type=int, metavar='PID', help='close the window when this process ends')
    args = ap.parse_args(argv)
    if os.name != 'nt':
        print('Her desktop pet window is Windows-only for now: open the page in your browser instead.', file=sys.stderr)
        return 2
    try:
        import webview
    except ImportError:
        print('Her desktop pet window needs pywebview, which is not installed in this Python. Install it once:\n'
              f'    "{sys.executable}" -m pip install -r {REQUIREMENTS}\n'
              '(from this folder; Start_OpenAmika.bat opens the page in your browser meanwhile)', file=sys.stderr)
        return 2
    base = (args.url or chat_url(ROOT / 'settings.json')).rstrip('/')
    if not reachable(base):
        print(f'OpenAmika is not answering at {base}: start her first (Start_OpenAmika_Pet.bat opens this window '
              'when she is up), or pass --url.', file=sys.stderr)
        return 1
    list(webview.screens)            # a lazy property: this loads pywebview's Windows backend, and webview.renderer
    if webview.renderer != 'edgechromium':          # without WebView2 pywebview falls back to Internet Explorer
        print('Her desktop pet window needs the Microsoft Edge WebView2 Runtime (part of Windows 11; for Windows 10: '
              'https://developer.microsoft.com/microsoft-edge/webview2/).', file=sys.stderr)
        return 2
    name = instance_name()
    held, already = claim_instance(name)
    if already:
        print('Her desktop pet window is already open.', file=sys.stderr)
        return 0
    handles, closing = [held], threading.Event()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    geo = placed(load_geometry(GEOMETRY))
    try:
        audio = json.loads(os.environ.get('AMIKA_PET_AUDIO', 'null'))
    except ValueError:
        print('Invalid desktop audio selections.', file=sys.stderr)
        release(handles)
        return 2
    api = PetApi(base, power=args.power, mic=args.mic, audio=audio, voice_output=args.voice_output,
                 on_ready=lambda: handles.append(page_alive(name)))
    window = webview.create_window(TITLE, pet_url(base), js_api=api, x=geo['x'], y=geo['y'], width=geo['width'],
                                   height=geo['height'], frameless=True, transparent=True, on_top=True, easy_drag=False,
                                   shadow=False, min_size=MIN_SIZE, background_color='#000000', text_select=False,
                                   zoomable=False)
    api._window = window
    saver = GeometrySaver(GEOMETRY, geo)
    window.events.before_show += lambda window: prepare(window, api, base, (geo['width'], geo['height']))
    window.events.moved += saver.moved
    window.events.resized += saver.resized
    window.events.closing += saver.flush
    close_when(window, held, args.close_with, closing)
    try:
        webview.start(private_mode=False, storage_path=str(PROFILE), debug=args.debug)
    except Exception as error:
        print(f'Her desktop pet window could not open: {type(error).__name__}: {error}', file=sys.stderr)
        return 1
    finally:
        closing.set()
        release(handles)                 # before the browser page below opens: it asks "is her window open?"
    saver.flush()
    if api._open_page:
        webbrowser.open(base + '/use')
    return 0


if __name__ == '__main__':
    sys.exit(main())
