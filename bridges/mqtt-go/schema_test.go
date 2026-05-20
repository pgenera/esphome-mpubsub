package main

import (
	"encoding/base64"
	"encoding/json"
	"reflect"
	"testing"
)

func TestSchemaCanonicalAndID(t *testing.T) {
	// Two declarations with reordered fields must canonicalize to the same
	// string and produce the same SCHEMA_ID. This is what lets a Go bridge
	// and an ESPHome node agree on ids without sharing source.
	a, err := NewSchema("reading", []SchemaField{
		{Name: "temperature", Type: "float", Tag: 2},
		{Name: "room", Type: "string", Tag: 1},
	})
	if err != nil {
		t.Fatalf("a: %v", err)
	}
	b, err := NewSchema("reading", []SchemaField{
		{Name: "room", Type: "string", Tag: 1},
		{Name: "temperature", Type: "float", Tag: 2},
	})
	if err != nil {
		t.Fatalf("b: %v", err)
	}
	if a.SchemaID() != b.SchemaID() {
		t.Fatalf("schema id mismatch: %x vs %x", a.SchemaID(), b.SchemaID())
	}
	if got := canonicalSchemaString(a); got != "1:string:room\n2:float:temperature" {
		t.Fatalf("canonical: %q", got)
	}
}

// TestSchemaIDParityWithPython locks the Go SCHEMA_ID computation to the
// value the Python proto_emitter produces for the same schema. If this
// drifts, a Go bridge and an ESPHome node disagree on schema identity and
// no proto packets ever match. Recompute via:
//
//	python3 -c 'import sys; sys.path.insert(0,"components/mpubsub"); from proto_emitter import *; \
//	    print(hex(schema_id(Message("reading", (Field("room","string",1), Field("temperature","float",2), \
//	    Field("blob","bytes",3), Field("tags","string",4,True))))))'
func TestSchemaIDParityWithPython(t *testing.T) {
	s, err := NewSchema("reading", []SchemaField{
		{Name: "room", Type: "string", Tag: 1},
		{Name: "temperature", Type: "float", Tag: 2},
		{Name: "blob", Type: "bytes", Tag: 3},
		{Name: "tags", Type: "string", Tag: 4, Repeated: true},
	})
	if err != nil {
		t.Fatal(err)
	}
	if got, want := s.SchemaID(), uint16(0x2ce0); got != want {
		t.Fatalf("SCHEMA_ID parity broken: got 0x%04x, want 0x%04x (python value)", got, want)
	}
}

func TestSchemaRejectsBadFields(t *testing.T) {
	for _, c := range []struct {
		name   string
		fields []SchemaField
	}{
		{"dup-tag", []SchemaField{{Name: "a", Type: "int32", Tag: 1}, {Name: "b", Type: "int32", Tag: 1}}},
		{"dup-name", []SchemaField{{Name: "a", Type: "int32", Tag: 1}, {Name: "a", Type: "int32", Tag: 2}}},
		{"unknown-type", []SchemaField{{Name: "a", Type: "uuid", Tag: 1}}},
		{"reserved-tag", []SchemaField{{Name: "a", Type: "int32", Tag: 19500}}},
		{"zero-tag", []SchemaField{{Name: "a", Type: "int32", Tag: 0}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			if _, err := NewSchema("x", c.fields); err == nil {
				t.Fatalf("expected error")
			}
		})
	}
}

