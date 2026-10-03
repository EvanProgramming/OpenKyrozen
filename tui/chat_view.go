package main

import (
	"charm.land/lipgloss/v2"
	"fmt"
	"path/filepath"
	"strings"
)

func (m model) chatView() string {
	contentWidth := m.contentWidth()
	mainWidth, panelWidth := m.layoutWidths()
	header := m.chatHeader()
	historyHeight := m.historyHeight()
	main := lipgloss.NewStyle().Width(mainWidth).MaxWidth(mainWidth).Height(historyHeight).MaxHeight(historyHeight).Render(
		limitRows(m.view.View(), historyHeight),
	)
	if panelWidth > 0 {
		main = lipgloss.JoinHorizontal(lipgloss.Top, main, " ", m.activityRail())
	}
	if navigationWidth := m.navigationWidth(); navigationWidth > 0 {
		main = lipgloss.JoinHorizontal(lipgloss.Top, m.navigationSidebar(navigationWidth, historyHeight), " ", main)
	} else if m.navigationOpen {
		main = m.navigationSidebar(minInt(34, contentWidth), historyHeight)
	}
	blocks := []string{header}
	blocks = append(blocks, main)
	if progress := m.progressBlock(contentWidth); progress != "" {
		blocks = append(blocks, progress)
	}
	if len(m.palette) > 0 {
		blocks = append(blocks, m.paletteView(contentWidth))
	}
	blocks = append(blocks, m.modeControls(contentWidth))
	blocks = append(blocks, m.composer(contentWidth))
	blocks = append(blocks, m.chatFooter(contentWidth))
	return strings.Join(blocks, "\n")
}

func (m model) modeControls(width int) string {
	modes := []string{"auto", "ask", "plan", "agent"}
	labels := []string{"AUTO", "ASK", "PLAN", "AGENT"}
	modePrefix := "MODE  "
	compactControls := width < 32
	if compactControls {
		modePrefix = "M"
		labels = []string{"A", "Q", "P", "G"}
	} else if width < 44 {
		modePrefix = "MODE "
		labels = []string{"A", "Q", "P", "G"}
	} else if width < 64 {
		labels[0] = "A"
	}
	mode := modePrefix
	for index, name := range modes {
		label := " " + labels[index] + " "
		if compactControls {
			label = labels[index]
		}
		if name == m.interactionMode {
			mode += titleStyle.Render("[" + label + "]")
		} else {
			mode += mutedStyle.Render("[" + label + "]")
		}
		if index < len(modes)-1 && !compactControls {
			mode += " "
		}
	}
	if m.interactionMode == "auto" && m.effectiveMode != "" && m.effectiveMode != "auto" {
		mode += mutedStyle.Render("  → " + strings.ToUpper(m.effectiveMode))
	}
	permission := "PERMISSIONS  "
	labels, compactPermissions := permissionModeControlLabels(width)
	choices := []struct{ id, label string }{{"ask", labels[0]}, {"full_jev", labels[1]}, {"full", labels[2]}}
	permissionPrefix := "PERMISSIONS  "
	if compactPermissions && compactControls {
		permissionPrefix = "P"
	} else if width < 44 {
		permissionPrefix = "ACCESS "
	}
	permission = permissionPrefix
	for index, choice := range choices {
		label := " " + choice.label + " "
		if compactControls {
			label = choice.label
		}
		if choice.id == m.permissionMode {
			permission += amberStyle.Render("[" + label + "]")
		} else {
			permission += mutedStyle.Render("[" + label + "]")
		}
		if index < len(choices)-1 && !compactControls {
			permission += " "
		}
	}
	if m.permissionStatus == "checking" {
		permission += amberStyle.Render("  Jev checking…")
	} else if m.permissionStatus == "approval_required" {
		permission += amberStyle.Render("  approval required")
	} else if m.permissionMode == "full_jev" && !m.jeVConfigured {
		permission += mutedStyle.Render("  Jev unavailable")
	}
	separator := mutedStyle.Render(" │ ")
	if m.height > 0 && m.height < 18 && width >= 100 {
		return lipgloss.NewStyle().Width(width).MaxWidth(width).Render(mode + separator + permission)
	}
	return lipgloss.NewStyle().Width(width).MaxWidth(width).Render(mode + "\n" + permission)
}

