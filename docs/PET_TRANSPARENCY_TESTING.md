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

## Manual acceptance on the affected Windows desktop

This requires the operator because it interrupts the interactive Windows session and needs visual observation on the
affected desktop. Use the patched worktree's `pet.py` with the existing brain interpreter and running gateway.
Do not copy files to the main checkout or start another launcher.

1. Leave the main OpenAmika services running. Close the current desktop pet with its own **×**, and close any
   OpenAmika **Use** tabs so they release their dialog/audio sessions. The launcher leaves a normally closed pet off.
2. In PowerShell, run the following. It reads only the main checkout's chat-port setting; the patched pet writes
   its geometry and separate WebView2 profile under this worktree's `logs/pet-window`. It starts with power and
   microphone off. The gateway continues serving its existing UI and model assets.

   ```powershell
   $trialRoot = (Get-Location).Path  # run from the patched worktree
   $mainRoot = 'C:\path\to\OpenAmika'  # replace with the existing installation
   $trialPort = (Get-Content -LiteralPath "$mainRoot\settings.json" -Raw | ConvertFrom-Json).chat_port
   if (-not $trialPort) { $trialPort = 53000 }
   & "$mainRoot\envs\brain\Scripts\python.exe" -B "$trialRoot\pet.py" --url "http://localhost:$trialPort"
   ```

3. Confirm the desktop shows through around the pet. Drag and resize with the wheel. Lock with **Win+L**, unlock,
   and check the background immediately and again after 30 seconds. Repeat twice without moving her into the browser.
4. Put Windows to sleep from the Start menu, wake/unlock, and repeat the same visual and drag/resize checks.
   Expected: no opaque rectangle, no close/reopen needed, and controls still work. Note which cycle and how long
   after wake any failure appears; capture a screenshot and the trial terminal's error text if it fails.
5. Close the patched pet with its own **×**, then run the command again and confirm it opens transparent and retains
   the trial geometry. Close it when done. To return to the original pet, open the existing main app's Use page and
   click **Desktop pet**. That button still launches the main checkout's unpatched `pet.py`.

The trial has its own instance event and profile. The main page's **Bring her back here** button does not manage
this separate worktree pet; use its own **×** or **Full page** control. Keep a single active dialog/audio session.
For a side-by-side visual comparison, the original and patched windows can both stay open with power and microphone
off; their instance events and profiles are independent.

Manual lock/unlock acceptance passed on Windows 11 in dark mode with pywebview 6.2.1 and pythonnet 3.2.1:
the original window became opaque after Win+L/unlock, while the patched window remained transparent. The operator
also accepted a side-by-side comparison and confirmed that actual sleep/resume works as expected with the fix.
The native probe separately replays resume notifications without suspending the PC.

## Native API references

- [DWM backdrop values](https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type):
  `DWMSBT_NONE` disables the backdrop; `DWMSBT_MAINWINDOW` is Mica.
- [Control.BeginInvoke](https://learn.microsoft.com/en-us/dotnet/api/system.windows.forms.control.begininvoke):
  marshal work to the handle's UI thread; handle loss can cause scheduling to fail.
- [SystemEvents.UserPreferenceChanged](https://learn.microsoft.com/en-us/dotnet/api/microsoft.win32.systemevents.userpreferencechanged):
  static subscriptions must be detached on disposal.
- [WM_DWMCOMPOSITIONCHANGED](https://learn.microsoft.com/en-us/windows/win32/dwm/wm-dwmcompositionchanged):
  Windows 8+ always enables composition and does not send this notification for video-mode changes.
