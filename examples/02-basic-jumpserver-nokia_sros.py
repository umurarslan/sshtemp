"""
Basic Nokia SR OS with jump server example — one device, show version and configuration.
"""

from sshtemp import ssht

TEMPLATE = r"""
_HOST: 172.29.112.112
_USERNAME: clab
_PASSWORD: clab
_PORT: 2222

_EXPECT: .*assword:
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@10.100.100.3

_EXPECT: #\s*$
_# can use env: os.environ["NOKIA_PASSWORD"]
NokiaSros1!

environment no more

_CAPTURE:show_version.txt
show version
_END

_ERROR:(?i)(critical:|error:|minor:|major:)
configure
    port 1/4/8
        description "lab test"
exit all
_ERROR:

_CAPTURE:show_port_description.txt
show port 1/4/8 description
_END

"""

result = ssht(TEMPLATE)

if not result.success:
    print("unsuccessful:", result.error)
else:
    print(result.captures["show_version.txt"])
    print(result.captures["show_port_description.txt"])

for warning in result.warnings:
    print(f"warning: {warning}")
