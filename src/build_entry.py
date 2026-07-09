"""PyInstaller entry point — explicit imports so the bundler sees all modules.

Importujemy REALNE moduły (core.*, plugins.*) — top-level *.py to compat-shimy
robiące `from .core... import *`, które padają w .exe (frozen = brak parent
package: "attempted relative import with no known parent package"). Entry =
core.app.main; shimów tu nie tykamy.
"""

import os
import sys

if getattr(sys, "frozen", False):
    sys.path.insert(0, sys._MEIPASS)
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.api_client  # noqa: F401
import core.app  # noqa: F401
import core.auto_discover  # noqa: F401
import core.config  # noqa: F401
import core.crypto  # noqa: F401
import core.keys  # noqa: F401
import core.plugin_base  # noqa: F401
import core.plugin_loader  # noqa: F401
import core.plugin_manifest  # noqa: F401
import core.tray  # noqa: F401
import core.updater  # noqa: F401
import plugins.dicom.plugin  # noqa: F401
import plugins.skyla.plugin  # noqa: F401
import setup_wizard  # noqa: F401

from core.app import main

main()
