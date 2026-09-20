package main

import (
	"strings"
	"testing"
)

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
	if got := commandMatches("/mode plan"); len(got) != 1 || got[0].name != "mode" {
		t.Fatalf("mode command was not discoverable: %#v", got)
	}
	if got := commandMatches("/question skip"); len(got) != 1 || got[0].name != "question" {
		t.Fatalf("question command was not discoverable: %#v", got)
	}
}

func TestInteractionCommandsAreVisibleOnTheFirstPalettePage(t *testing.T) {
	want := []string{"mode", "ask", "plan", "question", "agent"}
	for index, name := range want {
		if commands[index].name != name {
			t.Fatalf("first palette page[%d] = %q, want %q", index, commands[index].name, name)
		}
	}
	m := initialModel(".", true)
	m.palette = commandMatches("/")
	view := m.paletteView(80)
	if !strings.Contains(view, "1–5 OF ") || !strings.Contains(view, "↑↓ MORE") {
		t.Fatalf("palette did not disclose hidden commands: %s", view)
	}
}

func TestReplaceCommandPreservesArguments(t *testing.T) {
	got := replaceCommand("  /prov --local", commandMatches("/prov")[0])
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
