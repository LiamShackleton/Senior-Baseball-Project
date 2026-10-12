"""Extract structured at-bat data from a photo of a hitting/at-bat chart.

Usage:
    python3 extract_chart.py photo.jpg            # reads it, saves extracted/photo.json
    python3 extract_chart.py photo.jpg --dry-run  # cuts the photo and saves a preview, NO API call, no cost

The layout settings (how many batters, how the photo is rotated, where the
grid starts and ends) live in the SETTINGS block just below, so you set them
once instead of typing them each time. Any of them can still be overridden
for one run, for example:
    python3 extract_chart.py photo.jpg --rotate 0 --grid-top 0.15
    python3 extract_chart.py photo.jpg --rows 0   # send the whole sheet in one request instead

Requires a .env file in this folder containing:
    ANTHROPIC_API_KEY=your_key_here

This script only reads the image and prints what it found. It does NOT
write anything to the database. Run import_chart.py next to review the
data and save it.
"""

import argparse
import base64
import io
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageOps

load_dotenv()

MODEL = "claude-sonnet-5"

# True  = let Claude reason before answering (more careful on dense
#         handwriting, costs a bit more per request).
# False = answer immediately (cheaper and faster, less careful).
USE_THINKING = True

# ---------------------------------------------------------------------------
# SETTINGS: how your chart photos are laid out. Change these once and every
# run uses them. Run with --dry-run and open strips_debug/overlay.jpg to check
# them: each red band should hold exactly one batter.
# ---------------------------------------------------------------------------
DEFAULT_ROWS = 9            # batters on the sheet (0 = send the whole sheet at once)
DEFAULT_ROTATE = 90         # degrees clockwise to turn the photo so names run down the left
                            # (0 if your photos already come out upright)
DEFAULT_GRID_TOP = 0.2      # where the first batter row starts, as a fraction of the height
DEFAULT_GRID_BOTTOM = 0.81  # where the last batter row ends
DEFAULT_OVERLAP = 0.25      # extra image kept above and below each row
EXTRACT_FOLDER = "extracted"  # where the result .json files are saved

MAX_WORKERS = 3            # how many row-requests run at the same time
MAX_UPLOAD_EDGE = 3000      # shrink anything bigger than this before sending
MAX_UPLOAD_BYTES = 4_500_000  # the API rejects images over about 5 MB

STRIP_FOLDER = "strips_debug"

# ---------------------------------------------------------------------------
# Prompts. These describe YOUR chart notation. If your charts change, this is
# the place to update.
# ---------------------------------------------------------------------------

BOX_NOTATION = """
- Inside each at-bat box:
  - A small grid of X marks. The BOTTOM row of X's represents balls, the TOP
    row represents strikes. Each row reads LEFT TO RIGHT. The two rows are
    tracked separately, not interleaved, so you cannot tell the overall pitch
    order between a ball and a strike, only the order within each row.
  - A small diamond icon. If it is shaded/filled in, the batter scored as a
    baserunner. If unshaded, they did not score.
  - A text result code for the at-bat outcome. Standard codes include:
    K (strikeout), BB (walk), HBP (hit by pitch), 1B/2B/3B/HR (hits), and
    outs written as fielding position numbers (e.g. "6-3" = fielded by the
    shortstop, thrown to first base; "F7" = flyout to left field; "P6" =
    popout to shortstop; "E6" = error by the shortstop; "FC" = fielder's
    choice).
"""

AT_BAT_JSON = """{
          "column": <1-based column number>,
          "box_blank": <true if nothing is written in this box, else false>,
          "balls": <count of X's in the bottom row, or null>,
          "strikes": <count of X's in the top row, or null>,
          "result": "<the text code, e.g. 'K', 'BB', '6-3', or null>",
          "scored": <true or false, or null if blank>,
          "uncertain": <true if any field in this box was hard to read>
        }"""

