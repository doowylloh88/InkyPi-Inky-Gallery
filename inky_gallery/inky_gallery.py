from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image, ImageOps, ImageColor, ImageDraw, ImageEnhance, ImageFont
from PIL.IptcImagePlugin import getiptcinfo
import logging
import os
import random
import json
import re
from pathlib import Path
from io import BytesIO

from blueprints.plugin import plugin_bp
from utils.image_utils import pad_image_blur, apply_image_enhancement
from flask import jsonify, request, current_app

logger = logging.getLogger(__name__)

# tags.json holds exactly one entry: {"path": "<folder>", "tags": [...]}
# It is fully overwritten each time a folder is scanned, so there are
# never stale or duplicate entries.
TAGS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "tags.json")

FONT_PATH = Path(__file__).parent / "OpenSans-VariableFont_wdth,wght.ttf"
LUT_FILE = Path(__file__).parent / "lut.json"
CAPTION_PATTERN = re.compile(r"\[([^\]]+)\]")


# ---------------------------------------------------------------------------
# LUT helpers  (ported from Immich)
# ---------------------------------------------------------------------------

def load_lut_list() -> list[dict]:
    """Load LUT entries from lut.json."""
    try:
        if LUT_FILE.exists():
            with LUT_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Failed to load lut.json: {e}")
    return []


def find_lut_by_name(lut_name: str) -> dict | None:
    """Find a LUT entry by its lut_name."""
    if not lut_name:
        return None
    for entry in load_lut_list():
        if entry.get("lut_name") == lut_name:
            return entry
    return None


def apply_channel_adjust(img: Image.Image, channel_adjust: dict) -> Image.Image:
    """Apply per-channel brightness multipliers (R/G/B)."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    r, g, b = img.split()
    red_mult   = channel_adjust.get("red",   1.0)
    green_mult = channel_adjust.get("green", 1.0)
    blue_mult  = channel_adjust.get("blue",  1.0)
    if red_mult   != 1.0: r = ImageEnhance.Brightness(r).enhance(red_mult)
    if green_mult != 1.0: g = ImageEnhance.Brightness(g).enhance(green_mult)
    if blue_mult  != 1.0: b = ImageEnhance.Brightness(b).enhance(blue_mult)
    return Image.merge("RGB", (r, g, b))


def apply_palette_quantize(img: Image.Image, palette: dict) -> Image.Image:
    """Quantize image to a custom color palette using Floyd-Steinberg dithering."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageEnhance.Contrast(img).enhance(1.2)
    img = ImageEnhance.Color(img).enhance(1.3)

    palette_colors = []
    for color_name in ("black", "white", "red", "yellow", "green", "blue"):
        rgb = palette.get(color_name)
        if rgb and len(rgb) == 3:
            palette_colors.extend(rgb)

    if not palette_colors:
        logger.warning("No valid palette colors found, skipping quantization.")
        return img

    while len(palette_colors) < 768:
        palette_colors.extend([0, 0, 0])

    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(palette_colors)
    img = img.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    img = img.convert("RGB")
    return img


def apply_lut(img: Image.Image, lut: dict) -> Image.Image:
    """Apply LUT color adjustments to an image."""
    channel_adjust = lut.get("channel_adjust")
    if channel_adjust:
        img = apply_channel_adjust(img, channel_adjust)

    palette  = lut.get("palette")
    quantize = lut.get("quantize", 0)
    if palette and quantize:
        img = apply_palette_quantize(img, palette)

    return img


# ---------------------------------------------------------------------------
# Caption helpers  (ported from Immich, adapted for local files)
# ---------------------------------------------------------------------------

