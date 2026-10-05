package main

import (
	tea "charm.land/bubbletea/v2"
	"fmt"
	"os/exec"
	"strings"
	"unicode"
)

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Cmd, bool) {
	key := msg.String()
	if key == "ctrl+c" {
		return nil, true
	}
	if m.updateInProgress {
		return nil, false
	}
	if m.screen == screenOnboarding {
		if key == "enter" && !m.onboardingWaiting {
			m.onboardingWaiting = true
			m.status = "Preparing setup…"
			m.send("command", map[string]any{"name": "onboarding_continue"})
		}
		return nil, false
	}
	if m.screen == screenError {
		if key == "esc" || key == "enter" {
			if m.onboardingKind != "" {
				m.screen = screenOnboarding
			} else {
				m.screen = screenChat
			}
		}
		return nil, false
	}
	if m.screen == screenApproval {
		if key == "y" || key == "Y" || key == "enter" {
			m.send("approval_response", map[string]any{"request_id": m.approvalID, "approved": true})
			m.screen = screenChat
		} else if key == "n" || key == "N" || key == "esc" {
			m.send("approval_response", map[string]any{"request_id": m.approvalID, "approved": false})
			m.screen = screenChat
		}
		return nil, false
	}
	if m.screen == screenDeleteConfirm {
		switch key {
		case "y", "Y", "enter":
			m.send("navigate", map[string]any{
				"action": m.pendingDeleteKind, "scope_id": m.pendingDeleteScopeID,
				"session_id": m.pendingDeleteSessionID, "confirmed": true,
			})
			m.clearDeleteConfirmation()
			m.screen = screenChat
		case "n", "N", "esc":
			m.clearDeleteConfirmation()
			m.screen, m.navigationFocused = screenChat, true
		}
		return nil, false
	}
	if m.screen == screenGithubAuth {
		if key == "esc" {
			m.screen = screenChat
			return nil, false
		}
		if key == "enter" && m.githubBinary != "" {
			command := exec.Command(m.githubBinary, "auth", "login", "--hostname", firstNonEmpty(m.githubHostname, "github.com"), "--web", "--git-protocol", "https")
			return tea.ExecProcess(command, func(err error) tea.Msg { return githubAuthDoneMsg{err: err} }), false
		}
		return nil, false
	}
	if m.screen == screenGraph {
		return m.graphKey(msg), false
	}
	if m.screen == screenAgents {
		switch key {
		case "esc", "q":
			m.screen = screenChat
		case "up", "k":
			m.agentScroll = maxInt(0, m.agentScroll-1)
		case "down", "j":
			m.agentScroll++
		case "pgup":
			m.agentScroll = maxInt(0, m.agentScroll-maxInt(1, m.height-5))
		case "pgdown":
			m.agentScroll += maxInt(1, m.height-5)
		case "left", "h":
			m.agentSelected = maxInt(0, m.agentSelected-1)
			m.agentScroll = 0
		case "right", "l":
			m.agentSelected = minInt(maxInt(0, len(m.agents)-1), m.agentSelected+1)
			m.agentScroll = 0
		case "enter", "r":
			if len(m.agents) > 0 {
				m.send("command", map[string]any{"name": "agents", "args": map[string]any{"run_id": m.agents[m.agentSelected]["run_id"]}})
			}
		case "c":
			if len(m.agents) > 0 {
				m.send("command", map[string]any{"name": "agents", "args": map[string]any{"cancel": m.agents[m.agentSelected]["run_id"]}})
			}
		}
		return nil, false
	}
	if m.screen == screenMode {
		modes := []string{"auto", "ask", "plan", "agent"}
		if key == "esc" {
			m.screen = screenChat
			return nil, false
		}
		if key == "up" || key == "k" {
			m.modeIdx = (m.modeIdx - 1 + len(modes)) % len(modes)
		}
		if key == "down" || key == "j" {
			m.modeIdx = (m.modeIdx + 1) % len(modes)
		}
		if key == "enter" {
			m.send("command", map[string]any{"name": "mode", "args": modes[m.modeIdx]})
			m.screen = screenChat
		}
		return nil, false
	}
	if m.screen == screenPermissions {
		choices := []string{"ask", "full_jev", "full"}
		switch key {
		case "esc":
			m.screen = screenChat
			m.input.Focus()
		case "up", "k":
			m.permissionIdx = (m.permissionIdx - 1 + len(choices)) % len(choices)
		case "down", "j":
			m.permissionIdx = (m.permissionIdx + 1) % len(choices)
		case "enter":
			m.send("command", map[string]any{"name": "permissions", "args": choices[m.permissionIdx]})
			m.screen = screenChat
			m.input.Focus()
		}
		return nil, false
	}
	if m.screen == screenQuestion {
		return m.questionKey(key), false
	}
	if m.screen == screenPlan {
		switch key {
		case "up", "k":
			m.scrollPlan(-1)
		case "down", "j":
			m.scrollPlan(1)
		case "pgup":
			m.scrollPlan(-m.planPageHeight())
		case "pgdown":
			m.scrollPlan(m.planPageHeight())
		case "home":
			m.planScroll = 0
		case "end":
			m.planScroll = m.planMaxScroll()
		case "a", "A", "enter":
			if m.pendingPlan != nil {
				m.send("plan_action", map[string]any{"action": "accept", "plan_id": m.pendingPlan.planID, "version": m.pendingPlan.version})
			}
			m.screen = screenChat
		case "c", "C", "esc":
			if m.pendingPlan != nil {
				m.send("plan_action", map[string]any{"action": "cancel", "plan_id": m.pendingPlan.planID, "version": m.pendingPlan.version})
			}
			m.screen = screenChat
		case "r", "R":
			m.screen = screenChat
			m.input.Placeholder = "Describe the plan revision…"
			m.input.Focus()
		}
		return nil, false
	}
	if m.screen == screenProvider {
		return m.providerKey(key), false
	}
	if m.screen == screenSelfLearning {
		return m.featureKey(key), false
	}
	if m.screen == screenSettings {
		return m.settingsKey(key), false
	}
	if m.screen == screenProject {
		if key == "esc" {
			m.projectInput.Blur()
			m.screen = screenChat
			return nil, false
		}
		if key == "enter" {
			path := strings.TrimSpace(m.projectInput.Value())
			if path != "" {
				m.send("navigate", map[string]any{"action": "project", "path": path})
			}
			m.projectInput.Blur()
			m.screen = screenChat
			return nil, false
		}
		var cmd tea.Cmd
		m.projectInput, cmd = m.projectInput.Update(msg)
		return cmd, false
	}
	if m.screen == screenAPIKey {
		if key == "esc" {
			m.apiInput.Reset()
			m.fastKeyInput = false
			m.decisionAssistKeyInput = false
			m.permissionKeyInput = false
			if m.onboardingKind != "" {
				m.screen = screenOnboarding
				m.onboardingWaiting = false
			} else {
				m.screen = screenChat
			}
			return nil, false
		}
		if key == "enter" {
			if m.fastKeyInput {
				m.send("command", map[string]any{"name": "system-one", "args": map[string]any{"backend": "jev", "api_key": m.apiInput.Value()}})
				m.fastKeyInput = false
			} else if m.decisionAssistKeyInput {
				m.send("command", map[string]any{"name": "decision-assist", "args": map[string]any{"backend": "jev", "api_key": m.apiInput.Value()}})
				m.decisionAssistKeyInput = false
			} else if m.permissionKeyInput {
				m.send("command", map[string]any{"name": "permissions", "args": map[string]any{"mode": "full_jev", "api_key": m.apiInput.Value()}})
				m.permissionKeyInput = false
			} else {
				m.send("command", map[string]any{"name": "api_key", "args": map[string]any{"api_key": m.apiInput.Value()}})
			}
			m.apiInput.Reset()
			if m.onboardingKind != "" {
				m.screen = screenOnboarding
				m.onboardingWaiting = true
			} else {
				m.screen = screenChat
			}
			return nil, false
		}
		var cmd tea.Cmd
		m.apiInput, cmd = m.apiInput.Update(msg)
		return cmd, false
	}
	modeShortcut := key == "ctrl+t" || key == "ctrl+shift+t"
	tabShortcut := len(m.palette) == 0 && (key == "tab" || key == "shift+tab")
	if m.screen == screenChat && !m.busy && (modeShortcut || tabShortcut) {
		modes := []string{"auto", "ask", "plan", "agent"}
		delta := 1
		if key == "ctrl+shift+t" || key == "shift+tab" {
			delta = -1
		}
		mode := cycleSetting(firstNonEmpty(m.interactionMode, "auto"), modes, delta)
		m.send("command", map[string]any{"name": "mode", "args": mode})
		return nil, false
	}
	if m.screen == screenChat && !m.busy && key == "ctrl+p" {
		m.permissionIdx = 0
		for i, mode := range []string{"ask", "full_jev", "full"} {
			if mode == m.permissionMode {
				m.permissionIdx = i
			}
		}
		m.input.Blur()
		m.screen = screenPermissions
		return nil, false
	}
	if m.screen == screenChat && (key == "ctrl+b" || key == "ctrl+\\") {
		m.navigationOpen = !m.navigationOpen
		m.navigationFocused = m.navigationOpen
		if m.navigationFocused {
			m.input.Blur()
		} else {
			m.input.Focus()
		}
		return nil, false
	}
	if m.screen == screenChat && key == "ctrl+e" {
		m.send("command", map[string]any{"name": "agents"})
		return nil, false
	}
	if m.screen == screenChat && m.navigationFocused {
		switch key {
		case "esc":
			m.navigationFocused, m.navigationOpen = false, false
			m.input.Focus()
		case "up", "k":
			m.navigationIndex = maxInt(0, m.navigationIndex-1)
		case "down", "j":
			m.navigationIndex = minInt(maxInt(0, len(m.navigationTargets())-1), m.navigationIndex+1)
		case "enter":
			targets := m.navigationTargets()
			if len(targets) > 0 {
				target := targets[m.navigationIndex]
				switch target.kind {
				case "chat":
					m.send("navigate", map[string]any{"action": "switch", "scope_id": target.scopeID, "session_id": target.id})
				case "project":
					m.send("navigate", map[string]any{"action": "project", "path": target.path})
				default:
					m.send("navigate", map[string]any{"action": "new"})
				}
			}
		case "d":
			if m.busy {
				m.status = "Cannot delete while a turn is running."
				return nil, false
			}
			targets := m.navigationTargets()
			if len(targets) == 0 {
				break
			}
			target := targets[m.navigationIndex]
			if target.kind == "workspace" {
				m.status = "The global workspace cannot be deleted as a project."
				return nil, false
			}
			m.pendingDeleteKind = "delete_" + target.kind
			m.pendingDeleteScopeID = target.scopeID
			m.pendingDeleteSessionID = target.id
			m.pendingDeletePath = target.path
			m.pendingDeleteTitle = target.kind
			if target.kind == "project" {
				for _, group := range m.navigation {
					if group.scopeID == target.scopeID {
						m.pendingDeleteTitle = group.name
					}
				}
			} else if target.kind == "chat" {
				m.pendingDeleteTitle = "this saved conversation"
				for _, group := range m.navigation {
					if group.scopeID == target.scopeID {
						for _, chat := range group.chats {
							if chat.id == target.id {
								m.pendingDeleteTitle = chat.title
							}
						}
					}
				}
			}
			m.screen = screenDeleteConfirm
		case "n":
			m.send("navigate", map[string]any{"action": "new"})
		case "p":
			m.screen = screenProject
			m.projectInput.Reset()
			return m.projectInput.Focus(), false
		}
		return nil, false
	}
	if m.screen == screenChat && key == "ctrl+n" && strings.TrimSpace(m.input.Value()) == "" {
		m.send("navigate", map[string]any{"action": "new"})
		return nil, false
	}
	if m.screen == screenChat && key == "ctrl+o" && strings.TrimSpace(m.input.Value()) == "" {
		m.screen = screenProject
		m.projectInput.Reset()
		return m.projectInput.Focus(), false
	}
	if key == "esc" && len(m.palette) > 0 {
		m.palette = nil
		m.input.SetHeight(m.composerHeight())
		return nil, false
	}
	if key == "pgup" {
		m.followTail = false
		m.view.PageUp()
		return nil, false
	}
	if key == "pgdown" {
		m.view.PageDown()
		m.followTail = m.view.AtBottom()
		return nil, false
	}
	if key == "g" && len(m.palette) == 0 && strings.TrimSpace(m.input.Value()) == "" {
		m.screen = screenGraph
		m.send("graph_request", map[string]any{"action": "snapshot"})
		return nil, false
	}
	if len(m.palette) > 0 {
		switch key {
		case "up", "ctrl+p":
			m.paletteIndex = (m.paletteIndex - 1 + len(m.palette)) % len(m.palette)
			return nil, false
		case "down", "ctrl+n":
			m.paletteIndex = (m.paletteIndex + 1) % len(m.palette)
			return nil, false
		case "tab":
			m.input.SetValue(replaceCommand(m.input.Value(), m.palette[m.paletteIndex]))
			m.refreshPalette()
			return nil, false
		case "enter":
			_, _, token, exact := commandToken(m.input.Value())
			if exact && token == m.palette[m.paletteIndex].name {
				m.palette = nil
				return m.submit(), false
			}
			m.input.SetValue(replaceCommand(m.input.Value(), m.palette[m.paletteIndex]))
			m.refreshPalette()
			return nil, false
		}
	}
	if key == "enter" {
		return m.submit(), false
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	m.refreshPalette()
	return cmd, false
}

func (m *model) clearDeleteConfirmation() {
	m.pendingDeleteKind = ""
	m.pendingDeleteScopeID = ""
	m.pendingDeleteSessionID = ""
	m.pendingDeletePath = ""
	m.pendingDeleteTitle = ""
}

func (m *model) graphKey(msg tea.KeyPressMsg) tea.Cmd {
	key := msg.String()
	if m.graphSearching {
		if key == "esc" {
			m.graphSearching = false
			m.graphInput.Blur()
			return nil
		}
		if key == "enter" {
			m.send("graph_request", map[string]any{"action": "search", "query": m.graphInput.Value()})
			m.graphSearching = false
			m.graphInput.Blur()
			m.graphSelected = 0
			return nil
		}
		var cmd tea.Cmd
		m.graphInput, cmd = m.graphInput.Update(msg)
		return cmd
	}
	nodes := m.visibleGraphNodes()
	switch key {
	case "esc":
		m.screen = screenChat
	case "up", "k", "left", "h":
		m.graphSelected = maxInt(0, m.graphSelected-1)
	case "down", "j", "right", "l":
		m.graphSelected = minInt(maxInt(0, len(nodes)-1), m.graphSelected+1)
	case "+", "=":
		m.graphZoom = minInt(5, m.graphZoom+1)
	case "-":
		m.graphZoom = maxInt(-1, m.graphZoom-1)
	case "/":
		m.graphSearching = true
		m.graphInput.Reset()
		return m.graphInput.Focus()
	case "tab":
		if m.graph.communities <= 0 || m.graphCommunity >= m.graph.communities-1 {
			m.graphCommunity = -1
		} else {
			m.graphCommunity++
		}
		m.graphSelected = 0
	case "enter":
		if len(nodes) > 0 {
			m.send("graph_request", map[string]any{"action": "neighbors", "node_id": nodes[m.graphSelected].id})
			m.graphSelected = 0
		}
	case "p":
		if len(nodes) > 0 {
			if m.graphPathStart == "" {
				m.graphPathStart = nodes[m.graphSelected].id
			} else {
				m.send("graph_request", map[string]any{"action": "path", "left": m.graphPathStart, "right": nodes[m.graphSelected].id})
				m.graphPathStart = ""
			}
		}
	case "r":
		m.send("graph_request", map[string]any{"action": "refresh"})
	}
	return nil
}

func (m *model) questionKey(key string) tea.Cmd {
	if m.pendingQuestion == nil || len(m.pendingQuestion.questions) == 0 {
		m.screen = screenChat
		return nil
	}
	question := m.pendingQuestion.questions[minInt(m.questionIdx, len(m.pendingQuestion.questions)-1)]
	optionCount := len(question.choices) + 2 // Other and Skip are client-owned choices.
	if key == "up" || key == "k" {
		m.choiceIdx = (m.choiceIdx - 1 + optionCount) % optionCount
		return nil
	}
	if key == "down" || key == "j" {
		m.choiceIdx = (m.choiceIdx + 1) % optionCount
		return nil
	}
	if key == "esc" {
		m.send("question_response", map[string]any{"question_response": map[string]any{"request_id": m.pendingQuestion.requestID, "answers": map[string]any{}, "action": "cancel"}})
		m.screen = screenChat
		return nil
	}
	if key != "enter" {
		return nil
	}
	if m.choiceIdx == len(question.choices)+1 {
		m.send("question_response", map[string]any{"question_response": map[string]any{"request_id": m.pendingQuestion.requestID, "answers": map[string]any{}, "action": "skip"}})
		m.screen = screenChat
		return nil
	}
	if m.choiceIdx == len(question.choices) {
		m.screen = screenChat
		parts := make([]string, 0, len(m.questionAnswers)+1)
		for _, item := range m.pendingQuestion.questions {
			if answer := m.questionAnswers[item.id]; answer != nil {
				parts = append(parts, item.id+": "+fmt.Sprint(answer))
			}
		}
		parts = append(parts, question.id+": ")
		m.input.SetValue(strings.Join(parts, "; "))
		m.input.Placeholder = "Type your own clarification…"
		m.input.Focus()
		return nil
	}
	m.questionAnswers[question.id] = question.choices[m.choiceIdx].id
	if m.questionIdx+1 < len(m.pendingQuestion.questions) {
		m.questionIdx++
		m.choiceIdx = 0
		return nil
	}
	m.send("question_response", map[string]any{"question_response": map[string]any{"request_id": m.pendingQuestion.requestID, "answers": m.questionAnswers}})
	m.screen = screenChat
	return nil
}

func (m *model) submit() tea.Cmd {
	text := strings.TrimSpace(m.input.Value())
	if text == "" || (m.busy && !strings.EqualFold(text, "/agents")) {
		return nil
	}
	if strings.HasPrefix(strings.TrimLeftFunc(m.input.Value(), unicode.IsSpace), "/") {
		if strings.EqualFold(text, "/settings") {
			m.screen = screenSettings
			m.settingsIdx = 0
			m.input.Reset()
			m.palette = nil
			m.input.SetHeight(m.composerHeight())
			return nil
		}
		m.send("command", map[string]any{"name": text})
		if strings.EqualFold(text, "/quit") || strings.EqualFold(text, "/exit") {
			return tea.Quit
		}
		m.input.Reset()
		m.palette = nil
		m.input.SetHeight(m.composerHeight())
		return nil
	}
	m.messages = append(m.messages, chatMessage{role: "user", text: text})
	m.messages = append(m.messages, chatMessage{role: "assistant", streaming: true})
	m.busy = true
	m.status = "Thinking…"
	m.thinkingText = ""
	m.cursorVisible = true
	m.followTail = true
	m.input.Reset()
	m.palette = nil
	m.input.SetHeight(m.composerHeight())
	m.send("submit", map[string]any{"text": text})
	return nil
}

func (m *model) refreshPalette() {
	wasOpen := len(m.palette) > 0
	m.palette = commandMatches(m.input.Value())
	if len(m.palette) == 0 {
		m.paletteIndex = 0
	} else if m.paletteIndex >= len(m.palette) {
		m.paletteIndex = len(m.palette) - 1
	}
	if !wasOpen && len(m.palette) > 0 && !m.reducedMotion {
		m.transitionTick = 4
	}
	m.input.SetHeight(m.composerHeight())
}

func (m *model) providerKey(key string) tea.Cmd {
	if key == "esc" {
		if m.onboardingKind != "" {
			m.screen = screenOnboarding
			m.onboardingWaiting = false
		} else {
			m.screen = screenChat
		}
		return nil
	}
	if key == "up" || key == "k" {
		m.providerIdx = (m.providerIdx - 1 + len(m.providerList)) % len(m.providerList)
		return nil
	}
	if key == "down" || key == "j" {
		m.providerIdx = (m.providerIdx + 1) % len(m.providerList)
		return nil
	}
	if key == "enter" && len(m.providerList) > 0 {
		m.send("command", map[string]any{"name": "provider", "args": map[string]any{"provider": m.providerList[m.providerIdx]}})
		if m.onboardingKind != "" {
			m.onboardingWaiting = true
		} else {
			m.screen = screenChat
		}
	}
	return nil
}

func (m *model) featureKey(key string) tea.Cmd {
	if key == "esc" {
		if m.onboardingKind == "" {
			m.screen = screenChat
		}
		return nil
	}
	if key == "l" || key == "L" {
		m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"mode": "local", "onboarding": m.onboardingSelfLearning}})
		return nil
	}
	if key == "r" || key == "R" {
		m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"mode": "remote", "onboarding": m.onboardingSelfLearning}})
		return nil
	}
	if m.onboardingSelfLearning {
		switch key {
		case "up", "k":
			m.featureIdx = 0
		case "down", "j":
			m.featureIdx = 1
		case "enter":
			mode := "local"
			if m.featureIdx == 1 {
				mode = "remote"
			}
			m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"mode": mode, "onboarding": true}})
		}
		return nil
	}
	if len(m.features) == 0 {
		return nil
	}
	if key == "up" || key == "k" {
		m.featureIdx = (m.featureIdx - 1 + len(m.features)) % len(m.features)
	} else if key == "down" || key == "j" {
		m.featureIdx = (m.featureIdx + 1) % len(m.features)
	} else if key == "space" || key == "enter" {
		item := m.features[m.featureIdx]
		item.enabled = !item.enabled
		m.features[m.featureIdx] = item
		m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"feature": item.name, "enabled": item.enabled}})
	}
	return nil
}

