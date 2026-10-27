"""`orbitrows setup` says each part that isn't ready, never "Ready." over a failure; a message waits for the session's
preload (no model loaded twice). Nothing real is downloaded or started."""
import time

from orbitrows.services import ready


def test_setup_reports_each_part_not_ready_and_a_message_waits_for_the_preload(monkeypatch):
    monkeypatch.setattr(ready.duckling, "setup", lambda: "Docker is not installed")
    monkeypatch.setattr(ready, "corpipe", lambda say: None)
    monkeypatch.setattr(ready, "models", lambda: (_ for _ in ()).throw(OSError("disk full")))
    said = []
    failed = ready.setup(said.append)
    assert [f.split(":")[0] for f in failed] == ["Duckling (dates)", "Models"] and "CorPipe (references): ready" in said

    loaded = []
    monkeypatch.setattr(ready, "models", lambda: (time.sleep(0.2), loaded.append(1)))
    monkeypatch.setattr(ready, "_preload", [])
    ready.preload()
    ready.wait()
    assert loaded == [1]
