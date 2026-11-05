"""Manage page: fix or remove what is already in the database.

  Charts   see every imported chart, move one to a different game, or delete it
           (along with its at-bats and pitches)
  Players  fix a name, merge a duplicate player, remove players nobody uses
  Games    fix a date or opponent, remove empty games

Every change is made in one transaction after you confirm it, so if something
fails nothing is changed. Deleting cannot be undone.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

from db import get_connection, run_query

st.set_page_config(page_title="Manage charts", layout="wide")
st.title("🛠️ Manage charts, players and games")

ROOT = Path(__file__).resolve().parent.parent
ROLES = ["pitcher", "hitter", "both"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write(steps):
    """Run a list of (sql, params) in ONE transaction. Returns the rows changed by
    each step. If any step fails, everything is undone and the error is raised."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        changed = []
        for sql, params in steps:
            cursor.execute(sql, params)
            changed.append(cursor.rowcount)
        connection.commit()
        return changed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def flash(message):
    """Show a message after the page reloads (a change reruns the whole page)."""
    st.session_state["flash"] = message
    st.rerun()


def attempt(steps, success):
    """Run the steps, then flash `success` (a function of the row counts)."""
    try:
        changed = write(steps)
    except Exception as error:
        st.error(f"Something went wrong, so nothing was changed.\n\n{error}")
        return
    flash(success(changed))


def full_name(row):
    return f"{row['first_name']} {row['last_name']}"


def game_label(row):
    opponent = row.get("opponent") or "?"
    return f"Game {row['game_id']}: {row['game_date']} vs {opponent}"


if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

tab_charts, tab_players, tab_games = st.tabs(["Charts", "Players", "Games"])


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

with tab_charts:
    charts = run_query(
        """
        SELECT c.chart_id, c.chart_type, c.game_id, g.game_date, g.opponent,
               CONCAT(p.first_name, ' ', p.last_name) AS pitcher,
               c.uploaded_at, c.extraction_status, c.image_path,
               (SELECT COUNT(*) FROM at_bats a WHERE a.source_chart_id = c.chart_id) AS at_bats,
               (SELECT COUNT(*) FROM pitches pi JOIN at_bats a ON pi.at_bat_id = a.at_bat_id
                 WHERE a.source_chart_id = c.chart_id) AS pitches
        FROM source_charts c
        LEFT JOIN games g ON c.game_id = g.game_id
        LEFT JOIN players p ON c.pitcher_id = p.player_id
        ORDER BY c.uploaded_at DESC, c.chart_id DESC
        """
    )
    if not charts:
        st.info("No charts have been imported yet.")
    else:
        st.dataframe(
            pd.DataFrame(charts)[["chart_id", "chart_type", "game_date", "opponent", "pitcher",
                                  "at_bats", "pitches", "extraction_status", "uploaded_at"]],
            hide_index=True, width="stretch",
        )

        by_id = {c["chart_id"]: c for c in charts}
        chart_id = st.selectbox(
            "Pick a chart to change", list(by_id),
            format_func=lambda i: f"Chart {i}: {by_id[i]['chart_type']}, "
                                  f"{by_id[i]['game_date']} vs {by_id[i]['opponent'] or '?'}"
                                  + (f", pitcher {by_id[i]['pitcher']}" if by_id[i]["pitcher"] else ""),
        )
        chart = by_id[chart_id]

        left, right = st.columns([1, 2])
        photo = ROOT / chart["image_path"]
        if photo.is_file():
            left.image(str(photo), caption=chart["image_path"], width=260)
        else:
            left.caption(f"Photo not found on this computer: {chart['image_path']}")

        with right:
            st.markdown(f"**{chart['at_bats']} at-bats and {chart['pitches']} pitches** "
                        f"came from this chart.")
            preview = run_query(
                """
                SELECT a.at_bat_id, CONCAT(h.first_name, ' ', h.last_name) AS hitter,
                       CONCAT(p.first_name, ' ', p.last_name) AS pitcher, a.result,
                       a.final_balls, a.final_strikes, a.scored
                FROM at_bats a
                LEFT JOIN players h ON a.hitter_id = h.player_id
                LEFT JOIN players p ON a.pitcher_id = p.player_id
                WHERE a.source_chart_id = %s ORDER BY a.at_bat_id
                """,
                (chart_id,),
            )
            if preview:
                st.dataframe(pd.DataFrame(preview), hide_index=True, width="stretch", height=240)

        games = run_query("SELECT game_id, game_date, opponent FROM games ORDER BY game_date DESC, game_id DESC")
        other_games = {g["game_id"]: g for g in games if g["game_id"] != chart["game_id"]}

        with st.expander("Move this chart to a different game"):
            if not other_games:
                st.write("There is no other game to move it to. Add one by uploading a chart.")
            else:
                target = st.selectbox("Move to", list(other_games),
                                      format_func=lambda i: game_label(other_games[i]),
                                      key="move_target")
                if st.button("Move chart and its at-bats"):
                    attempt([
                        ("UPDATE source_charts SET game_id = %s WHERE chart_id = %s", (target, chart_id)),
                        ("UPDATE at_bats SET game_id = %s WHERE source_chart_id = %s", (target, chart_id)),
                    ], lambda n: f"Moved chart {chart_id} and {n[1]} at-bats to game {target}.")

        with st.expander("Delete this chart", expanded=False):
            st.warning(f"This permanently deletes chart {chart_id}, its {chart['at_bats']} at-bats "
                       f"and {chart['pitches']} pitches. The photo file is kept. This cannot be undone.")
            drop_game = st.checkbox("Also delete the game if nothing else uses it", value=True)
            confirmed = st.checkbox(f"Yes, delete chart {chart_id}", key=f"confirm_{chart_id}")
            if st.button("Delete chart", type="primary", disabled=not confirmed):
                steps = [
                    ("DELETE p FROM pitches p JOIN at_bats a ON p.at_bat_id = a.at_bat_id "
                     "WHERE a.source_chart_id = %s", (chart_id,)),
                    ("DELETE FROM at_bats WHERE source_chart_id = %s", (chart_id,)),
                    ("DELETE FROM source_charts WHERE chart_id = %s", (chart_id,)),
                ]
                if drop_game and chart["game_id"]:
                    steps.append((
                        "DELETE FROM games WHERE game_id = %s "
                        "AND NOT EXISTS (SELECT 1 FROM at_bats WHERE game_id = %s) "
                        "AND NOT EXISTS (SELECT 1 FROM source_charts WHERE game_id = %s)",
                        (chart["game_id"],) * 3))
                attempt(steps, lambda n: f"Deleted chart {chart_id}: {n[1]} at-bats, {n[0]} pitches"
                                         + (f", and {n[3]} game." if len(n) > 3 and n[3] else "."))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