func (m *model) settingsKey(key string) tea.Cmd {
	const settingCount = 5
	if key == "esc" {
		m.screen = screenChat
		return nil
	}
	switch key {
	case "up", "k":
		m.settingsIdx = (m.settingsIdx - 1 + settingCount) % settingCount
	case "down", "j":
		m.settingsIdx = (m.settingsIdx + 1) % settingCount
	case "left", "h":
		return m.changeSetting(-1)
	case "right", "l", "space", "enter":
		return m.changeSetting(1)
	}
	return nil
}

func cycleSetting(current string, values []string, delta int) string {
	index := 0
	for candidate, value := range values {
		if value == current {
			index = candidate
			break
		}
	}
	index = (index + delta + len(values)) % len(values)
	return values[index]
}

func (m *model) changeSetting(delta int) tea.Cmd {
	switch m.settingsIdx {
	case 0:
		m.showToolDetails = !m.showToolDetails
		settings := loadUISettings()
		settings.ShowToolDetails = m.showToolDetails
		if err := saveUISettings(settings); err != nil {
			m.setError("Could not save UI settings: " + err.Error())
		}
	case 1:
		mode := cycleSetting(firstNonEmpty(m.interactionMode, "auto"), []string{"auto", "ask", "plan", "agent"}, delta)
		m.send("command", map[string]any{"name": "mode", "args": mode})
	case 2:
		backend := cycleSetting(firstNonEmpty(m.fastBackend, "off"), []string{"off", "jev", "kev"}, delta)
		m.send("command", map[string]any{"name": "system-one", "args": map[string]any{"backend": backend}})
	case 3:
		backend := cycleSetting(firstNonEmpty(m.decisionAssistBackend, "off"), []string{"off", "jev", "kev"}, delta)
		if backend == "kev" && !m.decisionAssistConsent {
			m.settingsIdx = 4
			return nil
		}
		m.send("command", map[string]any{"name": "decision-assist", "args": map[string]any{
			"backend": backend, "private_consent": m.decisionAssistConsent,
		}})
	case 4:
		if m.decisionAssistBackend == "kev" && m.decisionAssistConsent {
			m.send("command", map[string]any{"name": "decision-assist", "args": map[string]any{"backend": "revoke"}})
			return nil
		}
		m.send("command", map[string]any{"name": "decision-assist", "args": map[string]any{
			"backend": "kev", "private_consent": true,
		}})
	}
	return nil
}
