package main

import (
	"bufio"
	"bytes"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

func TestAgentsScopedLiveUpdatesInspectionAndCancellation(t *testing.T) {
	m := initialModel("", true)
	m.activeSessionID, m.activeScopeID = "chat", "project"
	agent := map[string]any{"run_id": "run", "name": "Evan", "profile": "researcher", "status": "running", "version": float64(2),
		"provider_model": "openai:gpt-test", "icon": []any{"10101", "11011", "00100", "11111", "01010"},
		"assignment": map[string]any{"objective": "Inspect actual evidence"}}
	m.handleBackendEvent(backendEvent{"event": "subagent", "session_id": "other", "agent": agent})
	m.handleBackendEvent(backendEvent{"event": "subagent", "source_scope_id": "other", "agent": agent})
	if len(m.agents) != 0 {
		t.Fatal("cross-chat/project event leaked")
	}
	m.handleBackendEvent(backendEvent{"event": "subagent", "session_id": "chat", "source_scope_id": "project", "agent": agent})
	m.updateAgent(map[string]any{"run_id": "run", "version": float64(1), "status": "queued"})
	if stringValue(m.agents[0], "status") != "running" {
		t.Fatal("stale event overwrote progress")
	}
	agent["review_agent"] = map[string]any{"name": "Morning", "profile": "reviewer", "status": "reviewing", "icon": agent["icon"]}
	if len(m.activityAgents()) != 2 || !strings.Contains(stringValue(m.activityAgents()[1]["assignment"].(map[string]any), "objective"), "Evan") {
		t.Fatal("singleton reviewer is missing from activity rail")
	}
	agent["review_agent"].(map[string]any)["name"] = "Evan"
	if len(m.activityAgents()) != 1 {
		t.Fatal("peer reviewer identity was duplicated")
	}
	icon := agentIcon(agent)
	if icon != agentIcon(agent) || lipgloss.Width(icon) != 5 || len(strings.Split(icon, "\n")) != 3 {
		t.Fatal("unstable identicon")
	}
	m.screen = screenAgents
	m.width, m.height = 120, 30
	m.handleBackendEvent(backendEvent{"event": "agent_detail", "run_id": "run", "reset": true, "text": "receipt evidence\n"})
	m.handleBackendEvent(backendEvent{"event": "agent_detail", "run_id": "run", "text": strings.Repeat("details\n", 70)})
	m.handleKey(tea.KeyPressMsg{Code: tea.KeyPgDown})
	if m.agentScroll == 0 {
		t.Fatal("inspector does not scroll")
	}
	var output bytes.Buffer
	m.bridge = &bridge{stdin: bufio.NewWriter(&output)}
	m.handleKey(tea.KeyPressMsg{Code: 'c'})
	if !strings.Contains(output.String(), `"cancel":"run"`) {
		t.Fatal("cancellation was not sent")
	}
	m.handleKey(tea.KeyPressMsg{Code: tea.KeyEscape})
	if m.screen != screenChat {
		t.Fatal("Esc did not return to composer")
	}
	m.handleBackendEvent(backendEvent{"event": "navigation", "active_session_id": "new", "active_scope_id": "project"})
	if len(m.agents) != 0 {
		t.Fatal("old chat agents were retained")
	}
}

func TestAgentLayoutsFitSmallAndLargeTerminals(t *testing.T) {
	for _, size := range [][2]int{{120, 30}, {80, 24}, {60, 20}, {30, 10}, {12, 4}, {1, 1}} {
		for _, screen := range []screen{screenChat, screenAgents} {
			m := initialModel("", true)
			m.width, m.height, m.screen = size[0], size[1], screen
			m.agents = []map[string]any{{"run_id": "run", "name": "Old Fool", "profile": "researcher", "status": "reviewing", "provider_model": "anthropic:claude-test", "assignment": map[string]any{"objective": strings.Repeat("long assignment ", 30)}}}
			m.resize()
			lines := strings.Split(m.View().Content, "\n")
			if len(lines) != m.height {
				t.Fatalf("%v %s rendered %d rows", size, screen, len(lines))
			}
			for _, line := range lines {
				if lipgloss.Width(line) > m.width {
					t.Fatalf("%v %s row overflows", size, screen)
				}
			}
			if screen == screenChat && m.height >= 20 && !strings.Contains(m.View().Content, "Kyrozen anything") {
				t.Fatal("agents pushed composer offscreen")
			}
		}
	}
}
