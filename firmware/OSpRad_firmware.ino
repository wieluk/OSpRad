
/*
 * OSpRad firmware
 * For the Arduino Nano
 * The app must share this firmware's major version.
 */



#include <Servo.h>
#include <EEPROM.h>
Servo myservo;

#define FIRMWARE_VERSION "3.3.0"


// EEPROM layout: each *_ADDR holds one int (2 bytes).
#define EEPROM_MAGIC 0xA5
#define EE_ADDR_MAGIC 0
#define EE_ADDR_UNIT 1
#define EE_ADDR_DARK 3
#define EE_ADDR_IRR  5
#define EE_ADDR_RAD  7

bool configured = false; // true once EEPROM has been written at least once


// Fallback defaults. Overwritten by the Python app the first time the unit is
// configured. Manual "w<angle>" jogging still works to set them up by hand.

int unitNumber = 1;
int posDark = 98;   // angle for dark measurement
int posIrr = 146;   // angle for irradiance (cosine diffuser)
int posRad = 57;    // angle for radiance measurement (clear aperture)

int currentWheelAngle = 90; // last angle set via w<angle>, used by sD/sI/sR

// True once moveWheel() has actually driven the wheel to currentWheelAngle
// and let it settle. At boot the servo is unpowered and the wheel is wherever
// it was left, so currentWheelAngle is only a guess until then, and a guess is
// not something to skip a move on.
bool wheelSettled = false;

// Dark reference left sitting in data[][0] by the last measurement, for the
// live command to reuse. Only valid for the exposure and scan count it was
// taken at: dark current scales with both, and the DATA line divides by nScans.
bool darkValid = false;
long darkIntTime = 0;
int darkScans = 0;




int servoDelay = 300;
int servoDetachDelay = 1500; // Detach delay: leaving the servo powered adds electrical noise.
int servoPin = 8;

#define TRGpin A0
#define STpin A1
#define CLKpin A2
#define VIDEOpin A3

#define nSites 288
uint16_t data[nSites] [2];
int dataSaveDim = 0;

int delayTime = 1;
long intTime = 100;
long prevIntTime = 100;
long maxAutoIntTime = 5000;
long maxIntTime = 60000; // maximum integration time for auto measurement
long manIntTime = 0;
int satN = 0; // number of bands over exposed
int satVal = 1000; // over exposure threshold
int satSum = 0;
int maxVal = 0;
int prevMaxVal = 0;

int nScansMax = 50; // 65535 is the max uint16 data value, so can only deal with about 60 max
int nScansMin = 3;
long sampleTimeMax = 1000; // target sampling time for repeat scans
int nScans = 1;
int measureType = 0;

uint16_t lineChecksum = 0; // running checksum for the current DATA line


void loadConfig(){
  uint8_t magic;
  EEPROM.get(EE_ADDR_MAGIC, magic);
  if(magic == EEPROM_MAGIC){
    int v;
    EEPROM.get(EE_ADDR_UNIT, v); unitNumber = v;
    EEPROM.get(EE_ADDR_DARK, v); posDark = v;
    EEPROM.get(EE_ADDR_IRR, v); posIrr = v;
    EEPROM.get(EE_ADDR_RAD, v); posRad = v;
    configured = true;
  } else {
    configured = false; // still running on source level defaults above
  }
}

void saveUnitNumber(int n){
  EEPROM.put(EE_ADDR_UNIT, n);
  unitNumber = n;
  EEPROM.put(EE_ADDR_MAGIC, (uint8_t)EEPROM_MAGIC);
  configured = true;
}

void saveWheelPosition(char role, int angle){ // role: 'D','I','R'
  if(role == 'D'){
    EEPROM.put(EE_ADDR_DARK, angle);
    posDark = angle;
  } else if(role == 'I'){
    EEPROM.put(EE_ADDR_IRR, angle);
    posIrr = angle;
  } else if(role == 'R'){
    EEPROM.put(EE_ADDR_RAD, angle);
    posRad = angle;
  }
  EEPROM.put(EE_ADDR_MAGIC, (uint8_t)EEPROM_MAGIC);
  configured = true;
}


