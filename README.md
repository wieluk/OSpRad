# OSpRad

An open-source, low-cost, high-sensitivity spectroradiometer. Developed by Jolyon
Troscianko, 2022. Released under GPL-3.0, without warranty of any kind.

Build your own for measuring radiance/irradiance around the Hamamatsu C12880MA chip.
Spectral range ~310-880nm at ~9nm resolution, suited to visual modelling. Tested
sensitivity down to ~0.001 cd/sqm (radiance) and ~0.005 lx (irradiance). This repo
holds everything: STL files for the housing, the Arduino Nano firmware, and a Python
app for desktop or Android.

## Download

Prebuilt apps are attached to each [release](https://github.com/troscianko/OSpRad/releases):

| Asset | Platform |
| --- | --- |
| `OSpRad-<version>-windows-x64.exe` | Windows |
| `OSpRad-<version>-macos.zip` | macOS (unzip to get `OSpRad.app`) |
| `OSpRad-<version>-linux-x86_64.AppImage` | Linux (`chmod +x`, then run) |
| `OSpRad-<version>-linux-x86_64.tar.gz` | Linux without FUSE (extract, run `OSpRad/OSpRad`) |
| `OSpRad-<version>-android-arm64.apk` | Android (sideload) |
| `osprad-<version>-py3-none-any.whl` | Any (`pip install`, then run `osprad`) |

## Install from pypi

```sh
pip install osprad
```

## Run from source

```sh
pip install -r app/requirements.txt
python app/OSpRad.py
```

`app/*.py` must stay together, alongside `calibration_data.csv`. Measurements go to
`data.csv` in the same folder (or, for a `pip install`, a per-user dir such as
`~/.local/share/OSpRad`).

## Repository layout

| Folder | Contents |
| --- | --- |
| `app/` | The Python app. Run `app/OSpRad.py`. |
| `firmware/` | Arduino Nano sketch (open the folder in the Arduino IDE). |
| `3D components/` | STL files for the housing, shutter wheel and caps. |
| `calibration/` | Spreadsheets documenting the full manual calibration derivation. |
| `packaging/` | Build scripts for the desktop, Android and wheel packages. |

## Using the app

Plug the OSpRad in over USB (which also powers it) and launch the app. On Windows you
may need to install a driver for the Nano's USB-serial chip (CH340 or FTDI, depending
on the clone) if it doesn't show up in Device Manager. It saves
calibrated watts/(sqm·nm) or watts/(sr·sqm·nm), raw counts, integration time, saturated
photosite count, scans averaged, timestamp and an optional label. Tick **Automatic
repeat** for repeat measurements (radiance, irradiance or both).

The app and firmware are released together and must share a major version (currently
3.x). A 3.x app cannot talk to 1.x or 2.x firmware - if connecting reports an
unexpected reply, reflash. `data.csv` files written by 1.x are not readable (the
column layout changed when the firmware gained the framed reply protocol).

# Construction

## Parts list

- Hamamatsu C12880MA chip
- 3D printed components (black PLA or ABS; not PET, which is IR-transparent)
- Arduino Nano
- Cosine corrector: 8mm diameter, 0.5mm thick virgin PTFE, sanded circularly with 180 grit
- Digital micro servo (Savox SH-0256 recommended)
- USB cable(s) for a mobile phone (e.g. USB-C to USB-A female + USB-A to USB-mini)
- Solder and cables (e.g. 10cm lengths stripped from an old Ethernet cable)
- UV curing glue for the PTFE diffuser
- Optional: fused silica cover-slip or UV-transmitting PMMA disk for protection

The 3D printed parts need filing/sanding for a snug fit. File the shutter wheel shaft
smooth and circular, enlarge the housing hole with a circular file until the shaft
rotates without play, and heat the shaft end with a heat gun/lighter flame to press-fit
it onto the servo while the plastic is flexible.

## 3D printed parts

