// Command mqtt-pubsub-bridge mirrors messages between an MQTT broker and
// the multicast_pubsub fabric. Each entry in the YAML config is one
// direction (mqtt_to_mpubsub or mpubsub_to_mqtt) -- declare two entries
// to mirror a topic both ways.
package main

import (
	"context"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	var (
		cfgPath  = flag.String("config", "bridge.yaml", "path to YAML config file")
		logLevel = flag.String("log-level", "info", "debug | info | warn | error")
	)
	flag.Parse()

	lvl := slog.LevelInfo
	switch *logLevel {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))

	cfg, err := LoadConfig(*cfgPath)
	if err != nil {
		log.Error("load config", "err", err)
		os.Exit(1)
	}

	bridge, err := NewBridge(cfg, log)
	if err != nil {
		log.Error("bridge init", "err", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if err := bridge.Run(ctx); err != nil {
		log.Error("bridge run", "err", err)
		os.Exit(1)
	}
}
