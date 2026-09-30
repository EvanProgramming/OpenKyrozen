package main

import (
	"charm.land/bubbles/v2/textarea"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"fmt"
	"os"
	"time"
)

func initialModel(project string, global bool) model {
	reducedMotion := os.Getenv("KYROZEN_REDUCED_MOTION") == "1"
	settings := loadUISettings()
	onboardingKind, onboardingPreviousVersion := onboardingStatus(settings)
	input := textarea.New()
	input.Placeholder = "Ask Kyrozen anything…"
	input.Prompt = "› "
	input.CharLimit = 12_000
	input.ShowLineNumbers = false
	input.SetHeight(3)
	input.SetWidth(60)
	input.Focus()
	inputStyles := input.Styles()
	inputStyles.Focused.Text = bodyStyle
	inputStyles.Focused.Placeholder = mutedStyle
	inputStyles.Focused.Prompt = brandStyle
	inputStyles.Focused.CursorLine = lipgloss.NewStyle().Background(lipgloss.Color(deep))
	inputStyles.Cursor.Color = lipgloss.Color(cyan)
	inputStyles.Cursor.Blink = !reducedMotion
	input.SetStyles(inputStyles)
	apiInput := textinput.New()
	apiInput.Placeholder = "Paste an API key"
	apiInput.CharLimit = 512
	apiInput.EchoMode = textinput.EchoPassword
	apiInput.EchoCharacter = '•'
	apiStyles := apiInput.Styles()
	apiStyles.Focused.Text = bodyStyle
	apiStyles.Focused.Placeholder = mutedStyle
	apiStyles.Focused.Prompt = brandStyle
	apiStyles.Cursor.Color = lipgloss.Color(cyan)
	apiStyles.Cursor.Blink = !reducedMotion
	apiInput.SetStyles(apiStyles)
	graphInput := textinput.New()
	graphInput.Placeholder = "Search nodes"
	graphInput.CharLimit = 200
	graphInput.SetStyles(apiStyles)
	projectInput := textinput.New()
	projectInput.Placeholder = "Project directory"
	projectInput.CharLimit = 4096
	projectInput.SetStyles(apiStyles)
	transcript := viewport.New()
	transcript.MouseWheelEnabled = true
	transcript.MouseWheelDelta = mouseWheelScrollStep
	return model{
		bridge:                    newBridge(),
		project:                   project,
		global:                    global,
		view:                      transcript,
		input:                     input,
		apiInput:                  apiInput,
		graphInput:                graphInput,
		projectInput:              projectInput,
		screen:                    screenSplash,
		status:                    "Starting the workspace…",
		splashStarted:             time.Now(),
		cursorVisible:             true,
		followTail:                true,
		reducedMotion:             reducedMotion,
		showToolDetails:           settings.ShowToolDetails,
		onboardingKind:            onboardingKind,
		onboardingPreviousVersion: onboardingPreviousVersion,
		interactionMode:           "auto",
		fastBackend:               "off",
		settingsIdx:               0,
		effectiveMode:             "ask",
		questionAnswers:           make(map[string]any),
		graphCommunity:            -1,
	}
}

func (m model) Init() tea.Cmd {
	cmds := []tea.Cmd{startBackend(m.bridge)}
	if !m.reducedMotion {
		cmds = append(cmds, motionTick())
	}
	return tea.Batch(cmds...)
}

func startBackend(b *bridge) tea.Cmd {
	return func() tea.Msg { return backendStartedMsg{err: b.start()} }
}

func motionTick() tea.Cmd {
	return tea.Tick(100*time.Millisecond, func(now time.Time) tea.Msg { return tickMsg(now) })
}

func waitBackend(b *bridge) tea.Cmd {
	return func() tea.Msg {
		line, ok := <-b.events
		if !ok {
			return backendExitMsg{}
		}
		lines := []backendLineMsg{line}
		if isUpdateStatus(line) {
			return backendEventsMsg{lines: lines}
		}
		for {
			select {
			case line, ok = <-b.events:
				if !ok {
					return backendEventsMsg{lines: lines}
				}
				lines = append(lines, line)
				if isUpdateStatus(line) {
					return backendEventsMsg{lines: lines}
				}
			default:
				return backendEventsMsg{lines: lines}
			}
		}
	}
}

func isUpdateStatus(line backendLineMsg) bool {
	return line.err == nil && stringValue(line.event, "event") == "status" && stringValue(line.event, "state") == "updating"
}

func (m *model) send(command string, payload map[string]any) {
	if payload == nil {
		payload = map[string]any{}
	}
	payload["command"] = command
	if _, ok := payload["request_id"]; !ok {
		m.requestCount++
		payload["request_id"] = fmt.Sprintf("tui-%d", m.requestCount)
	}
	if err := m.bridge.send(payload); err != nil {
		m.setError(err.Error())
	}
}

func (m *model) setError(message string) {
	m.errorText = message
	m.status = "Error"
	m.screen = screenError
	m.busy = false
	m.thinkingText = ""
	m.updateInProgress = false
	if !m.reducedMotion {
		m.transitionTick = 4
	}
}
