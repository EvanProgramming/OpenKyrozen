package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
	"unicode"

	"charm.land/bubbles/v2/textarea"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

var version = "2.0.4"

type screen string

const (
	screenSplash       screen = "splash"
	screenChat         screen = "chat"
	screenProvider     screen = "provider"
	screenAPIKey       screen = "api_key"
	screenApproval     screen = "approval"
	screenSelfLearning screen = "self_learning"
	screenMode         screen = "mode"
	screenQuestion     screen = "question"
	screenPlan         screen = "plan"
	screenGraph        screen = "graph"
	screenGithubAuth   screen = "github_auth"
	screenSettings     screen = "settings"
	screenUpdating     screen = "updating"
	screenError        screen = "error"
)

type chatMessage struct {
	role          string
	text          string
	status        string
	toolAction    string
	toolDetail    string
	streaming     bool
	rendered      string
	renderedWidth int
}

type taskItem struct {
	id          string
	description string
	status      string
}

type featureItem struct {
	name        string
	enabled     bool
	description string
}

type interactionChoice struct{ id, label, description string }
type interactionQuestion struct {
	id, header, prompt string
	choices            []interactionChoice
}
type questionRequest struct {
	requestID string
	questions []interactionQuestion
}
type planStep struct {
	id, title, description string
	acceptance             []string
}
type planProposal struct {
	planID, title, summary string
	version                int
	steps                  []planStep
}

type model struct {
	bridge                *bridge
	project               string
	global                bool
	width                 int
	height                int
	view                  viewport.Model
	input                 textarea.Model
	apiInput              textinput.Model
	graphInput            textinput.Model
	screen                screen
	status                string
	provider              string
	modelName             string
	workspace             string
	messages              []chatMessage
	tasks                 []taskItem
	palette               []command
	paletteIndex          int
	providerList          []string
	providerIdx           int
	features              []featureItem
	featureIdx            int
	learningMode          string
	learningStatus        string
	learningModel         string
	learningDetail        string
	learningCostSource    string
	usageScope            string
	usageAttempts         int
	usagePromptTokens     int
	usageCompletionTokens int
	usageReasoningTokens  int
	usageCostPicos        int
	interactionMode       string
	effectiveMode         string
	modeIdx               int
	pendingQuestion       *questionRequest
	questionIdx           int
	choiceIdx             int
	questionAnswers       map[string]any
	pendingPlan           *planProposal
	planScroll            int
	graph                 graphSnapshot
	graphSelected         int
	graphZoom             int
	graphCommunity        int
	graphSearching        bool
	graphPathStart        string
	githubBinary          string
	githubHostname        string
	approvalID            string
	approvalTool          string
	approvalArgs          string
	errorText             string
	splashFrame           int
	splashStarted         time.Time
	readyAt               time.Time
	backendReady          bool
	motionFrame           int
	cursorVisible         bool
	thinkingText          string
	taskFlashID           string
	taskFlashTick         int
	followTail            bool
	transitionTick        int
	reducedMotion         bool
	showToolDetails       bool
	updateInProgress      bool
	busy                  bool
	requestCount          int
	restart               bool
}

type tickMsg time.Time
type backendStartedMsg struct{ err error }
type githubAuthDoneMsg struct{ err error }
type backendEventsMsg struct{ lines []backendLineMsg }

const (
	splashMinimumDuration = 2400 * time.Millisecond
	readyHoldDuration     = 300 * time.Millisecond
	mouseWheelScrollStep  = 5
)

var uiSensitiveArgRE = regexp.MustCompile(`(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[^\s,;]+`)

type uiSettings struct {
	ShowToolDetails bool `json:"show_tool_details"`
}

func uiSettingsPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".kyrozen", "ui_settings.json")
}

func loadUISettings() uiSettings {
	path := uiSettingsPath()
	if path == "" {
		return uiSettings{}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return uiSettings{}
	}
	var settings uiSettings
	if json.Unmarshal(data, &settings) != nil {
		return uiSettings{}
	}
	return settings
}

