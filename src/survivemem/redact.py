# SPDX-License-Identifier: GPL-3.0-or-later
"""Obvious token/key patterns. Memories containing them are rejected, excerpts redacted."""

import re

_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{36,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{50,}"
    r"|\bglpat-[A-Za-z0-9_\-]{20,}"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_\-]{35}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|(?i:\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
    r"\s*[:=]\s*['\"]?(?![$<{%])[^\s'\"]{8,})"
)


def contains_secret(text: str) -> bool:
    return _SECRET_RE.search(text) is not None


def redact(text: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", text)
