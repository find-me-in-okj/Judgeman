"""
compat.py — Cross-platform compatibility helpers.

Windows uses USERNAME; Unix uses USER.
Both may be absent in sandboxed/CI environments.
get_analyst_id() returns a sensible default on every platform.
"""
import os
import sys


def get_analyst_id(override: str = None) -> str:
    """
    Return the analyst identifier for the current session.

    Priority:
      1. Explicit override (from --analyst flag or request body)
      2. JUDGEMAN_ANALYST env var (useful for CI or shared installs)
      3. USERNAME (Windows) or USER (Unix/macOS)
      4. Fallback: 'analyst'
    """
    if override and override.strip():
        return override.strip()
    if os.environ.get('JUDGEMAN_ANALYST'):
        return os.environ['JUDGEMAN_ANALYST']
    # Windows uses USERNAME; Unix uses USER
    user = os.environ.get('USERNAME') or os.environ.get('USER')
    if user and user.strip():
        return user.strip()
    return 'analyst'


def get_home_dir() -> str:
    """Return the user's home directory as a string, cross-platform."""
    return str(os.path.expanduser('~'))


def is_windows() -> bool:
    return sys.platform.startswith('win')
