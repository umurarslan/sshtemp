"""
Basic Nokia SR OS — one device, show version and configuration.
"""

from sshtemp import ssht

TEMPLATE = """
_HOST: 10.100.100.3
_USERNAME: admin
_PASSWORD: NokiaSros1!

_EXPECT: #\s*$

_CAPTURE:show_version
show version
_END

_ERROR:(?i)(critical:|error:|minor:|major:)
configure
    port 1/4/8
        description "lab test"
exit all
_ERROR:
"""

result = ssht(TEMPLATE)

if not result.success:
    print("unsuccessful:", result.error)
else:
    print(result.captures["show_version"])
    print(result.warnings)
