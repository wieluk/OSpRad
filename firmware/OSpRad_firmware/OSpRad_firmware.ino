
/*
 * OSpRad firmware 3.1.0
 * Written by Jolyon Troscianko - 2022
 * Released under GPL-3.0 license
 * https://github.com/troscianko/OSpRad
 *
 * For the Arduino Nano. Tested with Elegoo units and unbranded clones; use the
 * "old bootloader 328p" option if normal upload fails.
 * Inspired by https://impfs.github.io/review/
 * C12880MA datasheet: https://www.farnell.com/datasheets/2822646.pdf
 *
 * ---- 2.0.0 ----
 * Unit number and shutter-wheel positions (dark/irradiance/radiance) moved to
 * EEPROM, settable from the Python app - no more editing constants and
 * reflashing per unit. Values below are defaults used only on a fresh Arduino.
 *
 * ---- 2.1.0 ----
 * 'd' diagnostic command: app flags likely-disconnected sensor at connect time.
 * No equivalent for the servo - RC servos are open-loop.
 *
 * ---- 3.0.0 ----
 * Commands are newline-terminated. The old readString() waited out ~1s of
 * serial silence to decide a command was "done", so every command cost ~1s,
 * and a UI slider firing faster than that could glue multiple commands into
 * garbage (arg.replace() strips every matching letter). Newline framing
 * parses exactly one command per call and replies in a few ms.
 *
 * Breaking wire-protocol change vs 2.x: the app must send '\n' after every
 * command. The 3.x app is required.
 *
 * ---- 3.1.0 ----
 * 'd' self-test also reports roughness (mean absolute difference between
 * adjacent pixels). Raw ADC swing (max-min) alone was worthless on
 * disconnected units - it ranged ~60-166 across identical conditions.
 * Roughness held at 0.7-1.2 because a floating pin picks up slow-drifting
 * interference that moves amplitude around without making adjacent samples
 * jump. The app now thresholds on roughness.
 *
 * New serial commands (see loop()):
 *   u<n>  - set unit number (EEPROM)
 *   sD/sI/sR - save current wheel angle as dark / irradiance / radiance position
 *   g     - report config: "CFG,unit:<n>,dark:<a>,irr:<a>,rad:<a>,configured:<0|1>,fw:<ver>"
 *   d     - sensor self-test: "DIAG,min:<n>,max:<n>,roughness:<f>"
 *           the app thresholds on roughness, not min/max.
 *
 * Replies are framed with a type prefix so the app never has to guess:
 * "OK,..." / "ERR,<reason>" / "CFG,..." / "DATA,..." / "DIAG,...".
 * DATA lines also carry a trailing checksum so a corrupted/truncated reply
 * from a flaky USB connection can be retried.
 *
 * Breaking protocol change vs 1.x: 3.x app requires this firmware line.
 */



#include <Servo.h>
#include <EEPROM.h>
Servo myservo;  // create servo object to control a servo

#define FIRMWARE_VERSION "3.1.0"


// ---- EEPROM layout ----
#define EEPROM_MAGIC 0xA5
#define EE_ADDR_MAGIC 0
#define EE_ADDR_UNIT 1     // int, 2 bytes
#define EE_ADDR_DARK 3     // int, 2 bytes
#define EE_ADDR_IRR 5      // int, 2 bytes
#define EE_ADDR_RAD 7      // int, 2 bytes

bool configured = false; // true once EEPROM has been written at least once


// THESE FOUR VALUES ARE ONLY USED UNTIL THE UNIT IS CONFIGURED VIA EEPROM (see above):

int unitNumber = 1; // Add a unit-specific number here. This number is looked up for applying calibration data

// Fallback defaults only - once configured, set/change these from the OSpRad app instead
// (Python app: Unit & wheel setup wizard). Manual "w<angle>" jogging still works if needed.
int posDark = 98; // angle for dark measurement
int posIrr = 146; // angle for irradiance (cosine diffuser)
int posRad = 57; // angle for radiance measurement (clear)

int currentWheelAngle = 90; // tracks the last angle set via w<angle>, used by sD/sI/sR




int servoDelay = 300; // millisecond delay for servo to move
int servoDetachDelay = 1500; // millisecond delay for servo to detach (causes feedback and noisy measurements)
int servoPin = 8;

#define TRGpin A0
#define STpin A1
#define CLKpin A2
#define VIDEOpin A3

#define nSites 288 //
uint16_t data[nSites] [2];
int dataSaveDim = 0;

int delayTime = 1;
long intTime = 100;
long prevIntTime = 100;
long maxAutoIntTime = 5000;
long maxIntTime = 60000; // maximum integration time for auto-measurement
long manIntTime = 0;
int satN = 0; // number of bands over-exposed
int satVal = 1000; // over-exposure value
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
    configured = false; // still running on source-level defaults above
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


