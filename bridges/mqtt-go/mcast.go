package main

import (
	"context"
	"fmt"
	"net"
	"syscall"

	"golang.org/x/net/ipv6"
	"golang.org/x/sys/unix"
)

// MulticastSocket wraps a single IPv6 UDP socket bound to [::]:port.
// It can join multiple groups for receiving and send to arbitrary groups
// using the same fd.
type MulticastSocket struct {
	conn   *net.UDPConn
	pc     *ipv6.PacketConn
	iface  *net.Interface // nil = kernel default
	port   int
	joined map[string]net.IP
}

func OpenMulticastSocket(cfg MPubsubConfig) (*MulticastSocket, error) {
	var iface *net.Interface
	if cfg.Interface != "" {
		ifi, err := net.InterfaceByName(cfg.Interface)
		if err != nil {
			return nil, fmt.Errorf("interface %q: %w", cfg.Interface, err)
		}
		iface = ifi
	}

	// SO_REUSEPORT so other tools on the same host (probes, additional
	// bridges, tcpdump-style listeners that actually join the group) can
	// co-bind the multicast port without colliding on the bind() call.
	lc := net.ListenConfig{
		Control: func(_, _ string, c syscall.RawConn) error {
			var cerr error
			ctrlErr := c.Control(func(fd uintptr) {
				cerr = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEPORT, 1)
			})
			if cerr != nil {
				return cerr
			}
			return ctrlErr
		},
	}
	pktConn, err := lc.ListenPacket(context.Background(), "udp6",
		(&net.UDPAddr{IP: net.IPv6unspecified, Port: int(cfg.Port)}).String())
	if err != nil {
		return nil, fmt.Errorf("listen [::]:%d: %w", cfg.Port, err)
	}
	conn := pktConn.(*net.UDPConn)
	pc := ipv6.NewPacketConn(conn)

	if err := pc.SetMulticastHopLimit(cfg.Hops); err != nil {
		conn.Close()
		return nil, fmt.Errorf("set multicast hop limit: %w", err)
	}
	if iface != nil {
		if err := pc.SetMulticastInterface(iface); err != nil {
			conn.Close()
				return nil, fmt.Errorf("set multicast interface: %w", err)
		}
	}
	// IPV6_MULTICAST_LOOP=1: allow our own publications to be delivered to
	// other local listeners (including a second bridge or a probe on the
	// same host). The bridge's own routing table is keyed on which topics
	// it forwards into MQTT, so echoing one direction does not feed back
	// into the other -- each entry is one-directional by design.
	if err := pc.SetMulticastLoopback(true); err != nil {
		conn.Close()
		return nil, fmt.Errorf("enable multicast loopback: %w", err)
	}

	return &MulticastSocket{
		conn:   conn,
		pc:     pc,
		iface:  iface,
		port:   int(cfg.Port),
		joined: make(map[string]net.IP),
	}, nil
}

// Join subscribes the socket to the given IPv6 multicast group. Idempotent --
// joining the same group twice is a no-op.
func (m *MulticastSocket) Join(group net.IP) error {
	key := group.String()
	if _, ok := m.joined[key]; ok {
		return nil
	}
	if err := m.pc.JoinGroup(m.iface, &net.UDPAddr{IP: group}); err != nil {
		return fmt.Errorf("join %s: %w", key, err)
	}
	m.joined[key] = group
	return nil
}

// SendTo writes data to the given group address using this socket. The egress
// interface (set via SetMulticastInterface above, or kernel default) decides
// which link the datagram is emitted on.
func (m *MulticastSocket) SendTo(group net.IP, data []byte) error {
	_, err := m.conn.WriteToUDP(data, &net.UDPAddr{IP: group, Port: m.port})
	return err
}

func (m *MulticastSocket) Read(buf []byte) (int, error) {
	n, _, err := m.conn.ReadFromUDP(buf)
	return n, err
}

func (m *MulticastSocket) Close() error {
	return m.conn.Close()
}