![image](https://user-images.githubusercontent.com/53558556/206735271-c7213dae-bb6c-4bfd-b26a-0d071d12910c.png)

## Circuit diagram

Note the separate 5v sources for the servo and spectrometer chip: the VIN pin suffers
a voltage drop from its protective diode, leaving it not quite high enough for stable
spectrometer operation.

![Circuit Diagram](https://user-images.githubusercontent.com/53558556/206735133-19c5051f-9946-49dd-95c0-88d3e2ee12a0.png)

## Firmware and first-time setup

Flash `firmware/OSpRad_firmware/` to the Arduino Nano with the Arduino IDE. The unit
number and wheel positions live in EEPROM and are set from the app, so the same
firmware goes on every unit (each only needs flashing once).

1. Flash, connect over USB and launch the app.
2. **Calibration → Import & export**: enter the unit number (each unit needs its own
   ID to look up its calibration data) and press *Save to unit*.
3. **Unit & wheel setup**: move the wheel to 90 degrees with the slider, remove it
   from the servo at that central position and re-attach as close to "closed" as
   possible.
4. Jog the slider (or +/- buttons) to find each position in turn, pressing *Set as
   Dark*, *Set as Irradiance* and *Set as Radiance* as each lines up.

# Calibration

Each unit's calibration lives in `calibration_data.csv`, four comma-delimited rows per
unit, keyed by the unit number in the first column.

![image](https://user-images.githubusercontent.com/53558556/206896550-cf35ebd2-01a4-46ef-b638-2797bc92ab76.png)

The **Calibration** tab covers most of this, writing straight into the CSV:

- **Linearisation** fits linCoefs from one steady source (daylight or incandescent; most
  LED/fluorescent lighting flickers). The coefficients set the overall scale, so
  re-derive or rescale the spectral sensitivity afterwards.
- **Spectral sensitivity**: load a 288-value curve from a file, rescale against one
  known reading, or derive a new one from a reference spectrum. Nothing is written
  until *Save*.
- **Import & export** saves/restores a unit's whole calibration (CSV + wheel positions
  on the Arduino) as one JSON file. Tick-boxes select what to include; an import
  merges rather than replaces.

The spreadsheets in `calibration/` are the reference for the full derivation.

# Monitor calibration and Psychtoolbox

**Monitor calibration** steps a fullscreen patch through black then a ladder of levels
for each of red/green/blue, measuring the spectrum at every step (point the unit at the
screen in Radiance mode). "Export for Psychtoolbox..." writes a fitted PsychCal `.mat`
that loads with `cal = LoadCalFile(...)` - no MATLAB-side step. See
[monitor_calibration.py](app/monitor_calibration.py)'s header for the model and
tone-curve choices; cal.describe.gamma.fitType says 'OSpRad-pchip' so the difference
from PTB's pipeline is visible. Calibrate in as dark a room as practical: the tab
measures one black-screen ambient, but OSpRad's dark-frame subtraction handles sensor
noise, not room ambient.

# Building

Desktop builds use [PyInstaller](https://pyinstaller.org/); Windows and macOS must be
built on their own OS, since PyInstaller does not cross-compile.

```sh
pip install -r app/requirements.txt pyinstaller

bash packaging/linux/build_linux.sh      # AppImage if appimagetool is on PATH, else a tarball
packaging\windows\build_windows.ps1      # OSpRad.exe
bash packaging/macos/build_macos.sh      # OSpRad.app

pip install build && python -m build     # wheel + sdist
```

Android: `bash packaging/android/build_full_app.sh [work_dir]` builds a real `.apk` via
`pyside6-android-deploy` (20-40+ min). Needs host Python 3.11 or lower, several GB of
scratch space, and a **short** `work_dir` (recipes invoke a hostpython3 pip whose
shebang embeds the full path; past the kernel's 127-char limit it fails as "Exec
format error"). scipy is deliberately not a dependency (no working Android build, so
the linearisation fit and gamma interpolation use hand-rolled numpy equivalents);
it's only needed for the Psychtoolbox `.mat` export (`pip install osprad[psychtoolbox]`).

# Releasing

[`.github/workflows/package.yml`](.github/workflows/package.yml) runs code checks, builds
every platform, attaches the artifacts to a GitHub Release and publishes the wheel to
PyPI.

Just `git tag v3.2.1 && git push --tags` - the tag becomes the release version, stamped
into every artifact by the `prepare` job. [`app/_version.py`](app/_version.py) is only a
placeholder for source checkouts between releases and doesn't need bumping to match.
Bump `FIRMWARE_VERSION` in the sketch (and `app/_version.py`'s major version to match)
only when the firmware's wire protocol actually changes.

To rebuild a single artifact without tagging, use **Actions → Run workflow**. PyPI uses
Trusted Publishing - one-time publisher registration on PyPI for this repo, workflow
`package.yml`, environment `pypi`.
