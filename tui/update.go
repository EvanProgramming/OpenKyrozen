package main

import (
	tea "charm.land/bubbletea/v2"
	"strings"
	"time"
)

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.resize()
	case backendStartedMsg:
		if msg.err != nil {
			m.setError("Could not start the Python backend: " + msg.err.Error())
		} else {
			payload := map[string]any{"project": m.project, "global": m.global}
			if m.onboardingKind != "" {
				payload["onboarding"] = m.onboardingKind
				payload["onboarding_previous_version"] = m.onboardingPreviousVersion
			}
			m.send("start", payload)
			cmds = append(cmds, waitBackend(m.bridge))
		}
	case backendEventsMsg:
		for _, line := range msg.lines {
			m.reduceBackendLine(line)
			if m.restart {
				m.bridge.stop()
				return m, tea.Quit
			}
		}
		cmds = append(cmds, waitBackend(m.bridge))
		m.maybeMotion(&cmds)
	case backendLineMsg:
		m.reduceBackendLine(msg)
		if m.restart {
			m.bridge.stop()
			return m, tea.Quit
		}
		cmds = append(cmds, waitBackend(m.bridge))
		m.maybeMotion(&cmds)
	case tea.MouseWheelMsg:
		// Capture wheel input while the TUI is active. Without mouse reporting,
		// Warp and native terminals scroll their own screen instead of this
		// viewport, which makes the full-screen app appear to disappear.
		if m.screen == screenGraph {
			if msg.Button == tea.MouseWheelUp {
				m.graphZoom = minInt(5, m.graphZoom+1)
			} else {
				m.graphZoom = maxInt(-1, m.graphZoom-1)
			}
		} else if m.screen == screenPlan {
			if msg.Button == tea.MouseWheelUp {
				m.scrollPlan(-3)
			} else {
				m.scrollPlan(3)
			}
		} else if m.screen == screenChat {
			switch msg.Button {
			case tea.MouseWheelUp:
				m.view.ScrollUp(mouseWheelScrollStep)
			case tea.MouseWheelDown:
				m.view.ScrollDown(mouseWheelScrollStep)
			default:
				m.view, _ = m.view.Update(msg)
			}
			m.followTail = m.view.AtBottom()
		}
	case tea.MouseClickMsg:
		if m.screen == screenChat && !m.busy && msg.Button == tea.MouseLeft {
			if index := m.modeTabAt(msg.X, msg.Y); index >= 0 {
				m.send("command", map[string]any{"name": "mode", "args": []string{"auto", "ask", "plan", "agent"}[index]})
			} else if index := m.permissionTabAt(msg.X, msg.Y); index >= 0 {
				m.send("command", map[string]any{"name": "permissions", "args": []string{"ask", "full_jev", "full"}[index]})
			}
		} else if m.screen == screenGraph && msg.Button == tea.MouseLeft {
			nodes := m.visibleGraphNodes()
			miniHeight := minInt(14, maxInt(8, maxInt(1, m.height-2)/3))
			firstNodeRow := miniHeight + 5
			if m.graphSearching {
				firstNodeRow += 2
			}
			index := msg.Y - firstNodeRow
			if index >= 0 && index < len(nodes) {
				m.graphSelected = index
			}
		}
	case githubAuthDoneMsg:
		m.screen = screenChat
		if msg.err != nil {
			m.messages = append(m.messages, chatMessage{role: "assistant", text: "GitHub authentication was not completed: " + msg.err.Error()})
		}
		m.send("command", map[string]any{"name": "/github status"})
	case backendExitMsg:
		if m.screen != screenError {
			m.status = "Backend stopped"
		}
	case tickMsg:
		now := time.Time(msg)
		m.motionFrame++
		if m.screen == screenSplash {
			m.splashFrame++
			if m.reducedMotion || m.canLeaveSplash(now) {
				m.screen = screenChat
			}
		}
		if m.busy {
			m.cursorVisible = !m.cursorVisible
		}
		if m.taskFlashTick > 0 {
			m.taskFlashTick--
			if m.taskFlashTick == 0 {
				m.taskFlashID = ""
			}
		}
		if m.transitionTick > 0 {
			m.transitionTick--
		}
		m.maybeMotion(&cmds)
	case tea.KeyPressMsg:
		cmd, quit := m.handleKey(msg)
		if quit {
			m.bridge.stop()
			return m, tea.Quit
		}
		if cmd != nil {
			cmds = append(cmds, cmd)
		}
	case tea.PasteMsg:
		if m.updateInProgress {
			break
		}
		var cmd tea.Cmd
		switch m.screen {
		case screenAPIKey:
			m.apiInput, cmd = m.apiInput.Update(msg)
		case screenModel:
			m.apiInput, cmd = m.apiInput.Update(msg)
		case screenGraph:
			if m.graphSearching {
				m.graphInput, cmd = m.graphInput.Update(msg)
			}
		case screenProject:
			m.projectInput, cmd = m.projectInput.Update(msg)
		case screenChat:
			m.input, cmd = m.input.Update(msg)
			m.refreshPalette()
		}
		if cmd != nil {
			cmds = append(cmds, cmd)
		}
	}
	m.syncViewport()
	return m, tea.Batch(cmds...)
}

func (m model) canLeaveSplash(now time.Time) bool {
	if !m.backendReady {
		return false
	}
	started := m.splashStarted
	ready := m.readyAt
	return (started.IsZero() || now.Sub(started) >= splashMinimumDuration) &&
		(ready.IsZero() || now.Sub(ready) >= readyHoldDuration)
}

func (m model) animating() bool {
	return !m.reducedMotion && (m.screen == screenSplash || (m.screen == screenChat && len(m.messages) == 0) || m.updateInProgress || m.busy || m.hasRunningTask() || m.taskFlashTick > 0 || m.transitionTick > 0)
}

func (m *model) maybeMotion(cmds *[]tea.Cmd) {
	if m.animating() {
		*cmds = append(*cmds, motionTick())
	}
}

func (m model) hasRunningTask() bool {
	for _, task := range m.tasks {
		if strings.EqualFold(task.status, "running") || strings.EqualFold(task.status, "active") || strings.EqualFold(task.status, "in_progress") {
			return true
		}
	}
	return false
}
