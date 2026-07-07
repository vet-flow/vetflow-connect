"""VETFL-658: autostart Windows Run key — no-op poza .exe/Windows, rejestracja w frozen-Windows."""
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from core.autostart import ensure_autostart, _is_frozen_windows  # noqa: E402


def test_no_op_on_non_windows(monkeypatch):
    # dev/Linux: nie rusza rejestru, nie wywala (brak importu winreg)
    monkeypatch.setattr(sys, "platform", "linux")
    assert _is_frozen_windows() is False
    ensure_autostart()  # nie powinno rzucić


def test_no_op_on_windows_when_not_frozen(monkeypatch):
    # python src na Windows (dev) — też no-op (tylko zbudowany .exe rejestruje)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert _is_frozen_windows() is False
    ensure_autostart()


def test_frozen_windows_registers_run_key(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\VF\vetflow-connect.exe", raising=False)
    calls = {}

    class FakeKey:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("winreg")
    fake.HKEY_CURRENT_USER = 0
    fake.REG_SZ = 1
    fake.CreateKey = lambda root, path: FakeKey()
    def _qv(key, name): raise FileNotFoundError
    fake.QueryValueEx = _qv
    fake.SetValueEx = lambda key, name, r, typ, val: calls.__setitem__("set", (name, val))
    monkeypatch.setitem(sys.modules, "winreg", fake)

    ensure_autostart()
    assert calls["set"][0] == "VetFlowConnect"
    assert "vetflow-connect.exe" in calls["set"][1]
