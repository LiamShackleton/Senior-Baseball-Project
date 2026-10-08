
import pandas as pd
import streamlit as st

from db import run_query

st.set_page_config(page_title="Baseball Charting", layout="wide")
st.title("Baseball Charting")


# ---------------------------------------------------------------------------
# Stat blocks. Each function takes a player_id, runs a query, and draws
# something on the page. Keeping them separate makes it easy to add more.
# ---------------------------------------------------------------------------

# ----- Hitter stats -----

def hitting_summary(pid):
    row = run_query(
        """
        SELECT COUNT(*)                            AS pa,
               SUM(result IN ('1B','2B','3B','HR')) AS hits,
               SUM(result = 'HR')                   AS hr,
               SUM(result = 'K')                    AS strikeouts,
               SUM(result IN ('BB','HBP'))          AS walks,
               SUM(scored)                          AS runs
        FROM at_bats
        WHERE hitter_id = %s
        """,
        (pid,),
    )[0]

    metrics = [
        ("Plate appearances", "pa"),
        ("Hits", "hits"),
        ("Home runs", "hr"),
        ("Strikeouts", "strikeouts"),
        ("Walks / HBP", "walks"),
        ("Runs scored", "runs"),
    ]
    columns = st.columns(len(metrics))
    for column, (label, key) in zip(columns, metrics):
        column.metric(label, int(row[key] or 0))


def hitter_results(pid):
    rows = run_query(
        """
        SELECT result, COUNT(*) AS total
        FROM at_bats
        WHERE hitter_id = %s
        GROUP BY result
        ORDER BY total DESC
        """,
        (pid,),
    )
    if not rows:
        st.write("No at-bats recorded.")
        return
    df = pd.DataFrame(rows).set_index("result")
    st.bar_chart(df["total"])


def pitches_seen(pid):
    rows = run_query(
        """
        SELECT p.pitch_type, COUNT(*) AS seen
        FROM pitches p
        JOIN at_bats a ON p.at_bat_id = a.at_bat_id
        WHERE a.hitter_id = %s AND p.pitch_type IS NOT NULL
        GROUP BY p.pitch_type
        ORDER BY seen DESC
        """,
        (pid,),
    )
    if not rows:
        st.write("No pitch-type data recorded.")
        return
    df = pd.DataFrame(rows).set_index("pitch_type")
    st.bar_chart(df["seen"])


def hitter_log(pid):
    rows = run_query(
        """
        SELECT g.game_date, a.inning, a.outs_before,
               CONCAT(pl.first_name, ' ', pl.last_name) AS pitcher,
               a.result, a.scored, a.final_balls, a.final_strikes
        FROM at_bats a
        JOIN games g    ON a.game_id = g.game_id
        JOIN players pl ON a.pitcher_id = pl.player_id
        WHERE a.hitter_id = %s
        ORDER BY g.game_date, a.inning, a.at_bat_id
        """,
        (pid,),
    )
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


# ----- Pitcher stats -----

def pitching_summary(pid):
    ab = run_query(
        """
        SELECT COUNT(*)                            AS faced,
               SUM(result = 'K')                    AS strikeouts,
               SUM(result IN ('BB','HBP'))          AS walks,
               SUM(result IN ('1B','2B','3B','HR')) AS hits_allowed
        FROM at_bats
        WHERE pitcher_id = %s
        """,
        (pid,),
    )[0]
    pc = run_query(
        """
        SELECT COUNT(*) AS thrown
        FROM pitches p
        JOIN at_bats a ON p.at_bat_id = a.at_bat_id
        WHERE a.pitcher_id = %s
        """,
        (pid,),
    )[0]

    metrics = [
        ("Batters faced", ab["faced"]),
        ("Pitches thrown", pc["thrown"]),
        ("Strikeouts", ab["strikeouts"]),
        ("Walks / HBP", ab["walks"]),
        ("Hits allowed", ab["hits_allowed"]),
    ]
    columns = st.columns(len(metrics))
    for column, (label, value) in zip(columns, metrics):
        column.metric(label, int(value or 0))