func permissionModeControlLabels(width int) ([]string, bool) {
	labels := []string{"ASK", "FULL + JEV", "FULL NO JEV"}
	if width < 32 {
		return []string{"A", "J", "O"}, true
	}
	if width < 44 {
		return []string{"A", "J", "O"}, false
	}
	if width < 76 {
		return []string{"ASK", "JEV", "NO JEV"}, false
	}
	return labels, false
}

func (m model) modeControlsModeWidth(width int) int {
	labels := []string{"AUTO", "ASK", "PLAN", "AGENT"}
	prefix := "MODE  "
	if width < 32 {
		prefix, labels = "M", []string{"A", "Q", "P", "G"}
	} else if width < 44 {
		prefix, labels = "MODE ", []string{"A", "Q", "P", "G"}
	} else if width < 64 {
		labels[0] = "A"
	}
	result := len(prefix)
	for index, label := range labels {
		result += len(label) + 4
		if width < 32 {
			result -= 2
		}
		if index < len(labels)-1 && width >= 32 {
			result++
		}
	}
	if m.interactionMode == "auto" && m.effectiveMode != "" && m.effectiveMode != "auto" {
		result += 4 + len(strings.ToUpper(m.effectiveMode))
	}
	return result
}

func (m model) modeControlsStartY() int {
	rows := lipgloss.Height(m.chatHeader()) + m.historyHeight()
	if progress := m.progressBlock(m.contentWidth()); progress != "" {
		rows += lipgloss.Height(progress)
	}
	if len(m.palette) > 0 {
		rows += lipgloss.Height(m.paletteView(m.contentWidth()))
	}
	return rows
}

func (m model) modeTabAt(x, y int) int {
	if y != m.modeControlsStartY() {
		return -1
	}
	start := len("MODE  ")
	labels := []string{"AUTO", "ASK", "PLAN", "AGENT"}
	compactControls := m.contentWidth() < 32
	if compactControls {
		start, labels = len("M"), []string{"A", "Q", "P", "G"}
	} else if m.contentWidth() < 44 {
		start, labels = len("MODE "), []string{"A", "Q", "P", "G"}
	} else if m.contentWidth() < 64 {
		labels[0] = "A"
	}
	for index, label := range labels {
		padding := 4
		if compactControls {
			padding = 2
		}
		end := start + len(label) + padding
		if x >= start && x < end {
			return index
		}
		start = end
		if !compactControls {
			start++
		}
	}
	return -1
}

func (m model) permissionTabAt(x, y int) int {
	row := m.modeControlsStartY() + 1
	compactHeight := m.height > 0 && m.height < 18 && m.contentWidth() >= 100
	if compactHeight {
		row = m.modeControlsStartY()
	}
	if y != row {
		return -1
	}
	start := len("PERMISSIONS  ")
	if compactHeight {
		start += m.modeControlsModeWidth(m.contentWidth()) + 3
	}
	labels, compactControls := permissionModeControlLabels(m.contentWidth())
	if compactControls {
		start = len("P")
	} else if m.contentWidth() < 44 {
		start = len("ACCESS ")
	}
	for index, label := range labels {
		padding := 4
		if compactControls {
			padding = 2
		}
		end := start + len(label) + padding
		if x >= start && x < end {
			return index
		}
		start = end
		if !compactControls {
			start++
		}
	}
	return -1
}

