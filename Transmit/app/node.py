// altimu_xbee_node.ino
// Arduino Nano (ATmega328P) + Pololu AltIMU-10 v5 + XBee telemetry node
//
// Samples LSM6DS33 (accel/gyro), LIS3MDL (mag) and LPS25H (baro) over I2C at
// 50 Hz, streams a framed binary packet to the XBee every sample, and prints
// human-readable values on USB serial at 10 Hz.
//
// Wiring (matches the schematic):
//   AltIMU VIN->5V  GND->GND  SCL->A5  SDA->A4   (VDD, SA0 unconnected)
//   XBee   DOUT->D2   DIN<-D3 through a 5V->3.3V shifter
//   XBee   VCC from a dedicated 3.3 V regulator, GND common
//
// Libraries (Arduino Library Manager): "LSM6", "LIS3MDL", "LPS" by Pololu.
//
// XBee: transparent (AT) mode, both radios on the same PAN ID/channel,
// interface baud 38400 (ATBD 5) to match XBEE_BAUD below.
//
// Packet format, 35 bytes, little-endian. Python: struct.unpack('<BBBIH9hihH', buf)
//   sync 0xAA 0x55 | len=30 | t_ms u32 | seq u16
//   ax ay az gx gy gz mx my mz  (int16 raw, 9 values)
//   p_raw int32 | t_raw int16 | crc u16
//   crc  = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over len..t_raw (31 bytes)
//   scale: accel 0.061 mg/LSB   gyro 8.75 mdps/LSB   mag 1/6842 gauss/LSB
//          pressure p_raw/4096 mbar   temp 42.5 + t_raw/480 degC
//
// Send 'z' (over USB or XBee) to zero the altitude reference.

#include <Wire.h>
#include <SoftwareSerial.h>
#include <LSM6.h>
#include <LIS3MDL.h>
#include <LPS.h>

// ---------------- configuration ----------------
static const uint8_t  XBEE_RX_PIN = 2;       // D2 <- XBee DOUT
static const uint8_t  XBEE_TX_PIN = 3;       // D3 -> XBee DIN (via shifter)
static const uint32_t XBEE_BAUD   = 38400;   // SoftwareSerial ceiling; match ATBD
static const uint32_t USB_BAUD    = 115200;
static const uint16_t PERIOD_MS   = 20;      // 50 Hz sample + transmit
static const uint8_t  PRINT_DIV   = 5;       // print every 5th sample (10 Hz)
#define PRINT_CSV 0                          // 1 = CSV lines, 0 = labeled text

// scale factors for the full-scale ranges configured in setup()
static const float ACC_G_PER_LSB   = 0.061f / 1000.0f;   // +/-2 g
static const float GYR_DPS_PER_LSB = 8.75f  / 1000.0f;   // +/-245 dps
static const float MAG_G_PER_LSB   = 1.0f   / 6842.0f;   // +/-4 gauss

// ---------------- objects ----------------
SoftwareSerial xbee(XBEE_RX_PIN, XBEE_TX_PIN);
LSM6    imu;
LIS3MDL mag;
LPS     ps;

struct __attribute__((packed)) Packet {
  uint8_t  sync0;
  uint8_t  sync1;
  uint8_t  len;       // payload bytes: t_ms .. t_raw = 30
  uint32_t t_ms;
  uint16_t seq;
  int16_t  a[3];
  int16_t  g[3];
  int16_t  m[3];
  int32_t  p_raw;
  int16_t  t_raw;
  uint16_t crc;
};

static Packet   pkt;
static uint16_t seq        = 0;
static float    p0_mbar    = 1013.25f;   // altitude reference
static uint32_t next_tick  = 0;
static uint8_t  print_ctr  = 0;

