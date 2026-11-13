"""Check how VibePod reads and forwards Windows console keys (issue #242).

Run it on Windows from a checkout of this branch, inside the console you want
to test (Windows Terminal, the classic console host, ...). The pixi manifest next
to this file provides Python and the few packages it imports::

    cd scripts/windows_console
    pixi shell
    python console_keys.py keys      # no container runtime needed
    python console_keys.py attach    # needs Docker or Podman

(``pixi run keys`` / ``pixi run attach`` do the same without a shell.)

``keys`` prints every console key record (``ReadConsoleInputW``) with the bytes
``_ConsoleKeyDecoder`` turns it into, i.e. what an attached agent would receive.

``attach`` starts a throwaway ``alpine`` container whose TTY prints the hex of
every byte it receives and attaches to it with ``DockerManager.attach_interactive``,
the code path ``vp run`` uses, so the end-to-end result can be compared with what
``keys`` predicts.

Ctrl+D quits both modes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from vibepod.core import docker as docker_mod  # noqa: E402

_STATE_NAMES = [
    (0x0001, "RAlt"),
    (0x0002, "LAlt"),
    (0x0004, "RCtrl"),
    (0x0008, "LCtrl"),
    (0x0010, "Shift"),
    (0x0020, "Num"),
    (0x0040, "Scroll"),
    (0x0080, "Caps"),
    (0x0100, "Enh"),
]

_QUIT = 0x04  # Ctrl+D

#: The container side: print the hex of every byte the TTY receives, one key
#: burst per spawned dd/od pair, a line break after Enter; Ctrl+D ends it.
_HEX_ECHO = r"""
stty raw -echo
printf 'Connected. Every byte the container receives is shown in hex. Ctrl+D quits.\r\n'
while :; do
  b=$(dd bs=1 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
  [ -z "$b" ] || [ "$b" = 04 ] && break
  printf '%s ' "$b"
  [ "$b" = 0d ] && printf '\r\n'
done
printf '\r\nbye\r\n'
"""


def _state(state: int) -> str:
    return "|".join(name for bit, name in _STATE_NAMES if state & bit) or "-"


def _char(code: int) -> str:
    if not code:
        return "-"
    if code < 0x20 or code == 0x7F:
        return f"U+{code:04X} ^{chr(code ^ 0x40)}"
    if 0xD800 <= code <= 0xDFFF:
        return f"U+{code:04X} (surrogate)"
    return f"U+{code:04X} {chr(code)!r}"


def keys() -> int:
    kernel32 = docker_mod._kernel32()
    if kernel32 is None:
        print("This mode needs a Windows console.")
        return 1
    old_mode = docker_mod._enter_console_raw_input()
    if old_mode is None:
        print("stdin is not a console window (or its mode cannot be changed).")
        return 1
    handle = docker_mod._console_input_handle(kernel32)
    decoder = docker_mod._ConsoleKeyDecoder()
    grace_ms = int(docker_mod._ESCAPE_SEQUENCE_GRACE_SECONDS * 1000)
    raw_mode = docker_mod._get_console_input_mode(kernel32) or 0
    print(f"console input mode 0x{old_mode:04X} -> 0x{raw_mode:04X}")
    print("Press keys; Ctrl+D quits. Columns: +ms since previous record, record, -> bytes sent.")
    print()
    last = time.monotonic()
    try:
        while True:
            events = docker_mod._read_console_key_events(
                kernel32, handle, grace_ms if decoder.pending else 100
            )
            if events is None:
                print("console read failed")
                return 1
            if not events:
                if decoder.pending:
                    print(f"{'':>7}  (no record within {grace_ms} ms) flush -> {decoder.flush()!r}")
                continue
            for event in events:
                now = time.monotonic()
                elapsed, last = (now - last) * 1000, now
                sent = decoder.feed(event)
                print(
                    f"{elapsed:+7.0f}  {'down' if event.key_down else 'up  '}"
                    f" vk=0x{event.vk:02X} scan=0x{event.scan:02X}"
                    f" char={_char(event.char):<16} state={_state(event.state):<16}"
                    f" rep={event.repeat}  -> {sent!r}",
                )
                if event.key_down and event.char == _QUIT:
                    return 0
    finally:
        docker_mod._leave_console_raw_input(old_mode)


def attach() -> int:
    manager = docker_mod.DockerManager()
    image = "alpine:3.20"
    try:
        manager.client.images.get(image)
    except Exception:
        print(f"pulling {image} ...")
        manager.client.images.pull(image)
    container = manager.client.containers.run(
        image,
        ["sh", "-c", _HEX_ECHO],
        tty=True,
        stdin_open=True,
        detach=True,
        labels={"vibepod.diagnostic": "windows-console-keys"},
    )
    try:
        manager.attach_interactive(container)
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "keys"
    if mode == "keys":
        return keys()
    if mode == "attach":
        return attach()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