// Position the filter wheel, skipping the move when the wheel is already there.
//
// A move costs servoDelay + servoDetachDelay whether or not the servo has
// anywhere to travel, and that 1.8s dwarfs a short measurement. Every
// measurement used to pay it twice: once to the light position (often
// already there) and once to dark. The detach delay is not padding:
// leaving the servo powered adds noise to the video line.
void moveWheel(int angle){
  if(wheelSettled && angle == currentWheelAngle)
    return;
  myservo.attach(servoPin);
  myservo.write(angle);
  currentWheelAngle = angle;
  delay(servoDelay);
  myservo.detach();
  delay(servoDetachDelay);
  wheelSettled = true;
}


// Stream helpers that write straight to Serial while accumulating the running
// line checksum, instead of building a temporary String for every value. The
// DATA line used to concatenate ~290 short lived String objects (one per
// site), which repeatedly allocated/freed on a 2KB heap and could fragment
// it; these overloads produce byte identical output with no heap use.
void csPrint(char c){
  Serial.write(c);
  lineChecksum += (uint8_t) c;
}

void csPrint(const char *s){
  Serial.print(s);
  while(*s) lineChecksum += (uint8_t) *s++;
}

void csPrint(const __FlashStringHelper *s){
  Serial.print(s);
  PGM_P p = reinterpret_cast<PGM_P>(s);
  char c;
  while((c = pgm_read_byte(p++)))
    lineChecksum += (uint8_t) c;
}

void csPrintLong(long v){
  char buf[12]; // fits -2147483648 + NUL
  ltoa(v, buf, 10);
  csPrint(buf);
}

void csPrintFloat2(float v){
  char buf[16]; // matches old String(float) default of 2 decimal places
  dtostrf(v, 0, 2, buf);
  csPrint(buf);
}


void setup(){
  loadConfig();

  pinMode(CLKpin, OUTPUT);
  pinMode(STpin, OUTPUT);

  digitalWrite(CLKpin, HIGH);
  digitalWrite(STpin, LOW);

  Serial.begin(115200);
  // Newline terminated commands (see loop()), so this only ever gets hit by a
  // malformed or incomplete command. Keep it short so that fails fast.
  Serial.setTimeout(200);
  while (! Serial);
  readSpectrometer();
  resetData();
}

void readSpectrometer(){

  // Start clock cycle and set start pulse to signal start
  digitalWrite(CLKpin, LOW);
  delayMicroseconds(delayTime);
  digitalWrite(CLKpin, HIGH);
  delayMicroseconds(delayTime);
  digitalWrite(CLKpin, LOW);
  digitalWrite(STpin, HIGH);
  delayMicroseconds(delayTime);

  unsigned long cTime = millis(); // start time
  unsigned long eTime = cTime + intTime; // end time

  //Sample for a period of time
 while(cTime < eTime){
      digitalWrite(CLKpin, HIGH);
      delayMicroseconds(delayTime);
      digitalWrite(CLKpin, LOW);
      delayMicroseconds(delayTime);
      cTime=millis();
  }

  //Set STpin to low
  digitalWrite(STpin, LOW);

  //Sample for a period of time
  for(int i = 0; i < 88; i++){ //87 aligns correctly

      digitalWrite(CLKpin, HIGH);
      delayMicroseconds(delayTime);
      digitalWrite(CLKpin, LOW);
      delayMicroseconds(delayTime);

  }

  int specRead = 0;
  satN = 0;
  for(int i = 0; i < nSites; i++){

      specRead = analogRead(VIDEOpin);
      data[i][dataSaveDim] += specRead;
      if(specRead > satVal)
        satN ++;

      digitalWrite(CLKpin, HIGH);
      delayMicroseconds(delayTime);
      digitalWrite(CLKpin, LOW);
      delayMicroseconds(delayTime);

  }
}



void resetData(){
  for (int i = 0; i < nSites; i++)
    data[i] [dataSaveDim] = 0;
}

void satTest(){
  for (int i = 0; i < nSites; i++){
    if(data[i][dataSaveDim] > maxVal)
        maxVal = data[i][dataSaveDim];
  }

}