def pitch_mix(pid):
    rows = run_query(
        """
        SELECT p.pitch_type,
               COUNT(*)                 AS thrown,
               ROUND(AVG(p.velocity),1) AS avg_velo,
               MAX(p.velocity)          AS max_velo
        FROM pitches p
        JOIN at_bats a ON p.at_bat_id = a.at_bat_id
        WHERE a.pitcher_id = %s AND p.pitch_type IS NOT NULL
        GROUP BY p.pitch_type
        ORDER BY thrown DESC
        """,
        (pid,),
    )
    if not rows:
        st.write("No pitch data recorded.")
        return
    df = pd.DataFrame(rows).set_index("pitch_type")
    left, right = st.columns(2)
    left.caption("Pitches thrown by type")
    left.bar_chart(df["thrown"])
    right.caption("Velocity by type (mph)")
    right.dataframe(df[["avg_velo", "max_velo"]], use_container_width=True)


def pitcher_results(pid):
    rows = run_query(
        """
        SELECT result, COUNT(*) AS total
        FROM at_bats
        WHERE pitcher_id = %s
        GROUP BY result
        ORDER BY total DESC
        """,
        (pid,),
    )
    if not rows:
        st.write("No at-bats recorded.")
        return
    df = pd.DataFrame(rows).set_index("result")
    st.bar_chart(df["total"])


def pitcher_log(pid):
    rows = run_query(
        """
        SELECT g.game_date, a.inning,
               CONCAT(h.first_name, ' ', h.last_name) AS hitter,
               p.sequence_number AS pitch_num, p.pitch_type, p.velocity,
               p.location_x, p.location_y,
               p.balls_before, p.strikes_before, p.result
        FROM pitches p
        JOIN at_bats a ON p.at_bat_id = a.at_bat_id
        JOIN games g   ON a.game_id = g.game_id
        JOIN players h ON a.hitter_id = h.player_id
        WHERE a.pitcher_id = %s
        ORDER BY g.game_date, a.inning, a.at_bat_id, p.sequence_number
        """,
        (pid,),
    )
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


# Label shown on the checkbox -> function that draws it.
HITTER_STATS = {
    "Hitting summary": hitting_summary,
    "At-bat results": hitter_results,
    "Pitch types seen": pitches_seen,
    "At-bat log": hitter_log,
}
PITCHER_STATS = {
    "Pitching summary": pitching_summary,
    "Pitch mix & velocity": pitch_mix,
    "Results allowed": pitcher_results,
    "Pitch-by-pitch log": pitcher_log,
}


# ---------------------------------------------------------------------------
# Sidebar: search box + player picker  (plan steps 2a and 2b)
# ---------------------------------------------------------------------------
st.sidebar.header("Find a player")
search = st.sidebar.text_input("Search by name")

players = run_query(
    """
    SELECT player_id, first_name, last_name, role, bats, throws, team
    FROM players
    WHERE CONCAT(first_name, ' ', last_name) LIKE %s
    ORDER BY last_name, first_name
    """,
    (f"%{search}%",),
)

if not players:
    st.info("No players match that search.")
    st.stop()

labels = {
    p["player_id"]: f'{p["last_name"]}, {p["first_name"]} ({p["role"]})'
    for p in players
}
player_id = st.sidebar.selectbox(
    "Select a player",
    options=list(labels.keys()),
    format_func=lambda pid: labels[pid],
)
player = next(p for p in players if p["player_id"] == player_id)

# ---------------------------------------------------------------------------
# Sidebar: which stats to show  (plan step 2c)
# ---------------------------------------------------------------------------
chosen = []  # list of (title, function) for every ticked checkbox

if player["role"] in ("hitter", "both"):
    st.sidebar.subheader("Hitting stats")
    for label, func in HITTER_STATS.items():
        if st.sidebar.checkbox(label, value=True, key=f"hit_{label}"):
            chosen.append((f"Hitting: {label}", func))

if player["role"] in ("pitcher", "both"):
    st.sidebar.subheader("Pitching stats")
    for label, func in PITCHER_STATS.items():
        if st.sidebar.checkbox(label, value=True, key=f"pit_{label}"):
            chosen.append((f"Pitching: {label}", func))

# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------
st.header(f'{player["first_name"]} {player["last_name"]}')
st.caption(
    f'{player["role"].title()} · {player["team"] or "No team"} · '
    f'Bats {player["bats"] or "?"} · Throws {player["throws"] or "?"}'
)

if not chosen:
    st.write("Tick at least one stat in the sidebar.")

for title, func in chosen:
    st.subheader(title)
    func(player_id)
