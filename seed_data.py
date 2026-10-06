
from db import get_connection

# (first, last, role, bats, throws, team)
PLAYERS = [
    ("Jake", "Miller", "pitcher", "R", "R", "Home Team"),
    ("Tom", "Alvarez", "pitcher", "L", "L", "Home Team"),
    ("Sam", "Carter", "hitter", "R", "R", "Opponent"),
    ("Luis", "Ortega", "hitter", "L", "R", "Opponent"),
    ("Ben", "Fisher", "hitter", "S", "R", "Opponent"),
    ("Dan", "Kowalski", "hitter", "R", "R", "Opponent"),
]

# Each pitch: (pitch_type, velocity, location_x, location_y, result)
# Location: 0-1 spans the strike zone; below 0 or above 1 is outside it.
#
# Each at-bat: (pitcher_last, hitter_last, inning, outs_before, result,
#               scored, hit_type, hit_x, hit_y, [pitches])
AT_BATS = [
    ("Miller", "Carter", 1, 0, "K", False, None, None, None, [
        ("FB", 86, 0.50, 0.55, "strike"),
        ("CB", 71, 0.30, -0.15, "ball"),
        ("FB", 87, 0.80, 0.70, "foul"),
        ("SL", 77, 0.45, 0.10, "strike"),
    ]),
    ("Miller", "Ortega", 1, 1, "1B", True, "line_drive", 0.70, 0.55, [
        ("FB", 85, 0.40, 0.45, "strike"),
        ("CH", 76, 0.55, 0.20, "ball"),
        ("FB", 86, 0.60, 0.50, "in_play"),
    ]),
    ("Miller", "Fisher", 1, 1, "BB", False, None, None, None, [
        ("FB", 86, -0.10, 0.50, "ball"),
        ("CB", 70, 0.20, -0.20, "ball"),
        ("FB", 87, 0.50, 0.55, "strike"),
        ("FB", 85, 0.95, 0.90, "ball"),
        ("SL", 76, 0.50, -0.10, "ball"),
    ]),
    ("Miller", "Kowalski", 1, 1, "GO", False, "ground_ball", 0.35, 0.30, [
        ("SL", 77, 0.50, 0.20, "strike"),
        ("FB", 87, 0.45, 0.55, "in_play"),
    ]),
    ("Miller", "Carter", 3, 0, "HR", True, "fly_ball", 0.50, 0.95, [
        ("CH", 77, 0.50, 0.45, "strike"),
        ("FB", 86, 0.55, 0.60, "ball"),
        ("CB", 72, 0.40, 0.30, "in_play"),
    ]),
    ("Alvarez", "Ortega", 5, 0, "K", False, None, None, None, [
        ("FB", 82, 0.50, 0.50, "strike"),
        ("CB", 68, 0.25, -0.10, "ball"),
        ("CB", 69, 0.40, 0.15, "strike"),
        ("FB", 84, 0.60, 0.80, "strike"),
    ]),
    ("Alvarez", "Fisher", 5, 1, "2B", True, "line_drive", 0.15, 0.70, [
        ("FB", 83, 0.60, 0.50, "ball"),
        ("SL", 74, 0.40, 0.35, "strike"),
        ("FB", 84, 0.55, 0.45, "in_play"),
    ]),
    ("Alvarez", "Kowalski", 5, 1, "FO", False, "fly_ball", 0.80, 0.80, [
        ("CH", 74, 0.50, 0.30, "strike"),
        ("FB", 83, 0.70, 0.70, "in_play"),
    ]),
    ("Alvarez", "Carter", 6, 2, "BB", False, None, None, None, [
        ("FB", 83, -0.15, 0.60, "ball"),
        ("CB", 69, 0.30, -0.25, "ball"),
        ("SL", 75, 0.50, 0.40, "strike"),
        ("FB", 84, 1.10, 0.50, "ball"),
        ("CH", 74, 0.45, -0.10, "ball"),
    ]),
    ("Alvarez", "Ortega", 7, 0, "HBP", False, None, None, None, [
        ("CB", 69, 0.50, 0.40, "strike"),
        ("FB", 84, 1.15, 0.50, "hit_by_pitch"),
    ]),
]


def main():
    connection = get_connection()
    cursor = connection.cursor()

    # Safety check so running this twice doesn't create duplicates.
    cursor.execute("SELECT COUNT(*) FROM players")
    if cursor.fetchone()[0] > 0:
        print("The players table already has data. Nothing inserted.")
        connection.close()
        return

    # 1. Players. cursor.lastrowid is the auto-generated player_id of the
    #    row we just inserted; we save it so at-bats can point to it.
    player_ids = {}
    for first, last, role, bats, throws, team in PLAYERS:
        cursor.execute(
            "INSERT INTO players (first_name, last_name, role, bats, throws, team) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (first, last, role, bats, throws, team),
        )
        player_ids[last] = cursor.lastrowid

    # 2. One game.
    cursor.execute(
        "INSERT INTO games (game_date, season, opponent, location, home_away) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("2025-04-12", "2025", "Riverside", "Home Field", "home"),
    )
    game_id = cursor.lastrowid

    # 3. At-bats and their pitches.
    for (pitcher, hitter, inning, outs, result, scored,
         hit_type, hit_x, hit_y, pitches) in AT_BATS:

        cursor.execute(
            "INSERT INTO at_bats (game_id, pitcher_id, hitter_id, inning, "
            "inning_half, outs_before, result, scored, hit_type, "
            "hit_location_x, hit_location_y) "
            "VALUES (%s, %s, %s, %s, 'top', %s, %s, %s, %s, %s, %s)",
            (game_id, player_ids[pitcher], player_ids[hitter], inning, outs,
             result, scored, hit_type, hit_x, hit_y),
        )
        at_bat_id = cursor.lastrowid

        # Walk through the pitches in order, tracking the count as we go.
        balls, strikes = 0, 0
        for number, (ptype, velo, x, y, presult) in enumerate(pitches, start=1):
            cursor.execute(
                "INSERT INTO pitches (at_bat_id, sequence_number, pitch_type, "
                "velocity, location_x, location_y, balls_before, "
                "strikes_before, result) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (at_bat_id, number, ptype, velo, x, y, balls, strikes, presult),
            )
            if presult == "ball":
                balls += 1
            elif presult == "strike":
                strikes += 1
            elif presult == "foul" and strikes < 2:
                strikes += 1  # a foul can't be strike three

        cursor.execute(
            "UPDATE at_bats SET final_balls = %s, final_strikes = %s "
            "WHERE at_bat_id = %s",
            (balls, strikes, at_bat_id),
        )

    # Nothing is saved to the database until you commit.
    connection.commit()
    connection.close()
    print(f"Inserted {len(PLAYERS)} players, 1 game, {len(AT_BATS)} at-bats.")


if __name__ == "__main__":
    main()
