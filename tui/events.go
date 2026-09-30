package main

import (
	"fmt"
	"strings"
	"time"
)

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
		m.activeSessionID = firstNonEmpty(stringValue(event, "session_id"), m.activeSessionID)
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
	case "navigation":
		m.applyNavigation(event)
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
	case "onboarding_complete":
		m.completeOnboarding()
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
	m.fastBackend = firstNonEmpty(stringValue(value, "system_one_backend"), stringValue(value, "fast_backend"), "off")
	if assist, ok := value["decision_assist"].(map[string]any); ok {
		m.decisionAssistBackend = firstNonEmpty(stringValue(assist, "backend"), "off")
		m.decisionAssistConsent = boolValue(assist, "kev_private_consent")
		m.systemOneModel = firstNonEmpty(stringValue(assist, "jev_model_alias"), "jev-latest")
		m.systemOneRelease = firstNonEmpty(stringValue(assist, "jev_model_release_date"), "release unknown")
		m.systemOneHealth = firstNonEmpty(stringValue(assist, "jev_health"), "unknown")
		currentBackend := firstNonEmpty(stringValue(value, "system_one_backend"), stringValue(value, "fast_backend"), "off")
		if calibration, ok := assist["calibration"].(map[string]any); ok {
			validated := false
			if backends, ok := calibration["backends"].(map[string]any); ok {
				if policies, ok := backends[currentBackend].(map[string]any); ok {
					for _, rawPolicy := range policies {
						if policy, ok := rawPolicy.(map[string]any); ok && boolValue(policy, "validated") {
							validated = true
						}
					}
				}
			}
			if validated {
				m.systemOneCalibration = "available"
			} else {
				m.systemOneCalibration = "not calibrated"
			}
		}
	}
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

func (m *model) applyNavigation(event backendEvent) {
	m.activeSessionID = stringValue(event, "active_session_id")
	m.activeScopeID = stringValue(event, "active_scope_id")
	m.workspace = firstNonEmpty(stringValue(event, "workspace"), m.workspace)
	m.navigation = nil
	if groups, ok := event["groups"].([]any); ok {
		for _, raw := range groups {
			item, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			group := navigationGroup{scope: stringValue(item, "scope"), scopeID: stringValue(item, "scope_id"), name: stringValue(item, "name"), path: stringValue(item, "path")}
			if chats, ok := item["chats"].([]any); ok {
				for _, rawChat := range chats {
					if chat, ok := rawChat.(map[string]any); ok {
						group.chats = append(group.chats, navigationChat{id: stringValue(chat, "session_id"), title: stringValue(chat, "title"), updatedAt: stringValue(chat, "updated_at")})
					}
				}
			}
			m.navigation = append(m.navigation, group)
		}
	}
	m.navigationIndex = 0
	for index, target := range m.navigationTargets() {
		if target.scopeID == m.activeScopeID && target.id == m.activeSessionID {
			m.navigationIndex = index
			break
		}
	}
	m.messages = nil
	if messages, ok := event["messages"].([]any); ok {
		for _, raw := range messages {
			if item, ok := raw.(map[string]any); ok {
				m.messages = append(m.messages, chatMessage{role: stringValue(item, "role"), text: stringValue(item, "text")})
			}
		}
	}
	m.syncViewport()
}

func (m *model) handlePrompt(event backendEvent) {
	switch stringValue(event, "kind") {
	case "onboarding":
		m.onboardingKind = firstNonEmpty(stringValue(event, "onboarding"), m.onboardingKind)
		m.onboardingPreviousVersion = stringValue(event, "previous_version")
		m.onboardingWaiting = false
		m.screen = screenOnboarding
		m.startTransition()
	case "api_key":
		m.fastKeyInput = false
		m.onboardingWaiting = false
		m.screen = screenAPIKey
		m.startTransition()
		m.apiInput.Reset()
		m.apiInput.Placeholder = firstNonEmpty(stringValue(event, "message"), "Enter API key")
		m.apiInput.Focus()
	case "fast_key":
		m.fastKeyInput = true
		m.decisionAssistKeyInput = false
		m.screen = screenAPIKey
		m.apiInput.Reset()
		m.apiInput.Placeholder = "Enter Jev API key for System One (paid TypeSafe calls)"
		m.apiInput.Focus()
	case "decision_assist_key":
		m.fastKeyInput = false
		m.decisionAssistKeyInput = true
		m.screen = screenAPIKey
		m.apiInput.Reset()
		m.apiInput.Placeholder = "Enter Jev API key (paid TypeSafe calls)"
		m.apiInput.Focus()
	case "provider":
		m.onboardingWaiting = false
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
		m.onboardingSelfLearning = boolValue(event, "onboarding")
		m.onboardingWaiting = false
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
	case "project":
		m.screen = screenProject
		m.projectInput.Reset()
		m.projectInput.Focus()
		m.startTransition()
	case "sessions":
		m.navigationOpen, m.navigationFocused = true, true
		m.input.Blur()
	}
}

func (m *model) completeOnboarding() {
	settings := loadUISettings()
	settings.LastSeenVersion = version
	settings.OnboardingComplete = true
	if err := saveUISettings(settings); err != nil {
		m.setError("Could not save onboarding state: " + err.Error())
		return
	}
	m.onboardingKind = ""
	m.onboardingPreviousVersion = ""
	m.onboardingWaiting = false
	m.onboardingSelfLearning = false
	m.screen = screenChat
	m.status = "Ready"
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