# Used when the WHOLE sheet is sent in one image.
SYSTEM_PROMPT = (
    """
You are reading a photo of a handwritten baseball at-bat chart. Work
carefully: accuracy matters more than speed.

ORIENTATION
- The photo may be rotated (often 90 degrees) or tilted. Mentally rotate it
  so the batter names run down the LEFT edge and the at-bat boxes run to the
  right. Read everything in that orientation.

LAYOUT
- Each ROW is one batter. The batter's lineup number, jersey number and name
  are at the left edge of the row.
- Each BOX in a row is one at-bat for that batter. Boxes line up in COLUMNS:
  column 1 is every batter's first at-bat, column 2 is every batter's second
  at-bat, and so on. Read columns left to right in chronological order.
- The sheet is pre-printed with more boxes than were used, so blank boxes,
  especially toward the right, are normal. A column "has writing" if at least
  one batter has something written in that column.
- A batter higher in the order can have one more at-bat than the batter
  behind them, so later batters may have a blank box in the last used column.
"""
    + BOX_NOTATION
    + """
PROCEDURE (follow these steps in order)
1. Count the batter rows.
2. Look down each column and decide which columns have any writing. Count
   them.
3. For every batter, report exactly one entry per column with writing, in
   column order. If that batter's box in a column is blank, still include
   the entry with "box_blank": true and null values. Never skip a box:
   skipping shifts every later at-bat into the wrong column.
4. Count the X's in each box one at a time. X's can crowd or overlap.
5. Consistency check. A walk (BB) needs 4 balls. A strikeout (K) needs at
   least 3 strikes. If a box doesn't add up, look at it again. If it still
   doesn't add up, report exactly what you see and set "uncertain": true.
   Do NOT invent or remove X's just to make the numbers agree, because the
   scorekeeper may simply have marked it wrong.

Return ONLY valid JSON (no markdown formatting, no explanation text before
or after) matching this exact structure:

{
  "layout": {
    "batter_rows": <number>,
    "columns_with_writing": <number>
  },
  "batters": [
    {
      "batting_order": <number or null if unclear>,
      "jersey_number": "<string or null>",
      "name": "<string>",
      "at_bats": [
        """
    + AT_BAT_JSON
    + """
      ]
    }
  ],
  "notes": "<anything illegible, ambiguous, or that doesn't fit the above>"
}

Every batter's "at_bats" list must contain exactly
layout.columns_with_writing entries. If a field is genuinely unreadable, use
null rather than guessing, and flag it in "notes".
"""
).strip()

# Used when the sheet is cut into one strip per batter (--rows).
ROW_PROMPT = (
    """
You are reading ONE horizontal strip cut from a photo of a handwritten
baseball at-bat chart. The strip contains a single batter's row: row __ROW__
of __ROWS__ in the batting order. Work carefully: accuracy matters more than
speed.

ORIENTATION AND CROPPING
- The batter's lineup number, jersey number and name are at the LEFT edge.
  Their at-bat boxes run to the right in columns: column 1 is their first
  at-bat, column 2 is their second, and so on.
- The strip was cut by position, so thin slivers of the row above or below
  can show at the top and bottom edges, and the cut may be slightly tilted.
  Ignore anything that belongs to a neighboring batter. Read only the row
  whose name is most fully visible.
- The sheet is pre-printed with more boxes than were used, so blank boxes at
  the right are normal.
"""
    + BOX_NOTATION
    + """
PROCEDURE (follow these steps in order)
1. Read the batter's name, lineup number and jersey number. If no lineup
   number is clearly written, use __ROW__.
2. Find the last box in the row that has writing. Report every box from
   column 1 through that box, left to right. A blank box in between gets
   "box_blank": true with null values. Do not report blank boxes after the
   last written one. Never skip a box and never report the same box twice.
3. Count the X's in each box one at a time. X's can crowd or overlap.
4. Consistency check. A walk (BB) needs 4 balls. A strikeout (K) needs at
   least 3 strikes. If a box doesn't add up, look at it again. If it still
   doesn't add up, report exactly what you see and set "uncertain": true.
   Do NOT invent or remove X's just to make the numbers agree, because the
   scorekeeper may simply have marked it wrong.

Return ONLY valid JSON (no markdown formatting, no explanation text before
or after) matching this exact structure:

{
  "batting_order": <number or null>,
  "jersey_number": "<string or null>",
  "name": "<string>",
  "at_bats": [
        """
    + AT_BAT_JSON
    + """
  ],
  "notes": "<anything illegible or ambiguous in this row>"
}

If a field is genuinely unreadable, use null rather than guessing, and flag
it in "notes".
"""
).strip()


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

