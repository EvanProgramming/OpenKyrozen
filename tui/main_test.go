package main

import (
	"strings"
	"testing"
	"time"

	"charm.land/lipgloss/v2"
)

func TestReducerProjectsStreamingResponseTasksAndApproval(t *testing.T) {
	m := initialModel(".", false)
	m.width, m.height = 120, 40
	m.resize()
	m.handleBackendEvent(backendEvent{
		"event": "ready", "provider": "ollama", "model": "llama3.2", "workspace": "/tmp/workspace",
	})
	if m.provider != "ollama" || m.modelName != "llama3.2" || m.workspace != "/tmp/workspace" {
		t.Fatalf("ready event was not reduced: %#v", m)
	}
	m.messages = append(m.messages, chatMessage{role: "assistant", streaming: true})
	m.handleBackendEvent(backendEvent{"event": "stream_delta", "text": "partial"})
	m.handleBackendEvent(backendEvent{"event": "response", "text": "complete"})
	if len(m.messages) != 1 || m.messages[0].text != "complete" || m.messages[0].streaming {
		t.Fatalf("stream response transition was not reduced: %#v", m.messages)
	}
	m.handleBackendEvent(backendEvent{"event": "tasks", "tasks": []any{
		map[string]any{"id": "t1", "description": "Inspect", "status": "running"},
	}})
	if len(m.tasks) != 1 || m.tasks[0].status != "running" {
		t.Fatalf("task event was not reduced: %#v", m.tasks)
	}
	m.handleBackendEvent(backendEvent{
		"event": "prompt", "kind": "approval", "request_id": "approval-1", "action": "git_push", "args": "origin main",
	})
	if m.screen != screenApproval || m.approvalID != "approval-1" {
		t.Fatalf("approval prompt was not reduced: %#v", m)
	}
}

func TestReducedMotionSkipsSplashAndBackgroundFillsWindow(t *testing.T) {
	m := initialModel("", true)
	m.reducedMotion = true
	m.width, m.height = 48, 12
	updated, _ := m.Update(tickMsg{})
	m = updated.(model)
	if m.screen != screenChat {
		t.Fatalf("reduced-motion splash did not finish: %s", m.screen)
	}
	view := m.View()
	if view.Content == "" || len(view.Content) < 12 {
		t.Fatal("window view was not rendered with a background")
	}
}

func TestSplashWaitsForBackendMinimumAndReadyHold(t *testing.T) {
	m := initialModel("", false)
	m.width, m.height = 90, 24
	m.reducedMotion = false
	now := time.Now()
	m.backendReady = true
	m.splashStarted = now.Add(-3 * time.Second)
	m.readyAt = now.Add(-100 * time.Millisecond)
	updated, _ := m.Update(tickMsg(now))
	if updated.(model).screen != screenSplash {
		t.Fatal("splash left before the ready hold elapsed")
	}
	m = updated.(model)
	m.readyAt = now.Add(-readyHoldDuration)
	updated, _ = m.Update(tickMsg(now))
	if updated.(model).screen != screenChat {
		t.Fatal("splash did not leave after the minimum duration and ready hold")
	}
}

func TestSplashBannerFitsSupportedSizes(t *testing.T) {
	for _, size := range [][2]int{{60, 16}, {90, 24}, {140, 40}} {
		m := initialModel("", false)
		m.width, m.height = size[0], size[1]
		m.reducedMotion = true
		for index, line := range strings.Split(m.splash(), "\n") {
			if width := lipgloss.Width(line); width > size[0] {
				t.Fatalf("%dx%d splash line %d is %d cells wide", size[0], size[1], index, width)
			}
		}
		if !strings.Contains(m.splash(), "OPENKYROZEN") {
			t.Fatalf("%dx%d splash lost the wordmark", size[0], size[1])
		}
		if len(bannerRows(size[0]-4)) < 5 {
			t.Fatal("banner is not multi-row")
		}
	}
}

func TestReducedMotionKeepsStaticFinalBanner(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 60, 16
	m.reducedMotion = true
	first := m.splash()
	m.splashFrame = 20
	if second := m.splash(); first != second {
		t.Fatal("reduced-motion splash changed between frames")
	}
}

