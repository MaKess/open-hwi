static constexpr uint8_t kRs485DePin = 3;
static constexpr uint8_t kRs485RePin = 2;
static constexpr uint32_t kBaud = 41666;
static constexpr uint8_t kPacketStart = 0xB0;
static constexpr uint32_t kBitsPerByte = 10; // 8N1 framing.
static constexpr uint32_t kMicrosPerSecond = 1000000;
static constexpr uint32_t kByteTimeMicros =
    (kBitsPerByte * kMicrosPerSecond + (kBaud - 1)) / kBaud;

enum ControlByte : uint8_t {
  ESCAPE_BYTE = 0xFF,
  ACK_BYTE = 0xFE,
  GAP_BYTE = 0xFD
};

enum ParserState : uint8_t {
  WAIT_START = 0,
  READ_TYPE,
  READ_LENGTH,
  READ_PAYLOAD,
  READ_CHECKSUM,
  READ_END
};

// Packet format (length includes all bytes):
// +--------+------+--------+-----------------+-----------+-----+
// | 0xB0   | TYPE | LENGTH | PAYLOAD (1..N)  | CHECKSUM  | 0x00|
// +--------+------+--------+-----------------+-----------+-----+
// LENGTH = total bytes from 0xB0 through 0x00.
// CHECKSUM makes the unsigned byte sum of all bytes equal 0.
class PacketParser {
 public:
  PacketParser() {
    reset();
  }

  void reset() {
    state_ = WAIT_START;
    type_ = 0;
    length_ = 0;
    remaining_ = 0;
    sum_ = 0;
    lastValid_ = false;
    packetDone_ = false;
    sawStart_ = false;
  }

  void update(uint8_t byte) {
    switch (state_) {
      case WAIT_START:
        if (byte == kPacketStart) {
          state_ = READ_TYPE;
          sum_ = byte;
          length_ = 0;
          remaining_ = 0;
          packetDone_ = false;
          sawStart_ = true;
        }
        break;
      case READ_TYPE:
        type_ = byte;
        sum_ = static_cast<uint8_t>(sum_ + byte);
        state_ = READ_LENGTH;
        break;
      case READ_LENGTH:
        length_ = byte;
        sum_ = static_cast<uint8_t>(sum_ + byte);
        if (length_ < 6) {
          reset();
          break;
        }
        remaining_ = static_cast<uint8_t>(length_ - 3);
        state_ = READ_PAYLOAD;
        break;
      case READ_PAYLOAD:
        sum_ = static_cast<uint8_t>(sum_ + byte);
        if (remaining_ > 0) {
          remaining_--;
        }
        if (remaining_ == 2) {
          state_ = READ_CHECKSUM;
        }
        break;
      case READ_CHECKSUM:
        lastValid_ = (static_cast<uint8_t>(sum_ + byte) == 0);
        if (remaining_ > 0) {
          remaining_--;
        }
        state_ = READ_END;
        break;
      case READ_END:
        if (byte != 0) {
          lastValid_ = false;
        }
        if (remaining_ > 0) {
          remaining_--;
        }
        state_ = WAIT_START;
        if (remaining_ == 0) {
          packetDone_ = true;
        }
        break;
      default:
        reset();
        break;
    }
  }

  bool lastValid() const {
    return lastValid_;
  }

  bool inPacket() const {
    return state_ != WAIT_START;
  }

  bool packetDone() const {
    return packetDone_;
  }

  void clearPacketDone() {
    packetDone_ = false;
  }

  bool sawStart() const {
    return sawStart_;
  }

  void clearSawStart() {
    sawStart_ = false;
  }

 private:
  ParserState state_;
  uint8_t type_;
  uint8_t length_;
  uint8_t remaining_;
  uint8_t sum_;
  bool lastValid_;
  bool packetDone_;
  bool sawStart_;
};

static PacketParser gUsbToUartParser;
static PacketParser gUartToUsbParser;
static uint32_t gLastUartByteMicros = 0;

static void sendUsbDataByte(uint8_t byte) {
  if (byte == ESCAPE_BYTE) {
    Serial.write(ESCAPE_BYTE);
  }
  Serial.write(byte);
}

static void sendUsbAck() {
  Serial.write(ESCAPE_BYTE);
  Serial.write(ACK_BYTE);
}

static void sendUsbGap() {
  Serial.write(ESCAPE_BYTE);
  Serial.write(GAP_BYTE);
}

class Rs485State {
 public:
  void init() {
    setMode(false);
    txActive_ = false;
  }

  void beginIfStartSeen(PacketParser &parser) {
    if (!txActive_ && parser.sawStart()) {
      setMode(true);
      delayMicroseconds(kByteTimeMicros);
      txActive_ = true;
      parser.clearSawStart();
    }
  }

  bool shouldTransmit() const {
    return txActive_;
  }

  void endIfPacketDone(PacketParser &parser) {
    if (txActive_ && parser.packetDone()) {
      Serial1.flush();
      setMode(false);
      txActive_ = false;
    }
  }

 private:
  void setMode(bool outputMode) {
    if (outputMode) {
      digitalWrite(kRs485RePin, HIGH);
      digitalWrite(kRs485DePin, HIGH);
    } else {
      digitalWrite(kRs485RePin, LOW);
      digitalWrite(kRs485DePin, LOW);
    }
  }

  bool txActive_;
};

static Rs485State gRs485State;

void setup() {
  Serial.begin(kBaud);
  Serial1.begin(kBaud);
  pinMode(kRs485DePin, OUTPUT);
  pinMode(kRs485RePin, OUTPUT);
  gRs485State.init();
  gUsbToUartParser.reset();
  gUartToUsbParser.reset();
  gLastUartByteMicros = micros();
}

void loop() {
  while (Serial1.available() > 0) {
    // UART -> USB path: forward every byte, track packet boundaries, and
    // insert a GAP marker when inter-byte spacing exceeds two byte times.
    const uint8_t inByte = static_cast<uint8_t>(Serial1.read());
    const uint32_t nowMicros = micros();
    const uint32_t deltaMicros = nowMicros - gLastUartByteMicros;
    if (deltaMicros > (kByteTimeMicros * 2)) {
      sendUsbGap();
    }
    gLastUartByteMicros = nowMicros;
    gUartToUsbParser.update(inByte);
    sendUsbDataByte(inByte);
  }

  while (!gUartToUsbParser.inPacket() && Serial.available() > 0) {
    // USB -> UART path: parse the USB byte stream, start RS485 TX only at the
    // 0xB0 packet boundary, and keep TX enabled until the full packet is sent.
    // We avoid starting TX while UART RX is mid-packet to keep RS485 simplex.
    const uint8_t inByte = static_cast<uint8_t>(Serial.read());
    gUsbToUartParser.update(inByte);
    gRs485State.beginIfStartSeen(gUsbToUartParser);
    if (!gRs485State.shouldTransmit())
      continue;
    Serial1.write(inByte);
    gRs485State.endIfPacketDone(gUsbToUartParser);
    if (gUsbToUartParser.packetDone()) {
      sendUsbAck();
      gUsbToUartParser.clearPacketDone();
    }
  }
}
