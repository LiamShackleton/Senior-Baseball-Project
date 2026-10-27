"""Read a photo of a handwritten at-bat chart and save the data as JSON.

    python3 extract_chart.py sample_charts/photo.png            # reads it, saves extracted/photo.json
    python3 extract_chart.py sample_charts/photo.png --dry-run  # only cuts the photo, NO API call, no cost

The photo is cut into one strip per batter, and each strip is read separately.
After a dry run, open strips_debug/overlay.jpg: each red band should hold
exactly one batter. If not, change the settings below.

Needs a .env file containing:  ANTHROPIC_API_KEY=your_key_here
Nothing is written to the database. Run import_chart.py next.
"""

import argparse
import base64
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
from PIL import Image, ImageDraw, ImageOps

load_dotenv()

MODEL = "claude-sonnet-5-5"

# ---------------------------------------------------------------------------
# SETTINGS: how your chart photos are laid out.
# ---------------------------------------------------------------------------
ROWS = 9            # batters on the sheet
COLUMNS = 5         # big at-bat boxes printed per batter row
ROTATE = 0          # degrees clockwise to turn the photo (0 if it's already upright)
AUTO_ROWS = True    # find the row lines in each photo (and straighten tilt). False = use the two numbers below
GRID_TOP = 0.145    # where the first batter row starts, as a fraction of the height
GRID_BOTTOM = 0.98  # where the last batter row ends
OVERLAP = 0.25      # extra image kept above and below each row
USE_THINKING = True # True = more careful but slower; False = faster and cheaper

# Second look at just the ball/strike X grids, zoomed in, to improve the counts.
# It adds one more request per batter row. Set COUNT_PASS = False to turn it off.
COUNT_PASS = True
COUNT_GRID_HEIGHT = 0.5  # top part of each row band that holds the X grids (0.5 = top half)
COUNT_LEFT = 0.16        # where the first box starts, as a fraction of the photo's width
COUNT_UPSCALE = 2.5      # enlarge the grid crop this many times before sending

# ---------------------------------------------------------------------------
# The prompt: describes YOUR chart notation. Update it if your charts change.
# ---------------------------------------------------------------------------
ROW_PROMPT = """
You are reading ONE horizontal strip cut from a photo of a handwritten
baseball at-bat chart. It holds a single batter's row. Accuracy matters more
than speed.

LAYOUT
- The batter's name is at the LEFT edge. To its right are exactly __COLS__
  large at-bat boxes side by side. Box 1 is their first at-bat, box 2 the
  second, and so on. There are NEVER more than __COLS__ boxes. The small grid
  of squares at the top of each box is where the X's are drawn; those small
  squares are NOT separate boxes. Unused boxes at the right are normal.
- The strip was cut by position, so slivers of the neighboring rows can show
  at the top and bottom. Ignore them. Read only the row whose name is most
  fully visible.

INSIDE EACH BOX
- X marks: the BOTTOM row of X's is balls, the TOP row is strikes. Count each
  row separately, one X at a time. X's can crowd or overlap.
- A small diamond: shaded means the batter scored, unshaded means not.
- A text result: K (strikeout), BB (walk), HBP (hit by pitch), 1B/2B/3B/HR,
  or an out written as fielding positions (6-3, F7, P6, E6, FC).

PROCEDURE
1. Read the batter's name.
2. Report exactly __COLS__ entries, one per box, left to right, numbered 1 to
   __COLS__. An empty box gets "box_blank": true and null values.
3. A walk (BB) needs 4 balls and a strikeout (K) needs at least 3 strikes. If
   a box doesn't add up, report exactly what you see and set "uncertain": true.
   Do NOT change a count to make the numbers agree.

Return ONLY valid JSON (no markdown, no extra text) in this structure:

{
  "name": "<string>",
  "at_bats": [
    {
      "column": <1 to __COLS__>,
      "box_blank": <true if nothing is written in the box>,
      "balls": <number of X's in the bottom row, or null>,
      "strikes": <number of X's in the top row, or null>,
      "result": "<text code, or null>",
      "scored": <true or false, or null if blank>,
      "uncertain": <true if anything in this box was hard to read>
    }
  ],
  "notes": "<anything illegible or ambiguous in this row>"
}

If a field is unreadable, use null rather than guessing, and say so in "notes".
""".strip()


