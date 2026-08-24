# OSpRad 3.1.0
## An open-source, low-cost, high-sensitivity spectroradiometer

Developed by Jolyon Troscianko - 2022

Released under GPL-3.0 license.

This project allows users to build their own low-cost, high-sensitivity spectroradiometer for measuring radiance/irradiance based on the Hamamatsu C12880MA chip. The spectral range of ~310 to 880nm and resoltuion of ~9nm make the OSpRad particularly well suited to visual modelling.

Testing shows that the system can measure spectral radiance down to around 0.001 cd/sqm, and irradiance down to around 0.005 lx.

Included in this project are the 3D STL files for creating your own housing, code for uploading to an arduino nano microcontroller, and a Python app for interfacing with the OSpRad spectroradiometer via desktop computer or Android smartphone (via Pydroid 3).

Code and data are released without any form of warranty, and the author accepts no liability.

## Repository layout

| Folder | Contents |
| --- | --- |
| `app/` | The Python app. Run `app/OSpRad.py`. Also holds calibration_data.csv, and data.csv is written here. |
| `firmware/` | Arduino Nano sketch (open the folder in the Arduino IDE). |
| `3D components/` | STL files for 3D printing the housing, shutter wheel and caps. |
| `calibration/` | Spreadsheets used to derive calibration data (see Calibration below). |
| `legacy/` | Previous app and firmware versions, kept for units that have not been updated. |

## User interface app
OSpRad units are controlled through the included graphical user interface. This is written in Python 3, and can run from desktop computers (currently Linux) or Android smartphones (requires Pydroid 3 app). Simply plug in the OSpRad via USB and run `app/OSpRad.py` to launch the app. The OSpRad communicates via serial connection and the USB also provides power.

The app is split across several files, all of which live in the `app` folder and must stay together: `OSpRad.py` (the file you run), `serial_io.py`, `calibration.py`, `calibration_wizard.py`, `plotting.py`, `datalog.py` and `analysis.py`.

Python dependences: tkinter, matplotlib, pyserial, numpy, scipy, sv-ttk, (and for Android) usb4a and usbserial4a. On a desktop these can be installed with `pip install -r app/requirements.txt`. Note that tkinter is not a pip package - on Linux install your distribution's python3-tkinter package.