func saveUISettings(settings uiSettings) error {
	path := uiSettingsPath()
	if path == "" {
		return fmt.Errorf("could not determine the user settings path")
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(settings)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(directory, ".ui_settings-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryName, path)
}

func initialModel(project string, global bool) model {
	reducedMotion := os.Getenv("KYROZEN_REDUCED_MOTION") == "1"
	settings := loadUISettings()
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
	transcript := viewport.New()
	transcript.MouseWheelEnabled = true
	transcript.MouseWheelDelta = mouseWheelScrollStep
	return model{
		bridge:          newBridge(),
		project:         project,
		global:          global,
		view:            transcript,
		input:           input,
		apiInput:        apiInput,
		graphInput:      graphInput,
		screen:          screenSplash,
		status:          "Starting the workspace…",
		splashStarted:   time.Now(),
		cursorVisible:   true,
		followTail:      true,
		reducedMotion:   reducedMotion,
		showToolDetails: settings.ShowToolDetails,
		interactionMode: "auto",
		effectiveMode:   "ask",
		questionAnswers: make(map[string]any),
		graphCommunity:  -1,
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
			m.send("start", map[string]any{"project": m.project, "global": m.global})
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
		if m.screen == screenGraph && msg.Button == tea.MouseLeft {
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
	return !m.reducedMotion && (m.screen == screenSplash || m.updateInProgress || m.busy || m.hasRunningTask() || m.taskFlashTick > 0 || m.transitionTick > 0)
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

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Cmd, bool) {
	key := msg.String()
	if key == "ctrl+c" {
		return nil, true
	}
	if m.updateInProgress {
		return nil, false
	}
	if m.screen == screenError {
		if key == "esc" || key == "enter" {
			m.screen = screenChat
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
		if key == "esc" {
			m.screen = screenChat
			return nil, false
		}
		if key == "space" || key == "enter" {
			m.showToolDetails = !m.showToolDetails
			if err := saveUISettings(uiSettings{ShowToolDetails: m.showToolDetails}); err != nil {
				m.setError("Could not save UI settings: " + err.Error())
			}
		}
		return nil, false
	}
	if m.screen == screenAPIKey {
		if key == "esc" {
			m.apiInput.Reset()
			m.screen = screenChat
			return nil, false
		}
		if key == "enter" {
			m.send("command", map[string]any{"name": "api_key", "args": map[string]any{"api_key": m.apiInput.Value()}})
			m.apiInput.Reset()
			m.screen = screenChat
			return nil, false
		}
		var cmd tea.Cmd
		m.apiInput, cmd = m.apiInput.Update(msg)
		return cmd, false
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
	if text == "" || m.busy {
		return nil
	}
	if strings.HasPrefix(strings.TrimLeftFunc(m.input.Value(), unicode.IsSpace), "/") {
		if strings.EqualFold(text, "/settings") {
			m.screen = screenSettings
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
		m.screen = screenChat
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
		m.screen = screenChat
	}
	return nil
}

func (m *model) featureKey(key string) tea.Cmd {
	if key == "esc" {
		m.screen = screenChat
		return nil
	}
	if key == "l" || key == "L" {
		m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"mode": "local"}})
		return nil
	}
	if key == "r" || key == "R" {
		m.send("command", map[string]any{"name": "self_learning", "args": map[string]any{"mode": "remote"}})
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

func stringValue(event map[string]any, key string) string {
	value, _ := event[key].(string)
	return value
}

func (m *model) handleBackendEvent(event backendEvent) {
	switch stringValue(event, "event") {
	case "status":
		state := stringValue(event, "state")
		m.status = firstNonEmpty(stringValue(event, "message"), state)
		if state == "updating" {
			m.updateInProgress = true
			m.screen = screenUpdating
			m.busy = false
			m.thinkingText = ""
		} else if m.updateInProgress && (state == "ready" || state == "error") {
			m.updateInProgress = false
			if m.screen == screenUpdating {
				m.screen = screenChat
			}
		}
		if state == "thinking" || state == "starting" {
			m.busy = state == "thinking"
		}
	case "ready":
		m.provider = stringValue(event, "provider")
		m.modelName = stringValue(event, "model")
		m.workspace = stringValue(event, "workspace")
		m.status = "Ready"
		m.busy = false
		m.backendReady = true
		m.readyAt = time.Now()
		m.thinkingText = ""
		if m.screen == screenSplash && (m.reducedMotion || m.canLeaveSplash(time.Now())) {
			m.screen = screenChat
		}
	case "usage":
		m.usageScope = firstNonEmpty(stringValue(event, "scope"), "workspace")
		m.usageAttempts = numberValue(event["attempts"])
		m.usagePromptTokens = numberValue(event["prompt_tokens"])
		m.usageCompletionTokens = numberValue(event["completion_tokens"])
		m.usageReasoningTokens = numberValue(event["reasoning_tokens"])
		m.usageCostPicos = numberValue(event["cost_picos"])
	case "stream_delta":
		m.busy, m.status = true, "Generating…"
		m.thinkingText = ""
		m.cursorVisible = true
		text := stringValue(event, "text")
		for i := len(m.messages) - 1; i >= 0; i-- {
			if m.messages[i].role == "assistant" && m.messages[i].streaming {
				m.messages[i].text += text
				return
			}
		}
		m.messages = append(m.messages, chatMessage{role: "assistant", text: text, streaming: true})
	case "thinking":
		m.thinkingText = firstNonEmpty(stringValue(event, "text"), "Working…")
		m.busy = true
		m.status = "Thinking…"
	case "response":
		text := stringValue(event, "text")
		if m.updateInProgress {
			m.updateInProgress = false
			m.errorText = firstNonEmpty(text, "The update did not complete.")
			m.status = "Update failed"
			m.screen = screenError
			m.busy = false
			m.thinkingText = ""
			return
		}
		m.input.Placeholder = "Ask Kyrozen anything…"
		for i := len(m.messages) - 1; i >= 0; i-- {
			if m.messages[i].role == "assistant" && m.messages[i].streaming {
				m.messages[i].text, m.messages[i].streaming = text, false
				m.messages[i].rendered = ""
				m.messages[i].renderedWidth = 0
				m.busy = false
				m.thinkingText = ""
				return
			}
		}
		m.messages = append(m.messages, chatMessage{role: "assistant", text: text})
		m.busy = false
		m.thinkingText = ""
	case "restart":
		m.status = "Restarting…"
		m.busy = false
		m.thinkingText = ""
		m.restart = true
	case "tool_receipt":
		if receipt, ok := event["receipt"].(map[string]any); ok {
			m.messages = append(m.messages, chatMessage{
				role: "receipt", status: receiptStatus(receipt),
				text: stringValue(receipt, "action"), toolAction: stringValue(receipt, "action"),
				toolDetail: safeToolDetail(stringValue(receipt, "result")),
			})
		}
	case "tasks":
		previous := make(map[string]string, len(m.tasks))
		for _, task := range m.tasks {
			previous[task.id] = task.status
		}
		m.tasks = parseTasks(event["tasks"])
		if !m.reducedMotion {
			for _, task := range m.tasks {
				if previous[task.id] == "running" && task.status == "succeeded" {
					m.taskFlashID, m.taskFlashTick = task.id, 8
					break
				}
			}
		}
	case "interaction":
		if value, ok := event["interaction"].(map[string]any); ok {
			m.applyInteraction(value)
		}
	case "graph_state":
		m.graph = parseGraph(event["graph"])
		m.graphSelected = minInt(m.graphSelected, maxInt(0, len(m.graph.miniNodes)-1))
	case "github_state":
		if value, ok := event["github"].(map[string]any); ok {
			m.messages = append(m.messages, chatMessage{role: "assistant", text: firstNonEmpty(stringValue(value, "message"), "GitHub CLI status unavailable.")})
		}
	case "prompt":
		m.handlePrompt(event)
	case "error":
		m.setError(firstNonEmpty(stringValue(event, "error"), "Backend error"))
	case "exit":
		m.status, m.busy = "Stopped", false
		m.thinkingText = ""
	}
}

func (m *model) reduceBackendLine(line backendLineMsg) {
	if line.err != nil {
		m.setError(line.err.Error())
		return
	}
	m.handleBackendEvent(line.event)
}

func (m *model) applyInteraction(value map[string]any) {
	m.interactionMode = firstNonEmpty(stringValue(value, "preference_mode"), "auto")
	m.effectiveMode = firstNonEmpty(stringValue(value, "effective_mode"), m.interactionMode)
	if raw, ok := value["pending_question"].(map[string]any); ok {
		request := &questionRequest{requestID: stringValue(raw, "request_id")}
		if questions, ok := raw["questions"].([]any); ok {
			for _, value := range questions {
				item, ok := value.(map[string]any)
				if !ok {
					continue
				}
				question := interactionQuestion{id: stringValue(item, "id"), header: stringValue(item, "header"), prompt: stringValue(item, "prompt")}
				if choices, ok := item["choices"].([]any); ok {
					for _, value := range choices {
						choice, ok := value.(map[string]any)
						if !ok {
							continue
						}
						question.choices = append(question.choices, interactionChoice{id: stringValue(choice, "id"), label: stringValue(choice, "label"), description: stringValue(choice, "description")})
					}
				}
				request.questions = append(request.questions, question)
			}
		}
		m.pendingQuestion, m.questionIdx, m.choiceIdx = request, 0, 0
		m.questionAnswers = make(map[string]any)
		if len(request.questions) > 0 {
			m.screen = screenQuestion
			m.startTransition()
		}
	} else {
		m.pendingQuestion = nil
		if m.screen == screenQuestion {
			m.screen = screenChat
		}
	}
	if raw, ok := value["pending_plan"].(map[string]any); ok {
		plan := &planProposal{planID: stringValue(raw, "plan_id"), title: stringValue(raw, "title"), summary: stringValue(raw, "summary")}
		if version, ok := raw["version"].(float64); ok {
			plan.version = int(version)
		}
		if version, ok := raw["version"].(int); ok {
			plan.version = version
		}
		if steps, ok := raw["steps"].([]any); ok {
			for _, value := range steps {
				item, ok := value.(map[string]any)
				if !ok {
					continue
				}
				step := planStep{id: stringValue(item, "id"), title: stringValue(item, "title"), description: stringValue(item, "description")}
				if acceptance, ok := item["acceptance"].([]any); ok {
					for _, criterion := range acceptance {
						step.acceptance = append(step.acceptance, fmt.Sprint(criterion))
					}
				}
				plan.steps = append(plan.steps, step)
			}
		}
		if m.pendingPlan == nil || m.pendingPlan.planID != plan.planID || m.pendingPlan.version != plan.version {
			m.planScroll = 0
		}
		m.pendingPlan = plan
		if m.pendingQuestion == nil {
			m.screen = screenPlan
			m.startTransition()
		}
	} else {
		m.pendingPlan = nil
		m.planScroll = 0
		if m.screen == screenPlan {
			m.screen = screenChat
		}
	}
}

func parseTasks(value any) []taskItem {
	items, _ := value.([]any)
	tasks := make([]taskItem, 0, len(items))
	for _, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		tasks = append(tasks, taskItem{id: stringValue(item, "id"), description: stringValue(item, "description"), status: stringValue(item, "status")})
	}
	return tasks
}

func (m *model) handlePrompt(event backendEvent) {
	switch stringValue(event, "kind") {
	case "api_key":
		m.screen = screenAPIKey
		m.startTransition()
		m.apiInput.Reset()
		m.apiInput.Placeholder = firstNonEmpty(stringValue(event, "message"), "Enter API key")
		m.apiInput.Focus()
	case "provider":
		m.providerList = nil
		if providers, ok := event["providers"].([]any); ok {
			for _, raw := range providers {
				if item, ok := raw.(map[string]any); ok {
					m.providerList = append(m.providerList, stringValue(item, "name"))
				}
			}
		}
		m.providerIdx, m.screen = 0, screenProvider
		m.startTransition()
	case "approval":
		m.approvalID = stringValue(event, "request_id")
		m.approvalTool = stringValue(event, "action")
		m.approvalArgs = safeApprovalArgs(stringValue(event, "args"))
		m.screen = screenApproval
		m.startTransition()
	case "self_learning":
		m.features = nil
		if features, ok := event["features"].([]any); ok {
			for _, raw := range features {
				if item, ok := raw.(map[string]any); ok {
					m.features = append(m.features, featureItem{name: stringValue(item, "name"), enabled: boolValue(item, "enabled"), description: stringValue(item, "description")})
				}
			}
		}
		if runtime, ok := event["runtime"].(map[string]any); ok {
			m.learningMode = stringValue(runtime, "mode")
			m.learningStatus = stringValue(runtime, "status")
			m.learningModel = stringValue(runtime, "model")
			m.learningDetail = stringValue(runtime, "detail")
		}
		m.learningCostSource = stringValue(event, "cost_source")
		m.featureIdx, m.screen = 0, screenSelfLearning
		m.startTransition()
	case "mode":
		modes := []string{"auto", "ask", "plan", "agent"}
		m.modeIdx = 0
		selected := stringValue(event, "selected")
		for index, mode := range modes {
			if mode == selected {
				m.modeIdx = index
			}
		}
		m.screen = screenMode
		m.startTransition()
	case "graph":
		m.screen = screenGraph
		m.startTransition()
	case "github_auth":
		m.githubBinary = stringValue(event, "binary")
		m.githubHostname = firstNonEmpty(stringValue(event, "hostname"), "github.com")
		m.screen = screenGithubAuth
		m.startTransition()
	}
}

func (m *model) startTransition() {
	if !m.reducedMotion {
		m.transitionTick = 4
	}
}

func safeApprovalArgs(value string) string {
	value = uiSensitiveArgRE.ReplaceAllString(value, "$1=<redacted>")
	return strings.TrimSpace(value)
}

func safeToolDetail(value string) string {
	return strings.TrimSpace(uiSensitiveArgRE.ReplaceAllString(value, "$1=<redacted>"))
}

func receiptStatus(receipt map[string]any) string {
	if success, ok := receipt["success"].(bool); ok && success {
		return "success"
	}
	failure := strings.ToLower(stringValue(receipt, "failure"))
	if strings.Contains(failure, "denied") || strings.Contains(failure, "blocked") {
		return "blocked"
	}
	return "failure"
}

func boolValue(values map[string]any, key string) bool {
	value, _ := values[key].(bool)
	return value
}

func (m *model) resize() {
	width := m.contentWidth()
	mainWidth, _ := m.layoutWidths()
	m.input.SetWidth(width)
	m.input.SetHeight(m.composerHeight())
	m.apiInput.SetWidth(maxInt(1, minInt(68, m.width-14)))
	m.view.SetWidth(mainWidth)
	m.view.SetHeight(m.historyHeight())
	m.planScroll = minInt(m.planScroll, m.planMaxScroll())
	m.syncViewport()
}

func (m model) contentWidth() int {
	return maxInt(1, m.width-4)
}

func (m model) layoutWidths() (int, int) {
	available := m.contentWidth()
	if m.width < 110 {
		return available, 0
	}
	railWidth := minInt(30, maxInt(24, available/4))
	return maxInt(1, available-railWidth-2), railWidth
}

func (m model) composerHeight() int {
	if m.height > 0 && m.height < 16 {
		return 1
	}
	return 3
}

func (m model) historyHeight() int {
	return maxInt(1, m.height-m.chatChromeHeight())
}

func formatUsageTokens(value int) string {
	if value <= 0 {
		return "0"
	}
	if value < 1000 {
		return fmt.Sprintf("%d", value)
	}
	if value < 1_000_000 {
		return fmt.Sprintf("%.1fK", float64(value)/1000)
	}
	return fmt.Sprintf("%.1fM", float64(value)/1_000_000)
}

func formatUsageCost(picos int) string {
	if picos <= 0 {
		return "$0.00"
	}
	dollars := float64(picos) / 1_000_000_000_000
	if dollars < 0.01 {
		return "<$0.01"
	}
	return fmt.Sprintf("$%.2f", dollars)
}

func (m model) usageSummary() string {
	tokens := m.usagePromptTokens + m.usageCompletionTokens + m.usageReasoningTokens
	if m.usageAttempts == 0 && tokens == 0 && m.usageCostPicos == 0 {
		return "No usage yet"
	}
	return fmt.Sprintf("%s tokens · %s · %d call(s)", formatUsageTokens(tokens), formatUsageCost(m.usageCostPicos), m.usageAttempts)
}

func (m model) chatChromeHeight() int {
	width := m.contentWidth()
	nonViewport := []string{m.chatHeader(), rule(width)}
	if progress := m.progressBlock(width); progress != "" {
		nonViewport = append(nonViewport, progress)
	}
	if len(m.palette) > 0 {
		nonViewport = append(nonViewport, m.paletteView(width))
	}
	nonViewport = append(nonViewport, m.composer(width), m.chatFooter(width))
	height := len(nonViewport)
	for _, block := range nonViewport {
		height += lipgloss.Height(block)
	}
	return height
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func (m *model) syncViewport() {
	if m.width < 1 || m.height < 1 {
		return
	}
	offset := m.view.YOffset()
	mainWidth, _ := m.layoutWidths()
	m.view.SetWidth(mainWidth)
	m.view.SetHeight(m.historyHeight())
	m.view.SetContent(m.history(mainWidth))
	if m.followTail {
		m.view.GotoBottom()
	} else {
		m.view.SetYOffset(offset)
	}
}

func (m *model) history(width int) string {
	if len(m.messages) == 0 {
		return softStyle.Render("No messages yet. Start with a question, or type / for commands.")
	}
	var lines []string
	for index := range m.messages {
		message := &m.messages[index]
		label, body := "KYROZEN", message.text
		labelStyle := brandStyle
		switch message.role {
		case "user":
			label, labelStyle = "YOU", titleStyle
		case "thinking":
			label, labelStyle = "THINKING", amberStyle
		case "receipt":
			status := strings.ToUpper(firstNonEmpty(message.status, "success"))
			action := firstNonEmpty(message.toolAction, message.text, "unknown tool")
			label, labelStyle = "TOOL · "+action+" · "+status, receiptStyle(message.status)
			body = ""
			if m.showToolDetails {
				body = message.toolDetail
			}
		}
		if message.role == "assistant" && body != "" && !message.streaming {
			renderWidth := maxInt(1, width-2)
			if message.renderedWidth != renderWidth {
				message.rendered = renderMarkdown(body, renderWidth)
				message.renderedWidth = renderWidth
			}
			body = message.rendered
		} else if body != "" {
			body = softStyle.Render(body)
		}
		if message.role == "assistant" && message.streaming {
			cursor := mutedStyle.Render("▌")
			if m.cursorVisible {
				cursor = brandStyle.Render("▌")
			}
			body += cursor
		}
		block := labelStyle.Render(label) + "\n" + body
		lines = append(lines, lipgloss.NewStyle().Width(maxInt(1, width-2)).Render(block))
	}
	return strings.Join(lines, "\n")
}

func (m model) View() tea.View {
	var content string
	if m.screen == screenSplash {
		content = m.splash()
	} else if m.screen == screenGraph {
		content = m.graphExplorer()
	} else if m.screen == screenUpdating {
		content = m.updatingView()
	} else {
		content = m.chatView()
		if m.screen != screenChat {
			content = m.modal(content)
		}
	}
	view := tea.NewView(fillBackground(content, m.width, m.height))
	view.AltScreen = true
	view.MouseMode = tea.MouseModeCellMotion
	view.BackgroundColor = lipgloss.Color(ink)
	view.ForegroundColor = lipgloss.Color(white)
	view.WindowTitle = "OpenKyrozen"
	return view
}

func (m model) updatingView() string {
	width := maxInt(1, m.width-8)
	lines := []string{
		brandStyle.Render("OPENKYROZEN"),
		"",
		titleStyle.Render("Updating OpenKyrozen"),
		"",
		amberStyle.Render(taskSpinner(m.motionFrame) + " " + firstNonEmpty(m.status, "Updating files and preparing the restart…")),
		"",
		softStyle.Render("Your workspace is temporarily locked while the update is installed."),
		softStyle.Render("OpenKyrozen will restart automatically when it is ready."),
		"",
		mutedStyle.Render("Ctrl+C  emergency quit"),
	}
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(
		lipgloss.NewStyle().Width(width).MaxWidth(width).Align(lipgloss.Center).Render(strings.Join(lines, "\n")),
	)
}

func (m model) splash() string {
	width := minInt(56, maxInt(28, m.width-4))
	rows := bannerRows(width)
	visible := len(rows)
	if !m.reducedMotion {
		visible = minInt(len(rows), maxInt(0, m.splashFrame-1))
	}
	logo := make([]string, 0, len(rows))
	for index, row := range rows {
		if index >= visible {
			row = strings.Repeat(" ", lipgloss.Width(row))
		}
		style := softStyle
		if index != 3 {
			style = brandStyle
		}
		logo = append(logo, style.Copy().Width(width).Align(lipgloss.Center).Render(row))
	}
	lines := append(logo, "")
	lines = append(lines, startupMilestones(m)...)
	lines = append(lines, "", splashStatus(m))
	content := strings.Join(lines, "\n")
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(content)
}

func (m model) chatView() string {
	contentWidth := m.contentWidth()
	mainWidth, panelWidth := m.layoutWidths()
	header := m.chatHeader()
	main := quietStyle.Copy().Width(mainWidth).MaxWidth(mainWidth).Height(m.historyHeight()).MaxHeight(m.historyHeight()).Render(m.view.View())
	if panelWidth > 0 {
		main = lipgloss.JoinHorizontal(lipgloss.Top, main, "  ", m.activityRail())
	}
	blocks := []string{header, rule(contentWidth), main}
	if progress := m.progressBlock(contentWidth); progress != "" {
		blocks = append(blocks, progress)
	}
	if len(m.palette) > 0 {
		blocks = append(blocks, m.paletteView(contentWidth))
	}
	blocks = append(blocks, m.composer(contentWidth))
	blocks = append(blocks, m.chatFooter(contentWidth))
	return strings.Join(blocks, "\n")
}

func (m model) chatHeader() string {
	width := m.contentWidth()
	compact := m.width < 80 || m.height < 16
	modelLabel := firstNonEmpty(m.provider, "provider pending") + " · " + firstNonEmpty(m.modelName, "model pending")
	mode := strings.ToUpper(firstNonEmpty(m.effectiveMode, m.interactionMode, "ask"))
	lines := []string{
		brandStyle.Render("OPENKYROZEN") + "  " + mutedStyle.Render(mode),
		softStyle.Render("MODEL  ") + softStyle.Render(compactText(modelLabel, width-8)),
		softStyle.Render("USAGE  ") + softStyle.Render(compactText(m.usageSummary(), width-8)),
	}
	if !compact && m.workspace != "" {
		lines = append(lines, mutedStyle.Render("WORKSPACE  "+compactText(m.workspace, width-11)))
	}
	_, panelWidth := m.layoutWidths()
	if !compact && panelWidth == 0 && m.height >= 18 {
		lines = append(lines, m.graphCompact())
	}
	return lipgloss.NewStyle().Width(width).MaxWidth(width).Render(strings.Join(lines, "\n"))
}

func (m model) chatFooter(width int) string {
	hints := "Enter send  ·  ↑↓ commands  ·  PgUp/PgDn scroll  ·  Ctrl+C quit"
	if width >= 80 && m.height >= 16 {
		hints = "Enter send  ·  Shift+Enter newline  ·  ↑↓ commands  ·  PgUp/PgDn scroll  ·  Ctrl+C quit"
	}
	status := mutedStyle.Render("○ " + firstNonEmpty(m.status, "Ready"))
	if m.busy {
		status = amberStyle.Render(taskSpinner(m.motionFrame) + " " + firstNonEmpty(m.status, "Working…"))
	}
	return lipgloss.NewStyle().Width(width).MaxWidth(width).Render(status + "  " + mutedStyle.Render(hints))
}

func (m model) paletteView(width int) string {
	if len(m.palette) == 0 {
		return ""
	}
	rowWidth := maxInt(1, width-2)
	visible := maxInt(1, minInt(len(m.palette), 5))
	start := 0
	if m.paletteIndex >= visible {
		start = m.paletteIndex - visible + 1
	}
	end := minInt(len(m.palette), start+visible)
	title := fmt.Sprintf("COMMANDS %d–%d OF %d", start+1, end, len(m.palette))
	if len(m.palette) > visible {
		title += " · ↑↓ MORE"
	}
	lines := []string{titleStyle.Render(title)}
	for index := start; index < end; index++ {
		item := m.palette[index]
		cursor := mutedStyle.Render("·")
		rowStyle := lipgloss.NewStyle().Width(rowWidth).Padding(0, 1)
		if index == m.paletteIndex {
			cursor = brandStyle.Render("›")
			rowStyle = rowStyle.Background(lipgloss.Color(surfaceHi))
			if m.transitionTick > 0 && m.motionFrame%2 == 0 {
				cursor = brandStyle.Render("»")
			}
		}
		row := cursor + " " + softStyle.Render("/"+item.name) + "  " + mutedStyle.Render(item.description)
		lines = append(lines, rowStyle.MaxWidth(rowWidth).Render(row))
	}
	return strings.Join(lines, "\n")
}

func (m model) composer(width int) string {
	style := quietStyle.Copy().Width(width).MaxWidth(width).BorderTop(true).BorderBottom(true).BorderForeground(lipgloss.Color(border))
	if m.input.Focused() {
		style = style.BorderBottomForeground(lipgloss.Color(cyan))
	}
	return style.Render(m.input.View())
}

func (m model) progressBlock(width int) string {
	if !m.busy && m.thinkingText == "" {
		return ""
	}
	message := firstNonEmpty(m.thinkingText, m.status, "Working…")
	return lipgloss.NewStyle().Width(width).Render(amberStyle.Render(taskSpinner(m.motionFrame)) + " " + softStyle.Render(message))
}

func (m model) activityRail() string {
	_, panelWidth := m.layoutWidths()
	width := maxInt(1, panelWidth-2)
	graphStatus := firstNonEmpty(m.graph.status, "missing")
	lines := []string{
		titleStyle.Render("PROJECT GRAPH") + "  " + graphStatusStyle(graphStatus).Render(strings.ToUpper(graphStatus)),
		m.graphMini(minInt(22, width), 8),
		mutedStyle.Render(fmt.Sprintf("%d nodes · %d edges", m.graph.nodes, m.graph.edges)),
		"", titleStyle.Render("ACTIVITY"), rule(width), mutedStyle.Render("PROVIDER"), softStyle.Render(firstNonEmpty(m.provider, "pending")),
	}
	if m.modelName != "" {
		lines = append(lines, mutedStyle.Render(compactText(m.modelName, width)))
	}
	if m.workspace != "" {
		lines = append(lines, "", mutedStyle.Render("WORKSPACE"), softStyle.Render(compactText(m.workspace, width)))
	}
	lines = append(lines, "", mutedStyle.Render("USAGE"), softStyle.Render(m.usageSummary()))
	lines = append(lines, "", mutedStyle.Render("MODE"), softStyle.Render(
		"preference "+firstNonEmpty(m.interactionMode, "auto")+" · active "+firstNonEmpty(m.effectiveMode, "ask"),
	))
	lines = append(lines, "", titleStyle.Render(fmt.Sprintf("TASKS  %d", len(m.tasks))))
	if len(m.tasks) == 0 {
		lines = append(lines, mutedStyle.Render("No active tasks"))
	}
	for _, task := range m.tasks {
		icon, stateStyle, label := taskState(task.status, m.motionFrame)
		if task.id == m.taskFlashID && m.taskFlashTick > 0 && task.status == "succeeded" {
			label = "completed ·"
		}
		lines = append(lines, stateStyle.Render(icon)+" "+softStyle.Render(compactText(task.description, width-4)), stateStyle.Render(label))
	}
	return lipgloss.NewStyle().Width(maxInt(1, panelWidth)).MaxWidth(maxInt(1, panelWidth)).BorderLeft(true).BorderForeground(lipgloss.Color(border)).PaddingLeft(2).Render(strings.Join(lines, "\n"))
}

func (m model) taskPanel() string { return m.activityRail() }

func (m model) modal(_ string) string {
	modalWidth := maxInt(1, minInt(78, m.width-4))
	var body string
	switch m.screen {
	case screenProvider:
		lines := []string{brandStyle.Render("PROVIDER SETUP"), titleStyle.Render("Choose a provider"), mutedStyle.Render("↑↓ select  Enter confirm  Esc cancel"), ""}
		for index, provider := range m.providerList {
			cursor, style := mutedStyle.Render("·"), softStyle
			rowStyle := lipgloss.NewStyle().Padding(0, 1)
			if index == m.providerIdx {
				cursor, style = brandStyle.Render("›"), titleStyle
				rowStyle = rowStyle.Background(lipgloss.Color(surfaceHi))
			}
			lines = append(lines, rowStyle.Render(cursor+" "+style.Render(provider)))
		}
		if len(m.providerList) == 0 {
			lines = append(lines, mutedStyle.Render("No providers are available."))
		}
		body = strings.Join(lines, "\n")
	case screenAPIKey:
		body = brandStyle.Render("PROVIDER SETUP") + "\n" + titleStyle.Render("Add your API key") + "\n" + mutedStyle.Render("Your key is masked and stored encrypted locally.") + "\n\n" + focusStyle.Copy().Width(maxInt(1, m.width-12)).MaxWidth(maxInt(1, m.width-12)).Render(m.apiInput.View()) + "\n\n" + mutedStyle.Render("Enter confirm  ·  Esc cancel")
	case screenApproval:
		body = amberStyle.Render("!  APPROVAL REQUIRED") + "\n" + titleStyle.Render("Confirm this action") + "\n\n" + softStyle.Render(m.approvalTool) + "\n" + softStyle.Render(m.approvalArgs) + "\n\n" + mutedStyle.Render("This may change local or remote state.") + "\n\n" + greenStyle.Render("Y / Enter  approve") + "    " + redStyle.Render("N / Esc  deny")
	case screenSelfLearning:
		runtime := firstNonEmpty(m.learningMode, "setup_required") + " / " + firstNonEmpty(m.learningStatus, "setup_required")
		if m.learningModel != "" {
			runtime += " / " + m.learningModel
		}
		lines := []string{brandStyle.Render("MEMORY"), titleStyle.Render("Self-learning settings"), mutedStyle.Render(runtime), mutedStyle.Render(m.learningDetail), mutedStyle.Render(m.learningCostSource), mutedStyle.Render("L local free Qwen2.5  ·  R remote API  ·  ↑↓ select  Space toggle  Esc close"), ""}
		for index, item := range m.features {
			cursor, style := mutedStyle.Render("·"), softStyle
			rowStyle := lipgloss.NewStyle().Padding(0, 1)
			if index == m.featureIdx {
				cursor, style = brandStyle.Render("›"), titleStyle
				rowStyle = rowStyle.Background(lipgloss.Color(surfaceHi))
			}
			check := "○"
			if item.enabled {
				check = "●"
			}
			lines = append(lines, rowStyle.Render(cursor+" "+style.Render(check+" "+item.name)), mutedStyle.Render("    "+item.description))
		}
		body = strings.Join(lines, "\n")
	case screenSettings:
		value := "HIDDEN"
		valueStyle := mutedStyle
		if m.showToolDetails {
			value, valueStyle = "VISIBLE", amberStyle
		}
		usageScope := firstNonEmpty(m.usageScope, "workspace")
		learning := firstNonEmpty(m.learningMode, "not configured") + " · " + firstNonEmpty(m.learningStatus, "waiting")
		body = strings.Join([]string{
			brandStyle.Render("SETTINGS"),
			titleStyle.Render("Session and display"),
			mutedStyle.Render("Space / Enter toggle  ·  Esc close"),
			"",
			brandStyle.Render("SESSION"),
			softStyle.Render("MODEL  " + compactText(firstNonEmpty(m.provider, "pending")+" · "+firstNonEmpty(m.modelName, "pending"), modalWidth-10)),
			softStyle.Render("USAGE  " + compactText(m.usageSummary()+" · "+usageScope, modalWidth-10)),
			softStyle.Render("MEMORY  " + compactText(learning, modalWidth-10)),
			"",
			brandStyle.Render("DISPLAY"),
			brandStyle.Render("›") + " " + titleStyle.Render("Show tool details") + "  " + valueStyle.Render(value),
			mutedStyle.Render("Tool names and semantic status are always shown. Results stay hidden by default."),
		}, "\n")
	case screenMode:
		modes := []string{"auto", "ask", "plan", "agent"}
		lines := []string{brandStyle.Render("INTERACTION MODE"), titleStyle.Render("Choose how Kyrozen responds"), mutedStyle.Render("↑↓ select  Enter confirm  Esc cancel"), ""}
		for index, mode := range modes {
			cursor, style := mutedStyle.Render("·"), softStyle
			row := lipgloss.NewStyle().Padding(0, 1)
			if index == m.modeIdx {
				cursor, style = brandStyle.Render("›"), titleStyle
				row = row.Background(lipgloss.Color(surfaceHi))
			}
			lines = append(lines, row.Render(cursor+" "+style.Render(mode)))
		}
		body = strings.Join(lines, "\n")
	case screenQuestion:
		lines := []string{brandStyle.Render("CLARIFICATION"), titleStyle.Render("Kyrozen needs a decision")}
		if m.pendingQuestion != nil && len(m.pendingQuestion.questions) > 0 {
			question := m.pendingQuestion.questions[minInt(m.questionIdx, len(m.pendingQuestion.questions)-1)]
			lines = append(lines, mutedStyle.Render(fmt.Sprintf("Question %d of %d", m.questionIdx+1, len(m.pendingQuestion.questions))), "", titleStyle.Render(question.header), softStyle.Render(question.prompt), "")
			for index, choice := range question.choices {
				cursor, style := mutedStyle.Render("·"), softStyle
				if index == m.choiceIdx {
					cursor, style = brandStyle.Render("›"), titleStyle
				}
				label := choice.label
				if choice.description != "" {
					label += " — " + choice.description
				}
				lines = append(lines, cursor+" "+style.Render(label))
			}
			extra := []string{"Other (type your own answer)", "Skip"}
			for offset, label := range extra {
				index := len(question.choices) + offset
				cursor, style := mutedStyle.Render("·"), softStyle
				if index == m.choiceIdx {
					cursor, style = brandStyle.Render("›"), titleStyle
				}
				lines = append(lines, cursor+" "+style.Render(label))
			}
			lines = append(lines, "", mutedStyle.Render("↑↓ select  Enter confirm  Esc cancel"))
		}
		body = strings.Join(lines, "\n")
	case screenPlan:
		body = m.planModal(maxInt(1, modalWidth-4))
	case screenGithubAuth:
		body = brandStyle.Render("GITHUB AUTHENTICATION") + "\n" + titleStyle.Render("Sign in through GitHub CLI") + "\n\n" +
			softStyle.Render("OpenKyrozen will suspend the TUI while gh opens the browser login for "+firstNonEmpty(m.githubHostname, "github.com")+".") +
			"\n" + mutedStyle.Render("Credentials remain owned by gh and are never read by OpenKyrozen.") +
			"\n\n" + greenStyle.Render("Enter  continue") + "    " + redStyle.Render("Esc  cancel")
	case screenError:
		body = redStyle.Render("ERROR") + "\n" + titleStyle.Render("OpenKyrozen needs attention") + "\n\n" + softStyle.Render(m.errorText) + "\n\n" + mutedStyle.Render("Press Enter or Esc to return to chat.")
	}
	style := modalStyle.Copy()
	if m.transitionTick > 0 {
		style = style.BorderForeground(lipgloss.Color(cyan))
	}
	body = lipgloss.NewStyle().Width(maxInt(1, modalWidth-4)).MaxWidth(maxInt(1, modalWidth-4)).Render(body)
	modal := style.Width(modalWidth).MaxWidth(modalWidth).Render(body)
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(modal)
}

func (m model) planPageHeight() int {
	return maxInt(1, m.height-12)
}

func (m model) planLines(width int) []string {
	if m.pendingPlan == nil {
		return nil
	}
	lines := []string{softStyle.Render(m.pendingPlan.summary), ""}
	for index, step := range m.pendingPlan.steps {
		lines = append(lines, titleStyle.Render(fmt.Sprintf("%d. %s", index+1, step.title)), softStyle.Render(step.description))
		for _, criterion := range step.acceptance {
			lines = append(lines, mutedStyle.Render("   ✓ "+criterion))
		}
	}
	return strings.Split(lipgloss.Wrap(strings.Join(lines, "\n"), maxInt(1, width), ""), "\n")
}

func (m model) planMaxScroll() int {
	modalWidth := maxInt(1, minInt(78, m.width-4))
	return maxInt(0, len(m.planLines(maxInt(1, modalWidth-4)))-m.planPageHeight())
}

func (m *model) scrollPlan(delta int) {
	m.planScroll = maxInt(0, minInt(m.planMaxScroll(), m.planScroll+delta))
}

func (m model) planModal(width int) string {
	if m.pendingPlan == nil {
		return brandStyle.Render("PLAN PROPOSAL")
	}
	lines := m.planLines(width)
	pageHeight := m.planPageHeight()
	start := maxInt(0, minInt(m.planScroll, maxInt(0, len(lines)-pageHeight)))
	end := minInt(len(lines), start+pageHeight)
	position := mutedStyle.Render(fmt.Sprintf("Lines %d–%d of %d  ·  ↑↓/PgUp/PgDn scroll", start+1, end, len(lines)))
	header := brandStyle.Render("PLAN PROPOSAL") + "\n" + titleStyle.Render(fmt.Sprintf("%s · v%d", m.pendingPlan.title, m.pendingPlan.version))
	footer := position + "\n" + greenStyle.Render("A / Enter  accept") + "    " + amberStyle.Render("R  revise") + "    " + redStyle.Render("C / Esc  cancel")
	return header + "\n\n" + strings.Join(lines[start:end], "\n") + "\n\n" + footer
}

func rule(width int) string { return ruleStyle.Render(strings.Repeat("─", maxInt(1, width))) }

func bannerRows(width int) []string {
	inner := maxInt(26, width-2)
	line := strings.Repeat("─", inner)
	return []string{
		"╭" + line + "╮",
		"│" + centerText("◈  OPENKYROZEN  ◈", inner) + "│",
		"│" + centerText("COMPUTER-NATIVE  /  SELF-LEARNING", inner) + "│",
		"│" + centerText("A LOCAL WORKBENCH FOR YOUR IDEAS", inner) + "│",
		"╰" + line + "╯",
	}
}

func centerText(value string, width int) string {
	if lipgloss.Width(value) > width {
		value = string([]rune(value)[:maxInt(0, width)])
	}
	padding := maxInt(0, width-lipgloss.Width(value))
	return strings.Repeat(" ", padding/2) + value + strings.Repeat(" ", padding-padding/2)
}

func startupMilestones(m model) []string {
	phase := m.splashFrame / 3
	if m.reducedMotion {
		phase = 5
	}
	return []string{
		startupMilestone("workspace", phase >= 2, phase == 1),
		startupMilestone("provider", m.backendReady, !m.backendReady && phase >= 2),
		startupMilestone("memory", m.backendReady && phase >= 5, m.backendReady && phase < 5),
	}
}

func startupMilestone(name string, done, active bool) string {
	icon, style := "·", mutedStyle
	if done {
		icon, style = "✓", greenStyle
	} else if active {
		icon, style = taskSpinner(1), amberStyle
	}
	return style.Render(icon) + " " + softStyle.Render(name)
}

func splashStatus(m model) string {
	if m.backendReady {
		return greenStyle.Render("READY") + "  " + softStyle.Render("Opening your workbench…")
	}
	return amberStyle.Render(taskSpinner(m.splashFrame)) + "  " + softStyle.Render(firstNonEmpty(m.status, "Starting the workspace…"))
}

func taskSpinner(frame int) string {
	return []string{"◐", "◓", "◑", "◒"}[maxInt(0, frame)%4]
}

func taskState(status string, frame int) (string, lipgloss.Style, string) {
	switch strings.ToLower(status) {
	case "succeeded", "completed", "complete", "done":
		return "✓", greenStyle, "completed"
	case "running", "active", "in_progress":
		return taskSpinner(frame), amberStyle, "running"
	case "failed":
		return "×", redStyle, "failed"
	case "blocked":
		return "⊘", redStyle, "blocked"
	default:
		return "○", mutedStyle, firstNonEmpty(status, "pending")
	}
}

func receiptStyle(status string) lipgloss.Style {
	switch strings.ToLower(status) {
	case "blocked", "approval_denied":
		return amberStyle
	case "failure", "failed", "error":
		return redStyle
	default:
		return greenStyle
	}
}

func compactText(value string, width int) string {
	value = strings.TrimSpace(value)
	if width < 1 || lipgloss.Width(value) <= width {
		return value
	}
	runes := []rune(value)
	if len(runes) <= width {
		return value
	}
	return string(runes[:maxInt(1, width-1)]) + "…"
}

func main() {
	const restartExitCode = 75

	project := flag.String("project", "", "active project path")
	global := flag.Bool("global", false, "use the global workspace")
	showVersion := flag.Bool("version", false, "show version")
	flag.Parse()
	if *showVersion {
		fmt.Printf("OpenKyrozen %s\n", version)
		return
	}
	m := initialModel(*project, *global || *project == "")
	p := tea.NewProgram(m)
	finalModel, err := p.Run()
	if err != nil {
		fmt.Fprintln(os.Stderr, "OpenKyrozen TUI:", err)
		os.Exit(1)
	}
	if m, ok := finalModel.(model); ok && m.restart {
		os.Exit(restartExitCode)
	}
}