def load_image(path, rotate=0):
    """Open a photo, fix phone-camera rotation, and optionally rotate it
    clockwise by 90/180/270 degrees."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # phones store rotation as hidden metadata
    img = img.convert("RGB")
    if rotate:
        img = img.rotate(-rotate, expand=True)  # PIL turns counterclockwise, so negate
    return img


def row_edges(height, rows, grid_top, grid_bottom):
    """Pixel positions of the lines that separate the batter rows.
    grid_top / grid_bottom say where the grid of batter rows starts and ends,
    as fractions of the photo's height (0 = top edge, 1 = bottom edge). Photos
    usually have a header above the grid and a summary section below it, so
    cutting the whole image into equal bands would slice through the rows."""
    start = height * grid_top
    band = (height * grid_bottom - start) / rows
    return [start + i * band for i in range(rows + 1)]


def split_into_rows(img, rows, overlap, grid_top=0.0, grid_bottom=1.0):
    """Cut the grid into `rows` equal horizontal bands. Each band gets
    `overlap` (a fraction of a band's height) of extra image above and below,
    so a slightly tilted photo doesn't slice a row in half."""
    width, height = img.size
    edges = row_edges(height, rows, grid_top, grid_bottom)
    pad = (edges[1] - edges[0]) * overlap
    strips = []
    for i in range(rows):
        top = max(0, int(round(edges[i] - pad)))
        bottom = min(height, int(round(edges[i + 1] + pad)))
        strips.append(img.crop((0, top, width, bottom)))
    return strips


def save_overlay(img, rows, grid_top, grid_bottom, folder=STRIP_FOLDER):
    """Save the photo with the row cut lines drawn on it. Check that each
    band holds exactly one batter, then adjust --grid-top / --grid-bottom."""
    Path(folder).mkdir(exist_ok=True)
    preview = img.copy()
    draw = ImageDraw.Draw(preview)
    line = max(3, preview.height // 400)
    for y in row_edges(preview.height, rows, grid_top, grid_bottom):
        draw.line([(0, y), (preview.width, y)], fill=(255, 0, 0), width=line)
    preview.save(Path(folder) / "overlay.jpg", quality=90)
    print(f"Saved overlay.jpg to the '{folder}' folder. Each red band should "
          f"hold exactly one batter. If not, adjust --grid-top and --grid-bottom.")


def save_strips(strips, folder=STRIP_FOLDER):
    """Write the strips to disk so you can check the cuts by eye."""
    Path(folder).mkdir(exist_ok=True)
    for i, strip in enumerate(strips, start=1):
        strip.save(Path(folder) / f"row_{i}.jpg", quality=90)
    print(f"Saved {len(strips)} strips to the '{folder}' folder. "
          f"Open them to check each one shows a single batter row.")


def to_jpeg_b64(img):
    """Prepare an image for the API: shrink if huge, keep under the size limit."""
    img = img.copy()
    long_edge = max(img.size)
    if long_edge > MAX_UPLOAD_EDGE:
        scale = MAX_UPLOAD_EDGE / long_edge
        img = img.resize(
            (round(img.width * scale), round(img.height * scale)),
            Image.Resampling.LANCZOS,
        )
    quality = 92
    while True:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= MAX_UPLOAD_BYTES or quality <= 60:
            break
        quality -= 8
    return base64.standard_b64encode(buffer.getvalue()).decode("utf-8")


# ---------------------------------------------------------------------------
# Talking to Claude
# ---------------------------------------------------------------------------

def parse_json(raw):
    text = raw.strip()
    if text.startswith("```"):  # tolerate accidental code fences
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def ask_claude(client, img, system_prompt, user_text):
    """Send one image + instructions, return the parsed JSON answer."""
    # Thinking tokens count toward max_tokens, so leave room for both the
    # reasoning and the JSON answer.
    if USE_THINKING:
        max_tokens, thinking = 16000, {"type": "adaptive"}
    else:
        max_tokens, thinking = 4096, {"type": "disabled"}

    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        thinking=thinking,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": to_jpeg_b64(img),
                        },
                    },
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    )

    # The response can hold several blocks (e.g. reasoning before the
    # answer), so find the text block instead of assuming it's first.
    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        raise RuntimeError("The response had no text block (it may have run out of tokens).")
    raw = "".join(text_blocks)
    try:
        return parse_json(raw)
    except json.JSONDecodeError:
        raise ValueError("Response wasn't valid JSON. Start of it: " + raw[:300])


