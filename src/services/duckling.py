"""Duckling, the service the date reader (units/datephrase) reads with. A service already answering at its address
(DATEPHRASE_DUCKLING_URL, else 127.0.0.1:8000) is used as it is; otherwise the CLI runs its own Docker container
(orbitrows-duckling): created on first use, started when a session starts, its image pulled by `orbitrows setup` so a
session never waits on a download. Left running when a session ends (other sessions read with it too).

    start()  None when Duckling answers (started if needed), else why it can't: the requests with dates are then
             refused with the reader's own message, the others run
    setup()  the image pulled, then start()
"""
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from orbitrows.units.datephrase.reader import DUCKLING_URL

IMAGE = "rasa/duckling:latest"
NAME = "orbitrows-duckling"
WAIT = 30                                                     # seconds for a started container to answer


def up() -> bool:
    try:
        urllib.request.urlopen(DUCKLING_URL, urllib.parse.urlencode({"locale": "en_US", "text": "today"}).encode(),
                               timeout=2).read()
        return True
    except urllib.error.HTTPError:                            # it answered, even with an error: it is up
        return True
    except OSError:
        return False


def docker(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def start() -> str | None:
    if up():
        return None
    if not shutil.which("docker"):
        return ("Docker is not installed, so Duckling (the date reader's service) can't run: install Docker, or run "
                "a Duckling service and set DATEPHRASE_DUCKLING_URL")
    url = urllib.parse.urlparse(DUCKLING_URL)
    if docker("start", NAME).returncode:                      # not created yet: created (pulled first if needed)
        made = docker("run", "-d", "--name", NAME, "-p", f"{url.hostname}:{url.port or 8000}:8000", IMAGE,
                      timeout=900)
        if made.returncode:
            return f"Duckling couldn't be started: {made.stderr.strip()[-300:]}"
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if up():
            return None
        time.sleep(0.5)
    return f"Duckling was started but didn't answer within {WAIT} s"


def setup() -> str | None:
    if not up() and shutil.which("docker"):
        pulled = docker("pull", IMAGE, timeout=900)
        if pulled.returncode:
            return f"Duckling's image couldn't be downloaded: {pulled.stderr.strip()[-300:]}"
    return start()
