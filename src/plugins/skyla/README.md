# Skyla Plugin

Bundled plugin for Skyla analyzers used by VetFlowConnect.

Implements:

- TCP listener (default port 12221) — one port for both protocols; the protocol is detected from the
  first byte of each connection (`<ENQ>` = ASTM, otherwise MLLP/HL7)
- HL7 ORU^R01 parsing for VM100 and Tutti
- ASTM E1381/E1394 for Solution (chemistry) and Tutti in ASTM mode (VETFL-967): ENQ/ACK, frames with
  checksum (NAK on mismatch), only markers go to VetFlow (analyzer QC records stay in the raw capture)
- Lab result upload to VetFlow device endpoints
- Captured raw HL7/ASTM payloads (`captured_raw/hl7_*`, `captured_raw/astm_*`) and analyzer images for diagnostics

Analyzer setup: in the analyzer's LIS settings set host = IP of the PC running VetFlowConnect, port 12221,
protocol ASTM (Solution) or HL7 (VM100/Tutti). The analyzer is the TCP client.
