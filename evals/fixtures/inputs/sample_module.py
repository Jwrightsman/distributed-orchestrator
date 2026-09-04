"""A small module used as a fixture for the static-analysis eval items."""
import json
import os
from collections import Counter


def load(path):
    """Read a JSON document from disk."""
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def tally(words):
    # TODO: this should stream rather than building the whole list in memory
    return Counter(words)


def describe(record):
    """Render one record as a single line."""
    return f"{record.get('name', 'unknown')} in {os.path.basename(record.get('path', ''))} with a deliberately long trailing comment to push this line past seventy-nine columns"


def main():
    # TODO: accept a path on the command line
    print(describe({'name': 'demo', 'path': '/tmp/demo.json'}))
