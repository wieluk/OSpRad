# Checks that app/_version.py and the firmware sketch agree on major version.
#
# app/_version.py is just a placeholder between releases - the release version comes
# from the git tag, which package.yml's prepare job stamps into every build artifact.
# App and firmware are released together and must share a major version, but the
# firmware only changes on reflash, so only the major needs to match.
#
# Usage: python packaging/check_versions.py

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_PY = os.path.join(REPO_ROOT, 'app', '_version.py')
FIRMWARE_INO = os.path.join(REPO_ROOT, 'firmware', 'OSpRad_firmware.ino')


def _search(path, pattern):
    with open(path) as f:
        match = re.search(pattern, f.read(), re.M)
    if not match:
        sys.exit('Could not find a version in %s' % path)
    return match.group(1)


def app_version():
    return _search(VERSION_PY, r"^__version__ = '([^']+)'")


def firmware_version():
    return _search(FIRMWARE_INO, r'^#define FIRMWARE_VERSION "([^"]+)"')


def main():
    app = app_version()
    firmware = firmware_version()
    print('app/_version.py:  %s' % app)
    print('firmware:         %s' % firmware)

    errors = []
    if app.split('.')[0] != firmware.split('.')[0]:
        errors.append('app %s and firmware %s have different major versions - a 3.x app '
                      'cannot talk to 2.x firmware' % (app, firmware))

    for error in errors:
        print('ERROR: %s' % error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
