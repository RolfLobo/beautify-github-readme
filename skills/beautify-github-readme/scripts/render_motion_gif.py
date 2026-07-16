#!/usr/bin/env python3
"""Render named SVG layers into a compact, GitHub-safe animated GIF."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from PIL import Image
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit("Pillow is required: python3 -m pip install Pillow") from exc


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Animate named SVG layers from a JSON motion spec and encode a GIF."
    )
    parser.add_argument("input_svg", type=Path)
    parser.add_argument("output_gif", type=Path)
    parser.add_argument("--spec", required=True, type=Path, help="JSON motion spec")
    parser.add_argument(
        "--keep-frames",
        type=Path,
        help="Keep rendered layers and PNG frames in this new or empty directory",
    )
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"motion spec not found: {path}")
    except json.JSONDecodeError as exc:
        fail(f"invalid motion spec JSON: {exc}")

    defaults = {
        "width": 960,
        "fps": 30,
        "duration": 5.0,
        "colors": 192,
        "dither": "none",
        "max_size_mb": 5.0,
        "reveals": [],
        "layers": [],
    }
    defaults.update(spec)
    return defaults


def validate_spec(spec: dict) -> None:
    if not 1 <= int(spec["fps"]) <= 60:
        fail("fps must be between 1 and 60")
    if float(spec["duration"]) <= 0:
        fail("duration must be positive")
    if int(spec["width"]) <= 0:
        fail("width must be positive")
    if not 2 <= int(spec["colors"]) <= 256:
        fail("colors must be between 2 and 256")
    allowed_dither = {
        "none",
        "bayer",
        "heckbert",
        "floyd_steinberg",
        "sierra2",
        "sierra2_4a",
    }
    if spec["dither"] not in allowed_dither:
        fail(f"unsupported dither mode: {spec['dither']}")

    ids: list[str] = []
    for item in [*spec["reveals"], *spec["layers"]]:
        element_id = item.get("id")
        if not element_id:
            fail("every reveal and layer needs a non-empty id")
        ids.append(element_id)
    if len(ids) != len(set(ids)):
        fail("motion element ids must be unique")


def command_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        fail(f"required command not found: {name}")
    return path


def choose_renderer() -> tuple[str, str]:
    if shutil.which("rsvg-convert"):
        return "rsvg-convert", command_path("rsvg-convert")
    if shutil.which("sips"):
        return "sips", command_path("sips")
    fail("install rsvg-convert, or run on macOS with sips available")


def write_svg(root: ET.Element, path: Path) -> None:
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def find_path(root: ET.Element, element_id: str) -> list[ET.Element] | None:
    if root.attrib.get("id") == element_id:
        return [root]
    for child in root:
        path = find_path(child, element_id)
        if path:
            return [root, *path]
    return None


def remove_ids(root: ET.Element, element_ids: set[str]) -> None:
    for parent in root.iter():
        for child in list(parent):
            if child.attrib.get("id") in element_ids:
                parent.remove(child)


def extracted_layer(root: ET.Element, element_id: str) -> ET.Element:
    path = find_path(root, element_id)
    if not path:
        fail(f"SVG element id not found: {element_id}")

    layer_root = ET.Element(root.tag, dict(root.attrib))
    for child in root:
        if child.tag.rsplit("}", 1)[-1] == "defs":
            layer_root.append(copy.deepcopy(child))

    destination = layer_root
    for node in path[1:-1]:
        shell = ET.Element(node.tag, dict(node.attrib))
        destination.append(shell)
        destination = shell
    destination.append(copy.deepcopy(path[-1]))
    return layer_root


def render_svg(
    renderer: tuple[str, str], svg_path: Path, png_path: Path
) -> None:
    name, executable = renderer
    if name == "rsvg-convert":
        command = [executable, str(svg_path), "-o", str(png_path)]
    else:
        command = [
            executable,
            "-s",
            "format",
            "png",
            str(svg_path),
            "--out",
            str(png_path),
        ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)


def ease_out_cubic(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return 1 - (1 - value) ** 3


def smoothstep(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


def progress(value: float, start: float, end: float) -> float:
    if end <= start:
        fail(f"motion end must be greater than start: {start} -> {end}")
    if value <= start:
        return 0.0
    if value >= end:
        return 1.0
    return (value - start) / (end - start)


def opacity_layer(image: Image.Image, opacity: float) -> Image.Image:
    if opacity >= 0.999:
        return image
    result = image.copy()
    result.putalpha(result.getchannel("A").point(lambda value: round(value * opacity)))
    return result


def motion_progress(time: float, block: dict, easing: str) -> float:
    value = progress(time, float(block["start"]), float(block["end"]))
    return ease_out_cubic(value) if easing == "enter" else smoothstep(value)


def build_frames(
    root: ET.Element,
    spec: dict,
    renderer: tuple[str, str],
    workspace: Path,
) -> tuple[Path, int, int, int]:
    moving_ids = {
        item["id"] for item in [*spec["reveals"], *spec["layers"]]
    }

    base_root = copy.deepcopy(root)
    remove_ids(base_root, moving_ids)
    base_svg = workspace / "base.svg"
    base_png = workspace / "base.png"
    write_svg(base_root, base_svg)
    render_svg(renderer, base_svg, base_png)

    rendered: dict[str, Image.Image] = {}
    for element_id in moving_ids:
        svg_path = workspace / f"layer-{element_id}.svg"
        png_path = workspace / f"layer-{element_id}.png"
        write_svg(extracted_layer(root, element_id), svg_path)
        render_svg(renderer, svg_path, png_path)
        rendered[element_id] = Image.open(png_path).convert("RGBA")

    base_source = Image.open(base_png).convert("RGBA")
    source_width, source_height = base_source.size
    output_width = int(spec["width"])
    output_height = round(source_height * output_width / source_width)
    scale = output_width / source_width
    size = (output_width, output_height)

    base = base_source.resize(size, Image.Resampling.LANCZOS)
    rendered = {
        key: image.resize(size, Image.Resampling.LANCZOS)
        for key, image in rendered.items()
    }

    fps = int(spec["fps"])
    frame_count = round(float(spec["duration"]) * fps)
    frames_dir = workspace / "frames"
    frames_dir.mkdir()

    for frame in range(frame_count):
        time = frame / fps
        canvas = base.copy()

        for reveal in spec["reveals"]:
            state = ease_out_cubic(
                progress(time, float(reveal["start"]), float(reveal["end"]))
            )
            exit_state = 0.0
            if reveal.get("exit"):
                exit_state = motion_progress(time, reveal["exit"], "exit")
            if state <= 0 or exit_state >= 1:
                continue

            layer = rendered[reveal["id"]]
            bbox = layer.getbbox()
            if not bbox:
                fail(f"rendered reveal is empty: {reveal['id']}")
            axis = reveal.get("axis", "x")
            if axis == "x":
                edge = round(bbox[0] + (bbox[2] - bbox[0]) * state)
                visible = layer.crop((0, 0, edge, output_height))
            elif axis == "y":
                edge = round(bbox[1] + (bbox[3] - bbox[1]) * state)
                visible = layer.crop((0, 0, output_width, edge))
            else:
                fail(f"unsupported reveal axis: {axis}")
            visible = opacity_layer(visible, 1 - exit_state)
            canvas.alpha_composite(visible, (0, 0))

        for item in spec["layers"]:
            entered = motion_progress(time, item["enter"], "enter")
            exit_state = motion_progress(time, item["exit"], "exit")
            opacity = entered * (1 - exit_state)
            if opacity <= 0:
                continue

            start_x, start_y = item["enter"].get("from", [0, 0])
            end_x, end_y = item["exit"].get("to", [0, 0])
            dx = start_x * scale * (1 - entered) + end_x * scale * exit_state
            dy = start_y * scale * (1 - entered) + end_y * scale * exit_state
            layer = opacity_layer(rendered[item["id"]], opacity)
            canvas.alpha_composite(layer, (round(dx), round(dy)))

        canvas.convert("RGB").save(frames_dir / f"frame-{frame:04d}.png")

    return frames_dir, frame_count, output_width, output_height


def encode_gif(
    frames_dir: Path, output: Path, spec: dict, ffmpeg: str
) -> None:
    palette = frames_dir.parent / "palette.png"
    fps = int(spec["fps"])
    input_pattern = frames_dir / "frame-%04d.png"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(input_pattern),
            "-vf",
            f"palettegen=stats_mode=diff:max_colors={int(spec['colors'])}",
            str(palette),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(input_pattern),
            "-i",
            str(palette),
            "-lavfi",
            f"paletteuse=dither={spec['dither']}:diff_mode=rectangle",
            "-loop",
            "0",
            str(output),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run(args: argparse.Namespace) -> None:
    input_svg = args.input_svg.expanduser().resolve()
    output_gif = args.output_gif.expanduser().resolve()
    spec_path = args.spec.expanduser().resolve()
    if not input_svg.is_file():
        fail(f"input SVG not found: {input_svg}")

    spec = load_spec(spec_path)
    validate_spec(spec)
    ffmpeg = command_path("ffmpeg")
    renderer = choose_renderer()

    try:
        root = ET.parse(input_svg).getroot()
    except ET.ParseError as exc:
        fail(f"invalid SVG XML: {exc}")

    if args.keep_frames:
        workspace = args.keep_frames.expanduser().resolve()
        if workspace.exists() and any(workspace.iterdir()):
            fail(f"keep-frames directory must be empty: {workspace}")
        workspace.mkdir(parents=True, exist_ok=True)
        temporary = None
    else:
        temporary = tempfile.TemporaryDirectory(prefix="readme-motion-")
        workspace = Path(temporary.name)

    try:
        frames_dir, frame_count, width, height = build_frames(
            root, spec, renderer, workspace
        )
        output_gif.parent.mkdir(parents=True, exist_ok=True)
        encode_gif(frames_dir, output_gif, spec, ffmpeg)
    finally:
        if temporary:
            temporary.cleanup()

    size_mb = output_gif.stat().st_size / (1024 * 1024)
    print(f"GIF: {output_gif}")
    print(
        f"Output: {width}x{height}, {frame_count} frames, "
        f"{spec['fps']} FPS, {float(spec['duration']):.2f}s, {size_mb:.2f} MB"
    )
    if size_mb > float(spec["max_size_mb"]):
        print(
            f"WARNING: exceeds preferred {spec['max_size_mb']} MB budget",
            file=sys.stderr,
        )


if __name__ == "__main__":
    run(parse_args())