// Stream helpers that write straight to Serial while accumulating the running
// line checksum, instead of building a temporary String for every value. The
// DATA line used to concatenate ~290 short-lived String objects (one per
// site), which repeatedly allocated/freed on a 2KB heap and could fragment
// it; these overloads produce byte-identical output with no heap use.
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
  //myservo.attach(servoPin);  // attaches the servo on pin 9 to the servo object

  loadConfig();

  //Set desired pins to OUTPUT
  pinMode(CLKpin, OUTPUT);
  pinMode(STpin, OUTPUT);

  digitalWrite(CLKpin, HIGH);
  digitalWrite(STpin, LOW);

  Serial.begin(115200); // Baud Rate set to 115200
  // Commands are newline-terminated (see loop()), so this only ever gets hit by a
  // truly malformed/incomplete command - keep it short so that fails fast rather
  // than hanging for a second.
  Serial.setTimeout(200);
  while (! Serial); // Wait untilSerial is ready
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

// Quick raw scan used by the 'd' diagnostic command to let the app flag a
// likely-disconnected sensor. Deliberately does not touch the servo (that
// has no feedback path at all, so it can't be self-tested this way) and
// restores intTime/dataSaveDim afterwards so it has no side effect on the
// user's configured measurement settings.
//
// Reports min/max (raw amplitude) AND roughness (mean absolute difference
// between adjacent pixels). Amplitude alone proved unreliable - the ambient
// noise floor on a floating/disconnected pin swings wildly with unrelated
// conditions (observed 20-166 on the same disconnected unit). Roughness is a
// different, more physically-grounded signal: a real sensor reads out as a
// smooth curve (adjacent pixels are physically/electrically similar, so
// consecutive samples are correlated), while noise picked up by a floating
// pin is essentially uncorrelated between samples - so its roughness should
// track its own amplitude (rough ~= range) regardless of how loud that
// amplitude is, while a connected sensor's roughness should stay low even
// when its overall range is large. Still no real connected-sensor data to
// confirm the second half of that - see the app-side comment where this is
// consumed.
void sensorSelfTest(int &outMin, int &outMax, float &outRoughness){
  long savedIntTime = intTime;
  int savedDim = dataSaveDim;

  intTime = 5;
  dataSaveDim = 0;
  resetData();
  readSpectrometer();

  outMin = data[0][0];
  outMax = data[0][0];
  long roughSum = 0;
  for(int i = 1; i < nSites; i++){
    if(data[i][0] < outMin) outMin = data[i][0];
    if(data[i][0] > outMax) outMax = data[i][0];
    roughSum += abs((long)data[i][0] - (long)data[i-1][0]);
  }
  outRoughness = float(roughSum) / float(nSites - 1);

  intTime = savedIntTime;
  dataSaveDim = savedDim;
}