func (m model) chatHeader() string {
	width := m.contentWidth()
	if width < 4 {
		return brandStyle.Render(compactText("K", width))
	}
	innerWidth := maxInt(1, width-2)
	brand := "◆ OPENKYROZEN"
	if innerWidth < 24 {
		brand = "◆ KYROZEN"
	}
	left := brandStyle.Render(compactText(brand, innerWidth))
	mode := strings.ToUpper(firstNonEmpty(m.effectiveMode, m.interactionMode, "ask"))

	statusText := compactText(firstNonEmpty(m.status, "Ready"), maxInt(4, minInt(18, innerWidth/3)))
	status := mutedStyle.Render("● " + statusText)
	if m.busy {
		status = amberStyle.Render(taskSpinner(m.motionFrame) + " " + statusText)
	} else if strings.EqualFold(statusText, "ready") {
		status = greenStyle.Render("● READY")
	}
	right := ""
	leftBudget := innerWidth
	if innerWidth >= 38 {
		right = status
		leftBudget = maxInt(1, innerWidth-lipgloss.Width(right)-2)
	}
	add := func(item string) {
		candidate := left + mutedStyle.Render("  ·  ") + item
		if lipgloss.Width(candidate) <= leftBudget {
			left = candidate
		}
	}
	add(badgeStyle.Render(mode))
	_, panelWidth := m.layoutWidths()
	if panelWidth == 0 {
		graphStatus := strings.ToUpper(firstNonEmpty(m.graph.status, "missing"))
		add(graphStatusStyle(graphStatus).Render("◇ " + graphStatus))
	}
	if m.modelName != "" {
		add(softStyle.Render(compactText(m.modelName, 22)))
	}
	tokens := m.usagePromptTokens + m.usageCompletionTokens + m.usageReasoningTokens
	usage := formatUsageTokens(tokens) + " TOK"
	if m.usageCostPicos > 0 {
		usage += " · " + formatUsageCost(m.usageCostPicos)
	}
	add(mutedStyle.Render(usage))

	row := left
	if right != "" {
		gap := maxInt(2, innerWidth-lipgloss.Width(left)-lipgloss.Width(right))
		row += strings.Repeat(" ", gap) + right
	}
	header := contextStyle.Copy().Width(innerWidth).MaxWidth(innerWidth).Render(row)
	if m.availableVersion != "" {
		notice := "↑ OpenKyrozen " + m.availableVersion + " available · Run /update"
		if width < 64 {
			notice = "Update available\nRun /update"
		}
		if width < 20 {
			notice = "Update available\n/update"
		}
		header += "\n" + amberStyle.Copy().Width(width).MaxWidth(width).Render(notice)
	}
	return header
}

func (m model) chatFooter(width int) string {
	if width < 4 {
		return ""
	}
	hints := "↵ send · / commands · ^C quit"
	if width >= 44 {
		hints = "Tab mode · Ctrl+P permissions · ↵ send · / commands · ^C quit"
	}
	if width >= 72 && m.height >= 18 {
		hints = "Tab mode · Ctrl+P permissions · Enter send · / commands · Ctrl+B chats · Ctrl+N new · Ctrl+O project · Ctrl+C quit"
	}
	workspace := ""
	if m.workspace != "" && width >= 52 {
		workspace = compactText(filepath.Base(m.workspace), maxInt(8, width/4))
	}
	if workspace == "" {
		return mutedStyle.Copy().Width(width).MaxWidth(width).Render(compactText(hints, width))
	}
	right := compactText(hints, maxInt(1, width-lipgloss.Width(workspace)-2))
	gap := maxInt(2, width-lipgloss.Width(workspace)-lipgloss.Width(right))
	return mutedStyle.Copy().Width(width).MaxWidth(width).Render(workspace + strings.Repeat(" ", gap) + right)
}

func (m model) paletteView(width int) string {
	if len(m.palette) == 0 {
		return ""
	}
	if width < 12 {
		return titleStyle.Render(compactText("COMMANDS", width))
	}
	rowWidth := maxInt(1, width-6)
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
	lines := []string{sectionStyle.Render(title)}
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
		descriptionWidth := maxInt(1, rowWidth-len(item.name)-5)
		row := cursor + " " + titleStyle.Render("/"+item.name) + "  " + mutedStyle.Render(compactText(item.description, descriptionWidth))
		lines = append(lines, rowStyle.MaxWidth(rowWidth).Render(row))
	}
	panelWidth := maxInt(1, width-4)
	return paletteStyle.Copy().Width(panelWidth).MaxWidth(panelWidth).Render(strings.Join(lines, "\n"))
}

func (m model) composer(width int) string {
	if m.height > 0 && m.height < 12 {
		return lipgloss.NewStyle().Width(width).MaxWidth(width).Render(m.input.View())
	}
	panelWidth := maxInt(1, width-4)
	style := composerStyle.Copy().Width(panelWidth).MaxWidth(panelWidth)
	if m.input.Focused() {
		style = style.BorderForeground(lipgloss.Color(cyan))
	}
	return style.Render(m.input.View())
}

func (m model) progressBlock(width int) string {
	if !m.busy && m.thinkingText == "" {
		return ""
	}
	message := firstNonEmpty(m.thinkingText, m.status, "Working…")
	innerWidth := maxInt(1, width-2)
	line := amberStyle.Render(taskSpinner(m.motionFrame)) + " " + softStyle.Render(compactText(message, maxInt(1, innerWidth-3)))
	return contextStyle.Copy().Width(innerWidth).MaxWidth(innerWidth).Render(line)
}

