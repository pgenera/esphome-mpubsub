package main

import (
	"strings"
	"testing"
)

func mustValidate(t *testing.T, c *Config) error {
	t.Helper()
	if err := c.applyDefaults(); err != nil {
		return err
	}
	return c.validate()
}

func minimalConfig() *Config {
	return &Config{
		MQTT: MQTTConfig{Broker: "tcp://127.0.0.1:1883"},
		Bridges: []BridgeEntry{
			{Direction: DirMQTTToMPubsub, MQTTTopic: "in", MPubsubTopic: "out"},
		},
	}
}

func TestMQTTToMPubsubAllowsEmptyMPubsubTopic(t *testing.T) {
	c := minimalConfig()
	c.Bridges[0].MPubsubTopic = "" // resolved-from-mqtt mode
	if err := mustValidate(t, c); err != nil {
		t.Errorf("mqtt_to_mpubsub with empty mpubsub_topic should validate: %v", err)
	}
}

func TestMPubsubToMQTTRequiresMPubsubTopic(t *testing.T) {
	c := minimalConfig()
	c.Bridges[0] = BridgeEntry{
		Direction: DirMPubsubToMQTT,
		MQTTTopic: "out",
		// no MPubsubTopic
	}
	err := mustValidate(t, c)
	if err == nil || !strings.Contains(err.Error(), "mpubsub_topic is required") {
		t.Errorf("expected mpubsub_topic required error, got %v", err)
	}
}

func TestMPubsubTopicWildcardRejected(t *testing.T) {
	c := minimalConfig()
	c.Bridges[0] = BridgeEntry{
		Direction:    DirMPubsubToMQTT,
		MQTTTopic:    "out",
		MPubsubTopic: "home/+/temp",
	}
	err := mustValidate(t, c)
	if err == nil || !strings.Contains(err.Error(), "wildcards") {
		t.Errorf("expected wildcard rejection for mpubsub_topic, got %v", err)
	}
}

func TestMQTTTopicRequired(t *testing.T) {
	c := minimalConfig()
	c.Bridges[0].MQTTTopic = ""
	err := mustValidate(t, c)
	if err == nil || !strings.Contains(err.Error(), "mqtt_topic is required") {
		t.Errorf("expected mqtt_topic required, got %v", err)
	}
}

func TestRequireEncryptionNeedsKey(t *testing.T) {
	c := minimalConfig()
	c.Bridges[0] = BridgeEntry{
		Direction:         DirMPubsubToMQTT,
		MQTTTopic:         "out",
		MPubsubTopic:      "home/livingroom/temp",
		RequireEncryption: true,
	}
	err := mustValidate(t, c)
	if err == nil || !strings.Contains(err.Error(), "encryption.key") {
		t.Errorf("expected encryption.key requirement, got %v", err)
	}
}

func TestRequireEncryptionWrongDirectionRejected(t *testing.T) {
	c := minimalConfig()
	c.MPubsub.Encryption.Key = "passphrase"
	c.Bridges[0] = BridgeEntry{
		Direction:         DirMQTTToMPubsub,
		MQTTTopic:         "in",
		MPubsubTopic:      "out",
		RequireEncryption: true,
	}
	err := mustValidate(t, c)
	if err == nil || !strings.Contains(err.Error(), "mpubsub_to_mqtt") {
		t.Errorf("expected require_encryption direction error, got %v", err)
	}
}
