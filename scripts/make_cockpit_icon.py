"""Generate the cockpit's .ico (the Start-menu / taskbar identity).

Draws the dark-cockpit motif -- a quiet near-black field, one green up-candle, one
amber caution lamp -- at 256px and saves a multi-size .ico into the packaged assets
dir. Committed (not a build step) because the icon changes ~never; re-run this and
re-run scripts/make_cockpit_shortcut.ps1 after editing. Shapes are deliberately
chunky so the 16px tray/taskbar size still reads.

    python scripts/make_cockpit_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

# tokens.css palette -- the icon is the UI's identity, not a new one.
BG = (11, 14, 19, 255)          # --bg   #0B0E13
BORDER = (38, 50, 74, 255)      # --line2 #26324a
GREEN = (63, 191, 127, 255)     # --green #3FBF7F
AMBER = (224, 169, 62, 255)     # --amber #E0A93E
DIM = (87, 102, 124, 255)       # --dim  #57667C

OUT = Path(__file__).resolve().parents[1] / "src/swing_screener/cockpit/assets/cockpit.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def draw(size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 256.0  # all coordinates are designed on a 256 grid

    def r(v: float) -> float:
        return v * s

    # the dark field with a subtle border (rounded so it reads as an app tile)
    d.rounded_rectangle((r(8), r(8), r(248), r(248)), radius=r(44), fill=BG,
                        outline=BORDER, width=max(1, round(r(10))))

    # dim baseline grid hint (two faint rails -- instrument, not chart junk)
    for y in (150, 196):
        d.line((r(44), r(y), r(212), r(y)), fill=(*DIM[:3], 70), width=max(1, round(r(6))))

    # the green up-candle: wick + body, left of center
    d.line((r(96), r(52), r(96), r(210)), fill=GREEN, width=max(1, round(r(12))))
    d.rounded_rectangle((r(68), r(92), r(124), r(178)), radius=r(10), fill=GREEN)

    # the amber caution lamp, upper right -- light means attention
    d.ellipse((r(152), r(64), r(204), r(116)), fill=AMBER)
    d.ellipse((r(140), r(52), r(216), r(128)), outline=(*AMBER[:3], 90),
              width=max(1, round(r(8))))

    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = draw(256)
    base.save(OUT, format="ICO", sizes=[(n, n) for n in SIZES])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {SIZES})")  # noqa: T201


if __name__ == "__main__":
    main()
