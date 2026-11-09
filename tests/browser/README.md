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
permission retry, and a missing saved device. Browser script errors fail the checks.
The thinking-state disconnect check uses a ready-avatar fixture to verify immediate
busy-display cleanup, timer cancellation, and protection from old socket callbacks.
Navigation checks cover both mode labels, resizing, and centered wrapping. A PNG
pixel check compares the status dot and ring at 100%, 125%, 150%, and 200% display
scales, since layout rectangles alone do not detect paint-time rounding.

HTTP APIs and dialog sockets are fixtures; camera and microphone streams use
Chromium's synthetic devices. No server is started, and no running OpenAmika stack,
keys, licensed model downloads, or physical camera/microphone are needed. These
checks do not verify real voice output, model rendering, or phone camera hardware.
