package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

func TestOnboardingDistinguishesNewAndUpdatedInstall(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	fresh := initialModel("", true)
	if fresh.onboardingKind != "new" {
		t.Fatalf("fresh install was classified as %q", fresh.onboardingKind)
	}
	fresh.width, fresh.height, fresh.screen = 80, 24, screenOnboarding
	fresh.resize()
	if !strings.Contains(fresh.View().Content, "WELCOME TO OPENKYROZEN") {
		t.Fatal("fresh install did not render the themed welcome screen")
	}

	if err := os.MkdirAll(filepath.Dir(uiSettingsPath()), 0o700); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(uiSettings{LastSeenVersion: "2.0.3", OnboardingComplete: true})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(uiSettingsPath(), data, 0o600); err != nil {
		t.Fatal(err)
	}
	updated := initialModel("", true)
	if updated.onboardingKind != "update" || updated.onboardingPreviousVersion != "2.0.3" {
		t.Fatalf("updated install was classified as %q from %q", updated.onboardingKind, updated.onboardingPreviousVersion)
	}
}

func TestOnboardingCompletionPersistsVersionAndReturnsToChat(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	m := initialModel("", true)
	m.screen, m.onboardingKind = screenOnboarding, "new"
	m.handleBackendEvent(backendEvent{"event": "onboarding_complete", "kind": "new"})
	if m.screen != screenChat || m.onboardingKind != "" {
		t.Fatalf("onboarding did not return to chat: screen=%s kind=%q", m.screen, m.onboardingKind)
	}
	settings := loadUISettings()
	if settings.LastSeenVersion != version || !settings.OnboardingComplete {
		t.Fatalf("onboarding completion was not persisted: %#v", settings)
	}
}

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

func TestSettingsExposeInteractionControlsAndKeepCommandDispatch(t *testing.T) {
	t.Setenv("HOME", t.TempDir())
	m := initialModel("", true)
	m.width, m.height, m.screen = 100, 30, screenSettings
	m.applyInteraction(map[string]any{
		"preference_mode": "plan",
		"fast_backend":    "kev",
		"decision_assist": map[string]any{"backend": "jev", "kev_private_consent": false},
	})
	view := m.View().Content
	for _, want := range []string{"Show tool details", "Interaction mode", "System One backend", "Decision Assist", "Kev private context", "PLAN", "KEV", "JEV"} {
		if !strings.Contains(view, want) {
			t.Fatalf("settings omitted %q: %s", want, view)
		}
	}

	var sent bytes.Buffer
	m.bridge.stdin = bufio.NewWriter(&sent)
	m.settingsIdx = 1
	if _, quit := m.handleKey(tea.KeyPressMsg{Code: tea.KeyRight}); quit {
		t.Fatal("settings interaction mode change unexpectedly quit")
	}
	var command map[string]any
	if err := json.Unmarshal(bytes.TrimSpace(sent.Bytes()), &command); err != nil {
		t.Fatalf("settings did not send valid command: %v", err)
	}
	if command["command"] != "command" || command["name"] != "mode" || command["args"] != "agent" {
		t.Fatalf("settings sent unexpected mode command: %#v", command)
	}
}

func TestUsageEventAppearsInChatAndSettings(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 110, 24
	m.screen = screenChat
	m.handleBackendEvent(backendEvent{"event": "ready", "provider": "deepseek", "model": "deepseek-chat"})
	m.handleBackendEvent(backendEvent{
		"event": "usage", "scope": "workspace", "attempts": 3,
		"prompt_tokens": 1200, "completion_tokens": 800, "reasoning_tokens": 100,
		"cost_picos": 12500000000,
	})
	m.resize()
	chat := m.View().Content
	if !strings.Contains(chat, "deepseek-chat") || !strings.Contains(chat, "2.1K TOK") || !strings.Contains(chat, "$0.01") {
		t.Fatalf("chat header omitted model or usage summary: %s", chat)
	}
	if got := lipgloss.Height(m.chatHeader()); got != 1 {
		t.Fatalf("context bar rendered %d rows, want 1", got)
	}
	m.screen = screenSettings
	settings := m.View().Content
	if !strings.Contains(settings, "SESSION") || !strings.Contains(settings, "MEMORY") || !strings.Contains(settings, "workspace") {
		t.Fatalf("settings did not show session details: %s", settings)
	}
}