// Raw scan pair for the 'd' diagnostic: takes two scans 150ms apart into
// dim 0 and dim 1 (the dark cache this overwrites is dropped below, so
// clobbering them here is safe) and reports roughness (mean |diff| between
// adjacent pixels) and repeat (mean |diff| between the two scans). Never
// touches the servo (RC servos are open loop, nothing to test) and restores
// intTime/dataSaveDim.
//
// The app thresholds on roughness/repeat. A connected sensor's readout
// repeats (pixel to pixel pattern is physical), so repeat stays at read
// noise level. A floating pin picks up slow drifting interference, so
// repeat is large while roughness stays small. The ratio is dimensionless
// and independent of light level, integration time, and wheel position,
// unlike the absolute roughness threshold this replaced.
void sensorSelfTest(int &outMin, int &outMax, float &outRoughness, float &outRepeat){
  long savedIntTime = intTime;
  int savedDim = dataSaveDim;
  darkValid = false; // dim 0 is about to stop being a dark reference

  intTime = 5;
  dataSaveDim = 0;
  resetData();
  readSpectrometer();

  delay(150);

  dataSaveDim = 1;
  resetData();
  readSpectrometer();
  dataSaveDim = 0;

  outMin = data[0][0];
  outMax = data[0][0];
  long roughSum = 0;
  long repeatSum = abs((long)data[0][0] - (long)data[0][1]);
  for(int i = 1; i < nSites; i++){
    if(data[i][0] < outMin) outMin = data[i][0];
    if(data[i][0] > outMax) outMax = data[i][0];
    roughSum += abs((long)data[i][0] - (long)data[i-1][0]);
    repeatSum += abs((long)data[i][0] - (long)data[i][1]);
  }
  outRoughness = float(roughSum) / float(nSites - 1);
  outRepeat = float(repeatSum) / float(nSites);

  intTime = savedIntTime;
  dataSaveDim = savedDim;
}


// How many scans the firmware averages at this exposure. Pulled out of
// takeMeasurement so a live update can work out, before taking any scans,
// whether the dark it is holding still fits (which is what lets it take its
// dark first, see below).
int scanCountFor(long t){
  long n = floor(sampleTimeMax / t);
  if(n < nScansMin)
    n = nScansMin;
  if(n > nScansMax)
    n = nScansMax;
  return (int) n;
}


