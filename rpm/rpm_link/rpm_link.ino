static constexpr uint32_t kBaud = 7812; // 128 us bit

void setup() {
  Serial.begin(kBaud);
  Serial1.begin(kBaud);
}

void loop() {
  while (Serial.available() > 0) {
    const uint8_t inByte = static_cast<uint8_t>(Serial.read());
    Serial1.write(inByte);
  }
}
