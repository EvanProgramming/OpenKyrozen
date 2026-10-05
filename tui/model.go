package main

import (
	"charm.land/bubbles/v2/textarea"
	"charm.land/bubbles/v2/textinput"
	"charm.land/bubbles/v2/viewport"
	"regexp"
	"time"
)

var version = "2.0.8"

var openKyrozenBanner = []string{
	" ███  ████  █████ █   █ █   █ █   █ ████   ███  █████ █████ █   █",
	"█   █ █   █ █     ██  █ █  █  █   █ █   █ █   █     █ █     ██  █",
	"█   █ █   █ █     ██  █ █ █    █ █  █   █ █   █    █  █     ██  █",
	"█   █ ████  ████  █ █ █ ██      █   ████  █   █   █   ████  █ █ █",
	"█   █ █     █     █  ██ █ █     █   █ █   █   █  █    █     █  ██",
	"█   █ █     █     █  ██ █  █    █   █  █  █   █ █     █     █  ██",
	" ███  █     █████ █   █ █   █   █   █   █  ███  █████ █████ █   █",
}

type screen string

const (
	screenSplash         screen = "splash"
	screenOnboarding     screen = "onboarding"
	screenChat           screen = "chat"
	screenProvider       screen = "provider"
	screenAPIKey         screen = "api_key"
	screenModel          screen = "model"
	screenCustomProvider screen = "custom_provider"
	screenApproval       screen = "approval"
	screenSelfLearning   screen = "self_learning"
	screenMode           screen = "mode"
	screenPermissions    screen = "permissions"
	screenQuestion       screen = "question"
	screenPlan           screen = "plan"
	screenGraph          screen = "graph"
	screenAgents         screen = "agents"
	screenGithubAuth     screen = "github_auth"
	screenSettings       screen = "settings"
	screenProject        screen = "project"
	screenDeleteConfirm  screen = "delete_confirm"
	screenUpdating       screen = "updating"
	screenError          screen = "error"
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

type navigationChat struct{ id, title, updatedAt string }

type navigationGroup struct {
	scope, scopeID, name, path string
	chats                      []navigationChat
}

type featureItem struct {
	name          string
	enabled       bool
	description   string
	status        string
	productStatus string
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
	bridge                    *bridge
	project                   string
	global                    bool
	width                     int
	height                    int
	view                      viewport.Model
	input                     textarea.Model
	apiInput                  textinput.Model
	fastKeyInput              bool
	decisionAssistKeyInput    bool
	permissionKeyInput        bool
	decisionAssistBackend     string
	decisionAssistConsent     bool
	jeVConfigured             bool
	systemOneModel            string
	systemOneRelease          string
	systemOneHealth           string
	systemOneCalibration      string
	graphInput                textinput.Model
	projectInput              textinput.Model
	screen                    screen
	status                    string
	provider                  string
	modelName                 string
	mainModel                 string
	modelPromptMessage        string
	modelPromptReturn         screen
	workspace                 string
	activeSessionID           string
	activeScopeID             string
	navigation                []navigationGroup
	navigationIndex           int
	navigationOpen            bool
	navigationFocused         bool
	pendingDeleteKind         string
	pendingDeleteScopeID      string
	pendingDeleteSessionID    string
	pendingDeletePath         string
	pendingDeleteTitle        string
	messages                  []chatMessage
	tasks                     []taskItem
	agents                    []map[string]any
	agentSelected             int
	agentScroll               int
	palette                   []command
	paletteIndex              int
	providerList              []string
	providerIdx               int
	features                  []featureItem
	featureIdx                int
	learningMode              string
	learningStatus            string
	learningModel             string
	learningDetail            string
	learningCostSource        string
	usageScope                string
	usageAttempts             int
	usagePromptTokens         int
	usageCompletionTokens     int
	usageReasoningTokens      int
	usageCostPicos            int
	interactionMode           string
	fastBackend               string
	effectiveMode             string
	modeIdx                   int
	permissionMode            string
	permissionStatus          string
	permissionIdx             int
	pendingQuestion           *questionRequest
	questionIdx               int
	choiceIdx                 int
	questionAnswers           map[string]any
	pendingPlan               *planProposal
	planScroll                int
	graph                     graphSnapshot
	graphSelected             int
	graphZoom                 int
	graphCommunity            int
	graphSearching            bool
	graphPathStart            string
	githubBinary              string
	githubHostname            string
	approvalID                string
	approvalTool              string
	approvalArgs              string
	errorText                 string
	splashFrame               int
	splashStarted             time.Time
	readyAt                   time.Time
	backendReady              bool
	motionFrame               int
	cursorVisible             bool
	thinkingText              string
	taskFlashID               string
	taskFlashTick             int
	followTail                bool
	transitionTick            int
	reducedMotion             bool
	showToolDetails           bool
	settingsIdx               int
	updateInProgress          bool
	availableVersion          string
	onboardingKind            string
	onboardingPreviousVersion string
	onboardingWaiting         bool
	onboardingSelfLearning    bool
	busy                      bool
	requestCount              int
	restart                   bool
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
