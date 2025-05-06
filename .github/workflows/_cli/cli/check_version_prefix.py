# Derived from https://github.com/canonical/data-platform-workflows/blob/v32.0.0/python/cli/data_platform_workflows_cli/check_semantic_version_prefix.py
import os
import re


def check(message: str, /, *, message_type="commit message") -> str:
    """Check that message begins with valid version prefix and return prefix"""
    error_message = f"""{message_type[0].upper() + message_type[1:]} must contain prefix to increment version

See https://github.com/canonical/charm-refresh?tab=readme-ov-file#versioning

An optional scope in parentheses may be included in the prefix. For example: 'breaking(kubernetes):'
Inside the parentheses, these characters are not allowed: `():`

Got invalid {message_type}: {repr(message)}
"""
    match = re.match(r"(?P<prefix>[^():]+)(?:\([^():]+\))?:", message)
    if not match:
        raise ValueError(error_message)
    prefix = match.group("prefix")
    if prefix not in ("REFRESH BREAKING", "breaking", "compatible", "patch"):
        raise ValueError(error_message)
    return prefix


def check_pr_title():
    check(os.environ["TITLE"], message_type="PR title")