// One measurement: light minus dark, reported as a DATA line.
//
// type: 1 = radiance (clear aperture), 0 = irradiance (cosine diffuser).
//
// live = true reuses the dark reference the last measurement left in dim 0,
// which is what makes continuous mode fast: it skips the move to posDark and
// the whole block of dark scans, and leaves the wheel on the light position.
//
// Dark current is a function of integration time and sensor temperature, not
// of what the wheel is pointing at. Within one exposure a recent dark is
// still right, and the app re takes one periodically with a plain r/i to
// follow the sensor warming up. Anything that changes the exposure, the
// scan count, or dim 0 clears the cache, so a stale dark is never used.
void takeMeasurement(int type, bool live){

  measureType = type;
  int lightPos = (type == 1) ? posRad : posIrr;
  nScans = 1;

  // At a fixed exposure everything the dark decision rests on is known before
  // a single scan is taken, which lets a live update take its dark FIRST and
  // finish standing on the light position. Taken light first, the measurement
  // ends on the dark position and the next update has to walk back.
  //
  // Auto exposure cannot do this: choosing an exposure means looking at the
  // scene, and a dark is only good for the exposure it was taken at.
  bool fixedExposure = (manIntTime != 0);
  bool reuseDark = false;
  if(fixedExposure){
    intTime = manIntTime;
    nScans = scanCountFor(intTime);
    reuseDark = live && darkValid && intTime == darkIntTime && nScans == darkScans;
  }

  if(live && fixedExposure && !reuseDark){
    moveWheel(posDark);
    dataSaveDim = 0; // dark data
    resetData();
    for(int i=0; i<nScans; i++){
      readSpectrometer();
      delay(10);
    }
    darkValid = true;
    darkIntTime = intTime;
    darkScans = nScans;
    reuseDark = true; // in hand now; only the light scans are left to do
  }

  moveWheel(lightPos);

  dataSaveDim = 1; // 1 = light, 0 = dark
  resetData();

  // Auto expose: ramp up until saturation, then estimate a near saturating value.
  if(!fixedExposure){

        // The ramp settles on a different intTime every time and takes its
        // scans in dim 0, so no cached dark can survive it.
        darkValid = false;
        dataSaveDim = 0;
        resetData();

        satN = 0;
        intTime = 1;
        prevIntTime = 1;
        maxVal = 0;

        readSpectrometer();
        satTest();

        if(satN == 0){ // initial 1ms scan wasn't already over exposed

          while(satN == 0){
            prevMaxVal = maxVal;
            prevIntTime = intTime;
            intTime = intTime*2;
            if(intTime > maxAutoIntTime) // do not go above max (takes ages to measure)
              satN = 1;
            else {
              resetData();
              readSpectrometer();
              satTest();
            }

            delay(10);
          }
          resetData();
          float tInt = floor(float(prevIntTime*0.9*satVal)/float(prevMaxVal));
          if(tInt > maxIntTime)
              intTime = maxIntTime;
          else intTime = tInt;
        }

        nScans = scanCountFor(intTime);
        // The ramp above cleared darkValid, so there is nothing to reuse here.
  }

  if(!reuseDark){
    dataSaveDim = 0;
    resetData();
    darkValid = false; // not a dark reference again until the scans below have run
  }

  satSum = 0;

  // Dark already in hand: light scans only, and the wheel does not move at all.
  // The interleaving further down exists to track dark drift across a long
  // measurement, which reusing a dark waives by definition, so a long exposure
  // takes this path too. Without that, a live run in a dim room would move
  // the wheel twice per scan however good the cached dark was.
  if(reuseDark){

    dataSaveDim = 1; // light data
    for(int i=0; i<nScans; i++){
      readSpectrometer();
      satTest();
      satSum = satSum + satN;
      delay(10);
    }

  // Short integration time: collect all light scans, then one block of dark scans.
  } else if(intTime < sampleTimeMax){

    dataSaveDim = 1; // light data
    for(int i=0; i<nScans; i++){
      readSpectrometer();
      satTest();
      satSum = satSum + satN;
      delay(10);
    }

    moveWheel(posDark);
    dataSaveDim = 0; // dark data

    for(int i=0; i<nScans; i++){
      readSpectrometer();
      delay(10);
    }

    darkValid = true;
    darkIntTime = intTime;
    darkScans = nScans;

  // Long integration time: interleave one light scan with one dark scan per loop.
  } else {

    for(int i=0; i<nScans; i++){
          // light measurement
          moveWheel(lightPos);
          dataSaveDim = 1; // light data
          readSpectrometer();
          satTest();
          satSum = satSum + satN;
          delay(10);

          moveWheel(posDark);
          dataSaveDim = 0; // dark data
          readSpectrometer();

    }

    // dim 0 now holds nScans dark scans at this exposure, exactly as the short
    // branch's block does, so it is just as reusable by a later live update.
    darkValid = true;
    darkIntTime = intTime;
    darkScans = nScans;

  }

  lineChecksum = 0;
  csPrint(F("DATA,"));
  csPrintLong(unitNumber);
  csPrint(',');
  csPrintLong(measureType);
  csPrint(',');
  csPrintLong(nScans);
  csPrint(',');
  csPrintLong(intTime);
  csPrint(',');
  csPrintFloat2(float(satSum)/float(nScans));

  for (int i = 0; i < nSites; i++){
   csPrint(',');
   csPrintFloat2((float(data[i][1])-float(data[i][0]  ))/float(nScans));
  }
  Serial.print(",");
  Serial.print(String(lineChecksum, HEX));
  Serial.print("\n");
  delay(50);
}


