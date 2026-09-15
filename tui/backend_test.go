package main

import "testing"

func TestParseBackendLineRejectsMalformedAndUncorrelatedData(t *testing.T) {
	if _, err := parseBackendLine([]byte("not json")); err == nil {
		t.Fatal("malformed JSONL was accepted")
	}
	if _, err := parseBackendLine([]byte(`{"v":1}`)); err == nil {
		t.Fatal("event without a name was accepted")
	}
	if _, err := parseBackendLine([]byte(`{"event":"response","request_id":false}`)); err == nil {
		t.Fatal("event with an invalid request id was accepted")
	}
	event, err := parseBackendLine([]byte(`{"v":1,"event":"response","request_id":"r1","text":"ok"}`))
	if err != nil || event["request_id"] != "r1" {
		t.Fatalf("valid correlated event was not decoded: %#v, %v", event, err)
	}
}
