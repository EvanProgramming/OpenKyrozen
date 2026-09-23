package main

import (
	"fmt"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
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

func TestRestartEventQuitsForLauncherRelaunch(t *testing.T) {
	m := initialModel(".", false)
	updated, cmd := m.Update(backendLineMsg{event: backendEvent{"event": "restart"}})
	m = updated.(model)
	if !m.restart || m.status != "Restarting…" {
		t.Fatalf("restart event was not reduced: %#v", m)
	}
	if _, ok := cmd().(tea.QuitMsg); !ok {
		t.Fatal("restart event did not quit the TUI")
	}
}

func TestBackendWaitBatchesBufferedEvents(t *testing.T) {
	b := &bridge{events: make(chan backendLineMsg, 3)}
	for _, event := range []string{"status", "stream_delta", "response"} {
		b.events <- backendLineMsg{event: backendEvent{"event": event}}
	}
	close(b.events)

	message, ok := waitBackend(b)().(backendEventsMsg)
	if !ok || len(message.lines) != 3 {
		t.Fatalf("buffered backend events were not batched: %#v", message)
	}
}

func TestBackendWaitStopsBatchAtUpdateNotification(t *testing.T) {
	b := &bridge{events: make(chan backendLineMsg, 2)}
	b.events <- backendLineMsg{event: backendEvent{"event": "status", "state": "updating", "message": "Updating OpenKyrozen…"}}
	b.events <- backendLineMsg{event: backendEvent{"event": "restart"}}

	message, ok := waitBackend(b)().(backendEventsMsg)
	if !ok || len(message.lines) != 1 || !isUpdateStatus(message.lines[0]) {
		t.Fatalf("update notification was not a batch barrier: %#v", message)
	}
}

func TestUpdatingLocksInputButKeepsEmergencyQuit(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 80, 24
	m.screen = screenChat
	updated, _ := m.Update(backendLineMsg{event: backendEvent{
		"event": "status", "state": "updating", "message": "Updating OpenKyrozen…",
	}})
	m = updated.(model)
	if m.screen != screenUpdating || !m.updateInProgress || !strings.Contains(m.View().Content, "Updating OpenKyrozen") {
		t.Fatalf("update lock notification was not rendered: %#v", m)
	}
	before := m.input.Value()
	if _, quit := m.handleKey(tea.KeyPressMsg{Code: tea.KeyEnter}); quit || m.input.Value() != before {
		t.Fatal("normal input was accepted while the update lock was active")
	}
	if _, quit := m.handleKey(tea.KeyPressMsg{Code: 'c', Mod: tea.ModCtrl}); !quit {
		t.Fatal("Ctrl+C was not preserved as an emergency quit")
	}
}

func TestToolDetailsAreHiddenByDefaultAndPersistWhenEnabled(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	m := initialModel("", true)
	m.handleBackendEvent(backendEvent{"event": "tool_receipt", "receipt": map[string]any{
		"action": "read_file", "result": "secret file contents", "success": true,
	}})
	m.handleBackendEvent(backendEvent{"event": "tool_receipt", "receipt": map[string]any{
		"action": "inspect_config", "result": "api_key=super-secret", "success": true,
	}})
	hidden := m.history(100)
	if !strings.Contains(hidden, "TOOL · read_file · SUCCESS") || strings.Contains(hidden, "secret file contents") || strings.Contains(hidden, "super-secret") {
		t.Fatalf("tool receipt default visibility is unsafe: %q", hidden)
	}
	m.screen = screenSettings
	if _, quit := m.handleKey(tea.KeyPressMsg{Code: tea.KeySpace}); quit || !m.showToolDetails {
		t.Fatal("settings toggle did not enable tool details")
	}
	m.screen = screenChat
	shown := m.history(100)
	if !strings.Contains(shown, "secret file contents") || strings.Contains(shown, "super-secret") || !strings.Contains(shown, "api_key=<redacted>") {
		t.Fatalf("enabled tool details were not rendered: %q", shown)
	}
	if loaded := loadUISettings(); !loaded.ShowToolDetails {
		t.Fatal("tool detail setting was not persisted")
	}
	if fresh := initialModel("", true); !fresh.showToolDetails {
		t.Fatal("persisted tool detail setting was not loaded")
	}
}

func TestSettingsSlashCommandOpensLocalScreen(t *testing.T) {
	m := initialModel("", true)
	m.screen = screenChat
	m.input.SetValue("  /settings")
	m.submit()
	if m.screen != screenSettings || m.input.Value() != "" {
		t.Fatalf("/settings was not handled locally: screen=%s input=%q", m.screen, m.input.Value())
	}
}

func TestInteractionCardsRestoreModeQuestionAndPlan(t *testing.T) {
	m := initialModel(".", false)
	m.width, m.height = 120, 40
	m.resize()
	m.handleBackendEvent(backendEvent{"event": "interaction", "interaction": map[string]any{
		"preference_mode": "plan", "effective_mode": "plan", "pending_question": map[string]any{
			"request_id": "question-1", "questions": []any{map[string]any{
				"id": "scope", "header": "Scope", "prompt": "Which target?", "choices": []any{
					map[string]any{"id": "core", "label": "Core", "description": "Small scope"},
					map[string]any{"id": "all", "label": "All", "description": "Broad scope"},
				},
			}},
		}, "pending_plan": map[string]any{
			"plan_id": "plan-under-question", "version": float64(1), "title": "Earlier plan",
			"summary": "Question takes priority.", "steps": []any{map[string]any{
				"id": "step-1", "title": "Wait", "description": "Resolve the question.", "acceptance": []any{"Answered"},
			}},
		},
	}})
	if m.screen != screenQuestion || m.interactionMode != "plan" || m.pendingQuestion == nil {
		t.Fatalf("question interaction was not reduced: %#v", m)
	}
	if card := m.modal(""); !strings.Contains(card, "Which target?") || !strings.Contains(card, "Other") || !strings.Contains(card, "Skip") {
		t.Fatalf("question card is incomplete: %s", card)
	}
	m.handleBackendEvent(backendEvent{"event": "interaction", "interaction": map[string]any{
		"preference_mode": "plan", "effective_mode": "plan", "pending_question": nil,
		"pending_plan": map[string]any{"plan_id": "plan-1", "version": float64(2), "title": "Feature", "summary": "Implement safely.", "steps": []any{
			map[string]any{"id": "step-1", "title": "Implement", "description": "Make the change.", "acceptance": []any{"Tests pass"}},
		}},
	}})
	if m.screen != screenPlan || m.pendingPlan == nil || m.pendingPlan.version != 2 {
		t.Fatalf("plan interaction was not reduced: %#v", m)
	}
	if card := m.modal(""); !strings.Contains(card, "Feature") || !strings.Contains(card, "Tests pass") || !strings.Contains(card, "accept") {
		t.Fatalf("plan card is incomplete: %s", card)
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

func TestSplashRevealHasSignalLogoTaglineAndMilestones(t *testing.T) {
	m := initialModel("", false)
	m.width, m.height = 90, 24
	m.reducedMotion = false
	for frame := 0; frame <= 8; frame++ {
		m.splashFrame = frame
		view := m.splash()
		if frame >= 3 && !strings.Contains(view, "OPENKYROZEN") {
			t.Fatalf("frame %d did not reveal the logo", frame)
		}
		if frame >= 8 && !strings.Contains(view, "workspace") {
			t.Fatalf("frame %d did not render startup milestones", frame)
		}
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

func TestStreamingAssistantSkipsMarkdownUntilResponse(t *testing.T) {
	m := initialModel("", true)
	m.messages = []chatMessage{{role: "assistant", text: "**bold**", streaming: true}}

	streaming := m.history(60)
	if !strings.Contains(streaming, "**bold**") {
		t.Fatalf("streaming output was parsed as Markdown: %q", streaming)
	}
	if m.messages[0].renderedWidth != 0 {
		t.Fatal("streaming output populated the completed-message render cache")
	}

	m.handleBackendEvent(backendEvent{"event": "response", "text": "**bold**"})
	completed := m.history(60)
	if strings.Contains(completed, "**bold**") || m.messages[0].renderedWidth == 0 {
		t.Fatalf("completed output did not render Markdown once: %q", completed)
	}
	rendered := m.messages[0].rendered
	m.history(60)
	if m.messages[0].rendered != rendered {
		t.Fatal("completed Markdown render was not reused")
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
	if !m.animating() {
		t.Fatal("running task did not keep its activity indicator animated")
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

func TestGraphStateRendersWideNarrowAndExplorer(t *testing.T) {
	m := initialModel("", false)
	m.handleBackendEvent(backendEvent{"event": "graph_state", "graph": map[string]any{
		"status": "ready", "nodes": float64(2), "edges": float64(1), "communities": float64(2),
		"mini": map[string]any{
			"nodes": []any{
				map[string]any{"id": "a", "label": "main", "community": float64(0), "degree": float64(1), "source": "main.py"},
				map[string]any{"id": "b", "label": "tools", "community": float64(1), "degree": float64(1), "source": "tools.py"},
			},
			"edges": []any{map[string]any{"source": "a", "target": "b", "relation": "calls"}},
		},
	}})
	m.width, m.height = 140, 40
	m.resize()
	if rail := m.activityRail(); !strings.Contains(rail, "PROJECT GRAPH") || !strings.Contains(rail, "2 nodes") {
		t.Fatalf("wide graph rail is incomplete: %s", rail)
	}
	m.width = 80
	if compact := m.graphCompact(); !strings.Contains(compact, "READY") || !strings.Contains(compact, "2 nodes") {
		t.Fatalf("narrow graph state is incomplete: %s", compact)
	}
	m.screen = screenGraph
	if view := m.graphExplorer(); !strings.Contains(view, "main") || !strings.Contains(view, "neighbors") {
		t.Fatalf("graph explorer is incomplete: %s", view)
	}
	m.height = 24
	if view := m.graphExplorer(); !strings.Contains(view, "Esc close") {
		t.Fatalf("graph explorer controls were clipped in a short terminal: %s", view)
	}
}

func TestGraphKeyboardAndMouseControlsStayBounded(t *testing.T) {
	m := initialModel("", false)
	m.screen = screenGraph
	m.graph = graphSnapshot{status: "ready", communities: 2, miniNodes: []graphNode{{id: "a", label: "a"}, {id: "b", label: "b"}}}
	updated, _ := m.Update(tea.KeyPressMsg{Code: tea.KeyDown})
	m = updated.(model)
	if m.graphSelected != 1 {
		t.Fatal("down did not move graph selection")
	}
	updated, _ = m.Update(tea.MouseWheelMsg{Button: tea.MouseWheelUp})
	m = updated.(model)
	if m.graphZoom != 1 {
		t.Fatal("mouse wheel did not zoom graph")
	}
	for range 20 {
		updated, _ = m.Update(tea.MouseWheelMsg{Button: tea.MouseWheelDown})
		m = updated.(model)
	}
	if m.graphZoom != -1 {
		t.Fatalf("graph zoom escaped its lower bound: %d", m.graphZoom)
	}
}

func TestMouseWheelStaysInsideViewportAndPreservesManualScroll(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 80, 24
	m.screen = screenChat
	m.messages = []chatMessage{{role: "assistant", text: strings.Repeat("line of transcript\n", 80)}}
	m.resize()
	m.view.GotoBottom()
	m.followTail = true

	beforeWheel := m.view.YOffset()
	updated, _ := m.Update(tea.MouseWheelMsg{Button: tea.MouseWheelUp})
	m = updated.(model)
	if beforeWheel-m.view.YOffset() != mouseWheelScrollStep {
		t.Fatalf("mouse wheel did not use the configured fast step: before=%d after=%d", beforeWheel, m.view.YOffset())
	}
	if m.followTail || m.view.AtBottom() {
		t.Fatal("mouse wheel did not move the transcript away from the bottom")
	}

	m.handleBackendEvent(backendEvent{"event": "stream_delta", "text": "more output"})
	if m.followTail {
		t.Fatal("streaming output overrode the user's manual scroll position")
	}
	if got := m.View().MouseMode; got != tea.MouseModeCellMotion {
		t.Fatalf("TUI is not capturing cell-motion mouse input: %v", got)
	}
}

func TestStreamingPreservesManualViewportOffset(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 80, 24
	m.screen = screenChat
	m.messages = []chatMessage{{role: "assistant", text: strings.Repeat("line of transcript\n", 100)}}
	m.resize()
	m.view.GotoBottom()
	m.followTail = true
	m.view.ScrollUp(mouseWheelScrollStep * 3)
	m.followTail = false
	before := m.view.YOffset()
	updated, _ := m.Update(backendLineMsg{event: backendEvent{"event": "stream_delta", "text": "more output"}})
	m = updated.(model)
	if m.followTail || m.view.YOffset() != before {
		t.Fatalf("streaming changed the manually selected viewport offset: before=%d after=%d", before, m.view.YOffset())
	}
	m.height = 10
	m.resize()
	if m.view.PastBottom() {
		t.Fatal("resize left the viewport beyond the available content")
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

func TestUpdateAndSettingsViewsFitSmallWindow(t *testing.T) {
	for _, screen := range []screen{screenUpdating, screenSettings} {
		m := initialModel("", true)
		m.width, m.height = 60, 16
		m.screen = screen
		m.updateInProgress = screen == screenUpdating
		m.status = "Updating OpenKyrozen…"
		m.resize()
		for index, line := range strings.Split(m.View().Content, "\n") {
			if width := lipgloss.Width(line); width > m.width {
				t.Fatalf("%s line %d is %d cells wide at width %d", screen, index, width, m.width)
			}
		}
	}
}

func TestLongPlanModalScrollsAndKeepsActionsVisible(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 80, 16
	m.screen = screenPlan
	m.pendingPlan = &planProposal{planID: "plan-1", version: 1, title: "Long plan", summary: strings.Repeat("summary ", 20)}
	for index := range 8 {
		m.pendingPlan.steps = append(m.pendingPlan.steps, planStep{
			title:       fmt.Sprintf("Step %d", index+1),
			description: strings.Repeat("detailed work ", 8),
			acceptance:  []string{fmt.Sprintf("criterion-%d", index+1)},
		})
	}
	m.resize()

	first := m.View().Content
	if !strings.Contains(first, "A / Enter  accept") || !strings.Contains(first, "PgUp/PgDn scroll") {
		t.Fatalf("plan actions or scroll hint were clipped: %s", first)
	}
	if strings.Contains(first, "criterion-8") {
		t.Fatal("long plan unexpectedly fit without scrolling")
	}

	updated, _ := m.Update(tea.KeyPressMsg{Code: tea.KeyEnd})
	m = updated.(model)
	last := m.View().Content
	if !strings.Contains(last, "criterion-8") || !strings.Contains(last, "A / Enter  accept") {
		t.Fatalf("last plan page or actions were not visible after scrolling: %s", last)
	}
	if got := len(strings.Split(last, "\n")); got != m.height {
		t.Fatalf("plan view rendered %d rows for a %d-row terminal", got, m.height)
	}
}

func TestEffectiveAgentModeIsProminentDuringPlanExecution(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 125, 39
	m.screen = screenChat
	m.applyInteraction(map[string]any{
		"preference_mode": "plan",
		"effective_mode":  "agent",
	})
	m.resize()
	view := m.View().Content
	if !strings.Contains(view, "AGENT") {
		t.Fatalf("header did not show effective Agent mode: %s", view)
	}
	if !strings.Contains(view, "preference plan") || !strings.Contains(view, "agent") {
		t.Fatalf("activity rail did not distinguish preference and active mode: %s", view)
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
