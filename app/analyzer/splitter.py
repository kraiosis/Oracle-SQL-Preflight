# ---------------------------------------------------------------------
# Oracle SQL Preflight
# Author:  Federico Guzman  (github.com/kraiosis)
# Website: https://fedeguzman.com    Blog: https://weblantropia.com
#
# Built with AI assistance from Claude (Anthropic). The analyzer's
# runtime behavior stays deterministic and AI-free -- see README.md,
# "Author & Credits", for what the AI assistance covers.
# ---------------------------------------------------------------------
"""
Oracle SQL Preflight Analyzer - Statement Splitter

scope.md section 3 (Phase 1 "Planned expansion") lists, among others:

    PL/SQL block parsing
    stored procedure calls
    dynamic SQL warnings
    multi-statement scripts

SQLGlot (the SQL parser this project builds on) parses plain SQL, not
PL/SQL. Left to its own multi-statement splitting, it cuts a script at
every top-level semicolon -- correct for ordinary SQL, but wrong for a
PL/SQL block: a semicolon inside a BEGIN...END body is not a statement
boundary. Handed a script with a CREATE PROCEDURE/FUNCTION/PACKAGE/
TRIGGER body, or an anonymous DECLARE/BEGIN block, SQLGlot's own
splitting fragments that body at its own internal semicolons and
produces garbage trailing "statements" (verified: a bare `END` after the
body gets parsed on its own as a Column reference).

This module splits a script into statement chunks itself, BEFORE
handing anything to SQLGlot, so the engine can treat a PL/SQL-shaped
statement as one opaque unit instead. It recognizes such a statement by
its leading keyword (DECLARE, BEGIN, or CREATE [OR REPLACE]
PROCEDURE/FUNCTION/PACKAGE [BODY]/TRIGGER) and, for those only, looks
for the conventional SQL*Plus/SQLcl block terminator: a "/" alone on
its own line -- exactly how real DBA scripts are actually written and
run -- instead of the next semicolon. Everything else is still split at
top-level semicolons, skipping over string/quoted-identifier literals
and line/block comments so a semicolon or keyword inside one of those
is never mistaken for real syntax.

If a PL/SQL-shaped statement has no "/" terminator before the end of
the script, the rest of the script is treated as part of that one
block (Chunk.had_slash_terminator is False) -- swallowing too much once
is safer than guessing wrong and fragmenting a body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PLSQL_START_RE = re.compile(
    r"^\s*(?:DECLARE\b|BEGIN\b|CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:PROCEDURE|FUNCTION|PACKAGE\s+BODY|PACKAGE|TRIGGER)\b)",
    re.IGNORECASE,
)

_SLASH_TERMINATOR_RE = re.compile(r"^[ \t]*/[ \t]*$", re.MULTILINE)

# How far into a statement to look when testing whether it opens a
# PL/SQL block. Long enough for "CREATE OR REPLACE PACKAGE BODY " (32
# chars) plus a comfortable margin; the regex is anchored at the start,
# so a longer window never causes a false match further into the text.
_SNIFF_WINDOW = 80


@dataclass
class Chunk:
    sql: str                    # statement text, terminator excluded
    is_plsql: bool               # recognized as a PL/SQL-shaped statement
    had_slash_terminator: bool   # True if closed by a "/" line, not EOF fallback


def scan_to_char(text: str, start: int, stop_char: str) -> tuple[int, bool]:
    """Scan forward from `start`, skipping over '...'-quoted string
    literals (with '' as an escaped quote), "..."-quoted identifiers,
    -- line comments, and /* block */ comments, and return the index of
    the first top-level occurrence of `stop_char`, or (len(text), False)
    if it never occurs outside of one of those.
    """
    n = len(text)
    i = start
    while i < n:
        c = text[i]
        if c == "'":
            i += 1
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 1
            i += 1
            continue
        if c == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = j + 2 if j != -1 else n
            continue
        if c == stop_char:
            return i, True
        i += 1
    return n, False


def split_statements(script: str) -> list[Chunk]:
    """Split a script into Chunks in source order. Empty/whitespace-only
    segments (e.g. a trailing semicolon with nothing after it) are
    dropped rather than returned as empty chunks.
    """
    chunks: list[Chunk] = []
    n = len(script)
    i = 0

    while True:
        while i < n and script[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        start = i

        if _PLSQL_START_RE.match(script[i:i + _SNIFF_WINDOW]):
            m = _SLASH_TERMINATOR_RE.search(script, i)
            end = m.start() if m else n
            text = script[start:end].rstrip()
            if text:
                chunks.append(Chunk(sql=text, is_plsql=True,
                                     had_slash_terminator=m is not None))
            if m is None:
                i = n
            else:
                i = m.end()
                if i < n and script[i] == "\n":
                    i += 1
        else:
            end, found = scan_to_char(script, i, ";")
            text = script[start:end].rstrip()
            if text:
                chunks.append(Chunk(sql=text, is_plsql=False,
                                     had_slash_terminator=False))
            i = end + 1 if found else end

    return chunks


_PLSQL_KIND_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?P<kind>PROCEDURE|FUNCTION|PACKAGE\s+BODY|PACKAGE|TRIGGER)\b",
    re.IGNORECASE,
)


def plsql_statement_kind(text: str) -> str:
    """A short, human-readable label for a PL/SQL-shaped chunk's kind --
    used as its `statement_type` in the analysis report."""
    m = _PLSQL_KIND_RE.match(text[:_SNIFF_WINDOW])
    if m:
        return " ".join(m.group("kind").upper().split())
    if re.match(r"^\s*DECLARE\b", text[:20], re.IGNORECASE):
        return "PL/SQL BLOCK (DECLARE)"
    return "PL/SQL BLOCK"
