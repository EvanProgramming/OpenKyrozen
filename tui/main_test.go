package main

import "testing"

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
