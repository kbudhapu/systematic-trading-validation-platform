"""
Use the OS certificate store for HTTPS on Windows.

Conda/miniconda Python often fails SSL verification with
CERTIFICATE_VERIFY_FAILED; importing this before requests/alpaca-py fixes it.
"""

from __future__ import annotations

import sys


def install_ssl_certificates() -> None:
    """Patch requests/urllib3 to trust the system CA bundle when needed."""
    if sys.platform == "win32":
        try:
            import pip_system_certs.wrapt_requests  # noqa: F401
        except ImportError:
            pass