with tab_players:
    players = run_query(
        """
        SELECT p.player_id, p.first_name, p.last_name, p.role, p.bats, p.throws, p.team,
               (SELECT COUNT(*) FROM at_bats a WHERE a.hitter_id = p.player_id) AS as_hitter,
               (SELECT COUNT(*) FROM at_bats a WHERE a.pitcher_id = p.player_id) AS as_pitcher,
               (SELECT COUNT(*) FROM source_charts c WHERE c.pitcher_id = p.player_id) AS charts
        FROM players p ORDER BY p.last_name, p.first_name
        """
    )
    if not players:
        st.info("There are no players yet.")
    else:
        st.dataframe(pd.DataFrame(players), hide_index=True, width="stretch")
        person = {p["player_id"]: p for p in players}

        def label(i):
            p = person[i]
            return f"{p['last_name']}, {p['first_name']} ({p['role']}) #{i}"

        with st.expander("Fix a player's name or details", expanded=True):
            pid = st.selectbox("Player", list(person), format_func=label, key="edit_player")
            p = person[pid]
            with st.form(f"edit_player_{pid}"):
                a, b, c = st.columns(3)
                first = a.text_input("First name", p["first_name"])
                last = b.text_input("Last name", p["last_name"])
                role = c.selectbox("Role", ROLES, index=ROLES.index(p["role"]))
                d, e, f = st.columns(3)
                bats = d.selectbox("Bats", ["", "L", "R", "S"], index=["", "L", "R", "S"].index(p["bats"] or ""))
                throws = e.selectbox("Throws", ["", "L", "R"], index=["", "L", "R"].index(p["throws"] or ""))
                team = f.text_input("Team", p["team"] or "")
                saved = st.form_submit_button("Save changes")
            if saved:
                if not first.strip() or not last.strip():
                    st.error("First and last name can't be empty.")
                else:
                    attempt([(
                        "UPDATE players SET first_name = %s, last_name = %s, role = %s, bats = %s, "
                        "throws = %s, team = %s WHERE player_id = %s",
                        (first.strip(), last.strip(), role, bats or None, throws or None,
                         team.strip() or None, pid))],
                        lambda n: f"Saved {first.strip()} {last.strip()}.")

        with st.expander("Merge a duplicate player (for example 'Suge' into 'Sage')"):
            st.write("Everything recorded under the duplicate moves to the player you keep, "
                     "then the duplicate is deleted.")
            a, b = st.columns(2)
            keep = a.selectbox("Keep this player", list(person), format_func=label, key="merge_keep")
            dupe = b.selectbox("Merge this duplicate into them", list(person), format_func=label,
                               key="merge_dupe")
            if keep == dupe:
                st.info("Pick two different players.")
            else:
                d = person[dupe]
                st.write(f"{full_name(d)} has {d['as_hitter']} at-bats as a hitter, "
                         f"{d['as_pitcher']} as a pitcher and {d['charts']} pitching chart(s). "
                         f"All of that moves to {full_name(person[keep])}.")
                if st.checkbox(f"Yes, merge #{dupe} into #{keep} and delete #{dupe}", key="merge_ok") \
                        and st.button("Merge players", type="primary"):
                    roles = {person[keep]["role"], d["role"]}
                    new_role = "both" if len(roles) > 1 else roles.pop()
                    attempt([
                        ("UPDATE at_bats SET hitter_id = %s WHERE hitter_id = %s", (keep, dupe)),
                        ("UPDATE at_bats SET pitcher_id = %s WHERE pitcher_id = %s", (keep, dupe)),
                        ("UPDATE source_charts SET pitcher_id = %s WHERE pitcher_id = %s", (keep, dupe)),
                        ("UPDATE players SET role = %s WHERE player_id = %s", (new_role, keep)),
                        ("DELETE FROM players WHERE player_id = %s", (dupe,)),
                    ], lambda n: f"Merged {full_name(d)} into {full_name(person[keep])} "
                                 f"({n[0] + n[1]} at-bats moved).")

        unused = [p for p in players if not (p["as_hitter"] or p["as_pitcher"] or p["charts"])]
        with st.expander(f"Remove players nobody uses ({len(unused)})"):
            if not unused:
                st.write("Every player has at least one at-bat or chart.")
            else:
                st.write("These players have no at-bats and no charts: "
                         + ", ".join(full_name(p) for p in unused))
                if st.checkbox("Yes, delete all of them", key="unused_ok") \
                        and st.button("Delete unused players", type="primary"):
                    attempt([("DELETE FROM players WHERE player_id = %s", (p["player_id"],)) for p in unused],
                            lambda n: f"Deleted {sum(n)} unused player(s).")


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

