#!/usr/bin/env python3
"""Die wenigen Root-Aktionen, die der ONVIF-Dienst (Benutzer 'onvipi') auslösen darf.

Aufruf nur über sudo, siehe /etc/sudoers.d/onvipi:

  onvipi_priv.py reboot             Pi neu starten (ONVIF SystemReboot)
  onvipi_priv.py set-time <epoch>   Uhr stellen (ONVIF SetSystemDateAndTime) – nur wenn
                                    der Pi seine Zeit nicht selbst per NTP bekommt
"""
import subprocess
import sys
import time


def ntp_synchronized():
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip() == "yes"


def main(argv):
    if argv == ["reboot"]:
        return subprocess.call(["systemctl", "reboot"])
    if len(argv) == 2 and argv[0] == "set-time" and argv[1].isdigit():
        epoch = int(argv[1])
        if not 1_600_000_000 < epoch < 4_102_444_800:   # 2020 … 2100
            print("time out of range", file=sys.stderr)
            return 2
        if ntp_synchronized():
            print("clock is NTP-synchronized – left unchanged")
            return 0
        if abs(epoch - time.time()) < 2:
            return 0
        return subprocess.call(["date", "-u", "-s", f"@{epoch}"], stdout=subprocess.DEVNULL)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
