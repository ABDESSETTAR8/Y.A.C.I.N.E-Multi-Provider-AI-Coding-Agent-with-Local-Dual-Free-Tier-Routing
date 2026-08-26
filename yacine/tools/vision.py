"""Vision tool (Phase F1).

Handles image/PDF upload + multimodal analysis. When a local vision model
(sentence-transformers image embeddings / a VLM via Ollama) is available it can
describe the image; otherwise it extracts safe metadata (dimensions, file type,
PDF page/text count) so the agent still has grounding. Fully offline-safe.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional

from .base import Tool, ToolResult
from .sandbox import Sandbox


class VisionTool(Tool):
    name = "vision"
    description = ("Analyze an uploaded image or PDF. Returns a description when a local "
                  "vision model is available, or structured metadata otherwise. Use for "
                  "screenshots, diagrams, UI mockups, and documents the user attaches.")
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path (relative to workspace) of the image/PDF."},
            "question": {"type": "string", "description": "Optional question about the image."},
        },
        "required": ["path"],
    }

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox

    def run(self, path: str, question: str = "") -> ToolResult:
        fp = self.sandbox.workspace / path
        if not fp.exists():
            return ToolResult(ok=False, output=f"File not found: {path}")
        try:
            size = fp.stat().st_size
            ext = fp.suffix.lower()
            sha = hashlib.sha1(fp.read_bytes()).hexdigest()[:12]
            meta = {"path": path, "ext": ext, "bytes": size, "sha": sha}
            if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
                meta.update(self._image_dims(fp))
                desc = self._try_vlm(fp, question)
                if desc:
                    meta["description"] = desc
                else:
                    meta["note"] = "No local vision model; metadata only."
            elif ext == ".pdf":
                meta.update(self._pdf_info(fp))
            return ToolResult(ok=True, output=str(meta), data=meta)
        except Exception as e:
            return ToolResult(ok=False, output=f"vision error: {e}")

    @staticmethod
    def _image_dims(fp: Path) -> dict:
        # Best-effort dimensions without heavy deps.
        try:
            from PIL import Image  # type: ignore
            with Image.open(fp) as im:
                return {"width": im.width, "height": im.height, "mode": im.mode}
        except Exception:
            return {}

    @staticmethod
    def _try_vlm(fp: Path, question: str) -> Optional[str]:
        """Call a local VLM via Ollama if 'llava'/'minicpm-v' is available."""
        try:
            import httpx
            b64 = base64.b64encode(fp.read_bytes()).decode()
            prompt = question or "Describe this image concisely."
            r = httpx.post("http://localhost:11434/api/generate", json={
                "model": "llava", "prompt": prompt,
                "images": [b64], "stream": False}, timeout=60)
            if r.status_code == 200:
                return r.json().get("response", "")
        except Exception:
            pass
        return None

    @staticmethod
    def _pdf_info(fp: Path) -> dict:
        try:
            from pypdf import PdfReader  # type: ignore
            r = PdfReader(str(fp))
            text = "\n".join((p.extract_text() or "") for p in r.pages[:5])
            return {"pages": len(r.pages), "text_excerpt": text[:500]}
        except Exception:
            return {"note": "pypdf not installed; cannot extract PDF text"}
