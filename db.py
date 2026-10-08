"""Shared database helpers.

Every other file imports from here, so the connection details live in
exactly one place. Change your password here and nowhere else.
"""

from decimal import Decimal

import pymysql

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "Cmsc250!",
    "database": "baseball_charting",
}


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def run_query(sql, params=None):
    """Run a SELECT and return the rows as a list of dictionaries.

    Always pass values through `params` (they replace the %s markers in the
    SQL) instead of pasting them into the SQL string yourself. That keeps
    the database safe from broken or malicious input.
    """
    connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    finally:
        connection.close()

    # MySQL hands back DECIMAL columns as Decimal objects; plain floats are
    # easier to chart and display.
    for row in rows:
        for key, value in row.items():
            if isinstance(value, Decimal):
                row[key] = float(value)
    return rows
