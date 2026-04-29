#include<EEPROM.h>

static constexpr uint8_t kRs485DePin = 3;
static constexpr uint8_t kRs485RePin = 2;
static constexpr uint32_t kBaudLink = 41666; // 24us bit
static constexpr uint32_t kBaudRpm = 7812; // 128 us bit
static constexpr uint32_t kBitsPerByte = 10; // 8N1 framing
static constexpr uint32_t kMicrosPerSecond = 1000000;
static constexpr uint32_t kByteTimeMicrosLink = (kBitsPerByte * kMicrosPerSecond + (kBaudLink - 1)) / kBaudLink;

enum ModeByte : uint8_t {
  LINK_MODE = 1,
  RPM_MODE = 2,
};

struct Config {
  enum ModeByte mode;
  uint8_t ident;
} config;

enum ControlByte : uint8_t {
  ESCAPE_BYTE = 0xFF,
  ACK_BYTE = 0xFE,
  GAP_BYTE = 0xFD,
  MODE_SET_BYTE = 0xFC,
  IDENT_SET_BYTE = 0xFB,
  MODE_GET_BYTE = 0xFA,
  IDENT_GET_BYTE = 0xF9,

  LUTRON_START_BYTE = 0xB0,
  LUTRON_ACK_BYTE = 0x00,
};

enum ParserState : uint8_t {
  WAIT_START = 0,
  READ_TYPE,
  READ_LENGTH,
  READ_PAYLOAD,
  READ_CHECKSUM,
  READ_END,
  MODE_SET,
  IDENT_SET,
};

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

static void sendUsbMode() {
  Serial.write(ESCAPE_BYTE);
  Serial.write(MODE_GET_BYTE);
  Serial.write(config.mode);
}

static void sendUsbIdent() {
  Serial.write(ESCAPE_BYTE);
  Serial.write(IDENT_GET_BYTE);
  Serial.write(config.ident);
}

