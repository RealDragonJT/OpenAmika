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

HTTP APIs and dialog sockets are fixtures; camera and microphone streams use
Chromium's synthetic devices. No server is started, and no running OpenAmika stack,
keys, licensed model downloads, or physical camera/microphone are needed. These
checks do not verify real voice output, model rendering, or phone camera hardware.