COUNT_PROMPT = """
You are counting marks on a zoomed crop of one batter's ball/strike tally grids
from a handwritten baseball at-bat chart. Accuracy matters more than speed.

LAYOUT
- The crop shows up to __COLS__ boxes side by side, left to right. Box 1 is the
  leftmost. Unused boxes at the right are often empty.
- Each box has a tally grid of small squares in TWO rows. X marks in the TOP
  row of squares are STRIKES. X marks in the BOTTOM row of squares are BALLS.
- An X can be drawn large and spill across neighboring squares. Count each
  separate X once. Do not count printed grid lines.
- Slivers of other rows may show at the top or bottom edge. Ignore them.

PROCEDURE
For each box, go left to right. Count the X's in the top row, then the X's in
the bottom row. Count only what you can see. Do not use any result or guess
what the count "should" be. A box with an empty grid has 0 and 0. If a count is
hard to make out, give your best count and set "uncertain": true.

Return ONLY valid JSON (no markdown, no extra text):

{
  "boxes": [
    {"column": <1 to __COLS__>, "strikes": <top-row X count>, "balls": <bottom-row X count>, "uncertain": <true or false>}
  ]
}
""".strip()


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def load_image(path, rotate=0):
    """Open a photo, fix phone-camera rotation, turn it clockwise if asked, and
    (when AUTO_ROWS is on) straighten a slightly tilted photo."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img = img.rotate(-rotate, expand=True) if rotate else img
    return straighten(img) if AUTO_ROWS else img


def _line_profile(gray):
    """For each pixel row, the fraction of the middle of the photo that is thick dark ink.
    Heavy printed row lines stand out; thin grid lines and handwriting mostly don't."""
    dark = gray < 0.55 * np.percentile(gray, 90)  # well below the paper's brightness
    thick = dark[:-1] & dark[1:]  # ink at least 2 pixels tall
    # Drop dark areas much taller than a printed line (a dark table around the
    # sheet, a big smudge): a line is dark for only a few pixel rows in a row.
    tall = 12
    cumulative = np.vstack([np.zeros((1, thick.shape[1]), int), np.cumsum(thick, axis=0)])
    solid = (cumulative[tall:] - cumulative[:-tall]) == tall  # runs of `tall` dark pixels
    near_solid = np.zeros_like(thick)
    for shift in range(tall):  # grow each solid run back to its full height
        near_solid[shift:shift + len(solid)] |= solid
    thick = thick & ~near_solid
    width = thick.shape[1]
    # the middle of the photo, so a table or margin at the sides doesn't dilute the lines
    return thick[:, int(width * 0.3):int(width * 0.7)].mean(axis=1)


def _small_gray(img, width=800):
    scale = width / img.width
    return img.convert("L").resize((width, max(1, round(img.height * scale)))), scale


def straighten(img):
    """Rotate a slightly tilted photo so the row lines are horizontal.
    Tries small angles and keeps the one that makes the lines sharpest."""
    small, _ = _small_gray(img)
    best_angle, best_score, base_score = 0.0, -1.0, None
    for angle in np.arange(-4, 4.01, 0.25):
        turned = small.rotate(float(angle), resample=Image.Resampling.BILINEAR, fillcolor=255)
        score = float(np.var(_line_profile(np.asarray(turned, dtype=float))))
        if abs(angle) < 1e-9:
            base_score = score
        if score > best_score:
            best_angle, best_score = float(angle), score
    # only turn the photo if it clearly helps
    if abs(best_angle) < 0.3 or best_score < 1.1 * (base_score or 0):
        best_angle = 0.0
    turned = img.rotate(best_angle, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255)) if best_angle else img
    turned.info["tilt"] = best_angle
    return turned


