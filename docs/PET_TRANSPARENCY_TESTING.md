# Desktop pet transparency recovery

The fix replaces only the pet form's pywebview preference subscription and reapplies its existing black client
area, disabled backdrop, and full glass frame on the UI thread after preference/display changes, power resume,
session unlock/reconnect, and native handle creation. Duplicate queued requests coalesce. Disposal detaches
the subscriptions and invalidates queued work. Installed packages are not modified.

## Automated checks

From this checkout, using a Python with the development dependencies:

```powershell
python -B -m pytest tests -k 'not live' -p no:cacheprovider -q
pnpm --dir tests/browser test
```

The offline regression tests cover callback scheduling, repeated events, handle replacement, disposal,
failed scheduling/repair, and rollback of a failed installation. Browser tests use fixture APIs and media.

On Windows 11 in dark mode, using a Python with pywebview and WebView2 already installed:

```powershell
python -B tests/windows_pet_transparency_probe.py
```

This creates a hidden, disposable window with the actual installed pywebview backend, inline blank HTML and a
temporary WebView2 profile. It reproduces the dependency's backdrop override and tests recovery using real DWM
attribute reads, process-local replay of .NET SystemEvents, worker-thread requests, HWND recreation and disposal.
It enables WinForms cross-thread checks. Reflection in this probe targets .NET Framework's SystemEvents internals;
it deliberately fails if those internals or the required DWM attribute are unavailable.
It does not connect to OpenAmika, open media devices, change the theme, or lock/sleep Windows.
Native attribute checks are not evidence of visual correctness after actual sleep or unlock.

## Manual check on a real desktop

Locking, unlocking and sleep cannot be automated. On Windows 11 in dark mode, with her desktop window open (on
the Use page: the expand menu at the top-right of her stage, then **Desktop pet**):

1. The desktop shows through around her. Drag her, and resize her with the wheel.
2. Lock Windows with **Win+L**, unlock it, and look at her background at once and again after 30 seconds.
3. Put Windows to sleep, wake it, unlock it, and look again.
4. Switch Windows between dark and light mode once (Settings, Personalization, Colors).

Expected each time: no opaque rectangle behind her, no need to close and reopen her, and dragging and resizing still
work. If it fails, note after which step and how long after it, and keep the last lines of `logs\pet.log` (or the
console's text, if you started `pet.py` by hand).

## Native API references

- [DWM backdrop values](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type):
  `DWMSBT_NONE` disables the backdrop; `DWMSBT_MAINWINDOW` is Mica.
- [Control.BeginInvoke](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.control.begininvoke):
  marshal work to the handle's UI thread; handle loss can cause scheduling to fail.
- [SystemEvents.UserPreferenceChanged](https://learn.microsoft.com/en-us/dotnet/api/microsoft.win32.systemevents.userpreferencechanged):
  static subscriptions must be detached on disposal.
- [WM_DWMCOMPOSITIONCHANGED](https://learn.microsoft.com/en-us/windows/win32/dwm/wm-dwmcompositionchanged):
  Windows 8+ always enables composition and does not send this notification for video-mode changes.
