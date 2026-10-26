"""Readable SQL for people: one clause per line, one selected column per line, subqueries indented.

Display only (approval cards, Sources, the replay script); the SQL that runs is never changed.
"""
from __future__ import annotations

import re

_TOKEN = re.compile(r"'(?:[^']|'')*'|\"[^\"]*\"|--[^\n]*|\s+|[(),]|[<>=!]+|[^\s(),'<>=!]+")
_CLAUSES = ["GROUP BY", "ORDER BY", "LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "FULL JOIN", "CROSS JOIN", "UNION ALL",
            "SELECT", "FROM", "WHERE", "HAVING", "LIMIT", "JOIN", "UNION", "WITH", "ON"]
_IN_LIST = {"SELECT", "GROUP BY", "ORDER BY"}          # clauses whose commas start a new line


def format_sql(sql: str, indent: str = "    ") -> str:
    words = [t for t in _TOKEN.findall(sql.strip().rstrip(";")) if not t.isspace()]
    lines: list[str] = []
    line, depth, clause, stack, between = "", 0, "", [], False

    def newline(level: int, text: str = "") -> None:
        nonlocal line
        if line.strip():
            lines.append(line.rstrip())
        line = indent * level + text

    i = 0
    while i < len(words):
        w, upper = words[i], words[i].upper()
        two = f"{upper} {words[i + 1].upper()}" if i + 1 < len(words) else ""
        keyword = two if two in _CLAUSES else upper if upper in _CLAUSES else ""
        if keyword:
            text = " ".join(words[i:i + len(keyword.split())])
            i += len(keyword.split())
            if keyword == "ON":
                line += f" {text}"
            else:
                newline(depth, text)
            clause = keyword
            if keyword in _IN_LIST:
                newline(depth + 1)
            continue
        if upper == "BETWEEN":
            between = True
        if upper == "AND" and between:                          # BETWEEN x AND y stays on one line
            between = False
            line += f" {w}"
        elif upper == "AND" and clause in ("WHERE", "HAVING", "ON") and not stack[-1:] == ["("]:
            newline(depth + 1, w)
        elif w == "(":
            nxt = words[i + 1].upper() if i + 1 < len(words) else ""
            if nxt in ("SELECT", "WITH"):                        # subquery: indent one level
                line += " (" if line.strip() else "("
                stack.append("sub")
                depth += 1
            else:
                prev = words[i - 1].upper() if i else ""
                spaced = prev in ("IN", "EXISTS", "AND", "OR", "NOT", "AS", "ON") or prev in ("=", "<>", "<", ">")
                line += " (" if spaced and not line.endswith((" ", "(")) else "("
                stack.append("(")
        elif w == ")":
            kind = stack.pop() if stack else "("
            if kind == "sub":
                depth -= 1
                newline(depth, ")")
            else:
                line = line.rstrip() + ")"
        elif w == ",":
            line = line.rstrip() + ","
            if clause in _IN_LIST and (not stack or stack[-1] == "sub"):
                newline(depth + 1)
        else:
            sep = "" if (not line.strip() or line.endswith(("(", " "))) else " "
            line += sep + w
        i += 1
    newline(0)
    return "\n".join(lines)