func (m model) activityRail() string {
	_, panelWidth := m.layoutWidths()
	width := maxInt(1, panelWidth-2)
	textWidth := maxInt(1, width-2)
	height := m.historyHeight()
	graphStatus := firstNonEmpty(m.graph.status, "missing")
	mapHeight := 3
	if height >= 22 {
		mapHeight = 5
	}
	lines := []string{
		sectionStyle.Render("PROJECT GRAPH") + "  " + graphStatusStyle(graphStatus).Render(strings.ToUpper(graphStatus)),
		m.graphMini(minInt(22, textWidth), mapHeight),
		mutedStyle.Render(fmt.Sprintf("%d nodes · %d edges", m.graph.nodes, m.graph.edges)),
		"",
		sectionStyle.Render(fmt.Sprintf("TASKS  %d", len(m.tasks))),
	}
	if len(m.agents) > 0 {
		agents := m.activityAgents()
		agentLines := []string{sectionStyle.Render(fmt.Sprintf("AGENTS  %d · /agents", len(agents)))}
		for index, agent := range agents {
			if index >= maxInt(1, minInt(3, height/8)) {
				agentLines = append(agentLines, mutedStyle.Render(fmt.Sprintf("+%d more", len(agents)-index)))
				break
			}
			brief, _ := agent["assignment"].(map[string]any)
			description := compactText(stringValue(agent, "name")+" · "+stringValue(agent, "profile")+" · "+stringValue(agent, "status"), maxInt(1, textWidth-6))
			avatar := agentIcon(agent)
			agentLines = append(agentLines, lipgloss.JoinHorizontal(lipgloss.Top, avatar, " ", description+"\n"+compactText(stringValue(brief, "objective"), maxInt(1, textWidth-6))+"\n"+compactText(stringValue(agent, "provider_model"), maxInt(1, textWidth-6))))
		}
		lines = append(agentLines, append([]string{""}, lines...)...)
	}
	if len(m.tasks) == 0 {
		lines = append(lines, mutedStyle.Render("No active tasks"))
	}
	maxTasks := maxInt(1, height-mapHeight-12)
	for index, task := range m.tasks {
		if index >= maxTasks {
			lines = append(lines, mutedStyle.Render(fmt.Sprintf("+%d more", len(m.tasks)-index)))
			break
		}
		icon, stateStyle, label := taskState(task.status, m.motionFrame)
		if task.id == m.taskFlashID && m.taskFlashTick > 0 && task.status == "succeeded" {
			label = "completed ·"
		}
		row := stateStyle.Render(icon) + " " + softStyle.Render(compactText(task.description, maxInt(4, textWidth-lipgloss.Width(label)-4))) + " " + stateStyle.Render(label)
		lines = append(lines, row)
	}
	workspace := "global workspace"
	if m.workspace != "" {
		workspace = filepath.Base(m.workspace)
	}
	usage := "No usage yet"
	if m.usageAttempts > 0 {
		usage = fmt.Sprintf("%d calls · %s", m.usageAttempts, firstNonEmpty(m.usageScope, "workspace"))
	}
	lines = append(lines, "", sectionStyle.Render("SESSION"), softStyle.Render(compactText(workspace, textWidth)), mutedStyle.Render(compactText(usage, textWidth)))
	backend := firstNonEmpty(m.fastBackend, "off")
	lines = append(lines, mutedStyle.Render(compactText("System One: "+backend, textWidth)))
	lines = append(lines, mutedStyle.Render(compactText("Decision Assist: "+firstNonEmpty(m.decisionAssistBackend, "off"), textWidth)))
	preferredMode := firstNonEmpty(m.interactionMode, "auto")
	activeMode := firstNonEmpty(m.effectiveMode, "ask")
	if m.interactionMode != "" && m.effectiveMode != "" && preferredMode != activeMode {
		lines = append(lines,
			mutedStyle.Render(compactText("preference "+preferredMode, textWidth)),
			mutedStyle.Render(compactText("active "+activeMode, textWidth)),
		)
	}
	// Keep the rail inside the transcript row budget. Without this cap,
	// JoinHorizontal adopts a task-heavy rail's natural height and pushes the
	// composer/footer below the terminal viewport.
	return activityStyle.Copy().Width(width).MaxWidth(width).Height(height).MaxHeight(height).Render(limitRows(strings.Join(lines, "\n"), height))
}

func (m model) taskPanel() string { return m.activityRail() }
