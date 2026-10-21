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
from PIL import Image, ImageDraw, ImageOps

load_dotenv()

MODEL = "claude-sonnet-5"

# ---------------------------------------------------------------------------
# SETTINGS: how your chart photos are laid out.
# ---------------------------------------------------------------------------
ROWS = 9            # batters on the sheet
COLUMNS = 5         # big at-bat boxes printed per batter row
ROTATE = 0          # degrees clockwise to turn the photo (0 if it's already upright)
GRID_TOP = 0.145    # where the first batter row starts, as a fraction of the height
GRID_BOTTOM = 0.98  # where the last batter row ends
OVERLAP = 0.25      # extra image kept above and below each row
USE_THINKING = True # True = more careful but slower; False = faster and cheaper

# ---------------------------------------------------------------------------
# The prompt: describes YOUR chart notation. Update it if your charts change.
# ---------------------------------------------------------------------------
ROW_PROMPT = """
You are reading ONE horizontal strip cut from a photo of a handwritten
baseball at-bat chart. It holds a single batter's row. Accuracy matters more
than speed. Ignore what you see on the edges of the strip; only read the row whose
 name is most fully visible.

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


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def load_image(path, rotate=0):
    """Open a photo, fix phone-camera rotation, and turn it clockwise if asked."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return img.rotate(-rotate, expand=True) if rotate else img


def row_edges(height):
    """Pixel positions of the lines that separate the batter rows."""
    start = height * GRID_TOP
    band = (height * GRID_BOTTOM - start) / ROWS
    return [start + i * band for i in range(ROWS + 1)]


def split_into_rows(img):
    """Cut the grid into one strip per batter, with a little extra above and below."""
    edges = row_edges(img.height)
    pad = (edges[1] - edges[0]) * OVERLAP
    return [
        img.crop((0, max(0, int(top - pad)), img.width, min(img.height, int(bottom + pad))))
        for top, bottom in zip(edges, edges[1:])
    ]


def overlay_image(img):
    """The photo with the row cut lines drawn on it in red."""
    preview = img.copy()
    draw = ImageDraw.Draw(preview)
    for y in row_edges(preview.height):
        draw.line([(0, y), (preview.width, y)], fill=(255, 0, 0), width=max(3, preview.height // 400))
    return preview


def save_debug_images(img, strips, folder="strips_debug"):
    """Save the photo with the cut lines drawn on it, plus each strip."""
    Path(folder).mkdir(exist_ok=True)
    overlay_image(img).save(Path(folder) / "overlay.jpg", quality=90)
    for i, strip in enumerate(strips, start=1):
        strip.save(Path(folder) / f"row_{i}.jpg", quality=90)
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


def extract_rows(client, strips, progress=None):
    """Read each batter's strip (a few at a time) and combine the results.
    progress, if given, is called as progress(rows_done, rows_total)."""
    print(f"Sending {len(strips)} rows to Claude, 3 at a time...")
    prompt = ROW_PROMPT.replace("__COLS__", str(COLUMNS))

    def read_row(item):
        index, strip = item
        try:
            row = ask_claude(client, strip, prompt, "Extract this batter's row as JSON.")
        except Exception as error:  # one bad row shouldn't lose the whole chart
            row = {"name": f"ROW {index}", "at_bats": [], "extraction_error": str(error)}
        print(f"  Row {index}/{len(strips)} done", flush=True)
        return row

    rows = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(read_row, item): item[0]
                   for item in enumerate(strips, start=1)}
        for done, future in enumerate(as_completed(futures), start=1):
            rows[futures[future]] = future.result()
            if progress:
                progress(done, len(strips))
    return merge_rows([rows[i] for i in sorted(rows)])


def read_hitting_chart(path, client=None, progress=None):
    """Photo in, result out: cut the chart into rows and read each one."""
    strips = split_into_rows(load_image(path, ROTATE))
    return extract_rows(client or make_client(), strips, progress)


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
            if code == "K" and strikes is not None and strikes < 3:
                warnings.append(f"{where}: strikeout (K) but {strikes} strikes")
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
    parser = argparse.ArgumentParser(description="Read a handwritten at-bat chart photo.")
    parser.add_argument("image", help="path to the chart photo")
    parser.add_argument("--dry-run", action="store_true",
                        help="cut the photo and save a preview, with no API call and no cost")
    args = parser.parse_args()

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