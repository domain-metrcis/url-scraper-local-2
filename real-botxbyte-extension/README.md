# BotXByte Workflow Runner

A minimal Chrome Extension for executing automated browser workflows via HTTP/curl commands.

## Architecture

```
┌─────────────┐      HTTP POST      ┌─────────────┐     WebSocket      ┌──────────────────┐
│   curl/API  │  ───────────────►   │  server.py  │  ──────────────►   │ Chrome Extension │
│             │  ◄───────────────   │  (Bridge)   │  ◄──────────────   │                  │
└─────────────┘      Response       └─────────────┘     Response       └──────────────────┘
                                    :8766 (HTTP)       :8765 (WS)
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Load Extension in Chrome

1. Open `chrome://extensions/`
2. Enable "Developer mode"
3. Click "Load unpacked"
4. Select this folder

### 3. Start the Bridge Server

```bash
python server.py
```

### 4. Execute Workflows

```bash
curl -X POST http://localhost:8766/workflow \
  -H 'Content-Type: application/json' \
  -d @workflow.json
```

---

## Workflow Schema

```json
{
  "url": "https://example.com",
  "variables": {
    "username": "user@example.com",
    "password": "secret"
  },
  "options": {
    "new_tab": true,
    "close_tab": false,
    "close_tab_on_error": false,
    "execution": {
      "stop_on_error": true
    }
  },
  "actions": [
    { "type": "wait_for", "selectors": ["#login"], "timeout": 5000 },
    { "type": "fill", "selectors": ["#email"], "value": "${username}" },
    { "type": "click", "selectors": ["#submit"] }
  ],
  "webhook": "https://webhook.site/xxx",
  "webhook_payload": { "status": "done", "user": "${username}" }
}
```

---

## Selectors

The extension supports three types of selectors:

### CSS Selectors (default)
```json
{ "selectors": ["#login-button", ".submit-btn", "button[type='submit']"] }
```

### Text-Based (XPath)
Find elements containing text:
```json
{ "selectors": ["text:Sign In", "text:Next", "text:Not now"] }
```

### Direct XPath
```json
{ "selectors": ["xpath://button[@aria-label='Close']", "xpath://div[@role='dialog']//button"] }
```

**Note:** Selectors are tried in order until one matches.

---

## Actions Reference

### Navigation

#### `navigate`
Navigate to a URL.
```json
{
  "type": "navigate",
  "url": "https://example.com/page"
}
```

---

### Wait Actions

#### `wait_for`
Wait for element to exist in DOM.
```json
{
  "type": "wait_for",
  "selectors": ["#element"],
  "timeout": 10000
}
```

#### `delay`
Wait for a fixed duration.
```json
{
  "type": "delay",
  "timeout": 2000
}
```

---

### Click Actions

#### `click`
Simple click on element.
```json
{
  "type": "click",
  "selectors": ["#button"],
  "wait_after": 1000
}
```

#### `mouse_click`
Realistic mouse click with coordinates.
```json
{
  "type": "mouse_click",
  "selectors": ["#button"],
  "wait_after": 500
}
```

#### `double_click`
Double-click on element.
```json
{
  "type": "double_click",
  "selectors": ["#item"],
  "wait_after": 500
}
```

---

### Input Actions

#### `fill`
Fill input field with text.
```json
{
  "type": "fill",
  "selectors": ["input[name='email']"],
  "value": "${email}",
  "human_typing": true,
  "delay": [50, 150]
}
```
- `human_typing`: Types character by character with random delays
- `delay`: `[min, max]` milliseconds between keystrokes

#### `clear`
Clear input field.
```json
{
  "type": "clear",
  "selectors": ["#search-input"]
}
```

#### `select`
Select dropdown option.
```json
{
  "type": "select",
  "selectors": ["select#country"],
  "value": "US"
}
```