def make_client():
    import anthropic  # imported here so --dry-run works without it

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY found. Check your .env file.")
        sys.exit(1)
    return anthropic.Anthropic(api_key=api_key)


def extract_sheet(client, img):
    """Whole sheet, one request."""
    print("Sending the whole chart to Claude. This can take a few minutes...")
    try:
        return ask_claude(client, img, SYSTEM_PROMPT,
                          "Extract the data from this at-bat chart as JSON.")
    except (RuntimeError, ValueError) as error:
        print(error)
        sys.exit(1)


def extract_rows(client, strips):
    """One request per batter row, a few at a time, then merge the results."""
    total = len(strips)
    print(f"Sending {total} rows to Claude ({MAX_WORKERS} at a time)...")

    def work(item):
        index, strip = item
        prompt = ROW_PROMPT.replace("__ROW__", str(index)).replace("__ROWS__", str(total))
        try:
            row = ask_claude(client, strip, prompt,
                             "Extract this batter's row as JSON.")
        except Exception as error:  # one bad row shouldn't lose the whole chart
            row = {"name": f"ROW {index}", "at_bats": [],
                   "extraction_error": str(error)}
        print(f"  Row {index}/{total} done", flush=True)
        return row

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        rows = list(pool.map(work, enumerate(strips, start=1)))
    return merge_rows(rows)


def merge_rows(rows):
    """Combine per-row results into the same shape the whole-sheet mode returns.
    Rows with fewer boxes get blank entries added so every batter lines up."""
    batters, notes = [], []
    for index, row in enumerate(rows, start=1):
        if row.get("notes"):
            notes.append(f"Row {index}: {row['notes']}")
        batter = {
            "batting_order": row.get("batting_order"),
            "jersey_number": row.get("jersey_number"),
            "name": row.get("name"),
            "at_bats": list(row.get("at_bats", [])),
        }
        if row.get("extraction_error"):
            batter["extraction_error"] = row["extraction_error"]
        batters.append(batter)

    columns = max((len(b["at_bats"]) for b in batters), default=0)
    for batter in batters:
        for column in range(len(batter["at_bats"]) + 1, columns + 1):
            batter["at_bats"].append({
                "column": column, "box_blank": True, "balls": None,
                "strikes": None, "result": None, "scored": None,
                "uncertain": False,
            })

    return {
        "layout": {"batter_rows": len(batters), "columns_with_writing": columns},
        "batters": batters,
        "notes": " | ".join(notes),
    }


# ---------------------------------------------------------------------------
# Checking the answer
# ---------------------------------------------------------------------------

def check_result(result):
    """Return a list of warnings for anything that doesn't add up.
    No AI involved: these are plain rules."""
    warnings = []
    expected_cols = result.get("layout", {}).get("columns_with_writing")

    for batter in result.get("batters", []):
        name = batter.get("name") or "?"
        at_bats = batter.get("at_bats", [])

        if batter.get("extraction_error"):
            warnings.append(f"{name}: this row FAILED to extract "
                            f"({batter['extraction_error']})")
            continue

        if expected_cols is not None and len(at_bats) != expected_cols:
            warnings.append(
                f"{name}: {len(at_bats)} at-bat entries, but the sheet has "
                f"{expected_cols} columns with writing"
            )

        previous = None
        for ab in at_bats:
            if ab.get("box_blank"):
                previous = None
                continue
            where = f"{name}, column {ab.get('column', '?')}"
            balls, strikes = ab.get("balls"), ab.get("strikes")

            # Last word of the result, so "1 BB" is treated as "BB".
            tokens = str(ab.get("result") or "").upper().split()
            code = tokens[-1] if tokens else ""

            if not code:
                warnings.append(f"{where}: no result was read")
            if code == "BB" and balls is not None and balls != 4:
                warnings.append(f"{where}: walk (BB) but {balls} balls")
            if code == "K" and strikes is not None and strikes < 3:
                warnings.append(f"{where}: strikeout (K) but {strikes} strikes")
            if ab.get("uncertain"):
                warnings.append(f"{where}: marked uncertain by the model")

            signature = (balls, strikes, ab.get("result"), ab.get("scored"))
            if previous is not None and signature == previous[0]:
                warnings.append(
                    f"{where}: identical to column {previous[1]} "
                    f"(possible duplicate read)"
                )
            previous = (signature, ab.get("column", "?"))

    return warnings


