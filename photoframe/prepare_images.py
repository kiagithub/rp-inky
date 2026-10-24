#!/usr/bin/env python3
"""Prepare photos for the Inky Impression 7.3 photo frame.

Centre-crops photos to 480x800 portrait JPEGs while preserving EXIF
(capture date and GPS) so the frame can show where and when each photo
was taken.
"""

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps

TARGET = (480, 800)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    IMAGE_EXTENSIONS |= {".heic", ".heif"}
except (ImportError, OSError, RuntimeError):
    pillow_heif = None


def _exif_bytes(image):
    """Return the source EXIF bytes, or None when there is no EXIF."""
    try:
        data = image.getexif().tobytes()
    except (AttributeError, OSError, ValueError):
        return None
    return data or None


def convert_image(source, output, quality=92, strip_metadata=False, force=False):
    """Convert one photo to a 480x800 JPEG.

    Returns one of: "converted", "skipped", or "error: ...".
    """
    source = Path(source)
    output = Path(output)

    if source.resolve() == output.resolve():
        return "skipped (source is already in the output directory)"
    if output.exists() and not force:
        return "skipped (exists)"

    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            # Capture EXIF after exif_transpose: it clears the Orientation tag
            # (so the frame will not rotate the already-corrected pixels again)
            # while keeping the capture date and GPS.
            exif = None if strip_metadata else _exif_bytes(image)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                image = image.convert("RGBA")
                background = Image.new("RGBA", image.size, (255, 255, 255, 255))
                image = Image.alpha_composite(background, image)
            image = image.convert("RGB")
            image = ImageOps.fit(image, TARGET, method=LANCZOS, centering=(0.5, 0.5))
            output.parent.mkdir(parents=True, exist_ok=True)
            save_options = {"quality": quality, "optimize": True}
            if exif:
                save_options["exif"] = exif
            image.save(output, "JPEG", **save_options)
    except Exception as exc:
        if source.suffix.lower() in (".heic", ".heif") and pillow_heif is None:
            return "error: HEIC support requires the pillow-heif package"
        return f"error: {exc}"
    return "converted"


def find_sources(source):
    """Return supported image files from a file or directory."""
    source = Path(source).expanduser()
    if source.is_file():
        return [source]
    if source.is_dir():
        return sorted(
            (path for path in source.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda path: str(path).lower(),
        )
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="Convert photos to 480x800 JPEGs for the Inky photo frame")
    parser.add_argument("source", type=Path, help="source photo or directory")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "image",
        help="output directory (default: image/ next to this script)",
    )
    parser.add_argument("--quality", type=int, default=92, help="JPEG quality (default: 92)")
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument("--strip-metadata", action="store_true", help="do not preserve EXIF metadata")
    args = parser.parse_args(argv)

    sources = find_sources(args.source)
    if not sources:
        print(f"No supported images found in {args.source}")
        if args.source.is_dir() and pillow_heif is None:
            print("HEIC/HEIF support is optional: pip install pillow-heif")
        return 1

    converted = skipped = errors = 0
    for source in sources:
        target = args.out / f"{source.stem}.jpg"
        result = convert_image(
            source,
            target,
            quality=args.quality,
            strip_metadata=args.strip_metadata,
            force=args.force,
        )
        if result == "converted":
            converted += 1
            print(f"converted  {source} -> {target}")
        elif result.startswith("skipped"):
            skipped += 1
            print(f"skipped    {source} ({result.removeprefix('skipped ').strip('()')})")
        else:
            errors += 1
            print(f"error      {source}: {result.removeprefix('error: ')}")

    print(f"\nConverted {converted}, skipped {skipped}, errors {errors}")
    print(f"Output directory: {args.out}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
