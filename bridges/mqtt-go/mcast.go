package main

import (
	"fmt"
	"net"

	"golang.org/x/net/ipv6"
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

	conn, err := net.ListenUDP("udp6", &net.UDPAddr{IP: net.IPv6unspecified, Port: int(cfg.Port)})
	if err != nil {
		return nil, fmt.Errorf("listen [::]:%d: %w", cfg.Port, err)
	}
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
	// We don't want to receive our own publications back. The kernel default
	// for IPV6_MULTICAST_LOOP varies; force it off so the receive loop never
	// has to filter out our own datagrams.
	if err := pc.SetMulticastLoopback(false); err != nil {
		conn.Close()
		return nil, fmt.Errorf("disable multicast loopback: %w", err)
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
