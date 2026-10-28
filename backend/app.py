"""Property Studio: deterministic, photo-aware property video generator.

Photo labels come from the user (or filename hints) rather than a generative
vision model. Captions are only associated with a room when the feature text
explicitly describes that room.
"""
from __future__ import annotations
from functools import lru_cache
from contextlib import asynccontextmanager, suppress
import asyncio
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import re
import secrets
import shutil
import time
import subprocess
import uuid
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "outputs"
OUTPUT.mkdir(exist_ok=True)
# Only generated files with this exact name format can be removed.
GENERATED_NAME = re.compile(r"[a-f0-9]{12}\.mp4")
RETENTION_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 5 * 60


def cleanup_outputs(keep: Path | None = None, now: float | None = None) -> int:
    """Keep the most recent successful export for at most 24 hours.

    Never touch sample_output, non-generated names, subfolders, or symlinks.
    Failures to delete an open file on Windows are retried next cleanup.
    """
    current = time.time() if now is None else now
    candidates = []
    for file in OUTPUT.iterdir():
        if file.is_symlink() or not file.is_file() or not GENERATED_NAME.fullmatch(file.name):
            continue
        try:
            candidates.append((file, file.stat().st_mtime))
        except OSError:
            continue
    valid = [(file, modified) for file, modified in candidates
             if modified > current - RETENTION_SECONDS]
    if keep is not None and any(file == keep for file, _ in valid):
        newest = keep
    else:
        newest = max(valid, key=lambda entry: (entry[1], entry[0].name))[0] if valid else None
    deleted = 0
    for file, _ in candidates:
        if file != newest:
            try:
                file.unlink()
                deleted += 1
            except OSError:
                # A video being downloaded may be locked on Windows.
                pass
    return deleted


async def periodic_cleanup():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cleanup_outputs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_outputs()  # Also clean up videos left from previous runs.
    task = asyncio.create_task(periodic_cleanup())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Property Studio — Room-Aware Video Generator", lifespan=lifespan)
# In Render, set CORS_ORIGINS to the exact Vercel production URL.
# When frontend is served by this backend locally, no cross-origin permission is needed.
allowed_origins = [origin.strip().rstrip("/") for origin in
                   os.environ.get("CORS_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=allowed_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"],
                   allow_credentials=False)

