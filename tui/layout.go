package main

import (
	"charm.land/lipgloss/v2"
	"fmt"
	"strings"
)

func (m *model) resize() {
	width := m.contentWidth()
	mainWidth, _ := m.layoutWidths()
	composerWidth := width
	if m.height >= 12 {
		composerWidth = maxInt(1, width-4)
	}
	m.input.SetWidth(composerWidth)
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

func (m model) navigationWidth() int {
	if m.width < 100 || m.height < 16 {
		return 0
	}
	return minInt(28, maxInt(20, m.contentWidth()/5))
}

func (m model) layoutWidths() (int, int) {
	available := m.contentWidth()
	if navigationWidth := m.navigationWidth(); navigationWidth > 0 {
		available = maxInt(1, available-navigationWidth-1)
	}
	if m.width < 120 || m.height < 24 {
		return available, 0
	}
	railWidth := minInt(30, maxInt(24, available/4))
	return maxInt(1, available-railWidth-1), railWidth
}

func (m model) composerHeight() int {
	if m.height > 0 && m.height < 16 {
		return 1
	}
	return 3
}

func (m model) compactChat() bool { return m.height > 0 && m.height < 14 }

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
	nonViewport := []string{m.chatHeader()}
	if progress := m.progressBlock(width); progress != "" {
		nonViewport = append(nonViewport, progress)
	}
	if len(m.palette) > 0 {
		nonViewport = append(nonViewport, m.paletteView(width))
	}
	nonViewport = append(nonViewport, m.modeControls(width), m.composer(width), m.chatFooter(width))
	return lipgloss.Height(strings.Join(nonViewport, "\n"))
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func limitRows(value string, height int) string {
	if height < 1 {
		return ""
	}
	lines := strings.Split(value, "\n")
	if len(lines) > height {
		lines = lines[:height]
	}
	return strings.Join(lines, "\n")
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
		if m.screen != screenChat {
			return ""
		}
		content := renderOpenKyrozenBanner(m.motionFrame, !m.reducedMotion)
		if lipgloss.Width(content) > width || lipgloss.Height(content) > m.historyHeight() {
			content = brandStyle.Render(compactText("OPENKYROZEN", width))
		}
		return lipgloss.NewStyle().Width(width).MaxWidth(width).Height(m.historyHeight()).MaxHeight(m.historyHeight()).Align(lipgloss.Center, lipgloss.Center).Render(content)
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
		innerWidth := maxInt(1, width-5)
		if message.role == "assistant" && body != "" && !message.streaming {
			renderWidth := innerWidth
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
		if width < 12 {
			lines = append(lines, softStyle.Render(compactText(label+" "+body, width)))
			continue
		}
		block := labelStyle.Render(label)
		if body != "" {
			block += "\n" + body
		}
		boxWidth := maxInt(1, width-5)
		style := assistantStyle
		switch message.role {
		case "user":
			style = userStyle
		case "thinking":
			style = thinkingStyle
		case "receipt":
			style = receiptBoxStyle
		}
		lines = append(lines, style.Copy().Width(boxWidth).MaxWidth(boxWidth).Render(block))
	}
	return strings.Join(lines, "\n\n")
}
