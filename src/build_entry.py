"""PyInstaller entry point — explicit imports so the bundler sees all modules."""

import os
import sys

if getattr(sys, "frozen", False):
    sys.path.insert(0, sys._MEIPASS)
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent  # noqa: F401
import auto_discover  # noqa: F401
import config  # noqa: F401
import core.app  # noqa: F401
import core.config  # noqa: F401
import core.plugin_base  # noqa: F401
import core.plugin_loader  # noqa: F401
import core.tray  # noqa: F401
import hl7_listener  # noqa: F401
import hl7_parser  # noqa: F401
import plugins.skyla.plugin  # noqa: F401
import plugins.dicom.plugin  # noqa: F401
import setup_wizard  # noqa: F401
import tray  # noqa: F401
import vetflow_client  # noqa: F401
import xml_builder  # noqa: F401

from agent import main

main()
