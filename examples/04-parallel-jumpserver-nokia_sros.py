"""Nokia SR OS parallel example — two devices at once.

export JUMP_PASSWORD=clab
export DEVICE_PASSWORD=NokiaSros1!
"""

import os

from sshtemp import ssht_parallel

DEVICES = ["10.100.100.3", "10.100.100.4"]


def main() -> None:
    jump_password = os.environ["JUMP_PASSWORD"]
    device_password = os.environ["DEVICE_PASSWORD"]

    sections = []
    for device in DEVICES:
        sections.append(f"""
_# --- {device} DEVICE CONNECTION ---
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: {jump_password}
_PORT: 2222
_EXPECT: #\\s*$
_LOGID: {device}

_IF: continue connecting
_THEN: yes
_IF: .*assword:
_THEN: {device_password}

ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@{device}

environment no more
_CAPTURE:show_version_{device}.txt
show version
_END
""")

    result = ssht_parallel(
        "\n\n".join(sections),
        max_workers=len(DEVICES),
        save_to_disk=True,
    )
    if not result.success:
        print("unsuccessful:", result.error)
        if result.failed_section_index is not None:
            failed = result.results[result.failed_section_index]
            print(f"section {result.failed_section_index} ({failed.log_id}):", failed.error)
    else:
        print("output:", result.output_dir)

    for index, section_result in enumerate(result.results):
        label = section_result.log_id or section_result.host or f"section {index}"
        for warning in section_result.warnings:
            print(f"[{label}] warning: {warning}")


if __name__ == "__main__":
    main()
