"""Install the application icon: tools/set_icon.py <icon.png|icon.ico>

Writes assets/semishigure.ico (16..256 px, used by the Windows installer and the
window), semishigure/ui/static/icon.png (256 px) and favicon.png (32 px).
Requires Pillow (pip install pillow). A PNG should be square, 512 px or larger,
with a transparent or plain background.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    im = Image.open(src)
    if src.suffix.lower() == ".ico":
        best = max(im.ico.sizes()) if hasattr(im, "ico") else im.size
        im = im.ico.getimage(best) if hasattr(im, "ico") else im
    im = im.convert("RGBA")
    if im.width != im.height:
        side = max(im.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = canvas
    frames = [im.resize((s, s), Image.LANCZOS) for s in SIZES]
    out_ico = ROOT / "assets" / "semishigure.ico"
    out_ico.parent.mkdir(exist_ok=True)
    frames[-1].save(out_ico, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    im.resize((256, 256), Image.LANCZOS).save(ROOT / "semishigure" / "ui" / "static" / "icon.png")
    im.resize((32, 32), Image.LANCZOS).save(ROOT / "semishigure" / "ui" / "static" / "favicon.png")
    print(f"wrote {out_ico}, ui/static/icon.png, ui/static/favicon.png from {src} ({im.width}x{im.height})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