void loop(){


  // Newline terminated: parses one command per call instead of readString()'s old
  // ~1s silence behaviour. See the 3.0.0 changelog.
  String arg = Serial.readStringUntil('\n');
  arg.trim();

  if (arg.length() > 0){

    if(arg.startsWith("t") == true){
      arg.replace("t", "");
      long t = (long) arg.toFloat();
      if(t < 0){
        Serial.println(F("ERR,bad_int_time"));
      } else {
        manIntTime = t;
        if(manIntTime > maxIntTime)
            manIntTime = maxIntTime;
        darkValid = false; // the held dark belonged to the old settings
        Serial.print(F("OK,int_time,"));
        Serial.println(manIntTime);
      }

    } else if(arg.startsWith("a") == true){
      arg.replace("a", "");
      int v = (int) arg.toFloat();
      if(v <= 0){
        Serial.println(F("ERR,bad_scan_count"));
      } else {
        nScansMax = v;
        if(nScansMax < nScansMin)
          nScansMin = nScansMax;
        darkValid = false; // the held dark belonged to the old settings
        Serial.print(F("OK,max_scans,"));
        Serial.println(nScansMax);
        delay(100);
      }

    } else if(arg.startsWith("n") == true){
      arg.replace("n", "");
      int v = (int) arg.toFloat();
      if(v <= 0){
        Serial.println(F("ERR,bad_scan_count"));
      } else {
        nScansMin = v;
        if(nScansMin > nScansMax)
          nScansMax = nScansMin;
        darkValid = false; // the held dark belonged to the old settings
        Serial.print(F("OK,min_scans,"));
        Serial.println(nScansMin);
        delay(100);
      }

    } else if(arg.startsWith("u") == true){
      arg.replace("u", "");
      int v = (int) arg.toFloat();
      if(v < 0 || v > 9999){
        Serial.println(F("ERR,bad_unit"));
      } else {
        saveUnitNumber(v);
        Serial.print(F("OK,unit,"));
        Serial.println(unitNumber);
      }

    } else if(arg.startsWith("sD") == true){
      saveWheelPosition('D', currentWheelAngle);
      Serial.print(F("OK,saved,dark,"));
      Serial.println(posDark);

    } else if(arg.startsWith("sI") == true){
      saveWheelPosition('I', currentWheelAngle);
      Serial.print(F("OK,saved,irr,"));
      Serial.println(posIrr);

    } else if(arg.startsWith("sR") == true){
      saveWheelPosition('R', currentWheelAngle);
      Serial.print(F("OK,saved,rad,"));
      Serial.println(posRad);

    } else if(arg.startsWith("g") == true || arg.startsWith("?") == true){
      Serial.print(F("CFG,unit:")); Serial.print(unitNumber);
      Serial.print(F(",dark:")); Serial.print(posDark);
      Serial.print(F(",irr:")); Serial.print(posIrr);
      Serial.print(F(",rad:")); Serial.print(posRad);
      Serial.print(F(",configured:")); Serial.print(configured ? 1 : 0);
      Serial.print(F(",fw:")); Serial.println(F(FIRMWARE_VERSION));

    } else if(arg.startsWith("d") == true){
      int rawMin, rawMax;
      float roughness, repeat;
      sensorSelfTest(rawMin, rawMax, roughness, repeat);
      Serial.print(F("DIAG,min:")); Serial.print(rawMin);
      Serial.print(F(",max:")); Serial.print(rawMax);
      Serial.print(F(",roughness:")); Serial.print(roughness, 2);
      Serial.print(F(",repeat:")); Serial.println(repeat, 2);

    } else if(arg.startsWith("w") == true){
        arg.replace("w", "");
        int wheelAngle = (int) arg.toFloat();
        if(wheelAngle < 0 || wheelAngle > 180){
          Serial.println(F("ERR,angle_out_of_range"));
        } else {
          myservo.attach(servoPin);
          myservo.write(wheelAngle);
          currentWheelAngle = wheelAngle;
          // Deliberately not moveWheel(): jogging always drives the servo,
          // because during calibration the wheel gets turned by hand between
          // jogs, and this path skips the detach settle delay a scan needs.
          // Leaving it "unsettled" makes the next measurement re drive and
          // settle it properly.
          wheelSettled = false;
          Serial.print(F("OK,wheel,"));
          Serial.println(wheelAngle);
          delay(servoDelay);
          myservo.detach();
        }

    } else if(arg.startsWith("p") == true){
      // Park the shutter closed. Every r/i measurement already ends on the
      // dark position; this is for the two paths that do not (a live run,
      // which leaves the wheel on the light position so it can stay there,
      // and a measurement the app abandoned part way through).
      //
      // The reply goes out before the move so the app is not held for the
      // servo's settle time. Commands sent meanwhile wait in the serial
      // buffer for the next pass through loop().
      Serial.println(F("OK,parked"));
      moveWheel(posDark);

    } else if(arg.startsWith("l") == true){
      // Live measurement: same DATA reply as r/i, but reuses the dark reference
      // when it can. See takeMeasurement().
      char m = arg.charAt(1);
      if(m == 'r' || m == 'i'){
        takeMeasurement(m == 'r' ? 1 : 0, true);
      } else {
        Serial.println(F("ERR,bad_mode"));
      }

    } else if(arg.startsWith("r") == true || arg.startsWith("i") == true){
      takeMeasurement(arg.startsWith("r") ? 1 : 0, false);

    } else {
      Serial.println(F("ERR,unknown_command"));
    }
  }

  delay(10);

}
