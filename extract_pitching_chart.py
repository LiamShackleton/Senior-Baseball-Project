"""Read a pitching chart: a velocity table on the left, strike-zone boxes on the right.

    python3 extract_pitching_chart.py sample_charts/photo.png            # reads it, saves extracted/photo_pitching.json
    python3 extract_pitching_chart.py sample_charts/photo.png --dry-run  # only cuts the photo, NO API call, no cost

How it works:
  1. The photo is cut into two parts: the velocity table and the zone boxes.
  2. Each part is read in its own request.
  3. This script (not the model) joins them by pitch number and checks that
     everything lines up.

After a dry run, open crops_debug/table.jpg and crops_debug/zones.jpg and check
that each one shows the right part. If not, change SPLIT below.

Nothing is written to the database; this only reads the chart.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from extract_chart import ask_claude, load_image, make_client

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
SPLIT = 0.40          # where the table ends and the boxes begin, as a fraction of the width
TABLE_SIDE = "left"   # which side of the cut holds the velocity table
ROTATE = 0            # degrees clockwise to turn the photo (0 if it's already upright)
MIN_VELO, MAX_VELO = 40, 100  # velocities outside this range get flagged
ZONE_UPSCALE = 1.5    # enlarge the zone boxes before sending, so thin lines are easier to see

# ---------------------------------------------------------------------------
# Prompts: describe YOUR chart. Update them if the chart layout changes.
# ---------------------------------------------------------------------------
TABLE_PROMPT = """
You are reading the velocity table from a handwritten baseball pitching chart
for one pitcher's outing. Accuracy matters more than speed.

LAYOUT
- The far-left column is "Pitch Count": the numbers 1, 2, 3, ... are PRINTED
  down the page, one per row. Next to it are columns for pitch types, with the
  type printed in the header (for example FB, CH, SL, OTH).
- Each handwritten entry is a velocity in mph, written in the column for its
  pitch type. Only one column is filled in per row.
- A pitch's number is the printed Pitch Count number on the SAME ROW as the
  velocity. Handwriting is often larger than a row, so follow the row lines
  carefully. Entries are written top to bottom in the order thrown, so the
  numbers should run 1, 2, 3, ... with no gaps.
- Ignore any blank rows below the last entry.

PROCEDURE
1. Read the header of each pitch-type column.
2. Go down the table one row at a time. For each row that has a velocity,
   record the row's pitch number, the column header, and the velocity.
3. If something is hard to read, use null and set "uncertain": true. Never
   guess a digit and never invent an entry.

Return ONLY valid JSON (no markdown, no extra text) in this structure:

{
  "pitcher": "<name or null>",
  "game_info": "<opponent/date text or null>",
  "pitches": [
    {"number": <integer>, "pitch_type": "<column header>", "velocity": <number or null>, "uncertain": <true or false>}
  ],
  "notes": "<anything illegible or ambiguous>"
}
""".strip()

ZONE_PROMPT = """
You are reading the strike-zone boxes from a handwritten baseball pitching
chart for one pitcher's outing. Accuracy matters more than speed.

LAYOUT
- The sheet has a grid of light-gray square panels. Each panel holds a
  hand-drawn rectangle with a BLACK outline. One panel is one at-bat.
- THE STRIKE ZONE IS ONLY THE AREA INSIDE THE BLACK OUTLINE. The gray square
  behind it is just background, not part of the zone. The black rectangle
  nearly fills its gray square, so the gray margin is very thin. A number is
  OUTSIDE the zone only if MORE THAN HALF of it lies beyond a black line, that
  is, its center is outside the line. A number that merely touches or overlaps
  a black line, with at least half of it still inside, is INSIDE the zone.
- Most panels are empty. Only report panels that have pitch numbers in them.
- Each pitch is a small NUMBER written where that pitch crossed the plate. It
  may have a small dot beside it; the location is where the number and dot are.
- Numbers count up continuously through the whole outing and do not restart for
  each batter, so the numbers in one panel are consecutive and continue from
  the previous panel in game order.
- Text written above a panel (such as K, BB, F7, 1B, 6-3) is the result of that
  at-bat. Use null if there is none.
- Ignore the title, the summary boxes (H, BB, HBP, K, velocity range) and the
  innings table; they are not strike zones.

LOCATION
For each pitch number, first decide "where" by comparing the CENTER of
the number to the BLACK OUTLINE. If the center is inside the lines, the answer
is "inside", even if part of the number touches a line. Use exactly one of:
  "inside"  (the center is within the black lines, even if the number touches a line)
  "above", "below", "left", "right"  (outside, beyond that one side)
  "above-left", "above-right", "below-left", "below-right"  (outside a corner)
Then estimate x and y relative to the black rectangle:
- x: 0.0 = left black line, 1.0 = right black line. Outside is below 0 or above 1.
- y: 0.0 = bottom black line, 1.0 = top black line. Below is negative, above is
  greater than 1.
