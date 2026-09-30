"""Draw the YtubeCatcher logo (red rounded rectangle + white play triangle) into installer/ytubecatcher.ico."""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def logo(size: int) -> Image.Image:
    ss = 8                                      # draw big, scale down = smooth edges
    s = size * ss
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    k = s / 24.0                                # same 24x24 design box as draw_icon("logo")
    d.rounded_rectangle((1 * k, 4.5 * k, 23 * k, 19.5 * k), radius=5 * k, fill="#ff0033")
    d.polygon([(9.5 * k, 8 * k), (16.5 * k, 12 * k), (9.5 * k, 16 * k)], fill="white")
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = os.path.join(HERE, "ytubecatcher.ico")
    big = logo(256)
    big.save(out, format="ICO", sizes=[(n, n) for n in SIZES],
             append_images=[logo(n) for n in SIZES if n != 256])
    print("wrote", out)