Calibration data for the OSpRad units must be provided by placing the calibration_data.csv file (with relevant data for each specific unit#) in the `app` folder alongside the code. Spectral data are written to data.csv in that same folder, whichever directory you launch the app from.

**The app and the firmware are released together and their major versions must match** (currently 3.x) - a 3.x app will not talk to 1.x or 2.x firmware, or vice versa. If the app reports an unexpected reply when connecting, reflash the unit (see below).
![image](https://user-images.githubusercontent.com/53558556/206735364-3b1cf770-dc8e-4b96-9161-38993c282523.png)

The app saves all relevant data, including calibrated watts/(sqm * nm) or watts/(sr * sqm * nm), raw count data, integration time, number of saturated photosites (to ensure measurement isn't over-exposed), number of scans averaged, time and date of the the measurement, and a label if one was chosen.

Repeat measurements can be made by ticking the relevant checkbox (for data logging), specify whether to measure radiance, irradiance, or both, and the frequency (in seconds).

Note: `data.csv` files produced by OSpRad 1.x are not compatible with this version (the column layout changed when the firmware gained the framed reply protocol); re-measure with the 3.x firmware to use this app's history browser.

## App running from smart phone
To run from an Android phone, install Pydroid 3, use pip to install the dependences (this requires an additional app, just follow the on-screen instructions). Copy the whole `app` folder onto the phone, then open and run `OSpRad.py` from within it - the other modules must sit beside it so Pydroid can find them. Ensure the calibration_data.csv file is in that same folder. Most modern phones have a USB-C port, so you'll need a USB-C to USB-A (female receptacle). Which is a standard cable. Older phones with a USB-micro port require a USB-micro OTG to USB-mini cable (less common, but easy to buy online).
![image](https://user-images.githubusercontent.com/53558556/207035761-f31efe3d-daf0-49bf-aa4e-54de707b840e.png)

# Construction
## Parts list:
- Hamamatsu C12880MA chip
- 3D printed components (black PLA or ABS recommended, not PET due to IR transparency)
- Arduino Nano
- Cosine corrector: 8mm diameter, 0.5mm thick virgin PTFE sheet, sanded in circular motion with 180 grit paper
- Digital micro servo (Savox – SH-0256 recommended)
- USB cable(s) for mobile phone. e.g. USB-C to USB-A female, and USB-A male to USB-mini
- Solder and cables (e.g. strip the cables from an old Ethernet cable, 10cm lengths)
- UV curing glue (or similar, for gluing the PTFE diffuser to the shutter wheel)
- Optional physical protection: Circular fused silica microscope cover-slip or UV-transmitting PMMA disk

The 3D printed parts will need some filing/sanding/drilling to get a snug fit between the housing and shutter wheel. File down the outside of the shutter wheel shaft to give it a smooth, circular cross-section (i.e. remove 3D printing imperfections). Then use a circular file to enlarge the hole in the housing until the two fit together snugly and the shaft rotates smoothly without play. The centre of the shutter wheel shaft might need to be enlarged slightly with a 4mm drill bit to get the screw head down inside it. To get a fit between the shutter wheel shaft and the servo, use a heat-gun or lighter flame to heat the end of the shaft, and push the two carefully together while the plastic is slightly flexible, ensuring it cools in the right angle. It will cool and harden in the correct shape, so that when screwed together it will have a nice grip.

## 3D printed parts:
![image](https://user-images.githubusercontent.com/53558556/206735271-c7213dae-bb6c-4bfd-b26a-0d071d12910c.png)


Solder the parts together as shown below. Roughly 10cm lengths of wire should be good. Note the use of different 5v sources for the servo and spectrometer chip. This is because the VIN pin suffers a voltage drop (due to its protective diode) from the USB's 5v source, which isn't quite high enough for stable spectrometer functioning.

## Circuit Diagram:
![Circuit Diagram](https://user-images.githubusercontent.com/53558556/206735133-19c5051f-9946-49dd-95c0-88d3e2ee12a0.png)

## Arduino code
Use Arduino IDE to flash `firmware/OSpRad_firmware/` to the Arduino Nano. Unlike earlier versions, you do **not** need to edit anything in the sketch before flashing - the unit number and the three shutter wheel positions are stored in the Arduino's EEPROM and are set from the app instead. The same firmware can therefore be flashed to every unit you build, and you only ever need to flash a unit once.

Setting up a newly built unit:

1. Flash the firmware, connect the OSpRad via USB and launch the app.
2. Open the **Calibration** tab, then **Unit & wheel setup**.
3. Enter the unit number (each unit needs its own ID so it can look up its calibration data) and press *Save unit number*.
4. Move the wheel to 90 degrees using the slider. With the wheel at this central position, remove the shutter wheel from the servo and re-attach it as close to "closed" as possible, to get it roughly into the correct place.
5. Jog the slider (or the +/- buttons) to find each of the three positions in turn, pressing *Set as Dark*, *Set as Irradiance* and *Set as Radiance* once each one lines up. Dark is the closed central position, irradiance uses the cosine diffuser, and radiance is the clear aperture.

Each setting is written to the Arduino as soon as you save it, and the panel at the top of the tab shows the values currently stored on the unit.

If you prefer to work by hand, the firmware still accepts commands over a 115200 baud serial connection: `w<angle>` moves the wheel, `sD`/`sI`/`sR` save the current angle as the dark/irradiance/radiance position, `u<n>` sets the unit number, and `g` reports the stored configuration.

# Calibration
Calibration data for each OSpRad unit are stored in calibration_data.csv, using comma delineation. The file takes the following format:

![image](https://user-images.githubusercontent.com/53558556/206896550-cf35ebd2-01a4-46ef-b638-2797bc92ab76.png)

The first column stores the unit#. This is the ID given to each unit (stored in the Arduino's EEPROM, see above). Each unit requires four rows of calibration data

## Calibrating from the app
Much of the calibration below can now be done from the app's **Calibration** tab rather than by hand in the spreadsheets. Anything saved there is written straight into calibration_data.csv.

- **Linearisation** measures one steady light source across a range of integration times and fits the linCoefs automatically. Use a source that does not flicker - daylight or an incandescent lamp are ideal, but most LED and fluorescent lighting is not. Because these coefficients set the overall scale of the linearised signal, re-derive or rescale the spectral sensitivity afterwards.
- **Spectral sensitivity** offers three routes: import a 288-value curve from a file (for example exported from the "sensitivity FINAL" sheet of calibration/calibration_calculations.ods), rescale the existing curve against a single known luminance/illuminance reading, or derive a new curve from scratch by measuring a source whose true spectrum you have already recorded on a reference spectroradiometer.

The spreadsheets remain the reference for the full derivation, and are still the better route if you want to inspect every intermediate step.

This includes:

## Wavelength calibration "wavCoef"
[six coefficients required] The coefficients for the equation matching each photosite to its peak wavelength sensitivity are provided by the manufacturer when you purchase the spectrometer chip.

## Linearisation data "linCoefs"
[two coefficients] This describes the non-linear relationship between raw photosite ADC count data and linear flux. I found this is described by the function:

c[linear] = c / ( a * ln(( c + 1 ) * b )  )

at each photosite. Where c is the raw count value, and a and b are coefficients.

Example of linearisation model fitting with linear x-axis:
![image](https://user-images.githubusercontent.com/53558556/206866765-3232aae8-63bd-4dec-80ab-747c6e76379e.png)

and logged x-axis to show effects at very low count numbers:
![image](https://user-images.githubusercontent.com/53558556/206866771-5d5c5ff3-211b-4721-a19d-9e68e6823c1b.png)

You can either measure this yourself for unit-specific linearisation values, or use a template, they are all very similar.


## Spectral sensitivity calibration "radSens" and "irrSens"
[288 numbers each] This requires access to a calibrated light source (with known emission spectra). Measure that source with the OSpRad in radiance and irradiance modes. See the spreadsheets in the calibration folder to see how spectral radiance and irradiance calibration data are created.

Alternatively, you could simply use the included data as a template, but this would cause some error as there are unit-specific spectral sensitivity differences:

## Spectral radiance sensitivity:
![image](https://user-images.githubusercontent.com/53558556/206866994-992bc599-04df-417b-9486-ac40f4764e75.png)

## Spectral irradiance sensitivity:
![image](https://user-images.githubusercontent.com/53558556/206867013-0940212b-1364-4cf7-a1a8-aa31dc41c986.png)

Note that unit "E" used a cosine corrector with a different construction, explaining its lower sensitivity.
