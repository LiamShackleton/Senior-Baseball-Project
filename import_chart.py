"""Review extracted at-bat data, then save it to the database.

Usage:
    python3 extract_chart.py photo.jpg                    # saves extracted/photo.json
    python3 import_chart.py extracted/photo.json
    python3 import_chart.py extracted/photo.json --dry-run   # review only, nothing is saved

What it does, in order:
  1. Shows every extracted at-bat, with warnings, and lets you fix mistakes.
  2. Asks which game this is. (At-bat charts don't record the pitcher, so
     none is saved.)
  3. For each batter name, asks which player in your database it is, or
     whether to create a new player. It never creates one without asking.
  4. Shows a summary and asks for a final yes.

Nothing is written to the database until that final yes, and if anything
fails partway through, everything is undone.
"""

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

from db import get_connection, run_query
from extract_chart import check_result


# ---------------------------------------------------------------------------
# Small input helpers
# ---------------------------------------------------------------------------

def ask(prompt, default=""):
    """Ask a question. Pressing Enter gives the default."""
    suffix = f" [{default}]" if default not in ("", None) else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        print("\nNo more input, so stopping. Nothing was saved.")
        sys.exit(1)
    return answer if answer else (default or "")


def ask_int(prompt, current):
    while True:
        raw = ask(prompt, "" if current is None else str(current))
        if raw == "":
            return None
        try:
            return int(raw)
        except ValueError:
            print("  Please type a whole number.")


def yes_no(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} ({hint})").lower()
    return default if not answer else answer.startswith("y")


# ---------------------------------------------------------------------------
# Step 1: review the extraction
# ---------------------------------------------------------------------------

def describe(ab):
    if ab.get("box_blank"):
        return "(blank box)"
    balls = "?" if ab.get("balls") is None else ab["balls"]
    strikes = "?" if ab.get("strikes") is None else ab["strikes"]
    flags = []
    if ab.get("scored"):
        flags.append("scored")
    if ab.get("uncertain"):
        flags.append("uncertain")
    tail = f"  [{', '.join(flags)}]" if flags else ""
    return f"count {balls}-{strikes}   {ab.get('result') or 'NO RESULT'}{tail}"


def show(result):
    expected = result.get("layout", {}).get("columns_with_writing")
    for number, batter in enumerate(result["batters"], start=1):
        print(f"\n{number}. {batter.get('name') or '?'}")
        for ab in batter.get("at_bats", []):
            print(f"     box {ab.get('column')}: {describe(ab)}")
        one_batter = {"layout": {"columns_with_writing": expected}, "batters": [batter]}
        for warning in check_result(one_batter):
            print(f"     ! {warning}")


def edit_box(batter, ab):
    print(f"\nFixing {batter.get('name')}, box {ab.get('column')}. "
          f"Press Enter to keep the current value.")
    ab["balls"] = ask_int("  Balls", ab.get("balls"))
    ab["strikes"] = ask_int("  Strikes", ab.get("strikes"))
    result = ask("  Result (type 'blank' if the box is empty)", ab.get("result") or "")
    if result.lower() == "blank":
        ab["box_blank"] = True
        ab["result"] = None
        return
    ab["box_blank"] = False
    ab["result"] = result or None
    ab["scored"] = yes_no("  Scored?", bool(ab.get("scored")))
    ab["uncertain"] = False  # you've looked at it now


def pick_batter(result, text):
    number = int(text)
    if not 1 <= number <= len(result["batters"]):
        raise IndexError
    return result["batters"][number - 1]


def review(result):
    while True:
        show(result)
        command = ask(
            "\nPress Enter if everything looks right. To fix an at-bat, type the "
            "batter number and box number (like '2 3'). 'n 2' renames batter 2. "
            "'q' quits"
        ).lower()
        if command == "":
            return
        if command == "q":
            print("Quit. Nothing was saved.")
            sys.exit(0)
        parts = command.split()
        try:
            if parts[0] == "n" and len(parts) == 2:
                batter = pick_batter(result, parts[1])
                batter["name"] = ask("  New name", batter.get("name") or "")
            elif len(parts) == 2:
                batter = pick_batter(result, parts[0])
                column = int(parts[1])
                box = next((a for a in batter.get("at_bats", [])
                            if a.get("column") == column), None)
                if box is None:
                    print(f"  {batter.get('name')} has no box {column}.")
                else:
                    edit_box(batter, box)
            else:
                print("  I didn't understand that.")
        except (ValueError, IndexError):
            print("  That isn't a valid batter number.")


# ---------------------------------------------------------------------------
# Steps 2 and 3: decide the game and the players (reads only, no writes)
# ---------------------------------------------------------------------------

