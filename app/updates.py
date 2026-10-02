# Release checks against GitHub: the latest app and firmware, which download fits
# the running build, and replacing the app in place where a single file can be
# swapped (Linux AppImage, Windows .exe). Elsewhere the download is opened instead.

import glob
import hashlib
import json
import logging
import os
import platform
import re
import ssl
import sys
import urllib.request

from _version import __version__

log = logging.getLogger('osprad.updates')

REPO = 'wieluk/OSpRad'
LATEST_URL = 'https://api.github.com/repos/%s/releases/latest' % REPO
CHECKSUMS = 'SHA256SUMS.txt'
IS_ANDROID = hasattr(sys, 'getandroidapilevel')


class UpdateError(Exception):
    pass


def version_tuple(text):
    """'v1.2.3' -> (1, 2, 3); anything unparseable -> ()."""
    match = re.match(r'v?(\d+(?:\.\d+)*)', str(text or ''))
    return tuple(int(p) for p in match.group(1).split('.')) if match else ()


def is_newer(candidate, current):
    return version_tuple(candidate) > version_tuple(current)


def _context():
    context = ssl.create_default_context()
    if IS_ANDROID:
        # Python on Android finds no CA bundle of its own; the system store is PEM files.
        pems = []
        for path in glob.glob('/system/etc/security/cacerts/*'):
            try:
                with open(path) as f:
                    pems.append(f.read())
            except OSError:
                pass
        if pems:
            context.load_verify_locations(cadata='\n'.join(pems))
    return context


def _get(url, timeout=15):
    request = urllib.request.Request(url, headers={'User-Agent': 'OSpRad/%s' % __version__,
                                                   'Accept': 'application/vnd.github+json'})
    return urllib.request.urlopen(request, timeout=timeout, context=_context())


class Release:
    def __init__(self, data):
        self.version = data.get('tag_name', '').lstrip('v')
        self.url = data.get('html_url', '')
        self.notes = data.get('body') or ''
        self.date = (data.get('published_at') or '')[:10]
        self.assets = {a['name']: a['browser_download_url'] for a in data.get('assets', [])}

    def app_asset(self):
        """(name, url) of the download for this build, or None if it has none."""
        suffix = _build_suffix()
        if suffix is None:
            return None
        for name, url in self.assets.items():
            if name.endswith(suffix):
                return name, url
        return None

    def firmware_asset(self):
        for name, url in self.assets.items():
            if name.startswith('OSpRad-firmware-') and name.endswith('.hex'):
                return name, url
        return None


def latest_release():
    try:
        with _get(LATEST_URL) as response:
            return Release(json.load(response))
    except Exception as exc:  # noqa: BLE001 - offline, rate limited, DNS, TLS...
        raise UpdateError('Could not reach GitHub (%s).' % exc) from exc


def _build_suffix():
    """Release asset name ending that matches how this copy is running."""
    if IS_ANDROID:
        return '-android-arm64.apk'
    if not getattr(sys, 'frozen', False):
        return None  # source checkout or pip: updated with git or pip
    if sys.platform == 'win32':
        return '-windows-x64.exe'
    if sys.platform == 'darwin':
        return '-macos-%s.zip' % ('arm64' if platform.machine() == 'arm64' else 'x86_64')
    return '-linux-x86_64.AppImage' if os.environ.get('APPIMAGE') else '-linux-x86_64.tar.gz'


def replaceable_file():
    """The single file that is this app, when it can be swapped for a new one."""
    if os.environ.get('APPIMAGE'):
        return os.environ['APPIMAGE']
    if getattr(sys, 'frozen', False) and sys.platform == 'win32':
        return sys.executable
    return None


def download(url, dest, progress=None, release=None, name=None):
    """Download to dest, checking it against the release's SHA256SUMS.txt if it has one."""
    expected = None
    if release is not None and name and CHECKSUMS in release.assets:
        try:
            with _get(release.assets[CHECKSUMS]) as response:
                for line in response.read().decode().splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1].lstrip('*') == name:
                        expected = parts[0].lower()
        except Exception as exc:  # noqa: BLE001
            raise UpdateError('Could not read the release checksums (%s).' % exc) from exc
    digest = hashlib.sha256()
    try:
        with _get(url, timeout=60) as response, open(dest, 'wb') as out:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    except Exception as exc:  # noqa: BLE001
        raise UpdateError('Download failed (%s).' % exc) from exc
    if expected and digest.hexdigest() != expected:
        os.remove(dest)
        raise UpdateError('The download does not match the release checksum; not using it.')
    return dest


def download_text(url):
    try:
        with _get(url) as response:
            return response.read().decode('ascii')
    except Exception as exc:  # noqa: BLE001
        raise UpdateError('Download failed (%s).' % exc) from exc


def install(new_file, target):
    """Swap the downloaded app in for `target`. The running copy keeps working until
    the restart: Linux keeps the replaced file open, and Windows allows renaming a
    running .exe but not overwriting it."""
    if sys.platform == 'win32':
        old = target + '.old'
        if os.path.exists(old):
            os.remove(old)
        os.replace(target, old)
    else:
        os.chmod(new_file, 0o755)
    os.replace(new_file, target)


def remove_leftovers():
    """Delete the .old copy a Windows update leaves behind; it is free once restarted."""
    target = replaceable_file()
    if target and os.path.exists(target + '.old'):
        try:
            os.remove(target + '.old')
        except OSError:
            pass
