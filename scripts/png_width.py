#!/usr/bin/env python3
"""Print the size of a PNG file as `width height ratio`; exit 1 if out of bounds.

Usage: png_width.py FILE [--min N] [--max N] [--aspect-min R] [--aspect-max R]
Reads the IHDR chunk directly (stdlib only). ratio = width / height.
"""
import argparse
import struct
import sys

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise ValueError(f"{path}: not a PNG file")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file")
    parser.add_argument("--min", type=int, default=0, dest="min_width")
    parser.add_argument("--max", type=int, default=0, dest="max_width", help="0 = no maximum")
    parser.add_argument("--aspect-min", type=float, default=0.0)
    parser.add_argument("--aspect-max", type=float, default=0.0, help="0 = no maximum")
    args = parser.parse_args(argv)
    try:
        width, height = png_size(args.file)
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if height == 0:
        print(f"{args.file}: height is 0", file=sys.stderr)
        return 2
    ratio = width / height
    print(f"{width} {height} {ratio:.2f}")
    errors = []
    if width < args.min_width:
        errors.append(f"width {width} < {args.min_width}")
    if args.max_width and width > args.max_width:
        errors.append(f"width {width} > {args.max_width}")
    if ratio < args.aspect_min:
        errors.append(f"aspect {ratio:.2f} < {args.aspect_min}")
    if args.aspect_max and ratio > args.aspect_max:
        errors.append(f"aspect {ratio:.2f} > {args.aspect_max}")
    for err in errors:
        print(f"{args.file}: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