void loop(){



  // Newline-terminated: parses exactly one command per call no matter how many are
  // already queued up in the receive buffer, instead of readString()'s old behaviour
  // of gulping everything available and waiting out ~1s of silence to decide a
  // command was "done" - see the 3.0.0 changelog above.
  String arg = Serial.readStringUntil('\n');
  arg.trim();

  if (arg.length() > 0){

    // manually set integration time
    if(arg.startsWith("t") == true){
      arg.replace("t", "");
      long t = (long) arg.toFloat();
      if(t < 0){
        Serial.println(F("ERR,bad_int_time"));
      } else {
        manIntTime = t;
        if(manIntTime > maxIntTime)
            manIntTime = maxIntTime;
        Serial.print(F("OK,int_time,"));
        Serial.println(manIntTime);
      }

    // change max number of scans
    } else if(arg.startsWith("a") == true){
      arg.replace("a", "");
      int v = (int) arg.toFloat();
      if(v <= 0){
        Serial.println(F("ERR,bad_scan_count"));
      } else {
        nScansMax = v;
        if(nScansMax < nScansMin)
          nScansMin = nScansMax;
        Serial.print(F("OK,max_scans,"));
        Serial.println(nScansMax);
        delay(100);
      }

    // change min number of scans
    } else if(arg.startsWith("n") == true){
      arg.replace("n", "");
      int v = (int) arg.toFloat();
      if(v <= 0){
        Serial.println(F("ERR,bad_scan_count"));
      } else {
        nScansMin = v;
        if(nScansMin > nScansMax)
          nScansMax = nScansMin;
        Serial.print(F("OK,min_scans,"));
        Serial.println(nScansMin);
        delay(100);
      }

    // set unit number (saved to EEPROM)
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

    // save current wheel angle as dark/irradiance/radiance position
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

    // report current config
    } else if(arg.startsWith("g") == true || arg.startsWith("?") == true){
      Serial.print(F("CFG,unit:")); Serial.print(unitNumber);
      Serial.print(F(",dark:")); Serial.print(posDark);
      Serial.print(F(",irr:")); Serial.print(posIrr);
      Serial.print(F(",rad:")); Serial.print(posRad);
      Serial.print(F(",configured:")); Serial.print(configured ? 1 : 0);
      Serial.print(F(",fw:")); Serial.println(F(FIRMWARE_VERSION));

    // quick sensor self-test: one raw scan, no servo movement. The app
    // decides what counts as "likely disconnected" from min/max - firmware
    // just reports the raw numbers.
    } else if(arg.startsWith("d") == true){
      int rawMin, rawMax;
      float roughness;
      sensorSelfTest(rawMin, rawMax, roughness);
      Serial.print(F("DIAG,min:")); Serial.print(rawMin);
      Serial.print(F(",max:")); Serial.print(rawMax);
      Serial.print(F(",roughness:")); Serial.println(roughness, 2);

    // manual wheel position
    } else if(arg.startsWith("w") == true){ // filter wheel position
        arg.replace("w", "");
        int wheelAngle = (int) arg.toFloat();
        if(wheelAngle < 0 || wheelAngle > 180){
          Serial.println(F("ERR,angle_out_of_range"));
        } else {
          myservo.attach(servoPin);
          myservo.write(wheelAngle);
          currentWheelAngle = wheelAngle;
          Serial.print(F("OK,wheel,"));
          Serial.println(wheelAngle);
          delay(servoDelay);
          myservo.detach();
        }


    // Spec measure
    } else if(arg.startsWith("r") == true || arg.startsWith("i") == true){ // radiance

      myservo.attach(servoPin);

      if(arg.startsWith("r") == true){
        myservo.write(posRad);
        currentWheelAngle = posRad;
        measureType = 1;
      } else {
        myservo.write(posIrr);
        currentWheelAngle = posIrr;
        measureType = 0;
      }

      delay(servoDelay);
      myservo.detach();
      delay(servoDetachDelay);
      nScans = 1;


      // reset all data
      dataSaveDim = 1; // 1= dark, 0=light
      resetData();
      dataSaveDim = 0;// must be left as 0 here for code below - temp light data
      resetData();

      // automatically work out integration time by increasing until saturation point, then estimate ideal value
      if(manIntTime == 0){

            satN = 0;
            intTime = 1;
            prevIntTime = 1;
            maxVal = 0;

            resetData(); // reset dim0 data
            readSpectrometer(); // read to dim0
            satTest();

            if(satN == 0){ // if initial 1ms scan is over-exposed don't go any further

              while(satN == 0){
                prevMaxVal = maxVal;
                prevIntTime = intTime;
                intTime = intTime*2;
                if(intTime > maxAutoIntTime) // do not go above max int time (takes ages to measure)
                  satN = 1;
                else {
                  resetData(); // reset dim0 data
                  readSpectrometer(); // read to dim0
                  satTest();
                }

                delay(10);
              }
              resetData();
              // ensure auto-value isn't too long
              float tInt = floor(float(prevIntTime*0.9*satVal)/float(prevMaxVal));
              if(tInt > maxIntTime)
                  intTime = maxIntTime;
              else intTime = tInt;
            }

      } else { // initial scan with manual integration time
            intTime = manIntTime;
      }


        nScans = floor(sampleTimeMax/intTime);
        if(nScans < nScansMin)
          nScans = nScansMin;
        if(nScans > nScansMax)
           nScans = nScansMax;

      satSum = 0;

      //-------------Integration time is short, collect sample data, then dark measurement---------
      if(intTime < sampleTimeMax){

        dataSaveDim = 1; // light data
        for(int i=0; i<nScans; i++){ // repeatedly read spec (note one fewer scan than below because one scan is already done above)
          readSpectrometer();
          satTest();
          satSum = satSum + satN;
          delay(10);
        }

        myservo.attach(servoPin);
        myservo.write(posDark);
        delay(servoDelay);
        myservo.detach();
        delay(servoDetachDelay);
        dataSaveDim = 0;// dark data
        resetData();

        for(int i=0; i<nScans; i++){ // repeatedly read spec
          readSpectrometer();
          delay(10);
       }

      //-------------Integration time is long, take interleaved dark measurements---------
      } else {

        for(int i=0; i<nScans; i++){
              // light measuremeant
              myservo.attach(servoPin);
              if(measureType == 1){
                myservo.write(posRad);
              } else {
                myservo.write(posIrr);
              }
              delay(servoDelay);
              myservo.detach();
              delay(servoDetachDelay);
              dataSaveDim = 1; // light data
              readSpectrometer(); // add light measurement
              satTest();
              satSum = satSum + satN;
              delay(10);

              myservo.attach(servoPin);
              myservo.write(posDark);
              delay(servoDelay);
              myservo.detach();
              delay(servoDetachDelay);
              dataSaveDim = 0; // dark data
              readSpectrometer(); // add dark measurement

        }

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

    } else {
      Serial.println(F("ERR,unknown_command"));
    }
  }

  delay(10);

}
