package main

import (
	"fmt"
	"os"

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
	ClientID string `yaml:"client_id"` // default "multicast-pubsub-bridge"
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
}

type BridgeEntry struct {
	Direction    Direction `yaml:"direction"`
	MQTTTopic    string    `yaml:"mqtt_topic"`
	MPubsubTopic string    `yaml:"mpubsub_topic"`
}

type Config struct {
	MQTT    MQTTConfig    `yaml:"mqtt"`
	MPubsub MPubsubConfig `yaml:"multicast_pubsub"`
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
	c.applyDefaults()
	if err := c.validate(); err != nil {
		return nil, err
	}
	return c, nil
}

func (c *Config) applyDefaults() {
	if c.MQTT.ClientID == "" {
		c.MQTT.ClientID = "multicast-pubsub-bridge"
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
}

func (c *Config) validate() error {
	if c.MQTT.Broker == "" {
		return fmt.Errorf("mqtt.broker is required")
	}
	if _, err := ParseScope(c.MPubsub.Scope); err != nil {
		return fmt.Errorf("multicast_pubsub.%w", err)
	}
	if c.MPubsub.Hops < 1 || c.MPubsub.Hops > 255 {
		return fmt.Errorf("multicast_pubsub.hops must be 1..255, got %d", c.MPubsub.Hops)
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
	}
	return nil
}