Round to one decimal place. x and y must agree with "where": an "inside" pitch
has both between 0 and 1, and "left" means x is below 0, and so on.

PROCEDURE
1. List the panels that contain pitch numbers, and note each one's row on the
   sheet (1 = top) and position in that row (1 = leftmost).
2. In each panel, read every number carefully. Numbers can have two digits and
   can touch the rectangle or each other.
3. For each number, trace the black outline first, then decide "where" from
   which side of the line the center of the number is on.
4. If a number or position is hard to read, set "uncertain": true instead of
   guessing. Never invent a number.

Return ONLY valid JSON (no markdown, no extra text) in this structure:

{
  "boxes": [
    {
      "sheet_row": <number>,
      "sheet_col": <number>,
      "result": "<text written above the panel, or null>",
      "pitches": [
        {"number": <integer>, "where": "<one of the values above>", "x": <number>, "y": <number>, "uncertain": <true or false>}
      ]
    }
  ],
  "notes": "<anything illegible or ambiguous>"
}
""".strip()


# ---------------------------------------------------------------------------
# Cutting the photo
# ---------------------------------------------------------------------------

def split_halves(img):
    """Cut the photo at SPLIT. Returns (table_image, zones_image)."""
    cut = round(img.width * SPLIT)
    left = img.crop((0, 0, cut, img.height))
    right = img.crop((cut, 0, img.width, img.height))
    return (left, right) if TABLE_SIDE == "left" else (right, left)


def save_crops(table_img, zones_img, folder="crops_debug"):
    Path(folder).mkdir(exist_ok=True)
    table_img.save(Path(folder) / "table.jpg", quality=90)
    zones_img.save(Path(folder) / "zones.jpg", quality=90)
    print(f"Saved table.jpg and zones.jpg to '{folder}'. Check that each shows the right part.")


# ---------------------------------------------------------------------------
# Joining the two reads, and checking them (plain rules, no AI)
# ---------------------------------------------------------------------------

def reconcile(mark):
    """The model's "where" (inside / above / below-left ...) is an easier call
    than exact coordinates, so when the two disagree, "where" wins and x and y
    are nudged to match it. Returns (x, y, adjusted)."""
    where = str(mark.get("where") or "").lower()
    x, y = mark.get("x"), mark.get("y")
    if not where or x is None or y is None:
        return x, y, False
    old = (x, y)
    if where == "inside":
        x, y = min(max(x, 0.05), 0.95), min(max(y, 0.05), 0.95)
    else:
        if "left" in where and x >= 0:
            x = -0.15
        if "right" in where and x <= 1:
            x = 1.15
        if "above" in where and y <= 1:
            y = 1.15
        if "below" in where and y >= 0:
            y = -0.15
        # A side-only answer such as "left" says nothing about the other axis.
        # Keep the model's other coordinate, but inside the zone's span.
        if "left" not in where and "right" not in where:
            x = min(max(x, 0.0), 1.0)
        if "above" not in where and "below" not in where:
            y = min(max(y, 0.0), 1.0)
    return x, y, (x, y) != old


def merge_pitching(table, zones):
    """Join the velocity table and the zone boxes by pitch number.
    Returns (result, warnings)."""
    warnings = []

    # Velocity table, keyed by pitch number.
    by_number = {}
    for entry in table.get("pitches", []):
        number, velocity = entry.get("number"), entry.get("velocity")
        if number is None:
            warnings.append(f"table: an entry ({entry.get('pitch_type')}, {velocity}) has no pitch number")
            continue
        if number in by_number:
            warnings.append(f"table: pitch {number} appears more than once")
        by_number[number] = entry
        if velocity is None:
            warnings.append(f"pitch {number}: no velocity was read")
        elif not MIN_VELO <= velocity <= MAX_VELO:
            warnings.append(f"pitch {number}: velocity {velocity} looks unlikely")
        if entry.get("uncertain"):
            warnings.append(f"pitch {number}: table entry marked uncertain")
    gaps = [n for n in range(1, max(by_number, default=0) + 1) if n not in by_number]
    if gaps:
        warnings.append(f"table: no entry for pitch number(s) {gaps}")

    # Zone boxes.
    drawn_in, boxes = {}, []
    for box in zones.get("boxes", []):
        where = f"box at row {box.get('sheet_row')}, position {box.get('sheet_col')}"
        pitches = []
        for mark in box.get("pitches", []):
            number = mark.get("number")
            if number is None:
                warnings.append(f"{where}: a pitch mark has no readable number")
                continue
            if number in drawn_in:
                warnings.append(f"pitch {number} is drawn more than once ({drawn_in[number]} and {where})")
            drawn_in[number] = where
            row = by_number.get(number)
            if row is None:
                warnings.append(f"pitch {number} ({where}) has no entry in the velocity table")
            if mark.get("uncertain"):
                warnings.append(f"pitch {number}: location marked uncertain")
            x, y, adjusted = reconcile(mark)
            if adjusted:
                warnings.append(f"pitch {number}: coordinates adjusted to match "
                                f"'{mark.get('where')}'")
            pitches.append({
                "number": number,
                "pitch_type": row.get("pitch_type") if row else None,
                "velocity": row.get("velocity") if row else None,
                "where": mark.get("where"),
                "x": x,
                "y": y,
                "uncertain": bool(mark.get("uncertain") or (row and row.get("uncertain"))),
            })
        if pitches:
            pitches.sort(key=lambda p: p["number"])
            boxes.append({"sheet_row": box.get("sheet_row"), "sheet_col": box.get("sheet_col"),
                          "result": box.get("result"), "pitches": pitches})

    # Game order comes from the pitch numbers, not from where a panel sits.
    boxes.sort(key=lambda b: b["pitches"][0]["number"])
    expected_next = 1
    for order, box in enumerate(boxes, start=1):
        numbers = [p["number"] for p in box["pitches"]]
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            warnings.append(f"at-bat {order}: pitch numbers {numbers} aren't consecutive")
        if numbers[0] != expected_next:
            warnings.append(f"at-bat {order}: starts at pitch {numbers[0]}, expected {expected_next}")
        expected_next = numbers[-1] + 1
        code = str(box["result"] or "").upper().split()
        code = code[-1] if code else ""
        if code == "BB" and len(numbers) < 4:
            warnings.append(f"at-bat {order}: walk (BB) but only {len(numbers)} pitches")
        if code == "K" and len(numbers) < 3:
            warnings.append(f"at-bat {order}: strikeout (K) but only {len(numbers)} pitches")
        box["order"] = order

    missing = sorted(set(by_number) - set(drawn_in))
    if missing:
        warnings.append(f"pitch(es) {missing} have a velocity but no location")

    notes = " | ".join(n for n in (table.get("notes"), zones.get("notes")) if n)
    return {"pitcher": table.get("pitcher"), "game_info": table.get("game_info"),
            "total_pitches_in_table": len(by_number), "boxes": boxes, "notes": notes}, warnings


def print_summary(result, warnings, max_warnings=12):
    """One line per at-bat. Each pitch is  number:type+velocity, with an 'o'
    after it if it was outside the strike zone, for example  3:CH76o"""
    for box in result["boxes"]:
        shown = []
        for p in box["pitches"]:
            x, y = p.get("x"), p.get("y")
            outside = x is None or y is None or not (0 <= x <= 1 and 0 <= y <= 1)
            shown.append(f"{p['number']}:{p.get('pitch_type') or '??'}{p.get('velocity') or '??'}"
                         + ("o" if outside else ""))
        print(f"AB{box['order']:<2} {str(box['result'] or '-'):<4} " + "  ".join(shown))

    located = sum(len(b["pitches"]) for b in result["boxes"])
    print(f"\n{result['total_pitches_in_table']} pitches in the table, "
          f"{located} located across {len(result['boxes'])} at-bats.  (o = outside the zone)")
    if not warnings:
        print("No consistency problems found.")
        return
    print(f"\n{len(warnings)} things to check:")
    for warning in warnings[:max_warnings]:
        print(" -", warning)
    if len(warnings) > max_warnings:
        print(f" ... and {len(warnings) - max_warnings} more (see the saved file).")


# ---------------------------------------------------------------------------

def read_pitching_chart(path, client=None):
    """Photo in, (result, warnings) out: read the table and the zone boxes,
    then join them by pitch number."""
    table_img, zones_img = split_halves(load_image(path, ROTATE))
    zones_img = zones_img.resize((round(zones_img.width * ZONE_UPSCALE),
                                  round(zones_img.height * ZONE_UPSCALE)))
    client = client or make_client()
    with ThreadPoolExecutor(max_workers=2) as pool:
        table_job = pool.submit(ask_claude, client, table_img, TABLE_PROMPT,
                                "Extract the velocity table as JSON.")
        zones_job = pool.submit(ask_claude, client, zones_img, ZONE_PROMPT,
                                "Extract the strike-zone boxes as JSON.")
        table, zones = table_job.result(), zones_job.result()
    return merge_pitching(table, zones)


def main():
    parser = argparse.ArgumentParser(description="Read a handwritten pitching chart photo.")
    parser.add_argument("image", help="path to the chart photo")
    parser.add_argument("--dry-run", action="store_true",
                        help="cut the photo and save the two parts, with no API call and no cost")
    args = parser.parse_args()

    path = Path(args.image)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    if args.dry_run:
        save_crops(*split_halves(load_image(path, ROTATE)))
        return

    print("Sending the table and the zone boxes to Claude (two requests)...")
    try:
        result, warnings = read_pitching_chart(path)
    except Exception as error:
        sys.exit(f"Extraction failed: {error}")

    out = Path("extracted") / f"{path.stem}_pitching.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"source_image": str(path), "warnings": warnings, **result}, indent=2))

    print_summary(result, warnings)
    print(f"\nFull details (coordinates, notes) are in {out}.")


if __name__ == "__main__":
    main()