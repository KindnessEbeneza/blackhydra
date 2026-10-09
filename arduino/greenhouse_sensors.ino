/**
 * Greenhouse Guardian — Sensor Sketch
 * Target: ESP32 (Wokwi simulation)
 *
 * Outputs one JSON line per second to Serial:
 *   {"z":[45,62,38,71],"t":28.4,"h":67,"l":820,"flow":2.3,"tank":78,"fault":""}
 *
 * Fault injection via digital pins:
 *   D2 = leak        D3 = blocked_line
 *   D4 = empty_tank  D5 = heatwave
 *   D6 = broken_sensor
 */

#include <math.h>

// ── Fault injection pins ──────────────────────────────────────────────────────
#define PIN_LEAK          2
#define PIN_BLOCKED       3
#define PIN_EMPTY_TANK    4
#define PIN_HEATWAVE      5
#define PIN_BROKEN_SENSOR 6

// ── Simulation state ──────────────────────────────────────────────────────────
float moisture[4]  = {55.0, 68.0, 42.0, 61.0};   // % per zone
float temp         = 22.0;   // °C
float humidity     = 65.0;   // %
int   light        = 600;    // lux proxy 0-1023
float flow         = 2.5;    // L/min
float tankLevel    = 85.0;   // %
long  tickCount    = 0;
unsigned long lastMs = 0;

// ── Helpers ───────────────────────────────────────────────────────────────────
float addNoise(float val, float amplitude) {
  return val + (((float)random(-1000, 1000)) / 1000.0) * amplitude;
}

float clamp(float v, float lo, float hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// ── Simulate a 24-h day cycling via tickCount ─────────────────────────────────
void updateEnvironment() {
  // One full day = 86400 ticks (at 1Hz, 24 real hours; sped up by Python)
  float hour = fmod((float)tickCount / 3600.0, 24.0);

  // Temperature: min 18°C at 06:00, max 34°C at 14:00
  temp = 26.0 + 8.0 * sin((hour - 10.0) * M_PI / 12.0);

  // Humidity: inverse of temperature cycle
  humidity = 72.0 - 14.0 * sin((hour - 10.0) * M_PI / 12.0);

  // Light: trapezoid 06:00–19:00, peak 900 lux 11:00–15:00
  if (hour < 6.0 || hour > 19.0)       light = 0;
  else if (hour < 11.0)                 light = (int)(900.0 * (hour - 6.0) / 5.0);
  else if (hour <= 15.0)                light = 900;
  else                                   light = (int)(900.0 * (19.0 - hour) / 4.0);

  // Passive soil drying: faster when hot + bright
  float etRate = 0.003 * (temp / 25.0) * (light / 900.0 + 0.1);
  for (int i = 0; i < 4; i++) {
    moisture[i] -= etRate;
    moisture[i] = clamp(moisture[i], 0, 100);
  }

  // Simple drip: water zone when below 30%, stop at 75%
  for (int i = 0; i < 4; i++) {
    if (moisture[i] < 30.0) moisture[i] += 0.8;   // drip ~0.8%/tick
    if (moisture[i] > 75.0) moisture[i] = 75.0;
  }

  // Tank drains slowly with use
  tankLevel -= 0.01;
  if (tankLevel < 0) tankLevel = 0;

  tickCount++;
}

// ── Read fault pins ───────────────────────────────────────────────────────────
String activeFault() {
  if (digitalRead(PIN_LEAK)          == HIGH) return "leak";
  if (digitalRead(PIN_BLOCKED)       == HIGH) return "blocked_line";
  if (digitalRead(PIN_EMPTY_TANK)    == HIGH) return "empty_tank";
  if (digitalRead(PIN_HEATWAVE)      == HIGH) return "heatwave";
  if (digitalRead(PIN_BROKEN_SENSOR) == HIGH) return "broken_sensor";
  return "";
}

// ── Apply fault modifications to readings ────────────────────────────────────
void applyFault(String fault, float outMoisture[], float &outTemp,
                float &outHumidity, float &outFlow, float &outTank) {
  if (fault == "leak") {
    // High flow but moisture barely rises → losing water
    outFlow = addNoise(8.5, 0.4);
    outTank -= 0.15;  // tank drains fast
  }
  else if (fault == "blocked_line") {
    // Pump on but no flow, moisture of zone 2 drops
    outFlow = addNoise(0.1, 0.05);
    outMoisture[2] -= 0.5;
  }
  else if (fault == "empty_tank") {
    outTank  = addNoise(0.0, 0.5);
    outFlow  = addNoise(0.0, 0.1);
  }
  else if (fault == "heatwave") {
    outTemp     = addNoise(38.5, 0.8);
    outHumidity = addNoise(28.0, 2.0);
    // Accelerate drying
    for (int i = 0; i < 4; i++) outMoisture[i] -= 1.2;
  }
  else if (fault == "broken_sensor") {
    // Zone 0 sensor reports stuck value
    outMoisture[0] = 52.0;   // stuck — won't change
  }
}

// ── Setup ─────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  pinMode(PIN_LEAK,          INPUT);
  pinMode(PIN_BLOCKED,       INPUT);
  pinMode(PIN_EMPTY_TANK,    INPUT);
  pinMode(PIN_HEATWAVE,      INPUT);
  pinMode(PIN_BROKEN_SENSOR, INPUT);

  randomSeed(analogRead(0));
  lastMs = millis();

  Serial.println("{\"status\":\"Greenhouse Guardian sensor node ready\"}");
}

// ── Main loop — emit JSON every 1000 ms ──────────────────────────────────────
void loop() {
  unsigned long now = millis();
  if (now - lastMs < 1000) return;
  lastMs = now;

  updateEnvironment();

  // Copy state for fault overlay
  float outM[4], outT, outH, outF, outK;
  for (int i = 0; i < 4; i++) outM[i] = moisture[i];
  outT = temp; outH = humidity; outF = flow; outK = tankLevel;

  String fault = activeFault();
  applyFault(fault, outM, outT, outH, outF, outK);

  // Add sensor noise to final readings
  for (int i = 0; i < 4; i++) outM[i] = clamp(addNoise(outM[i], 1.5), 0, 100);
  outT = addNoise(outT, 0.3);
  outH = clamp(addNoise(outH, 1.5), 0, 100);
  outF = max(0.0f, addNoise(outF, 0.1f));
  outK = clamp(addNoise(outK, 0.8), 0, 100);

  // Emit JSON
  Serial.print("{\"z\":[");
  for (int i = 0; i < 4; i++) {
    Serial.print(outM[i], 1);
    if (i < 3) Serial.print(",");
  }
  Serial.print("],\"t\":");  Serial.print(outT, 1);
  Serial.print(",\"h\":");   Serial.print(outH, 1);
  Serial.print(",\"l\":");   Serial.print(light);
  Serial.print(",\"flow\":"); Serial.print(outF, 2);
  Serial.print(",\"tank\":"); Serial.print(outK, 1);
  Serial.print(",\"fault\":\""); Serial.print(fault);
  Serial.println("\"}");
}