def plan_game(args):
    if args.game_id:
        found = run_query(
            "SELECT game_id, game_date, opponent FROM games WHERE game_id = %s",
            (args.game_id,))
        if not found:
            print(f"There is no game with id {args.game_id}.")
            sys.exit(1)
        g = found[0]
        print(f"\nUsing existing game {g['game_id']}: {g['game_date']} "
              f"vs {g['opponent'] or '?'}")
        return ("existing", args.game_id)

    print("\nWhich game is this chart from?")
    while True:
        date = ask("  Date (YYYY-MM-DD)", args.date or "")
        try:
            datetime.strptime(date, "%Y-%m-%d")
            break
        except ValueError:
            print("  Please use the format 2025-04-12.")
    opponent = ask("  Opponent", args.opponent or "")
    location = ask("  Location", args.location or "")
    return ("new", date, opponent or None, location or None)


def find_players(name):
    return run_query(
        "SELECT player_id, first_name, last_name, role FROM players "
        "WHERE LOWER(first_name) = LOWER(%s) OR LOWER(last_name) = LOWER(%s) "
        "OR LOWER(TRIM(CONCAT(first_name, ' ', last_name))) = LOWER(%s) "
        "ORDER BY last_name, first_name",
        (name, name, name))


def full_name(player):
    return f"{player['first_name']} {player['last_name']}".strip()


def plan_player(name, role):
    """Work out who a chart name is. Returns one of:
         ("use", player_id, current_role, needed_role)
         ("create", first_name, last_name, needed_role)
         ("skip",)
    """
    matches = find_players(name)
    print(f"\nOn the chart: {name}")
    while True:
        if matches:
            print("  Already in your database:")
            for i, p in enumerate(matches, start=1):
                print(f"    {i}. {full_name(p)} ({p['role']}, id {p['player_id']})")
            choice = ask("  Type a number to use that player, 'c' to create a new "
                         "player, or 's' to skip",
                         "1" if len(matches) == 1 else "").lower()
        else:
            print("  No player with that name in your database yet.")
            choice = ask("  Type 'c' to create a new player, an existing player's "
                         "id number to use them, or 's' to skip").lower()

        if choice == "s":
            return ("skip",)
        if choice == "c":
            first = ask("  First name", name)
            last = ask("  Last name (Enter to leave blank)")
            return ("create", first, last, role)
        if choice.isdigit():
            number = int(choice)
            if matches and 1 <= number <= len(matches):
                p = matches[number - 1]
                return ("use", p["player_id"], p["role"], role)
            if not matches:
                found = run_query(
                    "SELECT player_id, role FROM players WHERE player_id = %s", (number,))
                if found:
                    return ("use", found[0]["player_id"], found[0]["role"], role)
        print("  Please try again.")


def describe_decision(decision):
    kind = decision[0]
    if kind == "skip":
        return "SKIPPED (its at-bats won't be saved)"
    if kind == "use":
        return f"existing player id {decision[1]}"
    return f"NEW player '{f'{decision[1]} {decision[2]}'.strip()}'"


# ---------------------------------------------------------------------------
# Step 4: save everything in one transaction
# ---------------------------------------------------------------------------

def saveable(ab):
    return not ab.get("box_blank") and bool(ab.get("result"))


def save(result, original, game_plan, batter_plans, image, team):
    connection = get_connection()
    try:
        cursor = connection.cursor()

        def resolve(decision):
            if decision[0] == "use":
                _, player_id, current_role, needed = decision
                if current_role not in (needed, "both"):
                    cursor.execute(
                        "UPDATE players SET role = 'both' WHERE player_id = %s",
                        (player_id,))
                return player_id
            _, first, last, role = decision
            cursor.execute(
                "INSERT INTO players (first_name, last_name, role, team) "
                "VALUES (%s, %s, %s, %s)", (first, last, role, team))
            return cursor.lastrowid

        if game_plan[0] == "existing":
            game_id = game_plan[1]
        else:
            _, date, opponent, location = game_plan
            cursor.execute(
                "INSERT INTO games (game_date, season, opponent, location) "
                "VALUES (%s, %s, %s, %s)", (date, date[:4], opponent, location))
            game_id = cursor.lastrowid

        cursor.execute(
            "INSERT INTO source_charts (game_id, chart_type, image_path, "
            "extraction_status, raw_extraction_json, reviewed_by, reviewed_at) "
            "VALUES (%s, 'hitting', %s, 'reviewed', %s, 'manual review', NOW())",
            (game_id, image, json.dumps(original)))
        chart_id = cursor.lastrowid

        at_bats = 0
        for number, batter in enumerate(result["batters"], start=1):
            decision = batter_plans.get(number)
            if not decision or decision[0] == "skip":
                continue
            hitter_id = resolve(decision)
            for ab in batter.get("at_bats", []):
                if not saveable(ab):
                    continue
                cursor.execute(
                    "INSERT INTO at_bats (game_id, hitter_id, "
                    "source_chart_id, result, final_balls, final_strikes, scored, notes) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (game_id, hitter_id, chart_id,
                     " ".join(str(ab["result"]).upper().split()),
                     ab.get("balls"), ab.get("strikes"),
                     1 if ab.get("scored") else 0,
                     f"chart row {number}, box {ab.get('column')}"))
                at_bats += 1

        connection.commit()
        return game_id, chart_id, at_bats
    except Exception:
        connection.rollback()  # undo everything, then let the caller report it
        raise
    finally:
        connection.close()


# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Review and save an extracted at-bat chart.")
    parser.add_argument("json_file", help="the .json file made by extract_chart.py --out")
    parser.add_argument("--image", default="",
                        help="the chart photo this came from. Normally not needed: "
                             "extract_chart.py records it in the .json file")
    parser.add_argument("--game-id", type=int, default=0,
                        help="add to an existing game instead of creating one")
    parser.add_argument("--date", default="", help="game date, YYYY-MM-DD")
    parser.add_argument("--opponent", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--team", default="",
                        help="team name to store on any NEW players created")
    parser.add_argument("--force", action="store_true",
                        help="import even if this image was already imported")
    parser.add_argument("--dry-run", action="store_true",
                        help="go through everything but save nothing")
    return parser.parse_args()


def main():
    args = parse_args()
    path = Path(args.json_file)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)
    try:
        result = json.loads(path.read_text())
        if not isinstance(result.get("batters"), list) or not result["batters"]:
            raise ValueError("no 'batters' list")
    except (ValueError, AttributeError) as error:
        print(f"That doesn't look like output from extract_chart.py ({error}).")
        sys.exit(1)

    image = args.image or result.get("source_image")
    if not image:
        print("This file doesn't say which photo it came from. "
              "Add --image path/to/photo.png.")
        sys.exit(1)

    previous = run_query(
        "SELECT chart_id, uploaded_at FROM source_charts WHERE image_path = %s",
        (image,))
    if previous and not args.force:
        print(f"{image} was already imported (chart id {previous[0]['chart_id']}, "
              f"{previous[0]['uploaded_at']}). Importing again would duplicate every "
              f"at-bat. Use --force if you really mean to.")
        sys.exit(1)

    original = copy.deepcopy(result)  # kept as the audit copy; your fixes go on `result`

    review(result)
    game_plan = plan_game(args)

    batter_plans = {}
    by_name = {}
    for number, batter in enumerate(result["batters"], start=1):
        name = (batter.get("name") or "").strip()
        if batter.get("extraction_error"):
            print(f"\nSkipping row {number}: it failed to extract "
                  f"({batter['extraction_error']}).")
            continue
        if not name or name.startswith("ROW "):
            print(f"\nSkipping row {number}: no name was read. "
                  f"Rename it during review ('n {number}') to include it.")
            continue
        if name.lower() not in by_name:
            by_name[name.lower()] = plan_player(name, "hitter")
        batter_plans[number] = by_name[name.lower()]

    total = sum(1 for number, batter in enumerate(result["batters"], start=1)
                if batter_plans.get(number, ("skip",))[0] != "skip"
                for ab in batter.get("at_bats", []) if saveable(ab))
    unread = sum(1 for number, batter in enumerate(result["batters"], start=1)
                 if batter_plans.get(number, ("skip",))[0] != "skip"
                 for ab in batter.get("at_bats", [])
                 if not ab.get("box_blank") and not ab.get("result"))

    print("\n" + "=" * 60)
    print("About to save:")
    if game_plan[0] == "existing":
        print(f"  Game:    existing game {game_plan[1]}")
    else:
        print(f"  Game:    NEW, {game_plan[1]} vs {game_plan[2] or '?'}")
    print("  Pitcher: not recorded")
    for number, batter in enumerate(result["batters"], start=1):
        decision = batter_plans.get(number, ("skip",))
        print(f"  {batter.get('name') or '?':<12} -> {describe_decision(decision)}")
    print(f"  {total} at-bats")
    if unread:
        print(f"  ({unread} boxes have no result and will be left out. "
              f"Go back and fix them if you want them saved.)")
    print("=" * 60)

    if args.dry_run:
        print("[DRY RUN] Nothing was saved.")
        return
    if total == 0:
        print("Nothing to save.")
        return
    if not yes_no("Save all of this to the database?"):
        print("Nothing was saved.")
        return

    try:
        game_id, chart_id, count = save(result, original, game_plan, batter_plans,
                                        image, args.team or None)
    except Exception as error:
        sys.exit(f"\nSomething went wrong, so everything was undone. "
                 f"Nothing was saved.\n  {error}")
    print(f"\nSaved {count} at-bats (game {game_id}, chart {chart_id}). "
          f"Open the app to see them.")


if __name__ == "__main__":
    main()