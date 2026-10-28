"""Application state: who the user is, and the database that remembers their work.

Market data lives in Parquet and journals in JSONL; everything else the app has
to remember (strategies and their versions, runs, jobs, conversations, app
sessions, preferences) lives in one SQLite database, reached through this
package.
"""