func TestChatFitsResponsiveTerminalSizes(t *testing.T) {
	for _, size := range [][2]int{{60, 16}, {80, 24}, {110, 24}, {140, 40}} {
		assertChatFits(t, size[0], size[1])
	}
}

func TestChatFitsArbitraryTerminalSizes(t *testing.T) {
	for width := 1; width <= 160; width++ {
		for height := 1; height <= 50; height++ {
			assertChatFits(t, width, height)
		}
	}
}

func TestChatFitsLargeAndFullScreenTerminalSizes(t *testing.T) {
	widths := []int{1, 2, 3, 4, 5, 10, 20, 39, 40, 59, 60, 79, 80, 99, 100, 109, 110, 119, 120, 140, 160, 200, 240, 300, 400, 512, 800}
	heights := []int{1, 2, 3, 4, 5, 8, 12, 15, 16, 17, 23, 24, 30, 40, 50, 60, 80, 100, 120, 160, 200}
	for _, width := range widths {
		for _, height := range heights {
			assertChatFits(t, width, height)
		}
	}
	for _, size := range [][2]int{{1920, 1080}, {2560, 1440}} {
		assertChatFits(t, size[0], size[1])
	}
}

func TestSmallChatKeepsComposerAndFooterVisible(t *testing.T) {
	for _, size := range [][2]int{{20, 8}, {30, 10}, {60, 12}, {60, 16}} {
		m := initialModel("", true)
		m.width, m.height, m.screen = size[0], size[1], screenChat
		m.provider, m.modelName = "deepseek", "deepseek-chat"
		m.resize()
		content := m.View().Content
		if !strings.Contains(content, "Kyrozen") {
			t.Fatalf("terminal %dx%d hid the composer: %s", size[0], size[1], content)
		}
		if !strings.Contains(content, "send") {
			t.Fatalf("terminal %dx%d hid the footer: %s", size[0], size[1], content)
		}
	}
}

func TestPasteMsgReachesChatComposer(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height, m.screen = 80, 24, screenChat
	m.resize()
	updated, _ := m.Update(tea.PasteMsg{Content: `/attach "/tmp/file with spaces.png"`})
	m = updated.(model)
	if got := m.input.Value(); got != `/attach "/tmp/file with spaces.png"` {
		t.Fatalf("paste was not delivered to the composer: %q", got)
	}
}

func TestSplashFitsNarrowTerminalSizes(t *testing.T) {
	for width := 1; width <= 60; width++ {
		m := initialModel("", true)
		m.width, m.height, m.screen = width, 8, screenSplash
		m.reducedMotion = true
		for index, line := range strings.Split(m.View().Content, "\n") {
			if renderedWidth := lipgloss.Width(line); renderedWidth > width {
				t.Fatalf("splash width %d line %d is %d cells wide", width, index, renderedWidth)
			}
		}
	}
}

func assertChatFits(t *testing.T, width, height int) {
	t.Helper()
	m := initialModel("", true)
	m.width, m.height = width, height
	m.screen = screenChat
	m.provider, m.modelName = "deepseek", "deepseek-chat"
	m.messages = []chatMessage{{role: "assistant", text: "A response that must remain readable at every terminal size."}}
	m.activeScopeID, m.activeSessionID = "scope-project", "chat-project"
	m.navigation = []navigationGroup{
		{scope: "global", scopeID: "global", name: "No Project", chats: []navigationChat{{id: "chat-global", title: "Global chat"}}},
		{scope: "project", scopeID: "scope-project", name: "Example Project", chats: []navigationChat{{id: "chat-project", title: "Project chat"}}},
	}
	m.resize()
	lines := strings.Split(m.View().Content, "\n")
	if len(lines) != height {
		t.Fatalf("terminal %dx%d rendered %d rows", width, height, len(lines))
	}
	for index, line := range lines {
		if renderedWidth := lipgloss.Width(line); renderedWidth > width {
			t.Fatalf("terminal %dx%d line %d is %d cells wide", width, height, index, renderedWidth)
		}
	}
}

