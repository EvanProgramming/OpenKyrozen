package main

import "testing"

func TestCommandMatchesOnlyAtFirstNonWhitespaceCharacter(t *testing.T) {
	if got := commandMatches("please use /provider"); len(got) != 0 {
		t.Fatalf("slash in prose opened menu: %#v", got)
	}
	if got := commandMatches("https://example.test/"); len(got) != 0 {
		t.Fatalf("slash in URL opened menu: %#v", got)
	}
	if got := commandMatches("  /prov"); len(got) != 1 || got[0].name != "provider" {
		t.Fatalf("leading slash was not filtered: %#v", got)
	}
	if got := commandMatches("/tasks list"); len(got) != 1 || got[0].name != "tasks" {
		t.Fatalf("arguments changed command filtering: %#v", got)
	}
}

func TestReplaceCommandPreservesArguments(t *testing.T) {
	got := replaceCommand("  /prov --local", commands[0])
	if got != "  /provider --local" {
		t.Fatalf("unexpected replacement: %q", got)
	}
}

func TestFillBackgroundCoversSmallAndLargeViews(t *testing.T) {
	for _, size := range [][2]int{{1, 1}, {20, 5}, {120, 40}} {
		got := fillBackground("hello", size[0], size[1])
		if got == "" {
			t.Fatalf("empty background for %v", size)
		}
	}
}
