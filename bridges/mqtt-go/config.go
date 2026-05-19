package main

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// Direction tags a bridge entry. Each entry is uni-directional; declare two
// entries to mirror a topic both ways. Keeping it asymmetric makes loop risk
// explicit -- using the same topic name on both sides with a broker that
// re-delivers to its publisher would feed back on itself.
type Direction string

const (
	DirMQTTToMPubsub Direction = "mqtt_to_mpubsub"
	DirMPubsubToMQTT Direction = "mpubsub_to_mqtt"
)

type MQTTConfig struct {
	Broker   string `yaml:"broker"`    // e.g. tcp://broker:1883, ssl://broker:8883
	ClientID string `yaml:"client_id"` // default "mpubsub-bridge"
	Username string `yaml:"username"`
	Password string `yaml:"password"`
	QoS      byte   `yaml:"qos"` // default 0
	Retain   bool   `yaml:"retain"`
}

type MPubsubConfig struct {
	Port      uint16 `yaml:"port"`      // default 18512
	Scope     string `yaml:"scope"`     // default link-local
	Hops      int    `yaml:"hops"`      // default 1
	Interface string `yaml:"interface"` // default: kernel-picked

	// RetransmitCount is the number of UDP datagrams emitted per logical
	// publish.
	//   1   : no retransmission (default)
	//   N>1 : send N packets total, (N-1) deferred on a goroutine
	//   -1  : indefinite -- keep emitting at RetransmitDelay until
	//         another publish for the same topic supersedes it. Requires
	//         RetransmitDelay >= 1s (enforced at config time).
	// The first send is always synchronous.
	RetransmitCount int `yaml:"retransmit_count"`
	// RetransmitDelay is the spacing between successive sends. Accepts
	// any Go duration string (e.g. "100ms", "1s", "0s"). 0 is supported
	// for finite counts; -1 (indefinite) requires >= 1s.
	RetransmitDelayRaw string        `yaml:"retransmit_delay"`
	RetransmitDelay    time.Duration `yaml:"-"`

	// PromoteQoS adapts the effective retransmit_count based on the
	// incoming MQTT message's QoS, on the theory that a QoS>0 publisher
	// "cared more" about delivery and the UDP-multicast hop should
	// reflect that:
	//   QoS 0  → RetransmitCount  (unchanged)
	//   QoS 1  → max(RetransmitCount, 3)
	//   QoS 2  → -1 (indefinite, capped by the topic-supersede rule)
	// Default false to keep behavior predictable.
	PromoteQoS bool `yaml:"promote_qos"`

	// Encryption mirrors the C++ `mpubsub: encryption: key:` block: a
	// passphrase that's SHA-256'd to a 32-byte XXTEA-256 key. When set, the
	// bridge encrypts every mqtt → mpubsub publish and can decrypt encrypted
	// mpubsub → mqtt packets. Leave empty for plaintext.
	Encryption EncryptionConfig `yaml:"encryption"`

	// EncryptionKey is the 32-byte SHA-256 of Encryption.Key, derived at
	// config-load time. Not user-facing; populated by applyDefaults.
	EncryptionKey []byte `yaml:"-"`
}

// EncryptionConfig matches the ESPHome packet_transport / mpubsub idiom
// of `encryption: { key: "..." }` so YAML blocks are familiar.
type EncryptionConfig struct {
	Key string `yaml:"key"`
}

type BridgeEntry struct {
	Direction    Direction `yaml:"direction"`
	MQTTTopic    string    `yaml:"mqtt_topic"`
	MPubsubTopic string    `yaml:"mpubsub_topic"`
	// RequireEncryption applies only to mpubsub_to_mqtt entries: plaintext
	// multicast datagrams for this mpubsub topic are dropped at the
	// receiver instead of being forwarded to MQTT. Mirrors the C++ side's
	// per-topic require_encryption on on_message / sensor.subscribe.
	// Setting it requires mpubsub.encryption.key to be configured.
	RequireEncryption bool `yaml:"require_encryption"`
}