def find_row_lines(img):
    """Find the ROWS + 1 evenly spaced heavy lines that separate the batter rows.
    Returns (pixel positions, score from 0 to 1, lines clearly seen), or
    (None, score, 0) if unsure."""
    small, scale = _small_gray(img)
    profile = _line_profile(np.asarray(small, dtype=float))
    height = len(profile)
    near = np.array([profile[max(0, y - 2):y + 3].max() for y in range(height)])  # forgive 2px of wobble
    best = (None, 0.0, 0)
    for top in range(int(height * 0.04), int(height * 0.32)):
        for bottom in range(int(height * 0.78), int(height * 1.12)):
            pitch = (bottom - top) / ROWS
            if bottom > height + 0.75 * pitch:
                continue  # the last row must be mostly in the photo, or the whole grid has slipped down a row
            positions = np.round(top + pitch * np.arange(ROWS + 1)).astype(int)
            inside = positions < height
            # a line in the photo counts for how strong it is; the last line may
            # be cut off by the photo's edge, which counts for a little
            score = (near[positions[inside]].sum() + 0.1 * (~inside).sum()) / (ROWS + 1)
            positions = np.clip(positions, 0, height - 1)
            if score > best[1]:
                best = (positions, score, int((near[positions] > 0.25).sum()))
    if best[0] is None or best[1] < 0.35:
        return None, best[1], 0
    # A photo taken at a slight angle makes the rows a little uneven. Nudge each
    # line to the strongest nearby heavy line, if there is one.
    positions = best[0].astype(float)
    pitch = (positions[-1] - positions[0]) / ROWS
    strength = np.median([near[int(y)] for y in positions])
    for i, y in enumerate(positions):
        low, high = max(0, int(y - pitch * 0.2)), min(height, int(y + pitch * 0.2) + 1)
        if high > low:
            peak = low + int(np.argmax(profile[low:high]))
            if profile[peak] >= 0.5 * strength:
                positions[i] = peak
    positions = np.maximum.accumulate(positions)  # keep the lines in order
    return [float(y) / scale for y in positions], best[1], best[2]


def row_edges(img):
    """Pixel positions of the lines that separate the batter rows. Found from the
    photo itself (and remembered on it), or from GRID_TOP / GRID_BOTTOM."""
    if "row_edges" not in img.info:
        edges = None
        if AUTO_ROWS:
            edges, score, seen = find_row_lines(img)
            img.info["row_score"], img.info["row_seen"] = score, seen
        if edges is None:
            start = img.height * GRID_TOP
            band = (img.height * GRID_BOTTOM - start) / ROWS
            edges = [start + i * band for i in range(ROWS + 1)]
            img.info["row_method"] = "fixed"
        else:
            img.info["row_method"] = "auto"
        img.info["row_edges"] = edges
    return img.info["row_edges"]


def describe_rows(img):
    row_edges(img)
    if img.info["row_method"] == "auto":
        tilt = img.info.get("tilt", 0)
        text = (f"Row lines found automatically (confidence {img.info['row_score']:.2f})"
                + (f", photo straightened by {tilt:+.2f} degrees." if tilt else "."))
        seen = img.info.get("row_seen", ROWS + 1)
        if seen < ROWS:
            text += (f" WARNING: only {seen} of {ROWS + 1} row lines were clearly seen, "
                     f"so check strips_debug/overlay.jpg.")
        return text
    if AUTO_ROWS:
        return (f"Could not find the row lines (confidence {img.info.get('row_score', 0):.2f}), "
                f"so fixed positions were used. Check strips_debug/overlay.jpg.")
    return "Using the fixed row positions from GRID_TOP and GRID_BOTTOM."


def split_into_rows(img):
    """Cut the grid into one strip per batter, with a little extra above and below."""
    edges = row_edges(img)
    pad = (edges[1] - edges[0]) * OVERLAP
    return [
        img.crop((0, max(0, int(top - pad)), img.width, min(img.height, int(bottom + pad))))
        for top, bottom in zip(edges, edges[1:])
    ]


def split_grids(img):
    """For each batter row, crop just the strip holding the X grids (the top part
    of the row, to the right of the name) and enlarge it, so the marks are easy
    to count."""
    edges = row_edges(img)
    left = int(img.width * COUNT_LEFT)
    grids = []
    for top, bottom in zip(edges, edges[1:]):
        height = bottom - top
        crop = img.crop((left, max(0, int(top - height * 0.05)),
                         img.width, min(img.height, int(top + height * COUNT_GRID_HEIGHT))))
        grids.append(crop.resize((round(crop.width * COUNT_UPSCALE),
                                  round(crop.height * COUNT_UPSCALE)), Image.Resampling.LANCZOS))
    return grids