func TestActivityRailCannotPushComposerOffscreen(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height = 120, 30
	m.screen = screenChat
	m.provider, m.modelName = "deepseek", "deepseek-v4-flash"
	for index := 0; index < 5; index++ {
		m.tasks = append(m.tasks, taskItem{id: fmt.Sprintf("task-%d", index), description: "Task details", status: "completed"})
	}
	m.resize()
	content := m.View().Content
	if !strings.Contains(content, "Ctrl+C quit") {
		t.Fatalf("footer was pushed below a 120x30 terminal: %s", content)
	}
	if !strings.Contains(content, "Kyrozen anything") {
		t.Fatalf("composer was not rendered at a 120x30 terminal: %s", content)
	}
}

func TestProjectChatSidebarExposesCreationAndKeepsSelectedChatVisible(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height, m.screen = 120, 16, screenChat
	m.navigationFocused = true
	for project := 0; project < 8; project++ {
		group := navigationGroup{scopeID: fmt.Sprintf("scope-%d", project), name: fmt.Sprintf("Project %d", project)}
		for chat := 0; chat < 3; chat++ {
			group.chats = append(group.chats, navigationChat{id: fmt.Sprintf("chat-%d-%d", project, chat), title: fmt.Sprintf("Chat %d", chat)})
		}
		m.navigation = append(m.navigation, group)
	}
	m.activeScopeID, m.activeSessionID = "scope-7", "chat-7-2"
	eventGroups := make([]any, 0, len(m.navigation))
	for _, group := range m.navigation {
		chats := make([]any, 0, len(group.chats))
		for _, chat := range group.chats {
			chats = append(chats, map[string]any{"session_id": chat.id, "title": chat.title})
		}
		eventGroups = append(eventGroups, map[string]any{"scope_id": group.scopeID, "name": group.name, "chats": chats})
	}
	m.applyNavigation(backendEvent{"active_scope_id": m.activeScopeID, "active_session_id": m.activeSessionID, "groups": eventGroups})
	view := m.navigationSidebar(m.navigationWidth(), m.historyHeight())
	for _, text := range []string{"+ New chat", "+ New project", "Project 7", "Chat 2"} {
		if !strings.Contains(view, text) {
			t.Fatalf("vertical project/chat navigation lost %q: %s", text, view)
		}
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
	m.width, m.height = 140, 30
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

func TestFocusedCockpitRailBreakpointAndCompactGraph(t *testing.T) {
	m := initialModel("", true)
	m.graph = graphSnapshot{status: "ready", nodes: 12, edges: 18}
	for _, size := range [][2]int{{119, 40}, {120, 23}} {
		m.width, m.height = size[0], size[1]
		if _, rail := m.layoutWidths(); rail != 0 {
			t.Fatalf("terminal %dx%d unexpectedly rendered a rail", size[0], size[1])
		}
		if header := m.chatHeader(); !strings.Contains(header, "READY") {
			t.Fatalf("terminal %dx%d lost compact graph state: %s", size[0], size[1], header)
		}
	}
	m.width, m.height = 120, 24
	if _, rail := m.layoutWidths(); rail == 0 {
		t.Fatal("120x24 did not enable the contextual rail")
	}
	if header := m.chatHeader(); strings.Contains(header, "◇ READY") {
		t.Fatalf("wide header duplicated graph telemetry: %s", header)
	}
}

func TestFocusedCockpitMessageHierarchyAndEmptyState(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height, m.screen = 120, 30, screenChat
	mainWidth, _ := m.layoutWidths()
	empty := m.history(mainWidth)
	for _, unwanted := range []string{"WORKSPACE READY", "/plan", "/attach", "/graph", "ASK ANYTHING"} {
		if strings.Contains(empty, unwanted) {
			t.Fatalf("empty state contains unwanted text %q: %s", unwanted, empty)
		}
	}
	if lipgloss.Height(empty) != m.historyHeight() {
		t.Fatalf("empty state height is %d, want %d", lipgloss.Height(empty), m.historyHeight())
	}
	if !strings.Contains(empty, "█") || !strings.Contains(empty, "░") {
		t.Fatalf("empty state lost the readable face or 3D shadow: %s", empty)
	}
	bannerLine := -1
	for index, line := range strings.Split(empty, "\n") {
		if strings.Contains(line, "█") {
			bannerLine = index
			break
		}
	}
	if bannerLine < 2 || bannerLine > m.historyHeight()-lipgloss.Height(renderOpenKyrozenBanner(0, false))-2 {
		t.Fatalf("empty-state banner is not vertically centered: line %d of %d", bannerLine, m.historyHeight())
	}
	compact := m.history(20)
	if !strings.Contains(compact, "OPENKYROZEN") || strings.Contains(compact, "░") {
		t.Fatalf("narrow empty state did not use the compact wordmark: %s", compact)
	}
	for _, size := range [][2]int{{1, 1}, {20, 8}, {40, 12}, {60, 16}, {80, 24}, {120, 30}, {140, 40}, {200, 60}, {800, 200}} {
		sized := initialModel("", true)
		sized.width, sized.height, sized.screen = size[0], size[1], screenChat
		sized.resize()
		content := sized.View().Content
		if size[0] >= 11 && size[0] < 80 && !strings.Contains(content, "OPENKYROZEN") {
			t.Fatalf("%dx%d empty state lost the compact wordmark", size[0], size[1])
		}
		if size[0] >= 80 && (!strings.Contains(content, "█") || !strings.Contains(content, "░")) {
			t.Fatalf("%dx%d empty state lost the 3D wordmark", size[0], size[1])
		}
		if len(strings.Split(content, "\n")) != size[1] {
			t.Fatalf("%dx%d empty state exceeded terminal height", size[0], size[1])
		}
		for index, line := range strings.Split(content, "\n") {
			if width := lipgloss.Width(line); width > size[0] {
				t.Fatalf("%dx%d empty-state line %d is %d cells wide", size[0], size[1], index, width)
			}
		}
	}
	m.reducedMotion = false
	m.motionFrame = 12
	animated := m.history(mainWidth)
	m.motionFrame = 32
	if animated == m.history(mainWidth) || !m.animating() {
		t.Fatal("empty banner did not animate its light sweep")
	}
	m.reducedMotion = true
	still := m.history(mainWidth)
	m.motionFrame = 64
	if still != m.history(mainWidth) || m.animating() {
		t.Fatal("reduced motion did not freeze the banner")
	}
	m.messages = []chatMessage{
		{role: "user", text: "Review this"},
		{role: "assistant", text: "Working **carefully**.", streaming: true},
		{role: "thinking", text: "Inspecting the graph"},
		{role: "receipt", toolAction: "read_file", status: "success", toolDetail: "private details"},
	}
	history := m.history(80)
	for _, label := range []string{"YOU", "KYROZEN", "THINKING", "TOOL · read_file · SUCCESS", "▌"} {
		if !strings.Contains(history, label) {
			t.Fatalf("message hierarchy omitted %q: %s", label, history)
		}
	}
	if strings.Contains(history, "private details") {
		t.Fatalf("collapsed tool receipt exposed details: %s", history)
	}
	if strings.Contains(history, "░") {
		t.Fatal("empty-state banner remained after the conversation started")
	}
}

func TestEmptyBannerIsHiddenOutsideChat(t *testing.T) {
	for _, current := range []screen{
		screenSplash, screenOnboarding, screenProvider, screenAPIKey, screenApproval,
		screenSelfLearning, screenMode, screenQuestion, screenPlan, screenGraph,
		screenGithubAuth, screenSettings, screenUpdating, screenError,
	} {
		m := initialModel("", true)
		m.width, m.height, m.screen = 120, 30, current
		m.resize()
		if content := m.View().Content; strings.Contains(content, "░") {
			t.Fatalf("empty banner remained visible on %s screen", current)
		}
	}
}

func TestFocusedCockpitDoesNotDuplicateModelTelemetry(t *testing.T) {
	m := initialModel("/tmp/workspace", true)
	m.width, m.height, m.screen = 140, 40, screenChat
	m.modelName = "deepseek-v4-flash"
	m.usageScope, m.usageAttempts = "workspace", 4
	m.usagePromptTokens, m.usageCompletionTokens, m.usageReasoningTokens = 5400, 1800, 620
	m.resize()
	view := m.View().Content
	if got := strings.Count(view, "deepseek-v4-flash"); got != 1 {
		t.Fatalf("model telemetry appeared %d times, want once: %s", got, view)
	}
	if got := strings.Count(view, "7.8K TOK"); got != 1 || !strings.Contains(view, "4 calls · workspace") {
		t.Fatalf("session usage hierarchy is incomplete or duplicated: %s", view)
	}
}

func TestFocusedCockpitPaletteAndModalUseUnifiedFrame(t *testing.T) {
	m := initialModel("", true)
	m.palette = commandMatches("/")
	palette := m.paletteView(80)
	if !strings.Contains(palette, "╭") || !strings.Contains(palette, "COMMANDS") {
		t.Fatalf("command palette lost the cockpit frame: %s", palette)
	}
	m.width, m.height, m.screen = 80, 24, screenApproval
	m.approvalTool = "run_cmd"
	m.resize()
	modal := m.View().Content
	if !strings.Contains(modal, "╭") || !strings.Contains(modal, "APPROVAL REQUIRED") {
		t.Fatalf("approval modal lost the cockpit frame: %s", modal)
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

func TestOnboardingSelfLearningModalStaysCompact(t *testing.T) {
	m := initialModel("", true)
	m.width, m.height, m.screen = 117, 35, screenSelfLearning
	m.onboardingSelfLearning = true
	m.features = []featureItem{{name: "auto_learn_conversations", description: "Extract durable facts"}}
	m.resize()

	modal := m.modal("")
	if !strings.Contains(modal, "Choose self-learning") || !strings.Contains(modal, "Local Qwen2.5") {
		t.Fatalf("onboarding choice screen is incomplete: %s", modal)
	}
	if strings.Contains(modal, "auto_learn_conversations") {
		t.Fatalf("onboarding rendered the full feature catalog: %s", modal)
	}
	if height := lipgloss.Height(modal); height > m.height {
		t.Fatalf("onboarding modal rendered %d rows for a %d-row terminal", height, m.height)
	}
	for index, line := range strings.Split(modal, "\n") {
		if width := lipgloss.Width(line); width > m.width {
			t.Fatalf("onboarding modal line %d is %d cells wide at width %d", index, width, m.width)
		}
	}

	var sent bytes.Buffer
	m.bridge.stdin = bufio.NewWriter(&sent)
	m.featureKey("enter")
	if m.features[0].enabled {
		t.Fatal("Enter toggled a feature instead of continuing onboarding")
	}
	if !strings.Contains(sent.String(), `"mode":"local"`) || !strings.Contains(sent.String(), `"onboarding":true`) {
		t.Fatalf("Enter did not continue with the selected onboarding mode: %s", sent.String())
	}

	m.onboardingSelfLearning = false
	if normal := m.modal(""); !strings.Contains(normal, "auto_learn_conversations") {
		t.Fatal("normal self-learning settings lost the feature catalog")
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
		"fast_backend":    "kev",
	})
	m.resize()
	view := m.View().Content
	if !strings.Contains(view, "AGENT") {
		t.Fatalf("header did not show effective Agent mode: %s", view)
	}
	if !strings.Contains(view, "preference plan") || !strings.Contains(view, "agent") {
		t.Fatalf("activity rail did not distinguish preference and active mode: %s", view)
	}
	if !strings.Contains(view, "System One: kev") {
		t.Fatalf("activity rail did not show System One setting: %s", view)
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
