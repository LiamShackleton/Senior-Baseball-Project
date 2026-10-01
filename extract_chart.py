"""Extract structured at-bat data from a photo of a hitting/at-bat chart.

Usage:
    python3 extract_chart.py path/to/chart.jpg
    python3 extract_chart.py path/to/chart.jpg --dry-run   # no API call, no cost

Requires a .env file in this folder containing:
    ANTHROPIC_API_KEY=your_key_here

This script only reads the image and prints what it found — it does NOT
write anything to the database yet. That's a deliberate separation: you
should be able to read and sanity-check the extracted data before it ever
touches your real tables. The database-insert step comes after this is
working well.
"""

import base64
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
import os

load_dotenv()

# This describes YOUR specific chart notation, as we mapped it out together.
# If your actual charts ever diverge from this (new result codes, a changed
# layout), this is the one place to update.
SYSTEM_PROMPT = """
You are reading a handwritten baseball at-bat chart. Here is how it's laid out:

- Each ROW is one batter. The batter's name and jersey number are at the
  left edge of the row.
- Each BOX within a row is one at-bat for that batter, read left to right
  in chronological order through the game.
- Inside each at-bat box:
  - A small grid of X marks. The BOTTOM row of X's represents balls, the
    TOP row represents strikes. Read each row LEFT TO RIGHT to get the
    pitch sequence (1st pitch, 2nd pitch, etc.) — but note the two rows
    (balls and strikes) are tracked separately, not interleaved, so you
    cannot always tell overall pitch order between a ball and a strike,
    only the order within each row.
  - A small diamond icon. If it is shaded/filled in, the batter scored as
    a baserunner during this at-bat. If unshaded, they did not score.
  - A text result code for the at-bat outcome. Standard codes include:
    K (strikeout), BB (walk), HBP (hit by pitch), 1B/2B/3B/HR (hits),
    and groundball/flyball/popup outs written as fielding position
    numbers (e.g. "6-3" = fielded by shortstop, thrown to first base;
    "F7" = flyout to left field; "P6" = popout to shortstop;
    "E6" = error by shortstop; "FC" = fielder's choice).

Return ONLY valid JSON (no markdown formatting, no explanation text before
or after) matching this exact structure:

{
  "batters": [
    {
      "batting_order": <number or null if unclear>,
      "jersey_number": "<string or null>",
      "name": "<string>",
      "at_bats": [
        {
          "sequence_in_game": <number, 1st at-bat for this batter = 1>,
          "balls": <count of X's in the bottom row>,
          "strikes": <count of X's in the top row>,
          "result": "<the text code, e.g. 'K', 'BB', '6-3'>",
          "scored": <true or false>
        }
      ]
    }
  ],
  "notes": "<anything illegible, ambiguous, or that doesn't fit the above>"
}

If a field is genuinely unreadable, use null rather than guessing. Flag
anything uncertain in the "notes" field rather than silently picking an
answer.
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
            "batters": [
                {
                    "batting_order": 1,
                    "jersey_number": "23",
                    "name": "Example Player",
                    "at_bats": [
                        {"sequence_in_game": 1, "balls": 1, "strikes": 2,
                         "result": "K", "scored": False}
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

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2000,
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

    raw_text = response.content[0].text
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

    batter_count = len(result.get("batters", []))
    at_bat_count = sum(len(b.get("at_bats", [])) for b in result.get("batters", []))
    print(f"\n--- Found {batter_count} batters, {at_bat_count} at-bats total ---")
    if result.get("notes"):
        print(f"Notes from extraction: {result['notes']}")


if __name__ == "__main__":
    main()