type Config struct {
	MQTT    MQTTConfig    `yaml:"mqtt"`
	MPubsub MPubsubConfig `yaml:"mpubsub"`
	Bridges []BridgeEntry `yaml:"bridges"`
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}
	c := &Config{}
	if err := yaml.Unmarshal(data, c); err != nil {
		return nil, fmt.Errorf("parse yaml: %w", err)
	}
	if err := c.applyDefaults(); err != nil {
		return nil, err
	}
	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) applyDefaults() error {
	if c.MQTT.ClientID == "" {
		c.MQTT.ClientID = "mpubsub-bridge"
	}
	if c.MPubsub.Port == 0 {
		c.MPubsub.Port = 18512
	}
	if c.MPubsub.Scope == "" {
		c.MPubsub.Scope = "link-local"
	}
	if c.MPubsub.Hops == 0 {
		c.MPubsub.Hops = 1
	}
	// Zero value (Go's int default) means "not set in YAML"; map to the
	// real default of 1. validate() accepts 1..255 and -1.
	if c.MPubsub.RetransmitCount == 0 {
		c.MPubsub.RetransmitCount = 1
	}
	if c.MPubsub.RetransmitDelayRaw == "" {
		c.MPubsub.RetransmitDelay = 100 * time.Millisecond
	} else {
		d, err := time.ParseDuration(c.MPubsub.RetransmitDelayRaw)
		if err != nil {
			return fmt.Errorf("mpubsub.retransmit_delay %q: %w",
				c.MPubsub.RetransmitDelayRaw, err)
		}
		if d < 0 {
			return fmt.Errorf("mpubsub.retransmit_delay must be >= 0 (got %s)", d)
		}
		c.MPubsub.RetransmitDelay = d
	}
	if c.MPubsub.Encryption.Key != "" {
		c.MPubsub.EncryptionKey = DeriveKey(c.MPubsub.Encryption.Key)
	}
	return nil
}

func (c *Config) validate() error {
	if c.MQTT.Broker == "" {
		return fmt.Errorf("mqtt.broker is required")
	}
	if _, err := ParseScope(c.MPubsub.Scope); err != nil {
		return fmt.Errorf("mpubsub.%w", err)
	}
	if c.MPubsub.Hops < 1 || c.MPubsub.Hops > 255 {
		return fmt.Errorf("mpubsub.hops must be 1..255, got %d", c.MPubsub.Hops)
	}
	if c.MPubsub.RetransmitCount != -1 &&
		(c.MPubsub.RetransmitCount < 1 || c.MPubsub.RetransmitCount > 255) {
		return fmt.Errorf("mpubsub.retransmit_count must be 1..255 or -1 (indefinite); got %d",
			c.MPubsub.RetransmitCount)
	}
	if c.MPubsub.RetransmitCount == -1 && c.MPubsub.RetransmitDelay < time.Second {
		return fmt.Errorf(
			"mpubsub.retransmit_count: -1 (indefinite) requires retransmit_delay >= 1s; got %s",
			c.MPubsub.RetransmitDelay)
	}
	if len(c.Bridges) == 0 {
		return fmt.Errorf("at least one bridge entry is required")
	}
	for i, b := range c.Bridges {
		if b.MQTTTopic == "" || b.MPubsubTopic == "" {
			return fmt.Errorf("bridges[%d]: mqtt_topic and mpubsub_topic are required", i)
		}
		switch b.Direction {
		case DirMQTTToMPubsub, DirMPubsubToMQTT:
		default:
			return fmt.Errorf("bridges[%d]: unknown direction %q (want %q or %q)",
				i, b.Direction, DirMQTTToMPubsub, DirMPubsubToMQTT)
		}
		if b.RequireEncryption {
			if b.Direction != DirMPubsubToMQTT {
				return fmt.Errorf("bridges[%d]: require_encryption is only valid for mpubsub_to_mqtt entries (this one is %s)",
					i, b.Direction)
			}
			if len(c.MPubsub.EncryptionKey) == 0 {
				return fmt.Errorf("bridges[%d]: require_encryption needs mpubsub.encryption.key to be set", i)
			}
		}
	}
	return nil
}
