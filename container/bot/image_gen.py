"""
Image generation — provider-agnostic adapter.

Uses gpt-image-1 (owned b64 response, saved to disk).
Add new providers by subclassing ImageProvider and registering in PROVIDERS.

Supported providers: openai
Coming later: stability, replicate
"""

import os
import re
import base64
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

# Valid params for gpt-image-1
OPENAI_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
OPENAI_QUALITIES = {"low", "medium", "high", "auto"}


def _slug(prompt: str, max_len: int = 40) -> str:
    """Turn a prompt into a filename-safe slug."""
    s = prompt.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:max_len]


class ImageProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, size: str, quality: str, output_dir: Path) -> dict:
        """Generate an image, save to output_dir, return metadata + local path."""
        ...


class OpenAIImageProvider(ImageProvider):
    name = "openai"
    model = "gpt-image-1"

    def __init__(self, api_key: str):
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)

    def generate(self, prompt: str, size: str = "1024x1024", quality: str = "auto", output_dir: Path = Path("/tmp")) -> dict:
        if size not in OPENAI_SIZES:
            size = "1024x1024"
        if quality not in OPENAI_QUALITIES:
            quality = "auto"

        response = self.client.images.generate(
            model=self.model,
            prompt=prompt,
            size=size,
            quality=quality,
            n=1,
        )

        image_b64 = response.data[0].b64_json
        image_bytes = base64.b64decode(image_b64)

        # Save to disk
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-{_slug(prompt)}.png"
        path = output_dir / filename
        path.write_bytes(image_bytes)

        return {
            "path": str(path),
            "filename": filename,
            "provider": self.name,
            "model": self.model,
            "size": size,
            "quality": quality,
            "prompt": prompt,
        }


# Registry — add new providers here
PROVIDERS: dict[str, type[ImageProvider]] = {
    "openai": OpenAIImageProvider,
}


def _build_provider(name: str = "openai") -> ImageProvider:
    if name not in PROVIDERS:
        raise ValueError(f"Unknown image provider: {name}. Available: {list(PROVIDERS)}")
    if name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set — add it to .env to use image generation")
        return OpenAIImageProvider(api_key=api_key)
    raise ValueError(f"No credentials handler for provider: {name}")


def generate_image(
    prompt: str,
    size: str = "1024x1024",
    quality: str = "auto",
    output_dir: Path | str = None,
    provider: str = "openai",
) -> dict:
    """
    Generate an image, save to disk, return path + metadata.

    Args:
        prompt:     What to generate
        size:       "1024x1024" | "1536x1024" (landscape) | "1024x1536" (portrait) | "auto"
        quality:    "low" | "medium" | "high" | "auto"
        output_dir: Where to save the image. Defaults to WOLT_DIR/wolt/images/
        provider:   "openai" (default)

    Returns:
        {path, filename, provider, model, size, quality, prompt}
    """
    if output_dir is None:
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
        output_dir = wolt_dir / "wolt" / "images"
    output_dir = Path(output_dir)

    gen = _build_provider(provider)
    logger.info(f"[image_gen] generating via {provider}/{gen.model} — {prompt[:80]}")
    result = gen.generate(prompt=prompt, size=size, quality=quality, output_dir=output_dir)
    logger.info(f"[image_gen] saved to {result['path']}")
    return result