def overlay_image(img):
    """The photo with the row cut lines drawn on it in red."""
    edges = row_edges(img)
    preview = img.copy()
    draw = ImageDraw.Draw(preview)
    for y in edges:
        draw.line([(0, y), (preview.width, y)], fill=(255, 0, 0), width=max(3, preview.height // 400))
    return preview


def save_debug_images(img, strips, folder="strips_debug"):
    """Save the photo with the cut lines drawn on it, plus each strip."""
    Path(folder).mkdir(exist_ok=True)
    overlay_image(img).save(Path(folder) / "overlay.jpg", quality=90)
    for i, strip in enumerate(strips, start=1):
        strip.save(Path(folder) / f"row_{i}.jpg", quality=90)
    for i, grid in enumerate(split_grids(img), start=1):
        grid.save(Path(folder) / f"grid_{i}.jpg", quality=90)
    print(describe_rows(img))
    print(f"Saved overlay.jpg and {len(strips)} strips to '{folder}'. "
          f"Each red band in the overlay should hold exactly one batter.")


def to_jpeg_b64(img):
    """Shrink if huge and keep under the API's ~5 MB limit."""
    img = img.copy()
    img.thumbnail((3000, 3000))
    quality = 92
    while True:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= 4_500_000 or quality <= 60:
            return base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
        quality -= 8


# ---------------------------------------------------------------------------
# Talking to Claude
# ---------------------------------------------------------------------------

def make_client():
    import anthropic  # imported here so --dry-run works without it

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No ANTHROPIC_API_KEY found. Check your .env file.")
    return anthropic.Anthropic(api_key=api_key)


def ask_claude(client, img, system_prompt, user_text):
    """Send one image plus instructions; return the parsed JSON answer."""
    # Thinking tokens count toward max_tokens, so leave room for both.
    max_tokens, thinking = (16000, {"type": "adaptive"}) if USE_THINKING else (4096, {"type": "disabled"})
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        thinking=thinking,
        system=system_prompt,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": to_jpeg_b64(img)}},
            {"type": "text", "text": user_text},
        ]}],
    )
    # The reply can hold a reasoning block before the answer, so find the text.
    raw = "".join(b.text for b in response.content if b.type == "text").strip()
    if not raw:
        raise RuntimeError("The response had no text (it may have run out of tokens).")
    if raw.startswith("```"):  # tolerate accidental code fences
        raw = raw.strip("`").removeprefix("json")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("Response wasn't valid JSON. Start of it: " + raw[:300])


def extract_rows(client, strips, progress=None, grids=None):
    """Read each batter's strip (a few at a time) and combine the results.
    If grids are given, a second request per row re-counts the X marks.
    progress, if given, is called as progress(requests_done, requests_total)."""
    total = len(strips) * (2 if grids else 1)
    print(f"Sending {total} requests to Claude, 3 at a time...")
    prompt = ROW_PROMPT.replace("__COLS__", str(COLUMNS))
    count_prompt = COUNT_PROMPT.replace("__COLS__", str(COLUMNS))
    done = [0]

    def run_all(work, items):
        results = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(work, item): item[0] for item in items}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
                done[0] += 1
                if progress:
                    progress(done[0], total)
        return [results[i] for i in sorted(results)]

    def read_row(item):
        index, strip = item
        try:
            row = ask_claude(client, strip, prompt, "Extract this batter's row as JSON.")
        except Exception as error:  # one bad row shouldn't lose the whole chart
            row = {"name": f"ROW {index}", "at_bats": [], "extraction_error": str(error)}
        print(f"  Row {index} read", flush=True)
        return row

    def count_row(item):
        index, grid = item
        try:
            return ask_claude(client, grid, count_prompt, "Count the X marks as JSON.")
        except Exception as error:  # keep the first read's counts for this row
            print(f"  Row {index}: count pass failed ({error})", flush=True)
            return None

    rows = run_all(read_row, enumerate(strips, start=1))
    if grids:
        counts = run_all(count_row, enumerate(grids, start=1))
        for row, count in zip(rows, counts):
            if count:
                apply_counts(row, count)
    return merge_rows(rows)