def extract_iptc_caption_from_file(file_path: str) -> str | None:
    """
    Read IPTC caption (dataset 2:120) from a local image file.
    Returns the bracketed text [like this] if present, else the raw value,
    or None if no caption is found.
    """
    try:
        with Image.open(file_path) as img:
            iptc = getiptcinfo(img)
            if not iptc:
                return None
            raw = iptc.get((2, 120))
            if not raw:
                return None
            text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else str(raw)
            if text.strip().lower() == "none":
                return None
            match = CAPTION_PATTERN.search(text)
            if match:
                return match.group(1).strip()
            return None
    except Exception as e:
        logger.warning(f"Failed to extract IPTC caption from {file_path}: {e}")
    return None


def draw_caption(img: Image.Image, caption: str) -> Image.Image:
    font_size = max(12, int(img.height * 0.06))    
    try:
        font = ImageFont.truetype(str(FONT_PATH), font_size)
        try:
            font.set_variation_by_axes([600, 100])
        except Exception:
            pass  # font loaded fine, variation just not supported
    except Exception as e:
        logger.warning(f"Could not load Open-Sans font, falling back to default: {e}")
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img, "RGBA")
    x_padding = int(img.size[0] * 0.04)
    y_padding = int(img.size[0] * 0.02)
    bbox = draw.textbbox((0, 0), caption, font=font)
    text_h = bbox[3] - bbox[1]
    x = x_padding
    y = img.size[1] - text_h - y_padding * 2

    for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        draw.text((x + dx, y + dy), caption, font=font, fill=(0, 0, 0, 255))
    draw.text((x, y), caption, font=font, fill=(255, 255, 255, 255))

    return img


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def list_files_in_folder(folder_path):
    """Return a list of image file paths in the given folder, excluding hidden files."""
    image_extensions = ('.avif', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heif', '.heic')
    image_files = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.lower().endswith(image_extensions) and not f.startswith('.'):
                image_files.append(os.path.join(root, f))
    return image_files


def count_images_in_folder(folder_path):
    """Return recursive image count for a folder, excluding hidden files/folders."""
    image_extensions = ('.avif', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.heif', '.heic')
    count = 0
    try:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if f.lower().endswith(image_extensions) and not f.startswith('.'):
                    count += 1
        return count
    except Exception:
        return 0


def get_current_home():
    return str(Path(__file__).resolve().parents[4])


def get_allowed_root_candidates():
    home_dir = get_current_home()
    return [
        os.path.join(home_dir, "Pictures"),
        os.path.join(home_dir, "images"),
        os.path.join(home_dir, "media"),
        os.path.join(home_dir, "photos"),
    ]


def is_within_directory(path, directory):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(directory)]) == os.path.abspath(directory)
    except ValueError:
        return False


def build_tree_node(folder_path):
    """Return metadata for a single folder node."""
    folder_path = os.path.abspath(folder_path)
    name = os.path.basename(folder_path) or folder_path
    image_count = count_images_in_folder(folder_path)

    has_children = False
    try:
        for entry in os.listdir(folder_path):
            if entry.startswith('.'):
                continue
            full_path = os.path.join(folder_path, entry)
            if os.path.isdir(full_path):
                has_children = True
                break
    except Exception:
        has_children = False

    return {
        "name": name,
        "path": folder_path,
        "image_count": image_count,
        "has_children": has_children,
    }


# ---------------------------------------------------------------------------
# Tag extraction
# ---------------------------------------------------------------------------

def extract_tags_from_image(file_path: str) -> set[str]:
    """
    Extract keyword/tag strings from a single image file.

    Sources checked (all merged):
      1. IPTC record 2, dataset 25 (Keywords)
      2. Pillow's internal 'keywords' info key (covers some EXIF/XMP paths)
    """
    tags: set[str] = set()
    try:
        with Image.open(file_path) as img:
            try:
                iptc = getiptcinfo(img)
                if iptc:
                    raw = iptc.get((2, 25))
                    if raw:
                        if isinstance(raw, (bytes, bytearray)):
                            raw = [raw]
                        for item in raw:
                            if isinstance(item, (bytes, bytearray)):
                                kw = item.decode("utf-8", errors="ignore").strip()
                            else:
                                kw = str(item).strip()
                            if kw:
                                tags.add(kw)
            except Exception:
                pass

            try:
                kw_info = img.info.get("keywords") or img.info.get("Keywords") or ""
                if kw_info:
                    for kw in str(kw_info).split(";"):
                        kw = kw.strip()
                        if kw:
                            tags.add(kw)
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"Could not open {file_path} for tag extraction: {e}")

    return tags


