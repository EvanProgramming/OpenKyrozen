package main

import (
	"strings"

	"charm.land/glamour/v2"
	"charm.land/glamour/v2/ansi"
	glamourstyles "charm.land/glamour/v2/styles"
	"charm.land/lipgloss/v2"
)

const (
	ink       = "#060A0D"
	deep      = "#0B1117"
	surface   = "#111A22"
	surfaceHi = "#192632"
	border    = "#587180"
	white     = "#F6FBFD"
	offWhite  = "#DBE7EC"
	muted     = "#A9BBC4"
	cyan      = "#2DE2E6"
	green     = "#7CE38B"
	amber     = "#F6C177"
	red       = "#F38BA8"
)

var (
	brandStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(cyan)).Bold(true)
	titleStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(white)).Bold(true)
	bodyStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color(white))
	softStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color(offWhite))
	mutedStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(muted))
	ruleStyle  = lipgloss.NewStyle().Foreground(lipgloss.Color(border))
	quietStyle = lipgloss.NewStyle().Background(lipgloss.Color(deep)).Padding(0, 1)
	focusStyle = lipgloss.NewStyle().Background(lipgloss.Color(surface)).BorderBottom(true).BorderForeground(lipgloss.Color(cyan)).Padding(0, 1)
	modalStyle = lipgloss.NewStyle().Background(lipgloss.Color(surface)).Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(border)).Padding(1, 2)

	contextStyle    = lipgloss.NewStyle().Background(lipgloss.Color(deep))
	badgeStyle      = lipgloss.NewStyle().Foreground(lipgloss.Color(cyan)).Background(lipgloss.Color(surfaceHi)).Bold(true).Padding(0, 1)
	sectionStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color(muted)).Bold(true)
	assistantStyle  = lipgloss.NewStyle().BorderLeft(true).BorderForeground(lipgloss.Color(border)).PaddingLeft(1)
	userStyle       = lipgloss.NewStyle().Background(lipgloss.Color(surface)).BorderLeft(true).BorderForeground(lipgloss.Color(cyan)).Padding(0, 1)
	thinkingStyle   = lipgloss.NewStyle().BorderLeft(true).BorderForeground(lipgloss.Color(amber)).PaddingLeft(1)
	receiptBoxStyle = lipgloss.NewStyle().Background(lipgloss.Color(deep)).Padding(0, 1)
	paletteStyle    = lipgloss.NewStyle().Background(lipgloss.Color(surface)).Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(border)).Padding(0, 1)
	composerStyle   = lipgloss.NewStyle().Background(lipgloss.Color(deep)).Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(border)).Padding(0, 1)
	activityStyle   = lipgloss.NewStyle().Background(lipgloss.Color(deep)).BorderLeft(true).BorderForeground(lipgloss.Color(border)).PaddingLeft(1)

	amberStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(amber))
	greenStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(green))
	redStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color(red))
)

func colorPtr(value string) *string { return &value }

func kyrozenMarkdownStyles() ansi.StyleConfig {
	style := glamourstyles.DarkStyleConfig
	style.Document.Color = colorPtr(white)
	style.Paragraph.Color = colorPtr(white)
	style.Text.Color = colorPtr(white)
	style.Heading.Color = colorPtr(cyan)
	style.Heading.Bold = boolPtr(true)
	for _, heading := range []*ansi.StyleBlock{&style.H1, &style.H2, &style.H3, &style.H4, &style.H5, &style.H6} {
		heading.Color = colorPtr(cyan)
		heading.BackgroundColor = nil
		heading.Bold = boolPtr(true)
	}
	style.Strong.Color = colorPtr(cyan)
	style.Strong.Bold = boolPtr(true)
	style.Emph.Color = colorPtr(offWhite)
	style.Link.Color = colorPtr(cyan)
	style.LinkText.Color = colorPtr(cyan)
	style.LinkText.Bold = boolPtr(true)
	style.HorizontalRule.Color = colorPtr(border)
	style.Code.Color = colorPtr(white)
	style.Code.BackgroundColor = colorPtr(surfaceHi)
	style.CodeBlock.Color = colorPtr(white)
	style.CodeBlock.BackgroundColor = colorPtr(surface)
	style.CodeBlock.Chroma = &ansi.Chroma{Text: ansi.StylePrimitive{Color: colorPtr(white)}}
	return style
}

var markdownStyles = kyrozenMarkdownStyles()

func boolPtr(value bool) *bool { return &value }

func renderMarkdown(content string, width int) string {
	if width < 20 {
		return softStyle.Copy().Width(maxInt(1, width)).Render(content)
	}
	renderer, err := glamour.NewTermRenderer(
		glamour.WithStyles(markdownStyles),
		glamour.WithWordWrap(width),
	)
	if err != nil {
		return softStyle.Render(content)
	}
	rendered, err := renderer.Render(content)
	if err != nil {
		return softStyle.Render(content)
	}
	return strings.TrimSpace(rendered)
}

func clampSize(value int) int {
	if value < 1 {
		return 1
	}
	return value
}

// fillBackground gives every cell an explicit dark background. This keeps
// off-white text readable in terminals with a light or transparent default.
func fillBackground(content string, width, height int) string {
	width, height = clampSize(width), clampSize(height)
	lines := strings.Split(content, "\n")
	if len(lines) > height {
		lines = lines[:height]
	}
	for len(lines) < height {
		lines = append(lines, "")
	}
	style := lipgloss.NewStyle().Background(lipgloss.Color(ink)).Foreground(lipgloss.Color(white)).Width(width).MaxWidth(width)
	for index, line := range lines {
		lines[index] = style.Render(line)
	}
	return strings.Join(lines, "\n")
}