#### `keyboard`
Press keyboard key.
```json
{
  "type": "keyboard",
  "key": "Enter",
  "selectors": ["#input"]
}
```
Supported keys: `Enter`, `Tab`, `Escape`, `Backspace`, `Space`, `ArrowUp`, `ArrowDown`, `ArrowLeft`, `ArrowRight`

#### `focus`
Focus an element.
```json
{
  "type": "focus",
  "selectors": ["#input-field"]
}
```

---

### Data Extraction

#### `extract`
Extract text from element into variable.
```json
{
  "type": "extract",
  "selectors": [".result-text"],
  "set_variable": "result"
}
```

#### `extract_all`
Extract data from multiple elements.
```json
{
  "type": "extract_all",
  "selectors": [".product-card"],
  "fields": {
    "title": [".title"],
    "price": [".price"],
    "link": ["a@href"]
  },
  "set_variable": "products",
  "limit": 50
}
```
- Use `@attribute` suffix to extract attribute value

#### `set_variable`
Set a variable value.
```json
{
  "type": "set_variable",
  "name": "status",
  "value": "completed"
}
```
Or from element:
```json
{
  "type": "set_variable",
  "name": "pageTitle",
  "selectors": ["h1"]
}
```

---

### Conditional Actions

#### `if_exists`
Execute action based on element existence.
```json
{
  "type": "if_exists",
  "selectors": ["#popup"],
  "timeout": 3000,
  "then": { "type": "click", "selectors": ["#close-popup"] },
  "else": { "type": "delay", "timeout": 100 }
}
```

#### `if_visible`
Execute action based on element visibility.
```json
{
  "type": "if_visible",
  "selectors": [".modal"],
  "timeout": 2000,
  "then": { "type": "click", "selectors": [".dismiss"] }
}
```

---

### Scroll & Hover

#### `scroll`
Scroll the page.
```json
{
  "type": "scroll",
  "direction": "down",
  "amount": 500
}
```

#### `hover`
Hover over element.
```json
{
  "type": "hover",
  "selectors": [".dropdown-trigger"]
}
```

---

### Advanced Actions

#### `evaluate`
Execute custom JavaScript.
```json
{
  "type": "evaluate",
  "script": "document.title",
  "set_variable": "pageTitle"
}
```

#### `screenshot`
Take screenshot placeholder (returns timestamp ID).
```json
{
  "type": "screenshot",
  "selectors": ["body"],
  "set_variable": "screenshot_id"
}
```

#### `iframe`
Access iframe content (same-origin only).
```json
{
  "type": "iframe",
  "selectors": ["#myframe"]
}
```

---

## Variables

Variables can be used throughout the workflow using `${variableName}` syntax.

### Defining Variables
```json
{
  "variables": {
    "email": "user@example.com",
    "searchTerm": "laptop"
  }
}
```

### Using Variables
```json
{ "type": "fill", "selectors": ["#email"], "value": "${email}" }
{ "type": "navigate", "url": "https://example.com/search?q=${searchTerm}" }
```

### Extracting to Variables
```json
{ "type": "extract", "selectors": [".result"], "set_variable": "result" }
{ "type": "set_variable", "name": "status", "value": "done" }
```

---

## Workflow Options

