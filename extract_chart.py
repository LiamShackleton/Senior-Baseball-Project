"""Extract structured at-bat data from a photo of a hitting/at-bat chart.

Usage:
    python3 extract_chart.py path/to/chart.jpg
    python3 extract_chart.py path/to/chart.jpg --dry-run   # no API call, no cost

Requires a .env file in this folder containing:
    ANTHROPIC_API_KEY=your_key_here

This script only reads the image and prints what it found. It does NOT
write anything to the database yet. That's a deliberate separation: you
should be able to read and sanity-check the extracted data before it ever
touches your real tables.
"""

import base64
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# True  = let Claude reason before answering (more careful on dense
#         handwriting, costs a few more cents per image).
# False = answer immediately (cheaper and faster, less careful).
USE_THINKING = True

# This describes YOUR specific chart notation. If your charts ever change
# (new result codes, a different layout), this is the one place to update.
SYSTEM_PROMPT = """
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
        {
          "column": <1-based column number>,
          "box_blank": <true if nothing is written in this box, else false>,
          "balls": <count of X's in the bottom row, or null>,
          "strikes": <count of X's in the top row, or null>,
          "result": "<the text code, e.g. 'K', 'BB', '6-3', or null>",
          "scored": <true or false, or null if blank>,
          "uncertain": <true if any field in this box was hard to read>
        }
      ]
    }
  ],
  "notes": "<anything illegible, ambiguous, or that doesn't fit the above>"
}

Every batter's "at_bats" list must contain exactly
layout.columns_with_writing entries. If a field is genuinely unreadable, use
null rather than guessing, and flag it in "notes".
""".strip()


def encode_image(path):
    """Read an image file and return it as base64 text, which is the
    format the API expects for sending images."""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def guess_media_type(path):
    suffix = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix, "image/jpeg")


def check_result(result):
    """Return a list of warnings for anything in the extraction that
    doesn't add up. No AI involved: these are plain rules."""
    warnings = []
    expected_cols = result.get("layout", {}).get("columns_with_writing")

    for batter in result.get("batters", []):
        name = batter.get("name") or "?"
        at_bats = batter.get("at_bats", [])

        if expected_cols is not None and len(at_bats) != expected_cols:
            warnings.append(
                f"{name}: {len(at_bats)} at-bat entries, but the sheet has "
                f"{expected_cols} columns with writing"
            )

        for ab in at_bats:
            if ab.get("box_blank"):
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

    return warnings


def extract(image_path, dry_run=False):
    image_path = Path(image_path)
    if not image_path.exists():
        print(f"File not found: {image_path}")
        sys.exit(1)

    if dry_run:
        print(f"[DRY RUN] Would send {image_path.name} to Claude's API.")
        print("[DRY RUN] No request made, no cost incurred.")
        print("[DRY RUN] Returning a fake example response so you can see the shape:\n")
        return {
            "layout": {"batter_rows": 1, "columns_with_writing": 2},
            "batters": [
                {
                    "batting_order": 1,
                    "jersey_number": "23",
                    "name": "Example Player",
                    "at_bats": [
                        {"column": 1, "box_blank": False, "balls": 1,
                         "strikes": 3, "result": "K", "scored": False,
                         "uncertain": False},
                        {"column": 2, "box_blank": True, "balls": None,
                         "strikes": None, "result": None, "scored": None,
                         "uncertain": False},
                    ],
                }
            ],
            "notes": "This is placeholder dry-run data, not a real extraction.",
        }

    # Imported here, not at the top, so --dry-run works even before the
    # anthropic package is installed.
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("No ANTHROPIC_API_KEY found. Check your .env file.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    image_b64 = encode_image(image_path)

    # Thinking tokens count toward max_tokens, so leave plenty of room for
    # both the reasoning and the JSON answer.
    if USE_THINKING:
        max_tokens, thinking = 16000, {"type": "adaptive"}
    else:
        max_tokens, thinking = 4096, {"type": "disabled"}

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=max_tokens,
        thinking=thinking,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": guess_media_type(image_path),
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract the data from this at-bat chart as JSON.",
                    },
                ],
            }
        ],
    )

    # The response can contain multiple blocks (e.g. reasoning before the
    # actual answer), so find the text block instead of assuming it's first.
    text_blocks = [b.text for b in response.content if b.type == "text"]
    if not text_blocks:
        print("No text block found in the response. Full response for debugging:")
        print(response.content)
        sys.exit(1)
    raw_text = "".join(text_blocks)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        print("Response wasn't valid JSON. Raw output below for debugging:\n")
        print(raw_text)
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 extract_chart.py path/to/chart.jpg [--dry-run]")
        sys.exit(1)

    image_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    result = extract(image_path, dry_run=dry_run)

    print(json.dumps(result, indent=2))

    batters = result.get("batters", [])
    filled = sum(
        1 for b in batters for ab in b.get("at_bats", []) if not ab.get("box_blank")
    )
    layout = result.get("layout", {})
    print(
        f"\n--- {len(batters)} batters, {layout.get('columns_with_writing', '?')} "
        f"columns with writing, {filled} at-bats with results ---"
    )
    if result.get("notes"):
        print(f"Notes from extraction: {result['notes']}")

    warnings = check_result(result)
    if warnings:
        print(f"\n--- {len(warnings)} things to check by eye ---")
        for w in warnings:
            print(" -", w)
    else:
        print("\nNo consistency problems found.")


if __name__ == "__main__":
    main()