func TestThinkingCoalescesAndStreamingCursorPulses(t *testing.T) {
	m := initialModel("", false)
	m.messages = []chatMessage{{role: "user", text: "hello"}}
	m.handleBackendEvent(backendEvent{"event": "thinking", "text": "Inspecting"})
	m.handleBackendEvent(backendEvent{"event": "thinking", "text": "Planning"})
	if len(m.messages) != 1 || m.thinkingText != "Planning" {
		t.Fatalf("thinking events were appended instead of coalesced: %#v", m)
	}
	m.messages = append(m.messages, chatMessage{role: "assistant", streaming: true})
	m.cursorVisible = true
	first := m.history(60)
	m.cursorVisible = false
	second := m.history(60)
	if !strings.Contains(first, "▌") || !strings.Contains(second, "▌") || first == second {
		t.Fatal("streaming cursor did not pulse")
	}
}

func TestToolReceiptsKeepSemanticStatus(t *testing.T) {
	m := initialModel("", false)
	m.handleBackendEvent(backendEvent{"event": "tool_receipt", "receipt": map[string]any{
		"action": "run_cmd", "result": "permission denied", "success": false, "failure": "approval_denied",
	}})
	if len(m.messages) != 1 || m.messages[0].status != "blocked" || strings.Contains(m.messages[0].text, "✓") {
		t.Fatalf("tool receipt did not preserve semantic status: %#v", m.messages)
	}
}

func TestTaskStateTransitionAndAdaptiveRail(t *testing.T) {
	m := initialModel("", false)
	m.reducedMotion = false
	m.width = 140
	m.tasks = []taskItem{{id: "t1", description: "Build", status: "running"}}
	if _, rail := m.layoutWidths(); rail == 0 {
		t.Fatal("wide layout did not allocate an activity rail")
	}
	m.handleBackendEvent(backendEvent{"event": "tasks", "tasks": []any{map[string]any{"id": "t1", "description": "Build", "status": "succeeded"}}})
	if m.taskFlashID != "t1" || m.taskFlashTick == 0 {
		t.Fatal("running to completed task transition lost its one-shot state")
	}
	m.width = 90
	if _, rail := m.layoutWidths(); rail != 0 {
		t.Fatal("medium layout did not collapse the rail")
	}
}

func TestRenderedChatFitsSmallWindow(t *testing.T) {
	m := initialModel("", false)
	m.width, m.height = 60, 16
	m.screen = screenChat
	m.messages = []chatMessage{{role: "user", text: strings.Repeat("question ", 12)}, {role: "assistant", text: "A **bright** answer with a readable body."}}
	m.resize()
	for index, line := range strings.Split(m.View().Content, "\n") {
		if width := lipgloss.Width(line); width > m.width {
			t.Fatalf("chat line %d is %d cells wide at width %d", index, width, m.width)
		}
	}
}

func TestModalContentFitsSmallWindow(t *testing.T) {
	m := initialModel("", false)
	m.width, m.height = 60, 16
	m.screen = screenApproval
	m.approvalTool = "run_cmd"
	m.approvalArgs = strings.Repeat("path/to/workspace ", 5)
	m.resize()
	for index, line := range strings.Split(m.View().Content, "\n") {
		if width := lipgloss.Width(line); width > m.width {
			t.Fatalf("modal line %d is %d cells wide at width %d", index, width, m.width)
		}
	}
}

func TestProviderKeyIsMaskedAndApprovalArgsAreRedacted(t *testing.T) {
	m := initialModel("", false)
	m.width, m.height = 60, 16
	m.screen = screenAPIKey
	m.apiInput.SetValue("super-secret-key")
	m.resize()
	view := m.View().Content
	if strings.Contains(view, "super-secret-key") {
		t.Fatal("API key leaked into the rendered modal")
	}
	for index, line := range strings.Split(view, "\n") {
		if width := lipgloss.Width(line); width > m.width {
			t.Fatalf("API key modal line %d is %d cells wide at width %d", index, width, m.width)
		}
	}
	m.screen = screenApproval
	m.approvalTool = "run_cmd"
	m.approvalArgs = safeApprovalArgs("token=super-secret-key path=workspace")
	if strings.Contains(m.approvalArgs, "super-secret-key") || !strings.Contains(m.approvalArgs, "<redacted>") {
		t.Fatalf("approval arguments were not redacted: %q", m.approvalArgs)
	}
}
