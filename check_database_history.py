import sqlite3
from contextlib import closing
from pathlib import Path


DATABASE_PATH = Path(__file__).with_name("packing_verification.db")


def main() -> None:
    database_uri = f"{DATABASE_PATH.as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row

        table_rows = connection.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
        """).fetchall()
        print("Tables:")
        for row in table_rows:
            print(f"- {row['name']}")

        for table_name in ("scan_events", "verification_events"):
            columns = connection.execute(
                f"PRAGMA table_info({table_name})"
            ).fetchall()
            print(f"\n{table_name} columns:")
            for column in columns:
                print(f"- {column['name']}")

        scan_events = connection.execute("""
            SELECT *
            FROM scan_events
            ORDER BY scan_event_id
        """).fetchall()
        print("\nAll scan event records:")
        if scan_events:
            for event in scan_events:
                print(dict(event))
        else:
            print("(none)")

        verification_events = connection.execute("""
            SELECT *
            FROM verification_events
            ORDER BY event_id DESC
            LIMIT 10
        """).fetchall()
        print("\nLatest verification event records (up to 10):")
        if verification_events:
            for event in verification_events:
                print(dict(event))
        else:
            print("(none)")


if __name__ == "__main__":
    main()
