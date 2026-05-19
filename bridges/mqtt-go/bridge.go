package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"sync"

	mqtt "github.com/eclipse/paho.mqtt.golang"
)

// Bridge wires an MQTT client and a multicast_pubsub socket together
// per the configured entries. Each entry is one-directional.
type Bridge struct {
	cfg     *Config
	scope   Scope
	mqtt    mqtt.Client
	mcast   *MulticastSocket

	// crcToMQTT[topic_crc] = list of MQTT topic + QoS/retain to publish to
	// when a multicast packet arrives with that CRC. Multiple entries are
	// possible if the user configures more than one mqtt destination for
	// the same mpubsub topic.
	crcToMQTT map[uint32][]mpubsubToMQTTRoute
	log       *slog.Logger
}

type mpubsubToMQTTRoute struct {
	MQTTTopic string
}

func NewBridge(cfg *Config, log *slog.Logger) (*Bridge, error) {
	scope, err := ParseScope(cfg.MPubsub.Scope)
	if err != nil {
		return nil, err
	}
	sock, err := OpenMulticastSocket(cfg.MPubsub)
	if err != nil {
		return nil, err
	}

	opts := mqtt.NewClientOptions().
		AddBroker(cfg.MQTT.Broker).
		SetClientID(cfg.MQTT.ClientID).
		SetAutoReconnect(true).
		SetCleanSession(true)
	if cfg.MQTT.Username != "" {
		opts.SetUsername(cfg.MQTT.Username)
		opts.SetPassword(cfg.MQTT.Password)
	}
	client := mqtt.NewClient(opts)

	b := &Bridge{
		cfg:       cfg,
		scope:     scope,
		mqtt:      client,
		mcast:     sock,
		crcToMQTT: make(map[uint32][]mpubsubToMQTTRoute),
		log:       log,
	}
	return b, nil
}

func (b *Bridge) Run(ctx context.Context) error {
	if token := b.mqtt.Connect(); token.Wait() && token.Error() != nil {
		return token.Error()
	}
	b.log.Info("mqtt connected", "broker", b.cfg.MQTT.Broker)

	// Configure the routing tables and joins from the config.
	for _, entry := range b.cfg.Bridges {
		group := TopicToGroup(entry.MPubsubTopic, b.scope)
		switch entry.Direction {
		case DirMQTTToMPubsub:
			// Capture the loop variable so the closure refers to this entry.
			e := entry
			h := func(_ mqtt.Client, msg mqtt.Message) {
				b.handleMQTTMessage(e.MPubsubTopic, group, msg)
			}
			if token := b.mqtt.Subscribe(e.MQTTTopic, b.cfg.MQTT.QoS, h); token.Wait() && token.Error() != nil {
				return token.Error()
			}
			b.log.Info("subscribed mqtt -> mpubsub",
				"mqtt_topic", e.MQTTTopic, "mpubsub_topic", e.MPubsubTopic,
				"group", group.String())
		case DirMPubsubToMQTT:
			if err := b.mcast.Join(group); err != nil {
				return err
			}
			crc := TopicCRC32(entry.MPubsubTopic)
			b.crcToMQTT[crc] = append(b.crcToMQTT[crc], mpubsubToMQTTRoute{MQTTTopic: entry.MQTTTopic})
			b.log.Info("joined mpubsub -> mqtt",
				"mpubsub_topic", entry.MPubsubTopic, "mqtt_topic", entry.MQTTTopic,
				"group", group.String(), "crc", crc)
		}
	}

	// Multicast receiver pump.
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		b.mcastReceiveLoop(ctx)
	}()

	<-ctx.Done()
	b.log.Info("shutting down")
	b.mcast.Close()
	b.mqtt.Disconnect(250)
	wg.Wait()
	return nil
}

func (b *Bridge) handleMQTTMessage(mpubsubTopic string, group net.IP, msg mqtt.Message) {
	pkt, err := EncodePacket(mpubsubTopic, msg.Payload(), encodingRaw)
	if err != nil {
		b.log.Warn("encode packet failed",
			"mqtt_topic", msg.Topic(), "mpubsub_topic", mpubsubTopic, "err", err)
		return
	}
	if err := b.mcast.SendTo(group, pkt); err != nil {
		b.log.Warn("multicast send failed",
			"mpubsub_topic", mpubsubTopic, "group", group.String(), "err", err)
		return
	}
	b.log.Debug("mqtt -> mpubsub",
		"mqtt_topic", msg.Topic(), "mpubsub_topic", mpubsubTopic, "bytes", len(msg.Payload()))
}

func (b *Bridge) mcastReceiveLoop(ctx context.Context) {
	buf := make([]byte, maxDatagram)
	for {
		if ctx.Err() != nil {
			return
		}
		n, err := b.mcast.Read(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) || ctx.Err() != nil {
				return
			}
			b.log.Warn("mcast read error", "err", err)
			continue
		}
		pkt, err := DecodePacket(buf[:n])
		if err != nil {
			b.log.Debug("dropped packet", "err", err, "bytes", n)
			continue
		}
		routes := b.crcToMQTT[pkt.TopicCRC]
		if len(routes) == 0 {
			continue
		}
		// We only forward RAW packets to MQTT. PROTOBUF-encoded packets
		// have a 2-byte SCHEMA_ID prefix that an MQTT subscriber has no
		// way to interpret; dropping them here avoids handing junk to
		// downstream consumers.
		if pkt.Encoding != encodingRaw {
			b.log.Debug("skip non-raw packet", "crc", pkt.TopicCRC, "encoding", pkt.Encoding)
			continue
		}
		for _, r := range routes {
			t := b.mqtt.Publish(r.MQTTTopic, b.cfg.MQTT.QoS, b.cfg.MQTT.Retain, pkt.Payload)
			// Don't block the multicast loop on broker round-trips. Log
			// failures asynchronously.
			go func(tok mqtt.Token, mqttTopic string) {
				if tok.Wait() && tok.Error() != nil {
					b.log.Warn("mqtt publish failed", "mqtt_topic", mqttTopic, "err", tok.Error())
				}
			}(t, r.MQTTTopic)
			b.log.Debug("mpubsub -> mqtt", "mqtt_topic", r.MQTTTopic, "bytes", len(pkt.Payload))
		}
	}
}
