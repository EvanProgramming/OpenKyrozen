package main

import (
	"flag"
	"fmt"
	"os"
	"strings"
	"time"
	"unicode"

	"charm.land/bubbles/v2/textarea"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	tea "charm.land/bubbletea/v2"
	"charm.land/glamour/v2"
	"charm.land/lipgloss/v2"
)

var version = "2.0.3"

type screen string

const (
	screenSplash       screen = "splash"
	screenChat         screen = "chat"
	screenProvider     screen = "provider"
	screenAPIKey       screen = "api_key"
	screenApproval     screen = "approval"
	screenSelfLearning screen = "self_learning"
	screenError        screen = "error"
)

type chatMessage struct {
	role      string
	text      string
	streaming bool
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

type model struct {
	bridge        *bridge
	project       string
	global        bool
	width         int
	height        int
	view          viewport.Model
	input         textarea.Model
	apiInput      textinput.Model
	screen        screen
	status        string
	provider      string
	modelName     string
	workspace     string
	messages      []chatMessage
	tasks         []taskItem
	palette       []command
	paletteIndex  int
	providerList  []string
	providerIdx   int
	features      []featureItem
	featureIdx    int
	approvalID    string
	approvalTool  string
	approvalArgs  string
	errorText     string
	splashFrame   int
	reducedMotion bool
	busy          bool
	requestCount  int
}

type tickMsg time.Time
type backendStartedMsg struct{ err error }

func initialModel(project string, global bool) model {
	input := textarea.New()
	input.Placeholder = "Ask Kyrozen anything…"
	input.Prompt = "› "
	input.CharLimit = 12_000
	input.SetHeight(3)
	input.SetWidth(60)
	input.Focus()
	apiInput := textinput.New()
	apiInput.Placeholder = "Paste an API key"
	apiInput.CharLimit = 512
	apiInput.EchoMode = textinput.EchoPassword
	apiInput.EchoCharacter = '•'
	return model{
		bridge:        newBridge(),
		project:       project,
		global:        global,
		view:          viewport.New(),
		input:         input,
		apiInput:      apiInput,
		screen:        screenSplash,
		status:        "Starting the workspace…",
		reducedMotion: os.Getenv("KYROZEN_REDUCED_MOTION") == "1" || os.Getenv("NO_COLOR") != "",
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(startBackend(m.bridge), splashTick(m.reducedMotion))
}

func startBackend(b *bridge) tea.Cmd {
	return func() tea.Msg { return backendStartedMsg{err: b.start()} }
}

func splashTick(reduced bool) tea.Cmd {
	if reduced {
		return func() tea.Msg { return tickMsg(time.Now()) }
	}
	return tea.Tick(90*time.Millisecond, func(now time.Time) tea.Msg { return tickMsg(now) })
}

func waitBackend(b *bridge) tea.Cmd {
	return func() tea.Msg {
		event, ok := <-b.events
		if !ok {
			return backendExitMsg{}
		}
		return event
	}
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
	case backendLineMsg:
		if msg.err != nil {
			m.setError(msg.err.Error())
		} else {
			m.handleBackendEvent(msg.event)
		}
		cmds = append(cmds, waitBackend(m.bridge))
	case backendExitMsg:
		if m.screen != screenError {
			m.status = "Backend stopped"
		}
	case tickMsg:
		if m.screen == screenSplash && !m.reducedMotion {
			m.splashFrame++
			if m.splashFrame >= 10 {
				m.screen = screenChat
			}
			cmds = append(cmds, splashTick(m.reducedMotion))
		} else if m.screen == screenSplash {
			m.screen = screenChat
		}
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

func (m *model) handleKey(msg tea.KeyPressMsg) (tea.Cmd, bool) {
	key := msg.String()
	if key == "ctrl+c" {
		return nil, true
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
	if m.screen == screenProvider {
		return m.providerKey(key), false
	}
	if m.screen == screenSelfLearning {
		return m.featureKey(key), false
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

func (m *model) submit() tea.Cmd {
	text := strings.TrimSpace(m.input.Value())
	if text == "" || m.busy {
		return nil
	}
	if strings.HasPrefix(strings.TrimLeftFunc(m.input.Value(), unicode.IsSpace), "/") {
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
	m.input.Reset()
	m.palette = nil
	m.input.SetHeight(m.composerHeight())
	m.send("submit", map[string]any{"text": text})
	return nil
}

func (m *model) refreshPalette() {
	m.palette = commandMatches(m.input.Value())
	if len(m.palette) == 0 {
		m.paletteIndex = 0
	} else if m.paletteIndex >= len(m.palette) {
		m.paletteIndex = len(m.palette) - 1
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
		m.status = firstNonEmpty(stringValue(event, "message"), stringValue(event, "state"))
		if state := stringValue(event, "state"); state == "thinking" || state == "starting" {
			m.busy = state == "thinking"
		}
	case "ready":
		m.provider = stringValue(event, "provider")
		m.modelName = stringValue(event, "model")
		m.workspace = stringValue(event, "workspace")
		m.status = "Ready"
		m.busy = false
		if m.screen == screenSplash && (m.reducedMotion || m.splashFrame >= 10) {
			m.screen = screenChat
		}
	case "stream_delta":
		m.busy, m.status = true, "Generating…"
		text := stringValue(event, "text")
		for i := len(m.messages) - 1; i >= 0; i-- {
			if m.messages[i].role == "assistant" && m.messages[i].streaming {
				m.messages[i].text += text
				return
			}
		}
		m.messages = append(m.messages, chatMessage{role: "assistant", text: text, streaming: true})
	case "thinking":
		m.messages = append(m.messages, chatMessage{role: "thinking", text: stringValue(event, "text")})
	case "response":
		text := stringValue(event, "text")
		for i := len(m.messages) - 1; i >= 0; i-- {
			if m.messages[i].role == "assistant" && m.messages[i].streaming {
				m.messages[i].text, m.messages[i].streaming = text, false
				m.busy = false
				return
			}
		}
		m.messages = append(m.messages, chatMessage{role: "assistant", text: text})
		m.busy = false
	case "tool_receipt":
		if receipt, ok := event["receipt"].(map[string]any); ok {
			m.messages = append(m.messages, chatMessage{role: "receipt", text: "✓ " + stringValue(receipt, "action") + " — " + stringValue(receipt, "result")})
		}
	case "tasks":
		m.tasks = parseTasks(event["tasks"])
	case "prompt":
		m.handlePrompt(event)
	case "error":
		m.setError(firstNonEmpty(stringValue(event, "error"), "Backend error"))
	case "exit":
		m.status, m.busy = "Stopped", false
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
	case "approval":
		m.approvalID = stringValue(event, "request_id")
		m.approvalTool = stringValue(event, "action")
		m.approvalArgs = stringValue(event, "args")
		m.screen = screenApproval
	case "self_learning":
		m.features = nil
		if features, ok := event["features"].([]any); ok {
			for _, raw := range features {
				if item, ok := raw.(map[string]any); ok {
					m.features = append(m.features, featureItem{name: stringValue(item, "name"), enabled: boolValue(item, "enabled"), description: stringValue(item, "description")})
				}
			}
		}
		m.featureIdx, m.screen = 0, screenSelfLearning
	}
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
	m.view.SetWidth(mainWidth)
	m.view.SetHeight(m.historyHeight())
	m.syncViewport()
}

func (m model) contentWidth() int {
	return maxInt(1, m.width-4)
}

func (m model) layoutWidths() (int, int) {
	available := m.contentWidth()
	if m.width < 100 || len(m.tasks) == 0 {
		return available, 0
	}
	panelWidth := minInt(30, maxInt(22, available/4))
	return maxInt(1, available-panelWidth-2), panelWidth
}

func (m model) composerHeight() int {
	if len(m.palette) > 0 || (m.height > 0 && m.height < 16) {
		return 1
	}
	return 3
}

func (m model) historyHeight() int {
	height := maxInt(1, m.height-11)
	if len(m.palette) > 0 {
		height = maxInt(2, minInt(height, maxInt(2, m.height-10)))
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
	mainWidth, _ := m.layoutWidths()
	m.view.SetWidth(mainWidth)
	m.view.SetHeight(m.historyHeight())
	m.view.SetContent(m.history())
	m.view.GotoBottom()
}

func (m model) history() string {
	if len(m.messages) == 0 {
		return mutedStyle.Render("No messages yet. Start with a question, or type / for commands.")
	}
	var lines []string
	for _, message := range m.messages {
		label, body := "KYROZEN", message.text
		labelStyle := brandStyle
		switch message.role {
		case "user":
			label, labelStyle = "YOU", lipgloss.NewStyle().Foreground(lipgloss.Color(white)).Bold(true)
		case "thinking":
			label, labelStyle = "THINKING", amberStyle
		case "receipt":
			label, labelStyle = "TOOL RECEIPT", greenStyle
		}
		if message.role == "assistant" && body != "" {
			if rendered, err := glamour.Render(body, "dark"); err == nil {
				body = strings.TrimSpace(rendered)
			}
		}
		lines = append(lines, labelStyle.Render(label)+"\n"+body+"\n")
	}
	return strings.Join(lines, "\n")
}

func (m model) View() tea.View {
	var content string
	if m.screen == screenSplash {
		content = m.splash()
	} else {
		content = m.chatView()
		if m.screen != screenChat {
			content = m.modal(content)
		}
	}
	view := tea.NewView(fillBackground(content, m.width, m.height))
	view.AltScreen = true
	view.BackgroundColor = lipgloss.Color(ink)
	view.ForegroundColor = lipgloss.Color(white)
	view.WindowTitle = "OpenKyrozen"
	return view
}

func (m model) splash() string {
	word := "OPENKYROZEN"
	if !m.reducedMotion {
		frames := []string{"·", "✦", "✧", "✦"}
		marker := frames[m.splashFrame%len(frames)]
		word = marker + "  " + word + "  " + marker
	}
	logo := brandStyle.Copy().Align(lipgloss.Center).Render(word)
	tagline := mutedStyle.Copy().Align(lipgloss.Center).Render("A computer-native, self-learning agent")
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(logo + "\n\n" + tagline)
}

func (m model) chatView() string {
	header := lipgloss.JoinHorizontal(lipgloss.Top, brandStyle.Render("OPENKYROZEN"), "  "+mutedStyle.Render(firstNonEmpty(m.provider, "provider pending")), "  "+mutedStyle.Render(firstNonEmpty(m.modelName, "startup")))
	if m.workspace != "" {
		header += "\n" + mutedStyle.Render("workspace  "+m.workspace)
	}
	historyHeight := m.historyHeight()
	mainWidth, panelWidth := m.layoutWidths()
	history := m.view.View()
	if len(m.palette) > 0 {
		history = m.paletteView()
	}
	main := quietStyle.Copy().Width(mainWidth).MaxWidth(mainWidth).Height(historyHeight).MaxHeight(historyHeight).Render(history)
	if panelWidth > 0 {
		main = lipgloss.JoinHorizontal(lipgloss.Top, main, "  ", m.taskPanel())
	}
	composer := focusStyle.Copy().Width(m.contentWidth()).MaxWidth(m.contentWidth()).Render(m.input.View())
	footer := mutedStyle.Render("Enter send  ·  Shift+Enter newline  ·  ↑↓ command menu  ·  PgUp/PgDn scroll  ·  Ctrl+C quit")
	if m.busy {
		footer = amberStyle.Render("● "+m.status) + "  " + footer
	} else {
		footer = mutedStyle.Render("○ "+firstNonEmpty(m.status, "Ready")) + "  " + footer
	}
	return header + "\n\n" + main + "\n\n" + composer + "\n" + footer
}

func (m model) paletteView() string {
	if len(m.palette) == 0 {
		return ""
	}
	lines := []string{brandStyle.Render("COMMANDS")}
	visible := maxInt(1, minInt(len(m.palette), maxInt(1, m.height/8)))
	start := 0
	if m.paletteIndex >= visible {
		start = m.paletteIndex - visible + 1
	}
	end := minInt(len(m.palette), start+visible)
	for index := start; index < end; index++ {
		item := m.palette[index]
		style, cursor := mutedStyle, "  "
		if index == m.paletteIndex {
			style, cursor = brandStyle, "› "
		}
		lines = append(lines, style.Render(cursor+"/"+item.name)+"  "+mutedStyle.Render(item.description))
	}
	return strings.Join(lines, "\n")
}

func (m model) taskPanel() string {
	_, panelWidth := m.layoutWidths()
	lines := []string{titleStyle.Render("TASKS"), ""}
	for _, task := range m.tasks {
		icon, style := "○", mutedStyle
		switch task.status {
		case "succeeded":
			icon, style = "✓", greenStyle
		case "running":
			icon, style = "◷", amberStyle
		case "failed", "blocked":
			icon, style = "!", redStyle
		}
		lines = append(lines, style.Render(icon)+" "+task.description, mutedStyle.Render(task.status))
	}
	return panelStyle.Copy().Width(maxInt(1, panelWidth)).MaxWidth(maxInt(1, panelWidth)).Render(strings.Join(lines, "\n"))
}

func (m model) modal(_ string) string {
	var body string
	switch m.screen {
	case screenProvider:
		lines := []string{titleStyle.Render("Choose a provider"), mutedStyle.Render("↑↓ select  Enter confirm  Esc cancel"), ""}
		for index, provider := range m.providerList {
			cursor, style := "  ", mutedStyle
			if index == m.providerIdx {
				cursor, style = "› ", brandStyle
			}
			lines = append(lines, style.Render(cursor+provider))
		}
		body = strings.Join(lines, "\n")
	case screenAPIKey:
		body = titleStyle.Render("Provider setup") + "\n" + mutedStyle.Render("Your key is masked and stored encrypted locally.") + "\n\n" + focusStyle.Render(m.apiInput.View()) + "\n\n" + mutedStyle.Render("Enter confirm  ·  Esc cancel")
	case screenApproval:
		body = titleStyle.Render("Approval required") + "\n\n" + amberStyle.Render(m.approvalTool) + "\n" + mutedStyle.Render(m.approvalArgs) + "\n\n" + mutedStyle.Render("This may change local or remote state.") + "\n\n" + greenStyle.Render("Y / Enter approve") + "    " + redStyle.Render("N / Esc deny")
	case screenSelfLearning:
		lines := []string{titleStyle.Render("Self-learning settings"), mutedStyle.Render("↑↓ select  Space toggle  Esc close"), ""}
		for index, item := range m.features {
			cursor, style := "  ", mutedStyle
			if index == m.featureIdx {
				cursor, style = "› ", brandStyle
			}
			check := "○"
			if item.enabled {
				check = "●"
			}
			lines = append(lines, style.Render(cursor+check+" "+item.name), mutedStyle.Render("    "+item.description))
		}
		body = strings.Join(lines, "\n")
	case screenError:
		body = titleStyle.Render("OpenKyrozen needs attention") + "\n\n" + redStyle.Render(m.errorText) + "\n\n" + mutedStyle.Render("Press Enter or Esc to return to chat.")
	}
	modalWidth := maxInt(1, minInt(78, m.width-4))
	modal := panelStyle.Copy().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(muted)).Width(modalWidth).MaxWidth(modalWidth).Render(body)
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(modal)
}

var (
	amberStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(amber))
	greenStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(green))
	redStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color(red))
)

func main() {
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
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "OpenKyrozen TUI:", err)
		os.Exit(1)
	}
}