def _read_tags_cache() -> dict:
    """
    Read tags.json and return its contents, or {} on any error.
    Expected format: {"path": "<folder>", "tags": [...]}
    """
    try:
        if os.path.exists(TAGS_CACHE_FILE):
            with open(TAGS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.warning(f"Failed to read tags cache: {e}")
    return {}


def _write_tags_cache(folder_path: str, tags: list[str]) -> None:
    """
    Overwrite tags.json with a single entry for folder_path.
    Any previous folder's data is discarded.
    """
    try:
        with open(TAGS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"path": folder_path, "tags": tags}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to write tags cache: {e}")


def scan_tags_for_folder(folder_path: str) -> list[str]:
    """
    Walk all images in folder_path, collect tags, write a fresh single-entry
    tags.json (replacing whatever was there), and return the sorted tag list.
    """
    folder_path = os.path.abspath(folder_path)
    image_files = list_files_in_folder(folder_path)

    all_tags: set[str] = set()
    for fp in image_files:
        all_tags.update(extract_tags_from_image(fp))

    sorted_tags = sorted(all_tags, key=str.casefold)
    _write_tags_cache(folder_path, sorted_tags)

    logger.info(f"Tag scan complete for '{folder_path}': {len(sorted_tags)} unique tag(s) found.")
    return sorted_tags


def get_tags_for_folder(folder_path: str, force_rescan: bool = False) -> list[str]:
    """
    Return tags for folder_path.
    Serves from cache when cache["path"] matches; scans (and overwrites) otherwise.
    force_rescan=True always re-scans, used by the Refresh Tags button.
    """
    folder_path = os.path.abspath(folder_path)
    if not force_rescan:
        cache = _read_tags_cache()
        if cache.get("path") == folder_path:
            return cache.get("tags", [])
    return scan_tags_for_folder(folder_path)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@plugin_bp.route("/plugin/inky_gallery/tree_roots", methods=["GET"])
def tree_roots():
    blocked_root = os.path.abspath("/home/inky/InkyPi")
    roots = []

    for candidate in get_allowed_root_candidates():
        candidate = os.path.abspath(candidate)
        if not os.path.isdir(candidate):
            continue
        if candidate == blocked_root or is_within_directory(candidate, blocked_root):
            continue
        roots.append(build_tree_node(candidate))

    return jsonify({"roots": roots})


@plugin_bp.route("/plugin/inky_gallery/tree_children", methods=["GET"])
def tree_children():
    blocked_root = os.path.abspath("/home/inky/InkyPi")
    requested_path = os.path.abspath((request.args.get("path") or "").strip())

    if not requested_path:
        return jsonify({"error": "Folder path is required"}), 400

    allowed_roots = [
        os.path.abspath(path)
        for path in get_allowed_root_candidates()
        if os.path.isdir(path)
    ]

    if not any(is_within_directory(requested_path, root) for root in allowed_roots):
        return jsonify({"error": "Access denied"}), 403

    if requested_path == blocked_root or is_within_directory(requested_path, blocked_root):
        return jsonify({"error": "Access denied"}), 403

    if not os.path.isdir(requested_path):
        return jsonify({"error": f"Folder does not exist: {requested_path}"}), 400

    children = []
    try:
        for name in sorted(os.listdir(requested_path), key=str.lower):
            if name.startswith('.'):
                continue

            full_path = os.path.abspath(os.path.join(requested_path, name))
            if not os.path.isdir(full_path):
                continue

            if full_path == blocked_root or is_within_directory(full_path, blocked_root):
                continue

            if not any(is_within_directory(full_path, root) for root in allowed_roots):
                continue

            children.append(build_tree_node(full_path))

        return jsonify({"path": requested_path, "children": children})
    except Exception as e:
        logger.error(f"Error loading folder tree children for {requested_path}: {e}")
        return jsonify({"error": "Failed to browse folders"}), 500


@plugin_bp.route("/plugin/inky_gallery/folder_tags", methods=["GET"])
def folder_tags():
    """
    GET /plugin/inky_gallery/folder_tags?path=<folder_path>
    Returns cached tags if the cache is for this folder; scans otherwise.
    """
    blocked_root = os.path.abspath("/home/inky/InkyPi")
    requested_path = os.path.abspath((request.args.get("path") or "").strip())

    if not requested_path:
        return jsonify({"ok": False, "error": "Folder path is required"}), 400

    allowed_roots = [
        os.path.abspath(p)
        for p in get_allowed_root_candidates()
        if os.path.isdir(p)
    ]

    if not any(is_within_directory(requested_path, root) for root in allowed_roots):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    if requested_path == blocked_root or is_within_directory(requested_path, blocked_root):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    if not os.path.isdir(requested_path):
        return jsonify({"ok": False, "error": "Folder does not exist"}), 400

    try:
        tags = get_tags_for_folder(requested_path, force_rescan=False)
        return jsonify({"ok": True, "tags": tags})
    except Exception as e:
        logger.error(f"Error fetching tags for {requested_path}: {e}")
        return jsonify({"ok": False, "error": "Failed to retrieve tags"}), 500


@plugin_bp.route("/plugin/inky_gallery/refresh_tags", methods=["POST"])
def refresh_tags():
    """
    POST /plugin/inky_gallery/refresh_tags
    Body JSON: {"path": "<folder_path>"}
    Force re-scans the folder and overwrites tags.json.
    """
    blocked_root = os.path.abspath("/home/inky/InkyPi")
    body = request.get_json(silent=True) or {}
    requested_path = os.path.abspath((body.get("path") or "").strip())

    if not requested_path:
        return jsonify({"ok": False, "error": "Folder path is required"}), 400

    allowed_roots = [
        os.path.abspath(p)
        for p in get_allowed_root_candidates()
        if os.path.isdir(p)
    ]

    if not any(is_within_directory(requested_path, root) for root in allowed_roots):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    if requested_path == blocked_root or is_within_directory(requested_path, blocked_root):
        return jsonify({"ok": False, "error": "Access denied"}), 403

    if not os.path.isdir(requested_path):
        return jsonify({"ok": False, "error": "Folder does not exist"}), 400

    try:
        tags = scan_tags_for_folder(requested_path)
        return jsonify({"ok": True, "tags": tags})
    except Exception as e:
        logger.error(f"Error refreshing tags for {requested_path}: {e}")
        return jsonify({"ok": False, "error": "Failed to refresh tags"}), 500


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class InkyGallery(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()

        # Pass current system image settings to the template
        device_config = current_app.config.get("DEVICE_CONFIG")
        if device_config:
            img_settings = device_config.get_config("image_settings") or {}
            template_params["system_image_settings"] = {
                "saturation": img_settings.get("saturation", 1.0),
                "brightness":  img_settings.get("brightness",  1.0),
                "contrast":    img_settings.get("contrast",    1.0),
                "sharpness":   img_settings.get("sharpness",   1.0),
            }
        else:
            template_params["system_image_settings"] = {
                "saturation": 1.0, "brightness": 1.0,
                "contrast":   1.0, "sharpness":  1.0,
            }

        # Pass LUT options and full LUT data to the template
        lut_list = load_lut_list()
        template_params["lut_options"] = [
            {
                "value": entry.get("lut_name", ""),
                "label": entry.get("display name", entry.get("lut_name", "")),
            }
            for entry in lut_list
            if entry.get("lut_name")
        ]
        template_params["lut_data"] = {
            entry["lut_name"]: entry
            for entry in lut_list
            if entry.get("lut_name")
        }

        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== InkyGallery: Starting image generation ===")

        folder_path = settings.get('folder_path')
        if not folder_path:
            raise RuntimeError("Folder path is required.")
        if not os.path.exists(folder_path):
            raise RuntimeError(f"Folder does not exist: {folder_path}")
        if not os.path.isdir(folder_path):
            raise RuntimeError(f"Path is not a directory: {folder_path}")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tag_filter   = (settings.get("tagFilter")    or "").strip() or None
        show_captions = settings.get("showCaptions") == "true"

        image_files = list_files_in_folder(folder_path)
        if not image_files:
            raise RuntimeError(f"No image files found in folder: {folder_path}")

        # Apply tag filter
        if tag_filter:
            tagged_files = [
                fp for fp in image_files
                if any(t.casefold() == tag_filter.casefold()
                       for t in extract_tags_from_image(fp))
            ]
            if tagged_files:
                logger.info(f"Tag filter '{tag_filter}' matched {len(tagged_files)}/{len(image_files)} image(s)")
                image_files = tagged_files
            else:
                logger.warning(f"Tag filter '{tag_filter}' matched no images — showing unfiltered")

        image_path = random.choice(image_files)
        logger.info(f"Selected: {os.path.basename(image_path)}")

        # Caption extraction
        caption = None
        if show_captions:
            caption = extract_iptc_caption_from_file(image_path)
            if caption and len(caption) > 35:
                caption = caption[:35] + "..."
            if not caption:
                logger.info(f"No caption found for {os.path.basename(image_path)}")

        use_padding = settings.get('padImage') == "true"
        background_option = settings.get('backgroundOption', 'blur')

        img = self.image_loader.from_file(image_path, dimensions, resize=not use_padding)
        if not img:
            raise RuntimeError("Failed to load image, please check logs.")

        if caption:
            img = img.convert("RGBA")
            img = draw_caption(img, caption)
            img = img.convert("RGB")

        if use_padding:
            if background_option == "blur":
                img = pad_image_blur(img, dimensions)
            else:
                background_color = ImageColor.getcolor(
                    settings.get('backgroundColor') or "white", img.mode
                )
                img = ImageOps.pad(img, dimensions, color=background_color,
                                method=Image.Resampling.LANCZOS)

        # Apply LUT
        lut = None
        lut_name = (settings.get("lut") or "").strip()
        if lut_name:
            lut = find_lut_by_name(lut_name)
            if lut:
                img = apply_lut(img, lut)
                logger.info(f"Applied LUT '{lut_name}'")
            else:
                logger.warning(f"LUT '{lut_name}' not found in lut.json")

        # Merge enhancement values: defaults -> LUT sliders -> UI settings
        enhancement_settings = {
            "saturation": 1.0, "brightness": 1.0,
            "contrast":   1.0, "sharpness":  1.0,
        }

        lut_sliders = (lut or {}).get("sliders", {})
        for k in enhancement_settings:
            if k in lut_sliders and lut_sliders[k] not in (None, ""):
                enhancement_settings[k] = float(lut_sliders[k])

        for k in enhancement_settings:
            if k in settings and settings[k] not in (None, ""):
                enhancement_settings[k] = float(settings[k])

        img = apply_image_enhancement(img, enhancement_settings)

        # Persist applied values back to system image_settings
        current_settings = (device_config.get_config("image_settings") or {}).copy()
        changed = False
        for k in enhancement_settings:
            if enhancement_settings[k] != current_settings.get(k):
                current_settings[k] = enhancement_settings[k]
                changed = True
        if changed:
            device_config.update_value("image_settings", current_settings)

        return img