func TestProtoRoundTripAllTypes(t *testing.T) {
	s, err := NewSchema("all", []SchemaField{
		{Name: "b", Type: "bool", Tag: 1},
		{Name: "i32", Type: "int32", Tag: 2},
		{Name: "i64", Type: "int64", Tag: 3},
		{Name: "u32", Type: "uint32", Tag: 4},
		{Name: "u64", Type: "uint64", Tag: 5},
		{Name: "s32", Type: "sint32", Tag: 6},
		{Name: "s64", Type: "sint64", Tag: 7},
		{Name: "f", Type: "float", Tag: 8},
		{Name: "str", Type: "string", Tag: 9},
		{Name: "bin", Type: "bytes", Tag: 10},
	})
	if err != nil {
		t.Fatal(err)
	}
	in := map[string]any{
		"b": true, "i32": int64(-5), "i64": int64(1 << 40),
		"u32": uint64(7), "u64": uint64(1 << 50),
		"s32": int64(-1), "s64": int64(-1 << 30),
		"f": float64(1.5), "str": "hi", "bin": []byte{1, 2, 3},
	}
	body, err := EncodeProto(s, in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := DecodeProto(s, body)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("round-trip mismatch:\n in=%#v\nout=%#v", in, out)
	}
}

func TestProtoRepeated(t *testing.T) {
	s, err := NewSchema("rep", []SchemaField{
		{Name: "nums", Type: "int32", Tag: 1, Repeated: true},
		{Name: "tags", Type: "string", Tag: 2, Repeated: true},
	})
	if err != nil {
		t.Fatal(err)
	}
	in := map[string]any{
		"nums": []any{int64(1), int64(2), int64(-3)},
		"tags": []any{"a", "b"},
	}
	body, err := EncodeProto(s, in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := DecodeProto(s, body)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(in, out) {
		t.Fatalf("repeated round-trip mismatch:\n in=%#v\nout=%#v", in, out)
	}
}

func TestProtoSkipsUnknownFields(t *testing.T) {
	// Sender knows fields 1, 2, 3; receiver only knows 1.
	full, _ := NewSchema("v2", []SchemaField{
		{Name: "a", Type: "int32", Tag: 1},
		{Name: "b", Type: "string", Tag: 2},
		{Name: "c", Type: "float", Tag: 3},
	})
	partial, _ := NewSchema("v1", []SchemaField{
		{Name: "a", Type: "int32", Tag: 1},
	})
	body, err := EncodeProto(full, map[string]any{
		"a": int64(42), "b": "ignored", "c": float64(3.14),
	})
	if err != nil {
		t.Fatal(err)
	}
	out, err := DecodeProto(partial, body)
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != 1 || out["a"].(int64) != 42 {
		t.Fatalf("expected only a=42, got %#v", out)
	}
}

func TestJSONToProtoRoundTrip(t *testing.T) {
	s, _ := NewSchema("reading", []SchemaField{
		{Name: "room", Type: "string", Tag: 1},
		{Name: "temperature", Type: "float", Tag: 2},
		{Name: "blob", Type: "bytes", Tag: 3},
		{Name: "tags", Type: "string", Tag: 4, Repeated: true},
	})
	jsonIn := []byte(`{"room":"garage","temperature":22.5,"blob":"AQID","tags":["x","y"]}`)
	body, err := JSONToProto(s, jsonIn)
	if err != nil {
		t.Fatal(err)
	}
	out, err := ProtoToJSON(s, body)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	if got["room"] != "garage" {
		t.Fatalf("room: %v", got["room"])
	}
	if got["temperature"].(float64) != 22.5 {
		t.Fatalf("temperature: %v", got["temperature"])
	}
	if got["blob"].(string) != base64.StdEncoding.EncodeToString([]byte{1, 2, 3}) {
		t.Fatalf("blob: %v", got["blob"])
	}
	tags := got["tags"].([]any)
	if len(tags) != 2 || tags[0] != "x" || tags[1] != "y" {
		t.Fatalf("tags: %v", tags)
	}
}

func TestJSONToProtoRejectsUnknownField(t *testing.T) {
	s, _ := NewSchema("r", []SchemaField{{Name: "a", Type: "int32", Tag: 1}})
	_, err := JSONToProto(s, []byte(`{"a":1,"bogus":2}`))
	if err == nil {
		t.Fatal("expected error on unknown field")
	}
}