@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(shutil.which("ffmpeg"))}
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
MAX_SIZE = 10 * 1024 * 1024
W, H = 960, 540  # default landscape export
VIDEO_FORMATS = {"landscape": (960, 540), "portrait": (540, 960), "square": (720, 720)}
MUSIC = {
    "warm_keys": ("Warm Keys", "Soft piano-inspired keys"),
    "soft_ambient": ("Soft Ambient", "Airy, calm tones"),
    "bright_morning": ("Bright Morning", "Upbeat and bright"),
    "golden_hour": ("Golden Hour", "Relaxed, warm melody"),
    "modern_lounge": ("Modern Lounge", "Gentle rhythmic groove"),
}
# Every preset uses still photos. Scene changes are fades, not camera motion.
# Legacy keys are accepted for compatibility with any browser-cached form.
TRANSITIONS = {
    "smooth": ("fade",),        # standard 0.75s crossfade
    "gentle": ("fade",),        # longer 1.00s dissolve
    "quick": ("fade",),         # shorter 0.35s fade
    "clean": (),               # hard cut without animation
    "cinematic": ("fade",),    # old preset -> smooth fade
    "dissolve": ("fade",),     # old preset -> gentle fade
    "fadeblack": ("fade",),    # old preset -> smooth fade
    "slide": ("fade",),        # old preset -> smooth fade
    "zoom": ("fade",),         # old preset -> smooth fade
}
FADE_DURATIONS = {
    "smooth": 0.75, "gentle": 1.00, "quick": 0.35,
    "cinematic": 0.75, "dissolve": 1.00, "fadeblack": 0.75,
    "slide": 0.75, "zoom": 0.75,
}
FPS = 24
SCENE_SECONDS = 3.75
OVERLAP_SECONDS = 0.75
ROOMS = (
    "Exterior", "Living Room", "Bedroom", "Kitchen", "Dining Room",
    "Balcony", "Bathroom", "Parking", "Garden", "Other",
)
ROOM_PATTERNS = {
    "Exterior": r"\b(exterior|building|facade|front elevation)\b",
    "Living Room": r"\b(living|lounge|sitting room)\b",
    "Bedroom": r"\b(bedroom|bedrooms|bed|master suite)\b",
    "Kitchen": r"\b(kitchen|pantry|cooking)\b",
    "Dining Room": r"\b(dining)\b",
    "Balcony": r"\b(balcony|terrace|patio)\b",
    "Bathroom": r"\b(bathroom|washroom|toilet|bath)\b",
    "Parking": r"\b(parking|garage|car park)\b",
    "Garden": r"\b(garden|lawn|yard)\b",
}
WINDOWS_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
FONT_CANDIDATES = {
    False: [WINDOWS_FONTS / "arial.ttf", WINDOWS_FONTS / "segoeui.ttf",
            WINDOWS_FONTS / "calibri.ttf",
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf")],
    True: [WINDOWS_FONTS / "arialbd.ttf", WINDOWS_FONTS / "segoeuib.ttf",
           WINDOWS_FONTS / "calibrib.ttf",
           Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
           Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")],
}


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False):
    for path in FONT_CANDIDATES[bool(bold)]:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def wrap(draw, content: str, face, max_width: int):
    lines, line = [], ""
    for word in content.split():
        attempt = f"{line} {word}".strip()
        if draw.textbbox((0, 0), attempt, font=face)[2] <= max_width:
            line = attempt
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def scene_layers(image_path: Path, heading: str, subtitle: str, badge: str,
                 background_path: Path, overlay_path: Path,
                 width: int = W, height: int = H):
    """Compose a stationary image/typography layer for any supported aspect ratio."""
    with Image.open(image_path) as src:
        photo = ImageOps.fit(src.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    photo.save(background_path, quality=91)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    drawer = ImageDraw.Draw(layer)
    # Stronger bottom contrast keeps text legible across dark and light property photos.
    for y in range(height):
        amount = y / height
        opacity = int(12 + 189 * amount ** 1.9)
        drawer.line((0, y, width, y), fill=(5, 13, 29, opacity))
    margin = max(26, round(width * 0.053))
    pad = max(14, round(width * 0.017))
    badge_font = font(max(12, min(17, round(width * 0.020))), True)
    badge_text = badge.upper()[:30]
    badge_width = min(width - 2 * margin, max(156, int(drawer.textlength(badge_text, font=badge_font) + 2 * pad)))
    badge_h = max(38, round(width * 0.046))
    top = max(26, round(height * 0.068))
    drawer.rounded_rectangle((margin, top, margin + badge_width, top + badge_h),
                             radius=badge_h // 2, fill=(12, 26, 44, 209))
    drawer.text((margin + pad, top + (badge_h - badge_font.size) // 2 - 3),
                badge_text, font=badge_font, fill="#BAE6FD")

    is_portrait = height > width * 1.25
    title_size = 37 if is_portrait else max(32, min(44, round(width * 0.046)))
    subtitle_size = 21 if is_portrait else max(18, min(24, round(width * 0.025)))
    face_title, face_sub = font(title_size, True), font(subtitle_size)
    title_lines = wrap(drawer, heading, face_title, width - 2 * margin)[:3 if is_portrait else 2]
    sub_lines = wrap(drawer, subtitle, face_sub, width - 2 * margin)[:3 if is_portrait else 2]
    title_step = int(title_size * 1.3)
    sub_step = int(subtitle_size * 1.50)
    total_h = len(title_lines) * title_step + len(sub_lines) * sub_step + 24
    baseline = height - max(47, round(height * 0.085))
    y = max(top + badge_h + 30, baseline - total_h)
    for line in title_lines:
        drawer.text((margin, y), line, font=face_title, fill="white",
                    stroke_width=1, stroke_fill="#091526")
        y += title_step
    for line in sub_lines:
        drawer.text((margin + 2, y + 7), line, font=face_sub, fill="#E5E7EB")
        y += sub_step
    bar_y = height - max(31, round(height * 0.065))
    drawer.rounded_rectangle((margin, bar_y, width - margin, bar_y + 4),
                             radius=2, fill="#9FDBCD")
    layer.save(overlay_path)


def ffmpeg_run(command: list[str]):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:] or "FFmpeg video rendering failed")


def render_photo_clip(bg: Path, overlay: Path, output: Path, index: int):
    """Encode a completely still image per scene (no zoompan, no shake).

    The photograph and text are composed once with Pillow; FFmpeg repeats the
    exact same frame for the scene duration. Nothing moves inside a scene.
    """
    still_frame = output.with_suffix(".png")
    with Image.open(bg) as photo, Image.open(overlay) as graphics:
        flattened = Image.alpha_composite(photo.convert("RGBA"),
                                          graphics.convert("RGBA"))
        flattened.convert("RGB").save(still_frame)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-loop", "1", "-framerate", str(FPS), "-i", str(still_frame),
           "-frames:v", str(round(SCENE_SECONDS * FPS)),
           "-an", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS), str(output)]
    ffmpeg_run(cmd)


