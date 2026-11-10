# Use page browser checks

From the repository root, with Node.js 24 and pnpm installed:

```powershell
pnpm --dir tests/browser install --frozen-lockfile
pnpm --dir tests/browser exec playwright install chromium
pnpm --dir tests/browser test
```

The checks render the actual UI in Chromium at desktop, tablet, phone, and landscape
sizes. They exercise keyboard dismissal and focus, fullscreen and its fallback,
saved lock and camera-preview state, reconnects, retained text, media cancellation,
permission retry, a failed microphone start or switch, and a missing saved device.
Browser script errors fail the checks.
The thinking-state disconnect check uses a ready-avatar fixture to verify immediate
busy-display cleanup, timer cancellation, and protection from old socket callbacks.
Navigation checks cover both mode labels, resizing, and centered wrapping. A PNG
pixel check compares the status dot and ring at 100%, 125%, 150%, and 200% display
scales, since layout rectangles alone do not detect paint-time rounding.

Pet-mode checks cover the transparent stage and controls at 160–480px window widths,
keyboard focus, native-bridge calls, microphone visibility and cleanup, and suppression
of page-only shortcuts and donation overlays. Both page and pet modes exercise playback
acknowledgements, forced audio stops, and provider errors. Shared-control checks preserve
saved devices while forwarding the upstream model-unlock header. Handoff checks from the
page cover a saved device that it cannot name, and where focus rests while she is out.

HTTP APIs and dialog sockets are fixtures; camera and microphone streams use
Chromium's synthetic devices. No server is started, and no running OpenAmika stack,
keys, licensed model downloads, or physical camera/microphone are needed. The native
pet bridge and loaded-avatar callbacks are fixtures too. These checks do not verify
the native WebView2 window, real voice output, model rendering, or phone camera hardware.