with tab_games:
    games = run_query(
        """
        SELECT g.game_id, g.game_date, g.opponent, g.location, g.home_away,
               (SELECT COUNT(*) FROM source_charts c WHERE c.game_id = g.game_id) AS charts,
               (SELECT COUNT(*) FROM at_bats a WHERE a.game_id = g.game_id) AS at_bats
        FROM games g ORDER BY g.game_date DESC, g.game_id DESC
        """
    )
    if not games:
        st.info("There are no games yet.")
    else:
        st.dataframe(pd.DataFrame(games), hide_index=True, width="stretch")
        game = {g["game_id"]: g for g in games}

        with st.expander("Fix a game's date, opponent or location", expanded=True):
            gid = st.selectbox("Game", list(game), format_func=lambda i: game_label(game[i]), key="edit_game")
            g = game[gid]
            with st.form(f"edit_game_{gid}"):
                a, b = st.columns(2)
                when = a.date_input("Date", g["game_date"])
                opponent = b.text_input("Opponent", g["opponent"] or "")
                c, d = st.columns(2)
                location = c.text_input("Location", g["location"] or "")
                sides = ["", "home", "away", "neutral"]
                side = d.selectbox("Home / away", sides, index=sides.index(g["home_away"] or ""))
                saved = st.form_submit_button("Save changes")
            if saved:
                attempt([(
                    "UPDATE games SET game_date = %s, season = %s, opponent = %s, location = %s, "
                    "home_away = %s WHERE game_id = %s",
                    (when, str(when.year), opponent.strip() or None, location.strip() or None,
                     side or None, gid))],
                    lambda n: f"Saved game {gid}.")

        empty = [g for g in games if not (g["charts"] or g["at_bats"])]
        with st.expander(f"Remove empty games ({len(empty)})"):
            if not empty:
                st.write("Every game has at least one chart or at-bat.")
            else:
                st.write("These games have no charts and no at-bats: "
                         + ", ".join(game_label(g) for g in empty))
                if st.checkbox("Yes, delete all of them", key="empty_ok") \
                        and st.button("Delete empty games", type="primary"):
                    attempt([("DELETE FROM games WHERE game_id = %s", (g["game_id"],)) for g in empty],
                            lambda n: f"Deleted {sum(n)} empty game(s).")