def apply_counts(row, count_result):
    """Use the zoomed-in count for each used box. If it changes the count, the
    first read is kept in ab["first_pass_count"] so the change is visible."""
    by_column = {box.get("column"): box for box in count_result.get("boxes", [])}
    for number, ab in enumerate(row.get("at_bats", [])[:COLUMNS], start=1):
        box = by_column.get(number)
        if ab.get("box_blank") or not box:
            continue
        before = f"{ab.get('balls')}-{ab.get('strikes')}"
        for field in ("balls", "strikes"):
            if box.get(field) is not None:
                ab[field] = box[field]
        if f"{ab.get('balls')}-{ab.get('strikes')}" != before:
            ab["first_pass_count"] = before
        if box.get("uncertain"):
            ab["uncertain"] = True


def read_hitting_chart(path, client=None, progress=None):
    """Photo in, result out: cut the chart into rows and read each one."""
    img = load_image(path, ROTATE)
    print(describe_rows(img))
    grids = split_grids(img) if COUNT_PASS else None
    return extract_rows(client or make_client(), split_into_rows(img), progress, grids)


def fix_count(ab):
    """Make the count agree with the result when the result is a walk or strikeout.
    - BB is always exactly 4 balls.
    - K is at least 3 strikes. More than 3 is left alone, since fouls with two
      strikes add extra X's.
    The model's original count is kept in ab["original_count"] so the change is visible."""
    code = (str(ab.get("result") or "").upper().split() or [""])[-1]
    balls, strikes = ab.get("balls"), ab.get("strikes")
    if code == "BB" and balls != 4:
        ab["original_count"], ab["balls"] = f"{balls}-{strikes}", 4
    elif code == "K" and (strikes is None or strikes < 3):
        ab["original_count"], ab["strikes"] = f"{balls}-{strikes}", 3


def merge_rows(rows):
    """Combine the rows into one result. Anything past the printed number of
    boxes is dropped, and unused boxes at the right are trimmed."""
    batters, notes = [], []
    for index, row in enumerate(rows, start=1):
        if row.get("notes"):
            notes.append(f"Row {index}: {row['notes']}")
        at_bats = list(row.get("at_bats", []))[:COLUMNS]
        for number, ab in enumerate(at_bats, start=1):
            ab["column"] = number  # trust the position, not the model's numbering
            if not ab.get("box_blank"):
                fix_count(ab)
        while at_bats and at_bats[-1].get("box_blank"):
            at_bats.pop()
        batter = {"name": row.get("name"), "at_bats": at_bats}
        if row.get("extraction_error"):
            batter["extraction_error"] = row["extraction_error"]
        batters.append(batter)

    # Pad shorter rows with blanks so every batter has the same number of boxes.
    used = max((len(b["at_bats"]) for b in batters), default=0)
    for batter in batters:
        for column in range(len(batter["at_bats"]) + 1, used + 1):
            batter["at_bats"].append({"column": column, "box_blank": True, "balls": None,
                                      "strikes": None, "result": None, "scored": None,
                                      "uncertain": False})
    return {"layout": {"batter_rows": len(batters), "columns_with_writing": used},
            "batters": batters, "notes": " | ".join(notes)}


# ---------------------------------------------------------------------------
# Checking the answer (plain rules, no AI). import_chart.py uses this too.
# ---------------------------------------------------------------------------