def fake_result():
    return {
        "layout": {"batter_rows": 1, "columns_with_writing": 2},
        "batters": [{
            "batting_order": 1, "jersey_number": "23", "name": "Example Player",
            "at_bats": [
                {"column": 1, "box_blank": False, "balls": 1, "strikes": 3,
                 "result": "K", "scored": False, "uncertain": False},
                {"column": 2, "box_blank": True, "balls": None, "strikes": None,
                 "result": None, "scored": None, "uncertain": False},
            ],
        }],
        "notes": "This is placeholder dry-run data, not a real extraction.",
    }


def report(result):
    print(json.dumps(result, indent=2))

    batters = result.get("batters", [])
    filled = sum(1 for b in batters for ab in b.get("at_bats", [])
                 if not ab.get("box_blank"))
    layout = result.get("layout", {})
    print(f"\n--- {len(batters)} batters, {layout.get('columns_with_writing', '?')} "
          f"columns with writing, {filled} at-bats with results ---")
    if result.get("notes"):
        print(f"Notes from extraction: {result['notes']}")

    warnings = check_result(result)
    if warnings:
        print(f"\n--- {len(warnings)} things to check by eye ---")
        for w in warnings:
            print(" -", w)
    else:
        print("\nNo consistency problems found.")


# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Read a handwritten at-bat chart photo.")
    parser.add_argument("image", help="path to the chart photo")
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                        help=f"number of batter rows (default {DEFAULT_ROWS}); cuts the photo "
                             f"into one strip per row. 0 sends the whole sheet at once.")
    parser.add_argument("--rotate", type=int, default=DEFAULT_ROTATE, choices=[0, 90, 180, 270],
                        help=f"rotate the photo clockwise this many degrees first "
                             f"(default {DEFAULT_ROTATE})")
    parser.add_argument("--grid-top", type=float, default=DEFAULT_GRID_TOP,
                        help=f"where the first batter row starts, as a fraction of the photo "
                             f"height after rotating (default {DEFAULT_GRID_TOP}). "
                             f"Skips the header.")
    parser.add_argument("--grid-bottom", type=float, default=DEFAULT_GRID_BOTTOM,
                        help=f"where the last batter row ends, as a fraction of the photo "
                             f"height (default {DEFAULT_GRID_BOTTOM}). Skips the summary.")
    parser.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP,
                        help=f"extra image kept above/below each strip, as a fraction of "
                             f"a row (default {DEFAULT_OVERLAP})")
    parser.add_argument("--save-strips", action="store_true",
                        help="save the strips to disk so you can inspect them")
    parser.add_argument("--out", default="",
                        help=f"where to save the result .json "
                             f"(default {EXTRACT_FOLDER}/<photo name>.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="no API call, no cost; with --rows it still cuts and saves the strips")
    return parser.parse_args()


def main():
    args = parse_args()
    path = Path(args.image)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    img = load_image(path, args.rotate)
    if args.rows and not (0 <= args.grid_top < args.grid_bottom <= 1):
        print("--grid-top must be smaller than --grid-bottom, and both between 0 and 1.")
        sys.exit(1)

    strips = (split_into_rows(img, args.rows, args.overlap, args.grid_top, args.grid_bottom)
              if args.rows else [])

    if strips and (args.save_strips or args.dry_run):
        save_overlay(img, args.rows, args.grid_top, args.grid_bottom)
        save_strips(strips)

    if args.dry_run:
        print("[DRY RUN] No request made, no cost incurred.")
        print("[DRY RUN] Showing a fake example response so you can see the shape:\n")
        result = fake_result()
    else:
        client = make_client()
        result = extract_rows(client, strips) if strips else extract_sheet(client, img)

    report(result)

    if not args.dry_run:
        out = Path(args.out) if args.out else Path(EXTRACT_FOLDER) / f"{path.stem}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        # Remember which photo this came from, so the importer can find it.
        saved = {"source_image": str(path), **result}
        out.write_text(json.dumps(saved, indent=2))
        print(f"\nSaved the result to {out}.")
        print(f"Next: python3 import_chart.py {out}")


if __name__ == "__main__":
    main()