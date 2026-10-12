"""The board: a live database and dashboard built from the tape.

board.indexer follows the raw tape files the recorders write, rebuilds every order book, joins
Pinnacle fair values and keeps data/board.db current. board.server serves the dashboard and a JSON
API from that database. Neither touches the recorders, and the database can always be rebuilt
from the tape (python -m board.indexer --rebuild).
"""