// Packet format (length includes all bytes):
// +--------+------+--------+-----------------+-----------+-----+
// | 0xB0   | TYPE | LENGTH | PAYLOAD (1..N)  | CHECKSUM  | 0x00|
// +--------+------+--------+-----------------+-----------+-----+
// LENGTH = total bytes from 0xB0 through 0x00.
// CHECKSUM makes the unsigned byte sum of all bytes equal 0.
class PacketParser {
 public:
  PacketParser(bool usb_to_uart) :
    usb_to_uart_{usb_to_uart} {
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
        switch (ControlByte{byte}) {
          case LUTRON_START_BYTE:
            state_ = READ_TYPE;
            sum_ = byte;
            length_ = 0;
            remaining_ = 0;
            packetDone_ = false;
            sawStart_ = true;
            break;
          case LUTRON_ACK_BYTE:
            sum_ = byte;
            length_ = 0;
            remaining_ = 0;
            packetDone_ = true;
            sawStart_ = true;
            break;
          case MODE_SET_BYTE:
            if (usb_to_uart_)
              state_ = MODE_SET;
            break;
          case IDENT_SET_BYTE:
            if (usb_to_uart_)
              state_ = IDENT_SET;
            break;
          case MODE_GET_BYTE:
            if (usb_to_uart_)
              sendUsbMode();
            break;
          case IDENT_GET_BYTE:
            if (usb_to_uart_)
              sendUsbIdent();
            break;
          default:
            break;
        }
        break;
      case MODE_SET: {
        const enum ModeByte mode{byte};
        if (mode != config.mode) {
          config.mode = byte;
          EEPROM.put(0, config);

          Serial.end();
          Serial1.end();

          setup();
        }
        }
        state_ = WAIT_START;
        break;
      case IDENT_SET: {
        const uint8_t ident{byte};
        if (ident != config.ident) {
          config.ident = byte;
          EEPROM.put(0, config);
        }
        state_ = WAIT_START;
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
  const bool usb_to_uart_;
};

static PacketParser gUsbToUartParser(true);
static PacketParser gUartToUsbParser(false);
static uint32_t gLastUartByteMicros = 0;

class Rs485State {
 public:
  void init() {
    setMode(false);
  }

  void beginIfStartSeen(PacketParser &parser) {
    if (!txActive_ && parser.sawStart()) {
      setMode(true);
      delayMicroseconds(kByteTimeMicrosLink);
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
    txActive_ = outputMode;
  }

  bool txActive_;
};

static Rs485State gRs485State;

class BasicParser {
 public:
  BasicParser() {
    reset();
  }

  void reset() {
    state_ = WAIT_START;
  }

  void update(uint8_t byte) {
    switch (state_) {
      case WAIT_START:
        switch (byte) {
          case MODE_SET_BYTE:
            state_ = MODE_SET;
            break;
          case IDENT_SET_BYTE:
            state_ = IDENT_SET;
            break;
          case MODE_GET_BYTE:
            sendUsbMode();
            break;
          case IDENT_GET_BYTE:
            sendUsbIdent();
            break;
        }
        break;
      case MODE_SET: {
        const enum ModeByte mode{byte};
        if (mode != config.mode) {
          config.mode = byte;
          EEPROM.put(0, config);

          Serial.end();
          Serial1.end();

          setup();
        }
        }
        state_ = WAIT_START;
        break;
      case IDENT_SET: {
        const uint8_t ident{byte};
        if (ident != config.ident) {
          config.ident = byte;
          EEPROM.put(0, config);
        }
        state_ = WAIT_START;
        }
        break;
      default:
        reset();
        break;
    }
  }

  bool passThrough() const {
    return state_ == WAIT_START;
  }

 private:
  ParserState state_;
};

static BasicParser gUsbParser;


// LINK MODE
static void setupLink() {
  Serial.begin(kBaudLink);
  Serial1.begin(kBaudLink);
  pinMode(kRs485DePin, OUTPUT);
  pinMode(kRs485RePin, OUTPUT);
  gRs485State.init();
  gUsbToUartParser.reset();
  gUartToUsbParser.reset();
  gLastUartByteMicros = micros();
}

static void loopLink() {
  while (Serial1.available() > 0) {
    // UART -> USB path: forward every byte, track packet boundaries, and
    // insert a GAP marker when inter-byte spacing exceeds two byte times.
    const uint8_t inByte = static_cast<uint8_t>(Serial1.read());
    const uint32_t nowMicros = micros();
    const uint32_t deltaMicros = nowMicros - gLastUartByteMicros;
    if (deltaMicros > (kByteTimeMicrosLink * 2)) {
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

// RPM MODE
void setupRpm() {
  Serial.begin(kBaudRpm);
  Serial1.begin(kBaudRpm);
  gUsbParser.reset();
}

static void loopRpm() {
  while (Serial.available() > 0) {
    const uint8_t inByte = static_cast<uint8_t>(Serial.read());
    gUsbParser.update(inByte);
    if (gUsbParser.passThrough())
      Serial1.write(inByte);
  }
}

// default
static void setupDefault() {
  Serial.begin(9600);
  Serial1.begin(9600);
  gUsbParser.reset();
}

static void loopDefault() {
  while (Serial.available() > 0) {
    const uint8_t inByte = static_cast<uint8_t>(Serial.read());
    gUsbParser.update(inByte);
  }
}

// Arduino
void setup() {
  EEPROM.get(0, config);
  switch (config.mode) {
  case LINK_MODE:
    setupLink();
    break;
  case RPM_MODE:
    setupRpm();
    break;
  default:
    setupDefault();
    break;
  }
}

void loop() {
  switch (config.mode) {
  case LINK_MODE:
    loopLink();
    break;
  case RPM_MODE:
    loopRpm();
    break;
  default:
    loopDefault();
    break;
  }
}
