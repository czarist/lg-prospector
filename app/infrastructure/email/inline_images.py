"""Converte data:image (base64) do HTML em anexos CID — compatível com Gmail/Outlook.

- PNG/JPEG/GIF/WebP → CID direto
- SVG → convertido para PNG (clientes de e-mail não renderizam SVG)
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from email.mime.image import MIMEImage
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# data:image/png;base64,...  |  data:image/svg+xml;charset=utf-8;base64,...
DATA_URI_RE = re.compile(
    r"""(["'])data:image/(png|jpeg|jpg|gif|webp|svg\+xml)"""
    r"""(?:;charset=[^;,'"]+)?;base64,([A-Za-z0-9+/=\s]+)\1""",
    re.IGNORECASE,
)

MIME_MAP = {
    "png": "png",
    "jpeg": "jpeg",
    "jpg": "jpeg",
    "gif": "gif",
    "webp": "webp",
    "svg+xml": "svg+xml",
}


@dataclass
class InlineImage:
    cid: str
    content: bytes
    subtype: str  # png, jpeg, gif...
    original_type: str  # png, svg+xml, ...
    index: int


@dataclass
class HtmlWithCid:
    html: str
    images: list[InlineImage] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.images)


def _decode_b64(payload: str) -> bytes:
    cleaned = re.sub(r"\s+", "", payload)
    # padding
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned += "=" * pad
    return base64.b64decode(cleaned)


def _svg_viewport(svg_bytes: bytes) -> tuple[int, int]:
    text = svg_bytes.decode("utf-8", errors="replace")
    width, height = 200, 200
    wm = re.search(r'\bwidth=["\']?(\d+)', text, re.I)
    hm = re.search(r'\bheight=["\']?(\d+)', text, re.I)
    if wm:
        width = max(16, min(1200, int(wm.group(1))))
    if hm:
        height = max(16, min(1200, int(hm.group(1))))
    vb = re.search(r'viewBox=["\']?\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)', text, re.I)
    if vb and (not wm or not hm):
        width = max(16, min(1200, int(float(vb.group(1)))))
        height = max(16, min(1200, int(float(vb.group(2)))))
    return width, height


def flatten_png(png_bytes: bytes, *, bg: tuple[int, int, int] | None = None) -> bytes:
    """Reencode PNG. Keeps alpha unless an explicit `bg` is given to flatten onto."""
    from io import BytesIO

    from PIL import Image

    im = Image.open(BytesIO(png_bytes)).convert("RGBA")
    alpha = im.getchannel("A")
    out = BytesIO()
    if bg is None:
        if alpha.getextrema() == (255, 255):
            im.convert("RGB").save(out, format="PNG", optimize=True)
        else:
            im.save(out, format="PNG", optimize=True)
        return out.getvalue()

    canvas = Image.new("RGB", im.size, bg)
    canvas.paste(im, mask=alpha)
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def svg_bytes_to_png(svg_bytes: bytes, *, browser=None) -> bytes:
    """Renderiza SVG → PNG via Playwright (Chromium). Reusa `browser` se passado."""
    from playwright.async_api import async_playwright

    width, height = _svg_viewport(svg_bytes)
    b64 = base64.b64encode(svg_bytes).decode("ascii")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0;background:transparent;}}
  img{{display:block;max-width:none;}}
</style></head>
<body>
<img id="img" width="{width}" height="{height}"
  src="data:image/svg+xml;base64,{b64}" />
</body></html>"""

    async def _shot(browser_obj) -> bytes:
        page = await browser_obj.new_page(
            viewport={"width": width + 4, "height": height + 4},
            device_scale_factor=2,
        )
        try:
            await page.set_content(html, wait_until="load")
            locator = page.locator("#img")
            await locator.wait_for(state="visible", timeout=10000)
            return await locator.screenshot(type="png", omit_background=True)
        finally:
            await page.close()

    if browser is not None:
        return await _shot(browser)

    async with async_playwright() as p:
        launched = await p.chromium.launch(headless=True)
        try:
            return await _shot(launched)
        finally:
            await launched.close()


async def prepare_html_with_cid(html: str, *, convert_svg: bool = True) -> HtmlWithCid:
    """
    Substitui data:image;base64 por cid:imgN e devolve os bytes para anexar.

    O conteúdo textual do e-mail permanece o mesmo; só o transporte das imagens muda.
    """
    images: list[InlineImage] = []
    parts: list[str] = []
    last = 0
    idx = 0
    matches = list(DATA_URI_RE.finditer(html))
    need_svg = convert_svg and any(m.group(2).lower() == "svg+xml" for m in matches)

    browser = None
    playwright_cm = None
    if need_svg:
        from playwright.async_api import async_playwright

        playwright_cm = async_playwright()
        p = await playwright_cm.__aenter__()
        browser = await p.chromium.launch(headless=True)

    try:
        for match in matches:
            quote = match.group(1)
            img_type = match.group(2).lower()
            b64_payload = match.group(3)
            parts.append(html[last : match.start()])

            try:
                raw = _decode_b64(b64_payload)
            except Exception as exc:
                logger.warning("inline_image_decode_failed", error=str(exc), type=img_type)
                parts.append(match.group(0))
                last = match.end()
                continue

            original = img_type
            content = raw
            subtype = MIME_MAP.get(img_type, "png")

            if img_type == "svg+xml" and convert_svg:
                try:
                    content = await svg_bytes_to_png(raw, browser=browser)
                    subtype = "png"
                    logger.info("svg_converted_to_png", index=idx, bytes=len(content))
                except Exception as exc:
                    logger.error("svg_convert_failed", error=str(exc), index=idx)
                    last = match.end()
                    parts.append(match.group(0))
                    continue

            if subtype in {"png", "jpeg", "jpg", "gif", "webp"}:
                try:
                    content = flatten_png(content)
                    subtype = "png"
                except Exception as exc:
                    logger.warning("png_flatten_failed", error=str(exc), index=idx)

            cid = f"img{idx}@trentin.software"
            images.append(
                InlineImage(
                    cid=cid,
                    content=content,
                    subtype=subtype,
                    original_type=original,
                    index=idx,
                )
            )
            parts.append(f"{quote}cid:{cid}{quote}")
            idx += 1
            last = match.end()
    finally:
        if browser is not None:
            await browser.close()
        if playwright_cm is not None:
            await playwright_cm.__aexit__(None, None, None)

    parts.append(html[last:])
    new_html = "".join(parts)

    remaining = len(DATA_URI_RE.findall(new_html))
    if remaining:
        logger.warning("inline_images_remaining", count=remaining)

    logger.info(
        "inline_images_prepared",
        total=len(images),
        svg_converted=sum(1 for i in images if i.original_type == "svg+xml" and i.subtype == "png"),
        html_delta=len(html) - len(new_html),
    )
    return HtmlWithCid(html=new_html, images=images)


def build_mime_images(images: list[InlineImage]) -> list[MIMEImage]:
    """Cria partes MIMEImage com Content-ID para multipart/related."""
    parts: list[MIMEImage] = []
    for img in images:
        subtype = "png" if img.subtype == "svg+xml" else img.subtype
        part = MIMEImage(img.content, _subtype=subtype)
        part.add_header("Content-ID", f"<{img.cid}>")
        part.add_header("Content-Disposition", "inline", filename=f"img{img.index}.{subtype}")
        part.add_header("X-Attachment-Id", img.cid)
        parts.append(part)
    return parts
