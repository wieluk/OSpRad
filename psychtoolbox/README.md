# OSpRad + Psychtoolbox monitor calibration

Characterise a display with OSpRad's "Monitor calibration" tab and load the
result directly in Psychtoolbox - no MATLAB-side step required, matching what
the standard [`CalibrateMonSpd`](http://psychtoolbox.org/docs/CalibrateMonSpd)
workflow produces.

In the OSpRad app: Monitor calibration tab. Point the unit at the screen
(Radiance mode), press Start. It steps a fullscreen patch through black, then
a ladder of levels for each of Red/Green/Blue, measuring the spectrum at every
step. Click "Export for Psychtoolbox..." to save an already-fitted `.mat`
file, then in MATLAB:

```matlab
cal = LoadCalFile('/path/to/exported/file');
```

## What's in the file, and how it was fit

- **Linear device model** (`P_device`, `T_device`, `rawdata.rawGammaTable`) -
  ported exactly from PTB's own `CalibrateFitLinMod.m`/`FindModelWeights.m`
  for the single-primary-basis case: each channel's `P_device` is its
  spectrum at the highest measured level, and each level's raw gamma weight
  is the least-squares projection of its spectrum onto that basis (`B \
  input` in MATLAB).
- **Tone curve** (`gammaTable`/`gammaInput`) - a monotone PCHIP interpolation
  through the measured points, **not** a port of PTB's own
  `CalibrateFitGamma`. That function's default `crtPolyLinear` fit is a
  multi-stage blend (a polynomial fit and a linear-interpolation fit,
  stitched together via a threshold) whose sub-functions weren't fully
  confirmed from source - reimplementing them from a partial reading risked
  silently producing a plausible-but-wrong curve. A PCHIP fit makes no
  assumption about the tone curve's parametric shape and stays faithful to
  what was actually measured; `cal.describe.gamma.fitType` is set to
  `'OSpRad-pchip'` (not `'crtPolyLinear'`) so this is visible rather than
  silently implied to be PTB's own algorithm.
- `T_device`/`T_ambient` use OSpRad's own analytic CIE 1931 XYZ approximation
  (the same one `calibration.py` uses for luminance/chromaticity/CCT
  elsewhere in the app) rather than PTB's tabulated CIE data - close, but not
  guaranteed bit-identical.

See `app/monitor_calibration.py`'s module docstring for the full detail.

## Caveats

- OSpRad's dark-frame subtraction handles sensor noise, not room ambient.
  The tab measures one black-screen ambient spectrum (`cal.P_ambient`), but
  for best accuracy still calibrate in as dark a room as practical.
- Requires the unit's shutter wheel Dark/Irradiance/Radiance positions to
  already be saved (Calibration -> Unit & wheel setup), and the unit to have
  wavelength/sensitivity/linearisation data in `calibration_data.csv` - the
  tab blocks Start with an error if either is missing, since without them
  every measurement would be near-zero noise, not a real spectrum.
- The C12880MA sensor is far less sensitive than a dedicated photometer like
  the PR-650 series - near-black patches need multi-second integration
  times, so a full sweep is slower than with lab-grade hardware.