def merge_clips_with_music(clips: list[Path], track: str | None,
                           volume: float, transition: str, out: Path):
    """Combine locked-off scene cards with subtle fades or clean cuts + audio."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for clip in clips:
        command += ["-i", str(clip)]
    if track:
        command += ["-stream_loop", "-1", "-i", str(ROOT / "music" / f"{track}.mp3")]
    filters = [f"[{i}:v]fps={FPS},format=yuv420p[v{i}]"
               for i in range(len(clips))]
    styles = TRANSITIONS[transition]
    overlap = FADE_DURATIONS.get(transition, 0.0)
    if not styles:
        # Concatenation means zero camera or transition motion anywhere.
        final_label = "joined"
        filters.append("".join(f"[v{i}]" for i in range(len(clips))) +
                       f"concat=n={len(clips)}:v=1:a=0[{final_label}]")
        total = len(clips) * SCENE_SECONDS
    else:
        previous = "v0"
        for i in range(1, len(clips)):
            new = f"joined{i}"
            offset = (SCENE_SECONDS - overlap) * i
            style = styles[(i - 1) % len(styles)]
            filters.append(f"[{previous}][v{i}]xfade=transition={style}:"
                           f"duration={overlap:.3f}:offset={offset:.3f}[{new}]")
            previous = new
        final_label = previous
        total = len(clips) * SCENE_SECONDS - (len(clips)-1) * overlap
    if track:
        filters.append(
            f"[{len(clips)}:a]atrim=duration={total:.3f},asetpts=PTS-STARTPTS,"
            f"volume={volume:.3f},afade=t=in:st=0:d=0.7,"
            f"afade=t=out:st={max(0,total-1):.3f}:d=1[aout]"
        )
    command += ["-filter_complex", ";".join(filters), "-map", f"[{final_label}]"]
    if track:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    else:
        command += ["-an"]
    command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    ffmpeg_run(command)


def guess_room(filename: str) -> str:
    """Filename hints are suggestions only, not an image recognition claim."""
    normalized = re.sub(r"[_\-.\d]+", " ", Path(filename).stem.lower())
    for room, pattern in ROOM_PATTERNS.items():
        if re.search(pattern, normalized):
            return room
    return "Other"


def room_feature(room: str, features: list[str]) -> str | None:
    """Match provided features to explicitly named rooms, never by position."""
    pattern = ROOM_PATTERNS.get(room)
    if not pattern:
        return None
    for feature in features:
        if re.search(pattern, feature, flags=re.IGNORECASE):
            return feature
    return None


def make_voiceover_script(name, location, price, property_type, features, cta):
    parts = [f"Introducing {name}."]
    if location:
        parts.append(f"Located in {location}.")
    if property_type:
        parts.append(f"Property type: {property_type}.")
    if features:
        parts.append("Highlights: " + ", ".join(features) + ".")
    if price:
        parts.append(f"Price: {price}.")
    if cta:
        parts.append(cta)
    return " ".join(parts)


def build_plan(imgs, name, location, price, property_type, features, cta,
               room_labels, captions):
    """Every photo gets a scene with its own room-specific caption."""
    overview = " • ".join(x for x in (location, property_type) if x)
    plan = [{"photo": 0, "title": name, "subtitle": overview,
             "badge": "PROPERTY SPOTLIGHT"}]
    for i, (room, caption) in enumerate(zip(room_labels, captions)):
        # Explicit caption wins. Otherwise match only relevant supplied features.
        title = caption or room_feature(room, features) or (
            room if room != "Other" else f"Property Photo {i + 1}")
        plan.append({"photo": i, "title": title, "subtitle": name,
                     "badge": room if room != "Other" else "PROPERTY PHOTO"})
    if price:
        plan.append({"photo": len(imgs) - 1, "title": f"Price: {price}",
                     "subtitle": overview, "badge": "PROPERTY DETAILS"})
    plan.append({"photo": len(imgs) - 1, "title": "Interested in this property?",
                 "subtitle": cta, "badge": "GET IN TOUCH"})
    return plan


def render_video(imgs, name, location, price, property_type, features, cta,
                 room_labels, captions, out, music="random", transition="smooth",
                 music_volume=0.30, video_format="landscape"):
    if video_format not in VIDEO_FORMATS:
        raise ValueError("Choose a valid video format.")
    width, height = VIDEO_FORMATS[video_format]
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed or not on PATH.")
    if music not in (*MUSIC.keys(), "random", "none"):
        raise ValueError("Select a valid background music option.")
    if transition not in TRANSITIONS:
        raise ValueError("Select a valid transition style.")
    if not 0 <= music_volume <= 1:
        raise ValueError("Music volume must be between 0 and 1.")
    chosen_track = secrets.choice(list(MUSIC.keys())) if music == "random" else (None if music == "none" else music)
    plan = build_plan(imgs, name, location, price, property_type,
                      features, cta, room_labels, captions)
    with TemporaryDirectory(prefix="propertyvideo_") as temp:
        folder = Path(temp)
        clips = []
        for i, info in enumerate(plan):
            bg = folder / f"photo_{i:02d}.jpg"
            overlay = folder / f"overlay_{i:02d}.png"
            clip = folder / f"clip_{i:02d}.mp4"
            scene_layers(imgs[info["photo"]], info["title"], info["subtitle"],
                         info["badge"], bg, overlay, width, height)
            render_photo_clip(bg, overlay, clip, i)
            clips.append(clip)
        merge_clips_with_music(clips, chosen_track, music_volume, transition, out)
    script = make_voiceover_script(name, location, price, property_type, features, cta)
    return script, plan, chosen_track


def parse_list(raw: str, count: int, field: str):
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(400, f"{field} must be a JSON array.")
    if not isinstance(value, list) or len(value) != count:
        raise HTTPException(400, f"{field} must have exactly one item per uploaded photo.")
    if not all(isinstance(item, str) for item in value):
        raise HTTPException(400, f"{field} must contain only strings.")
    return value


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.post("/generate")
async def generate(
    name: str = Form(...), location: str = Form(""), price: str = Form(""),
    property_type: str = Form(""), features: str = Form(""),
    cta: str = Form("Contact us to schedule a viewing."),
    scene_labels: str = Form(""), scene_captions: str = Form(""),
    music: str = Form("random"), transition: str = Form("smooth"),
    music_volume: float = Form(0.30), video_format: str = Form("landscape"),
    images: List[UploadFile] = File(...),
):
    if not name.strip():
        raise HTTPException(400, "Enter a property name.")
    if video_format not in VIDEO_FORMATS:
        raise HTTPException(400, "Choose landscape, portrait or square format.")
    if not 1 <= len(images) <= 8:
        raise HTTPException(400, "Upload 1–8 images (4–8 recommended).")
    if music not in (*MUSIC.keys(), "random", "none"):
        raise HTTPException(400, "Choose a valid soundtrack.")
    if transition not in TRANSITIONS:
        raise HTTPException(400, "Choose a valid transition style.")
    if not 0 <= music_volume <= 1:
        raise HTTPException(400, "Music volume must be between 0 and 1.")
    features_list = [x.strip()[:140] for x in re.split(r"[\n,]+", features) if x.strip()][:12]
    rooms = parse_list(scene_labels, len(images), "scene_labels") if scene_labels else [
        guess_room(image.filename or "") for image in images]
    if any(room not in ROOMS for room in rooms):
        raise HTTPException(400, "Invalid room label. Select a valid room for each image.")
    captions = parse_list(scene_captions, len(images), "scene_captions") if scene_captions else [""] * len(images)
    if any(len(caption) > 140 for caption in captions):
        raise HTTPException(400, "Photo captions must be 140 characters or fewer.")
    captions = [text.strip() for text in captions]
    with TemporaryDirectory(prefix="property_upload_") as temp:
        paths = []
        for i, upload in enumerate(images):
            data = await upload.read(MAX_SIZE + 1)
            if len(data) > MAX_SIZE:
                raise HTTPException(400, f"Photo {i + 1} exceeds 10 MB.")
            path = Path(temp) / f"photo_{i}.jpg"
            try:
                with Image.open(BytesIO(data)) as image:
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    ImageOps.exif_transpose(image).convert("RGB").save(path, quality=90)
            except (OSError, ValueError, UnidentifiedImageError):
                raise HTTPException(400, f"Photo {i + 1} is not a valid image.")
            paths.append(path)
        key = uuid.uuid4().hex[:12]
        output = OUTPUT / f"{key}.mp4"
        # Render inside the temporary folder first: an unfinished MP4 never
        # gets mistaken for the latest downloadable video.
        staged_output = Path(temp) / "finished.mp4"
        try:
            script, plan, chosen_track = render_video(
                paths, name.strip(), location.strip(), price.strip(),
                property_type.strip(), features_list, cta.strip(),
                rooms, captions, staged_output, music, transition, music_volume, video_format)
            os.replace(staged_output, output)
        except Exception as exc:
            output.unlink(missing_ok=True)
            raise HTTPException(500, f"Could not render video: {exc}")
    # Delete previous exports only AFTER the new render succeeds.
    cleanup_outputs(keep=output)
    return {"video_url": f"/videos/{key}.mp4", "script": script,
            "music": {"id": chosen_track or "none",
                      "name": MUSIC[chosen_track][0] if chosen_track else "No Music",
                      "mode": music}, "transition": transition,
            "scenes": len(plan), "video_format": video_format,
            "dimensions": {"width": VIDEO_FORMATS[video_format][0], "height": VIDEO_FORMATS[video_format][1]},
            "scene_plan": [{k: v for k, v in p.items() if k != "photo"}
                                               for p in plan]}


@app.get("/videos/{filename}")
def video(filename: str):
    if not re.fullmatch(r"[a-f0-9]{12}\.mp4", filename):
        raise HTTPException(404, "Not found")
    cleanup_outputs()  # Reject expired or replaced video links.
    path = OUTPUT / filename
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="video/mp4", filename="property-video.mp4")


@app.get("/music-tracks")
def music_tracks():
    return [{"id": track, "name": value[0], "description": value[1],
             "preview_url": f"/music/{track}.mp3"} for track, value in MUSIC.items()]


@app.get("/music/{track}.mp3")
def preview_music(track: str):
    if track not in MUSIC:
        raise HTTPException(404, "Unknown soundtrack")
    return FileResponse(ROOT / "music" / f"{track}.mp3", media_type="audio/mpeg")


# Public synthetic photos and previously rendered example, used only on explicit user request.
app.mount("/demo-images", StaticFiles(directory=str(ROOT / "sample_images")), name="demo-images")
app.mount("/demo-video", StaticFiles(directory=str(ROOT / "sample_output")), name="demo-video")
