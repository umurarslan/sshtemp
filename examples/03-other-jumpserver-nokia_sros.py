"""Nokia SR OS jump-host example.

export JUMP_PASSWORD=clab
export DEVICE_PASSWORD=NokiaSros1!
"""

import os

from sshtemp import ssht

DEVICES = ["10.100.100.3", "10.100.100.4"]


def main() -> None:
    jump_password = os.environ["JUMP_PASSWORD"]
    device_password = os.environ["DEVICE_PASSWORD"]

    sections = []
    for device in DEVICES:
        sections.append(rf"""
_# --- {device} DEVICE CONNECTION ---
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: {jump_password}
_PORT: 2222
_EXPECT: #\s*$
_LOGID: {device}

_IF: continue connecting
_THEN: yes
_IF: .*assword:
_THEN: {device_password}

ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@{device}

environment no more
_CAPTURE:show_version_all_nodes.txt
show version
_END

_WAIT: 120
_CAPTURE:BACKUP_CONFIGS/backup_config_{device}.txt
admin display-config
_END

_ERROR:(?i)(critical:|error:|minor:|major:)
configure
    port 1/4/8
        description "lab test"
exit all
_ERROR:
""")

    result = ssht("\n\n".join(sections), save_to_disk=True)
    if not result.success:
        print("unsuccessful:", result.error)
    else:
        for warning in result.warnings:
            print(f"warning: {warning}")


if __name__ == "__main__":
    main()
