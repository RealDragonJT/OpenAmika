"""On-demand MCP image analysis; does not write the camera's shared State."""
from __future__ import annotations

import base64
import io
import threading


class ImageAnalyzer:
    def __init__(self, model):
        self.model = model
        self.slot = threading.Lock()

    def analyze(self, document):
        from PIL import Image
        if not self.slot.acquire(blocking=False):
            raise RuntimeError("Image analysis is busy.")
        try:
            if not self.model.is_loaded:
                raise RuntimeError("Vision model is not loaded.")
            encoded = document.get("image", "")
            if not isinstance(encoded, str) or len(encoded) > 7 * 1024 * 1024:
                raise ValueError("Image exceeds the size limit.")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("Image exceeds the size limit.")
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 16_000_000:
                    raise ValueError("Image dimensions exceed the limit.")
                mime = Image.MIME.get(image.format)
                if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif") or mime != document.get("mime_type"):
                    raise ValueError("Unsupported image format.")
                image.load()
                prompt = str(document.get("prompt") or "Describe this image.")[:2000]
                # Same lock as camera inference; one GPU generation at a time.
                with self.model.inference_lock:
                    inputs = self.model.preprocess(image.convert("RGB"), prompt)
                    return self.model.generate(inputs)
        finally:
            self.slot.release()


def install_image_analysis(app, model):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    import asyncio
    import json
    analyzer = ImageAnalyzer(model)

    # Avoid a forward-reference annotation to the locally imported Request.
    async def analyze(request):
        if request.headers.get("origin") or request.headers.get("x-openamika-action") != "analyze-image":
            return JSONResponse({"ok": False, "error": "Use the local image-analysis client."}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 7 * 1024 * 1024 + 4096:
                return JSONResponse({"ok": False, "error": "Image exceeds size limit."}, status_code=413)
        try:
            document = json.loads(body)
            if not isinstance(document, dict):
                raise ValueError("Expected a JSON object.")
            result = await asyncio.to_thread(analyzer.analyze, document)
            return JSONResponse({"ok": True, "description": result, "model": model.key})
        except (ValueError, OSError):
            return JSONResponse({"ok": False, "error": "Invalid image."}, status_code=400)
        except RuntimeError:
            return JSONResponse({"ok": False, "error": "Vision analysis unavailable or busy."}, status_code=503)
    analyze.__annotations__["request"] = Request
    app.post("/api/analyze")(analyze)
