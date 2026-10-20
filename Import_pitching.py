"""Review an extracted pitching chart, then save it to the database.

    python3 extract_pitching_chart.py sample_charts/photo.png     # saves extracted/photo_pitching.json
    python3 import_pitching.py extracted/photo_pitching.json
    python3 import_pitching.py extracted/photo_pitching.json --dry-run   # review only, nothing saved

What it does, in order:
  1. Shows every at-bat and pitch, and lets you fix mistakes.
  2. Asks who the pitcher is, and which game this is.
  3. Shows a summary and asks for a final yes.

Nothing is written until that yes, and if anything fails partway through,
everything is undone. Pitching charts don't record who was batting, so the
hitter is left blank on these at-bats.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from db import get_connection, run_query
from import_chart import ask, ask_int, describe_decision, plan_game, plan_player, yes_no


# ---------------------------------------------------------------------------
# Step 1: review
# ---------------------------------------------------------------------------

# Where a pitch is placed when you correct its location by hand
# (x: 0 = left edge of the zone, 1 = right edge; y: 0 = bottom, 1 = top).
PLACES = {
    "inside": (0.5, 0.5),
    "above": (0.5, 1.15), "below": (0.5, -0.15),
    "left": (-0.15, 0.5), "right": (1.15, 0.5),
    "above-left": (-0.15, 1.15), "above-right": (1.15, 1.15),
    "below-left": (-0.15, -0.15), "below-right": (1.15, -0.15),
}


def where_of(p):
    """Where a pitch landed, as one of the PLACES words. Uses the model's own
    answer when it gave one, otherwise works it out from x and y."""
    if p.get("where") in PLACES:
        return p["where"]
    x, y = p.get("x"), p.get("y")
    if x is None or y is None:
        return "inside"
    side = "left" if x < 0 else "right" if x > 1 else ""
    height = "above" if y > 1 else "below" if y < 0 else ""
    return "-".join(part for part in (height, side) if part) or "inside"


def in_zone(p):
    x, y = p.get("x"), p.get("y")
    return x is not None and y is not None and 0 <= x <= 1 and 0 <= y <= 1


def show(result):
    for warning in result.get("warnings", []):
        print(f"  ! {warning}")
    for box in result["boxes"]:
        print(f"\nAB{box['order']}  result: {box.get('result') or '-'}")
        for p in box["pitches"]:
            where = "in zone " if in_zone(p) else "outside "
            flag = "  [uncertain]" if p.get("uncertain") else ""
            print(f"   {p['number']:>2}  {p.get('pitch_type') or '??':<4} "
                  f"{p.get('velocity') or '??':>4} mph   {where} "
                  f"(x {p.get('x')}, y {p.get('y')}){flag}")


def edit_pitch(p):
    print(f"\nFixing pitch {p['number']}. Press Enter to keep the current value.")
    p["pitch_type"] = ask("  Pitch type", p.get("pitch_type") or "").upper() or None
    p["velocity"] = ask_int("  Velocity", p.get("velocity"))
    where = ask("  Location: inside, above, below, left, right, or a corner like "
                "above-left (Enter to keep)", "").lower()
    if where in PLACES:
        p["where"] = where
        p["x"], p["y"] = PLACES[where]
    elif where:
        print("  Didn't recognize that location, so it was kept as it was.")
    p["uncertain"] = False  # you've looked at it now


def review(result):
    while True:
        show(result)
        command = ask(
            "\nPress Enter if everything looks right. To fix a pitch, type the at-bat "
            "number and pitch number (like '2 5'). 'r 2' changes the result of "
            "at-bat 2. 'q' quits"
        ).lower()
        if command == "":
            return
        if command == "q":
            print("Quit. Nothing was saved.")
            sys.exit(0)
        parts = command.split()
        try:
            if parts[0] == "r" and len(parts) == 2:
                box = result["boxes"][int(parts[1]) - 1]
                box["result"] = ask("  Result", box.get("result") or "").upper() or None
            elif len(parts) == 2:
                box = result["boxes"][int(parts[0]) - 1]
                pitch = next((p for p in box["pitches"] if p["number"] == int(parts[1])), None)
                if pitch is None:
                    print(f"  At-bat {parts[0]} has no pitch {parts[1]}.")
                else:
                    edit_pitch(pitch)
            else:
                print("  I didn't understand that.")
        except (ValueError, IndexError):
            print("  That isn't a valid at-bat number.")


# ---------------------------------------------------------------------------
# Step 3: save everything in one transaction
# ---------------------------------------------------------------------------

def final_count(code):
    """Only a walk or strikeout tells us the final count."""
    if code == "BB":
        return 4, None
    if code == "K":
        return None, 3
    return None, None


def save(result, original, game_plan, pitcher_decision, image, team):
    connection = get_connection()
    try:
        cursor = connection.cursor()

        if pitcher_decision[0] == "use":
            _, pitcher_id, current_role, needed = pitcher_decision
            if current_role not in (needed, "both"):
                cursor.execute("UPDATE players SET role = 'both' WHERE player_id = %s", (pitcher_id,))
        else:
            _, first, last, role = pitcher_decision
            cursor.execute("INSERT INTO players (first_name, last_name, role, team) "
                           "VALUES (%s, %s, %s, %s)", (first, last, role, team))
            pitcher_id = cursor.lastrowid

        if game_plan[0] == "existing":
            game_id = game_plan[1]
        else:
            _, date, opponent, location = game_plan
            cursor.execute("INSERT INTO games (game_date, season, opponent, location) "
                           "VALUES (%s, %s, %s, %s)", (date, date[:4], opponent, location))
            game_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO source_charts (game_id, pitcher_id, chart_type, image_path, "
            "extraction_status, raw_extraction_json, reviewed_by, reviewed_at) "
            "VALUES (%s, %s, 'pitching', %s, 'reviewed', %s, 'manual review', NOW())",
            (game_id, pitcher_id, image, json.dumps(original)))
        chart_id = cursor.lastrowid

        at_bats = pitches = 0
        for box in result["boxes"]:
            code = " ".join(str(box.get("result") or "").upper().split())
            if not code:
                continue  # an at-bat with no result can't be saved
            balls, strikes = final_count(code.split()[-1])
            cursor.execute(
                "INSERT INTO at_bats (game_id, pitcher_id, source_chart_id, result, "
                "final_balls, final_strikes, notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (game_id, pitcher_id, chart_id, code, balls, strikes,
                 f"pitching chart at-bat {box['order']}"))
            at_bat_id = cursor.lastrowid
            at_bats += 1

            for sequence, p in enumerate(box["pitches"], start=1):
                cursor.execute(
                    "INSERT INTO pitches (at_bat_id, sequence_number, pitch_type, velocity, "
                    "location_x, location_y, result, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (at_bat_id, sequence, p.get("pitch_type"), p.get("velocity"),
                     p.get("x"), p.get("y"),
                     "in_zone" if in_zone(p) else "out_of_zone",
                     f"chart pitch number {p['number']}"))
                pitches += 1

        connection.commit()
        return game_id, chart_id, at_bats, pitches
    except Exception:
        connection.rollback()  # undo everything, then let the caller report it
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Review and save an extracted pitching chart.")
    parser.add_argument("json_file", help="the .json file made by extract_pitching_chart.py")
    parser.add_argument("--game-id", type=int, default=0, help="add to an existing game")
    parser.add_argument("--date", default="", help="game date, YYYY-MM-DD")
    parser.add_argument("--opponent", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--team", default="", help="team name to store on a NEW pitcher")
    parser.add_argument("--force", action="store_true", help="import even if already imported")
    parser.add_argument("--dry-run", action="store_true", help="go through everything but save nothing")
    args = parser.parse_args()

    path = Path(args.json_file)
    if not path.exists():
        sys.exit(f"File not found: {path}")
    try:
        result = json.loads(path.read_text())
        if not isinstance(result.get("boxes"), list) or not result["boxes"]:
            raise ValueError("no 'boxes' list")
    except (ValueError, AttributeError) as error:
        sys.exit(f"That doesn't look like output from extract_pitching_chart.py ({error}).")

    image = result.get("source_image")
    if not image:
        sys.exit("This file doesn't say which photo it came from.")
    previous = run_query("SELECT chart_id, uploaded_at FROM source_charts WHERE image_path = %s", (image,))
    if previous and not args.force:
        sys.exit(f"{image} was already imported (chart id {previous[0]['chart_id']}). "
                 f"Importing again would duplicate everything. Use --force if you really mean to.")

    original = copy.deepcopy(result)  # audit copy; your fixes go on `result`
    review(result)

    game_plan = plan_game(args)
    name = (result.get("pitcher") or "").strip() or ask("\nPitcher's name")
    pitcher_decision = plan_player(name, "pitcher")
    if pitcher_decision[0] == "skip":
        sys.exit("A pitching chart needs a pitcher. Nothing was saved.")

    saveable = [b for b in result["boxes"] if b.get("result")]
    skipped = len(result["boxes"]) - len(saveable)
    n_pitches = sum(len(b["pitches"]) for b in saveable)

    print("\n" + "=" * 60)
    print("About to save:")
    print("  Game:    " + (f"existing game {game_plan[1]}" if game_plan[0] == "existing"
                           else f"NEW, {game_plan[1]} vs {game_plan[2] or '?'}"))
    print(f"  Pitcher: {name} -> {describe_decision(pitcher_decision)}")
    print(f"  {len(saveable)} at-bats, {n_pitches} pitches (hitters not recorded)")
    if skipped:
        print(f"  ({skipped} at-bat(s) have no result and will be left out.)")
    print("=" * 60)

    if args.dry_run:
        print("[DRY RUN] Nothing was saved.")
        return
    if not saveable:
        sys.exit("Nothing to save.")
    if not yes_no("Save all of this to the database?"):
        sys.exit("Nothing was saved.")

    try:
        game_id, chart_id, at_bats, pitches = save(result, original, game_plan,
                                                   pitcher_decision, image, args.team or None)
    except Exception as error:
        sys.exit(f"\nSomething went wrong, so everything was undone. "
                 f"Nothing was saved.\n  {error}")
    print(f"\nSaved {at_bats} at-bats and {pitches} pitches (game {game_id}, chart {chart_id}). "
          f"Open the app to see them.")


if __name__ == "__main__":
    main()