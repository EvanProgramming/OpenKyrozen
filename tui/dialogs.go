package main

import (
	"charm.land/lipgloss/v2"
	"fmt"
	"strings"
)

func (m model) settingsRow(index int, label, value string) string {
	cursor, labelStyle := mutedStyle.Render("·"), softStyle
	if index == m.settingsIdx {
		cursor, labelStyle = brandStyle.Render("›"), titleStyle
	}
	return cursor + " " + labelStyle.Render(label) + "  " + mutedStyle.Render(value)
}

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
	case screenOnboarding:
		if m.onboardingKind == "update" {
			previous := firstNonEmpty(m.onboardingPreviousVersion, "an earlier release")
			body = strings.Join([]string{
				brandStyle.Render("OPENKYROZEN UPDATE"),
				titleStyle.Render("Your setup is already here"),
				softStyle.Render(fmt.Sprintf("Updated from %s to %s.", previous, version)),
				mutedStyle.Render("Your provider, workspace, memory, and settings stay in place."),
				"",
				greenStyle.Render("Enter  continue"),
				mutedStyle.Render("Ctrl+C  quit"),
			}, "\n")
		} else {
			body = strings.Join([]string{
				brandStyle.Render("WELCOME TO OPENKYROZEN"),
				titleStyle.Render("Let’s get your workspace ready"),
				softStyle.Render("This first-run setup takes care of the provider and self-learning choices."),
				mutedStyle.Render("Nothing is sent anywhere until you choose a provider and start chatting."),
				"",
				brandStyle.Render("1") + "  Choose a provider",
				brandStyle.Render("2") + "  Add a key if that provider needs one",
				brandStyle.Render("3") + "  Choose local or remote self-learning",
				"",
				greenStyle.Render("Enter  begin setup"),
				mutedStyle.Render("Ctrl+C  quit"),
			}, "\n")
		}
	case screenAPIKey:
		body = brandStyle.Render("API KEY SETUP") + "\n" + titleStyle.Render("Add your API key") + "\n" + mutedStyle.Render("Your key is masked and stored encrypted locally.") + "\n\n" + focusStyle.Copy().Width(maxInt(1, m.width-12)).MaxWidth(maxInt(1, m.width-12)).Render(m.apiInput.View()) + "\n\n" + mutedStyle.Render("Enter confirm  ·  Esc cancel")
	case screenProject:
		body = brandStyle.Render("NEW PROJECT") + "\n" + titleStyle.Render("Create or open a project directory") + "\n" + mutedStyle.Render("Enter a folder path. OpenKyrozen creates it if needed and starts a new chat.") + "\n\n" + focusStyle.Copy().Width(maxInt(1, m.width-12)).MaxWidth(maxInt(1, m.width-12)).Render(m.projectInput.View()) + "\n\n" + mutedStyle.Render("Enter create / open  ·  Esc cancel")
	case screenApproval:
		body = amberStyle.Render("!  APPROVAL REQUIRED") + "\n" + titleStyle.Render("Confirm this action") + "\n\n" + softStyle.Render(m.approvalTool) + "\n" + softStyle.Render(m.approvalArgs) + "\n\n" + mutedStyle.Render("This may change local or remote state.") + "\n\n" + greenStyle.Render("Y / Enter  approve") + "    " + redStyle.Render("N / Esc  deny")
	case screenSelfLearning:
		if m.onboardingSelfLearning {
			localCursor, localStyle := mutedStyle.Render("·"), softStyle
			remoteCursor, remoteStyle := mutedStyle.Render("·"), softStyle
			if m.featureIdx == 0 {
				localCursor, localStyle = brandStyle.Render("›"), titleStyle
			} else {
				remoteCursor, remoteStyle = brandStyle.Render("›"), titleStyle
			}
			body = strings.Join([]string{
				brandStyle.Render("MEMORY"),
				titleStyle.Render("Choose self-learning"),
				mutedStyle.Render("Pick how Kyrozen should learn during setup."),
				"",
				mutedStyle.Render("↑↓ select  Enter continue  L/R choose directly  Esc cancel"),
				localCursor + " " + localStyle.Render("L  Local Qwen2.5"),
				softStyle.Render("   Private, CPU/RAM/disk only; no API cost."),
				remoteCursor + " " + remoteStyle.Render("R  Remote provider-backed"),
				softStyle.Render("   Uses your configured provider and API key."),
				"",
				greenStyle.Render("Choose one to finish setup."),
			}, "\n")
			break
		}
		runtime := firstNonEmpty(m.learningMode, "setup_required") + " / " + firstNonEmpty(m.learningStatus, "setup_required")
		if m.learningModel != "" {
			runtime += " / " + m.learningModel
		}
		hint := "L local free Qwen2.5  ·  R remote API  ·  ↑↓ select  Space toggle  Esc close"
		if m.onboardingSelfLearning {
			hint = "L local free Qwen2.5  ·  R remote provider-backed learning  ·  choose one to finish setup"
		}
		lines := []string{brandStyle.Render("MEMORY"), titleStyle.Render("Self-learning settings"), mutedStyle.Render(runtime), mutedStyle.Render(m.learningDetail), mutedStyle.Render(m.learningCostSource), mutedStyle.Render(hint), ""}
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
		usageScope := firstNonEmpty(m.usageScope, "workspace")
		learning := firstNonEmpty(m.learningMode, "not configured") + " · " + firstNonEmpty(m.learningStatus, "waiting")
		toolDetails := "HIDDEN"
		if m.showToolDetails {
			toolDetails = "VISIBLE"
		}
		consent := "OFF"
		if m.decisionAssistConsent {
			consent = "ON"
		}
		body = strings.Join([]string{
			brandStyle.Render("SETTINGS"),
			titleStyle.Render("Controls and session"),
			mutedStyle.Render("↑↓ select  ←→/Space change  ·  Esc close"),
			"",
			brandStyle.Render("CONTROLS"),
			m.settingsRow(0, "Show tool details", toolDetails),
			m.settingsRow(1, "Interaction mode", strings.ToUpper(firstNonEmpty(m.interactionMode, "auto"))),
			m.settingsRow(2, "System One backend", strings.ToUpper(firstNonEmpty(m.fastBackend, "off"))),
			m.settingsRow(3, "Decision Assist", strings.ToUpper(firstNonEmpty(m.decisionAssistBackend, "off"))),
			m.settingsRow(4, "Kev private context", consent),
			mutedStyle.Render("Jev uses paid TypeSafe calls; Kev is local and less accurate. Kev needs private-context consent."),
			mutedStyle.Render("/fast remains a legacy command alias for System One."),
			mutedStyle.Render("Jev model " + firstNonEmpty(m.systemOneModel, "jev-latest") + " · release " + firstNonEmpty(m.systemOneRelease, "release unknown") + " · health " + firstNonEmpty(m.systemOneHealth, "unknown") + " · calibration " + firstNonEmpty(m.systemOneCalibration, "not calibrated")),
			"",
			brandStyle.Render("SESSION"),
			softStyle.Render("MODEL  " + compactText(firstNonEmpty(m.provider, "pending")+" · "+firstNonEmpty(m.modelName, "pending"), modalWidth-10)),
			softStyle.Render("USAGE  " + compactText(m.usageSummary()+" · "+usageScope, modalWidth-10)),
			softStyle.Render("MEMORY  " + compactText(learning, modalWidth-10)),
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