def check_result(result):
    """Return a list of warnings for anything that doesn't add up."""
    warnings = []
    expected = result.get("layout", {}).get("columns_with_writing")

    for batter in result.get("batters", []):
        name = batter.get("name") or "?"
        at_bats = batter.get("at_bats", [])

        if batter.get("extraction_error"):
            warnings.append(f"{name}: this row FAILED to extract ({batter['extraction_error']})")
            continue
        if expected is not None and len(at_bats) != expected:
            warnings.append(f"{name}: {len(at_bats)} at-bat entries, but the sheet has {expected}")

        previous = None
        for ab in at_bats:
            if ab.get("box_blank"):
                previous = None
                continue
            where = f"{name}, column {ab.get('column', '?')}"
            balls, strikes = ab.get("balls"), ab.get("strikes")
            tokens = str(ab.get("result") or "").upper().split()
            code = tokens[-1] if tokens else ""

            if not code:
                warnings.append(f"{where}: no result was read")
            if code == "BB" and balls is not None and balls != 4:
                warnings.append(f"{where}: walk (BB) but {balls} balls")
            if code == "BB" and strikes is not None and strikes >= 3:
                warnings.append(f"{where}: walk (BB) but {strikes} strikes")
            if code != "BB" and balls is not None and balls >= 4:
                warnings.append(f"{where}: {balls} balls but the result is {code or '?'}, not BB")
            if code == "K" and strikes is not None and strikes < 3:
                warnings.append(f"{where}: strikeout (K) but {strikes} strikes")
            if ab.get("first_pass_count"):
                warnings.append(f"{where}: second count changed {ab['first_pass_count']} to "
                                f"{ab.get('balls')}-{ab.get('strikes')} (before any BB/K fix)")
            if ab.get("original_count"):
                warnings.append(f"{where}: count changed from {ab['original_count']} to "
                                f"{ab.get('balls')}-{ab.get('strikes')} to match the {code}")
            if ab.get("uncertain"):
                warnings.append(f"{where}: marked uncertain by the model")

            signature = (balls, strikes, ab.get("result"), ab.get("scored"))
            if previous and signature == previous[0]:
                warnings.append(f"{where}: identical to column {previous[1]} (possible duplicate read)")
            previous = (signature, ab.get("column", "?"))
    return warnings


def print_summary(result, max_warnings=12):
    """Short readout: one line per batter, then the first few warnings.
    Format of each at-bat:  RESULT balls-strikes
    (R = scored, ? = model was unsure, * = count was corrected to match a BB or K)"""
    for batter in result["batters"]:
        boxes = []
        for ab in batter["at_bats"]:
            if ab.get("box_blank"):
                boxes.append("--")
            else:
                mark = ("*" if ab.get("original_count") else "") + ("?" if ab.get("uncertain") else "")
                scored = " R" if ab.get("scored") else ""
                boxes.append(f"{ab.get('result') or '??'} {ab.get('balls')}-{ab.get('strikes')}{mark}{scored}")
        print(f"{(batter.get('name') or '?'):<10} " + " | ".join(boxes))

    warnings = check_result(result)
    if not warnings:
        print("\nNo consistency problems found.")
        return
    print(f"\n{len(warnings)} things to check:")
    for warning in warnings[:max_warnings]:
        print(" -", warning)
    if len(warnings) > max_warnings:
        print(f" ... and {len(warnings) - max_warnings} more (import_chart.py shows them all).")


# ---------------------------------------------------------------------------

def main():
    global GRID_TOP, GRID_BOTTOM, AUTO_ROWS
    parser = argparse.ArgumentParser(description="Read a handwritten at-bat chart photo.")
    parser.add_argument("image", help="path to the chart photo")
    parser.add_argument("--dry-run", action="store_true",
                        help="cut the photo and save a preview, with no API call and no cost")
    parser.add_argument("--top", type=float,
                        help=f"where the first batter row starts, 0 to 1 (default {GRID_TOP})")
    parser.add_argument("--bottom", type=float,
                        help=f"where the last batter row ends, 0 to 1 (default {GRID_BOTTOM})")
    args = parser.parse_args()
    GRID_TOP = args.top if args.top is not None else GRID_TOP
    GRID_BOTTOM = args.bottom if args.bottom is not None else GRID_BOTTOM
    if args.top is not None or args.bottom is not None:
        AUTO_ROWS = False  # you picked the positions yourself

    path = Path(args.image)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    if args.dry_run:
        img = load_image(path, ROTATE)
        save_debug_images(img, split_into_rows(img))
        return

    try:
        result = read_hitting_chart(path)
    except RuntimeError as error:
        sys.exit(str(error))

    out = Path("extracted") / f"{path.stem}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"source_image": str(path), **result}, indent=2))

    print_summary(result)
    print(f"\nFull details (including the model's notes) are in {out}.")
    print(f"Next: python3 import_chart.py {out}")


if __name__ == "__main__":
    main()