// ---------------- helpers ----------------
static uint16_t crc16(const uint8_t* d, uint8_t n) {
  uint16_t crc = 0xFFFF;
  while (n--) {
    crc ^= (uint16_t)(*d++) << 8;
    for (uint8_t i = 0; i < 8; i++) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                           : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

static void halt(const __FlashStringHelper* msg) {
  Serial.println(msg);
  pinMode(LED_BUILTIN, OUTPUT);
  for (;;) {
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
    delay(150);
  }
}

static void handleCommand(int c) {
  if (c == 'z' || c == 'Z') {
    p0_mbar = ps.readPressureMillibars();
    Serial.print(F("# altitude zeroed at "));
    Serial.print(p0_mbar, 2);
    Serial.println(F(" mbar"));
  }
}

static void print3(float x, float y, float z, uint8_t dec, char sep) {
  Serial.print(x, dec); Serial.print(sep);
  Serial.print(y, dec); Serial.print(sep);
  Serial.print(z, dec);
}

// ---------------- setup ----------------
void setup() {
  Serial.begin(USB_BAUD);
  xbee.begin(XBEE_BAUD);
  Wire.begin();
  Wire.setClock(400000);                    // all three chips support fast mode

  if (!imu.init()) halt(F("LSM6DS33 not found on I2C"));
  if (!mag.init()) halt(F("LIS3MDL not found on I2C"));
  if (!ps.init())  halt(F("LPS25H not found on I2C"));

  imu.enableDefault();
  imu.writeReg(LSM6::CTRL1_XL, 0x40);       // accel 104 Hz, +/-2 g
  imu.writeReg(LSM6::CTRL2_G,  0x40);       // gyro  104 Hz, +/-245 dps

  mag.enableDefault();                      // +/-4 gauss, continuous
  mag.writeReg(LIS3MDL::CTRL_REG1, 0x7C);   // ultra-high-perf XY, 80 Hz

  ps.enableDefault();
  ps.writeReg(LPS::CTRL_REG1, 0xC0);        // active, 25 Hz

  delay(100);                               // let the first conversions land
  p0_mbar = ps.readPressureMillibars();

  pkt.sync0 = 0xAA;
  pkt.sync1 = 0x55;
  pkt.len   = 30;

#if PRINT_CSV
  Serial.println(F("t_ms,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps,mx_G,my_G,mz_G,p_mbar,alt_m,temp_C"));
#else
  Serial.println(F("# AltIMU-10 v5 node up. 'z' zeroes altitude."));
#endif
  next_tick = millis();
}

// ---------------- loop ----------------
void loop() {
  while (xbee.available())   handleCommand(xbee.read());
  while (Serial.available()) handleCommand(Serial.read());

  uint32_t now = millis();
  if ((int32_t)(now - next_tick) < 0) return;   // not time yet
  next_tick += PERIOD_MS;

  // ---- read sensors ----
  imu.read();
  mag.read();
  int32_t p_raw = ps.readPressureRaw();
  int16_t t_raw = ps.readTemperatureRaw();

  // ---- transmit ----
  pkt.t_ms  = now;
  pkt.seq   = seq++;
  pkt.a[0] = imu.a.x; pkt.a[1] = imu.a.y; pkt.a[2] = imu.a.z;
  pkt.g[0] = imu.g.x; pkt.g[1] = imu.g.y; pkt.g[2] = imu.g.z;
  pkt.m[0] = mag.m.x; pkt.m[1] = mag.m.y; pkt.m[2] = mag.m.z;
  pkt.p_raw = p_raw;
  pkt.t_raw = t_raw;
  pkt.crc   = crc16((const uint8_t*)&pkt + 2, 1 + pkt.len);
  xbee.write((const uint8_t*)&pkt, sizeof(pkt));

  // ---- print (decimated) ----
  if (++print_ctr < PRINT_DIV) return;
  print_ctr = 0;

  float p_mbar = p_raw / 4096.0f;
  float alt_m  = ps.pressureToAltitudeMeters(p_mbar, p0_mbar);
  float temp_c = 42.5f + t_raw / 480.0f;

#if PRINT_CSV
  Serial.print(now); Serial.print(',');
  print3(imu.a.x * ACC_G_PER_LSB,   imu.a.y * ACC_G_PER_LSB,   imu.a.z * ACC_G_PER_LSB,   3, ','); Serial.print(',');
  print3(imu.g.x * GYR_DPS_PER_LSB, imu.g.y * GYR_DPS_PER_LSB, imu.g.z * GYR_DPS_PER_LSB, 2, ','); Serial.print(',');
  print3(mag.m.x * MAG_G_PER_LSB,   mag.m.y * MAG_G_PER_LSB,   mag.m.z * MAG_G_PER_LSB,   3, ','); Serial.print(',');
  Serial.print(p_mbar, 2); Serial.print(',');
  Serial.print(alt_m, 1);  Serial.print(',');
  Serial.println(temp_c, 1);
#else
  Serial.print(F("t="));        Serial.print(now);
  Serial.print(F(" | a(g) "));  print3(imu.a.x * ACC_G_PER_LSB,   imu.a.y * ACC_G_PER_LSB,   imu.a.z * ACC_G_PER_LSB,   3, ' ');
  Serial.print(F(" | g(dps) ")); print3(imu.g.x * GYR_DPS_PER_LSB, imu.g.y * GYR_DPS_PER_LSB, imu.g.z * GYR_DPS_PER_LSB, 1, ' ');
  Serial.print(F(" | m(G) "));  print3(mag.m.x * MAG_G_PER_LSB,   mag.m.y * MAG_G_PER_LSB,   mag.m.z * MAG_G_PER_LSB,   3, ' ');
  Serial.print(F(" | p "));     Serial.print(p_mbar, 2);
  Serial.print(F(" mb alt "));  Serial.print(alt_m, 1);
  Serial.print(F(" m T "));     Serial.print(temp_c, 1);
  Serial.println(F(" C"));
#endif
}