```json
{
  "options": {
    "new_tab": true,
    "close_tab": false,
    "close_tab_on_error": false,
    "execution": {
      "stop_on_error": true
    }
  }
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `new_tab` | `true` | Open workflow in new tab |
| `close_tab` | `false` | Close tab when workflow completes |
| `close_tab_on_error` | `false` | Close tab if workflow fails |
| `stop_on_error` | `false` | Stop workflow on first error |

---

## Webhooks

Send results to a webhook when workflow completes:

```json
{
  "webhook": "https://webhook.site/xxx",
  "webhook_payload": {
    "status": "completed",
    "extracted_data": "${data}",
    "timestamp": "${timestamp}"
  }
}
```

---

## Response Format

```json
{
  "success": true,
  "message": "Workflow completed",
  "variables": {
    "email": "user@example.com",
    "extracted_value": "some text"
  },
  "logs": [
    { "time": "2026-02-10T12:00:00.000Z", "type": "workflow", "message": "Starting workflow" },
    { "time": "2026-02-10T12:00:01.000Z", "type": "action", "message": "click -> SUCCESS" }
  ]
}
```

---

## Example Workflows

### Google Search
```json
{
  "url": "https://www.google.com",
  "variables": { "query": "chrome extension automation" },
  "actions": [
    { "type": "wait_for", "selectors": ["textarea[name='q']"], "timeout": 5000 },
    { "type": "fill", "selectors": ["textarea[name='q']"], "value": "${query}", "human_typing": true },
    { "type": "keyboard", "key": "Enter" },
    { "type": "wait_for", "selectors": ["#search"], "timeout": 10000 },
    { "type": "extract_all", "selectors": [".g"], "fields": { "title": ["h3"], "url": ["a@href"] }, "set_variable": "results", "limit": 10 }
  ]
}
```

### Form Login
```json
{
  "url": "https://example.com/login",
  "variables": {
    "email": "user@example.com",
    "password": "secret123"
  },
  "options": { "close_tab_on_error": true },
  "actions": [
    { "type": "wait_for", "selectors": ["#email"], "timeout": 5000 },
    { "type": "fill", "selectors": ["#email"], "value": "${email}", "human_typing": true },
    { "type": "fill", "selectors": ["#password"], "value": "${password}", "human_typing": true },
    { "type": "click", "selectors": ["button[type='submit']"] },
    { "type": "wait_for", "selectors": [".dashboard", ".welcome"], "timeout": 10000 }
  ]
}
```

### Handle Popups
```json
{
  "url": "https://example.com",
  "actions": [
    { "type": "wait_for", "selectors": ["body"], "timeout": 5000 },
    { 
      "type": "if_exists", 
      "selectors": [".cookie-popup", "#gdpr-banner"],
      "timeout": 3000,
      "then": { "type": "click", "selectors": ["text:Accept", "text:Got it", ".accept-btn"] }
    },
    { "type": "click", "selectors": ["#main-action"] }
  ]
}
```

---

## Debugging

### View Extension Logs
1. Go to `chrome://extensions/`
2. Click "Inspect views: service worker" under the extension
3. Check Console tab for logs

### View Content Script Logs
1. Open DevTools on the page (F12)
2. Check Console tab for `[action]`, `[find]`, `[error]` logs

### Response Logs
Every response includes a `logs` array with detailed execution trace:
```bash
curl ... | jq '.logs'
```

---

## Troubleshooting

### "Extension not connected" error
- Make sure the extension is loaded and enabled
- Check that server.py is running
- Reload the extension from `chrome://extensions/`

### Element not found
- Try different selectors (CSS, text:, xpath:)
- Add `wait_for` before interacting with elements
- Increase timeout values
- Check if element is in an iframe

### Actions failing after navigation
- Add `wait_for` after any action that causes page navigation
- Use `navigate` action explicitly for URL changes

### Service worker going to sleep
- The extension uses `chrome.alarms` to keep alive
- If issues persist, reload the extension

---

## Files Structure

```
real-botxbyte-extension/
├── manifest.json      # Extension configuration
├── background.js      # Service worker (WebSocket, workflow orchestration)
├── content.js         # DOM action handlers
├── server.py          # HTTP→WebSocket bridge
├── requirements.txt   # Python dependencies
└── README.md          # This documentation
```

---

## API Reference

### HTTP Endpoint

**POST** `http://localhost:8766/workflow`

**Headers:**
- `Content-Type: application/json`

**Body:** Workflow JSON (see schema above)

**Response:** JSON with `success`, `variables`, `logs`

### Status Codes

| Code | Description |
|------|-------------|
| 200 | Workflow completed |
| 503 | Extension not connected |
| 504 | Workflow timeout (120s) |

---

## License

MIT
- `icons/icon16.png` (16x16)
- `icons/icon48.png` (48x48)
- `icons/icon128.png` (128x128)
