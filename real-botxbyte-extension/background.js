// Parallel workflow support - each workflow has its own state
console.log('=== BACKGROUND.JS LOADED v7 (CDP click + Turnstile Click Queue + GeoSpoof) ===');
const runningWorkflows = new Map(); // workflowId -> { variables, logs, tabId }
const popupTracking = new Map(); // tabId -> { parentTabId, workflowId, createdAt, windowId }
let ws = null;
let reconnectDelay = 1000;
const WS_URL = 'ws://localhost:8765';
const MAX_RECONNECT_DELAY = 30000;

// === Turnstile Click Queue System ===
// When multiple tabs need to click Turnstile, they queue up and get processed one at a time
// This ensures each tab gets focused before the CDP click is attempted
const turnstileClickQueue = []; // Array of { tabId, payload, resolve, reject, addedAt }
let turnstileClickProcessing = false;
const TURNSTILE_POST_CLICK_WAIT = 3000; // Wait 3 seconds after click to let Turnstile verify

async function processTurnstileClickQueue() {
  if (turnstileClickProcessing) return;
  turnstileClickProcessing = true;

  while (turnstileClickQueue.length > 0) {
    const item = turnstileClickQueue.shift();
    const { tabId, payload, resolve, reject, addedAt } = item;

    console.log(`[TurnstileClickQueue] Processing tab ${tabId}, queue length remaining: ${turnstileClickQueue.length}`);

    try {
      // Check if tab still exists
      try {
        await chrome.tabs.get(tabId);
      } catch (e) {
        console.log(`[TurnstileClickQueue] Tab ${tabId} closed, skipping`);
        resolve({ success: true, skipped: true, reason: 'tab_closed' });
        continue;
      }

      // Focus the tab and window
      console.log(`[TurnstileClickQueue] Focusing tab ${tabId}...`);
      await chrome.tabs.update(tabId, { active: true });
      const tab = await chrome.tabs.get(tabId);
      if (tab.windowId) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }

      // Wait for focus to apply
      await new Promise(r => setTimeout(r, 500));

      console.log(`[TurnstileClickQueue] Tab ${tabId} focused, attempting CDP click...`);

      // Perform the CDP click
      await turnstileAttachDebugger(tabId);
      const send = (m, p) => chrome.debugger.sendCommand({ tabId }, m, p);
      await send("Page.enable");
      await send("Runtime.enable");
      await send("DOM.enable");

      const result = await findTurnstileIframeAndClick(tabId, payload);

      // Detach debugger
      try {
        await chrome.debugger.detach({ tabId });
      } catch (e) {
        // Ignore detach errors
      }

      console.log(`[TurnstileClickQueue] Tab ${tabId} click result:`, result);

      // Wait a bit after click for Turnstile to process
      await new Promise(r => setTimeout(r, TURNSTILE_POST_CLICK_WAIT));

      resolve(result);

    } catch (error) {
      console.error(`[TurnstileClickQueue] Tab ${tabId} error:`, error.message);
      try {
        await chrome.debugger.detach({ tabId });
      } catch (e) {
        // Ignore
      }
      reject(error);
    }
  }

  turnstileClickProcessing = false;
  console.log('[TurnstileClickQueue] Queue empty, processing stopped');
}

function addToTurnstileClickQueue(tabId, payload) {
  return new Promise((resolve, reject) => {
    const queueItem = { tabId, payload, resolve, reject, addedAt: Date.now() };
    turnstileClickQueue.push(queueItem);
    console.log(`[TurnstileClickQueue] Tab ${tabId} added to click queue, position: ${turnstileClickQueue.length}`);

    // Start processing if not already running
    processTurnstileClickQueue();
  });
}

// === Turnstile Focus Queue System (for evaluate scripts) ===
// When multiple tabs need to solve Turnstile, they queue up and get focused one at a time
const turnstileFocusQueue = []; // Array of { tabId, resolve, reject, addedAt }
let turnstileFocusProcessing = false;
const TURNSTILE_FOCUS_DURATION = 12000; // 12 seconds focus per tab
const TURNSTILE_FOCUS_CHECK_INTERVAL = 500; // Check every 500ms if solved

async function processTurnstileFocusQueue() {
  if (turnstileFocusProcessing) return;
  turnstileFocusProcessing = true;

  while (turnstileFocusQueue.length > 0) {
    const item = turnstileFocusQueue.shift();
    const { tabId, resolve, reject, addedAt } = item;

    console.log(`[TurnstileFocusQueue] Processing tab ${tabId}, queue length: ${turnstileFocusQueue.length}`);

    try {
      // Check if tab still exists
      try {
        await chrome.tabs.get(tabId);
      } catch (e) {
        console.log(`[TurnstileFocusQueue] Tab ${tabId} closed, skipping`);
        resolve({ solved: true, reason: 'tab_closed' });
        continue;
      }

      // Focus the tab
      await chrome.tabs.update(tabId, { active: true });
      const tab = await chrome.tabs.get(tabId);
      if (tab.windowId) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }

      console.log(`[TurnstileFocusQueue] Tab ${tabId} focused, waiting for Turnstile solve`);

      // Wait for Turnstile to be solved or timeout
      const startTime = Date.now();
      let solved = false;
      let reason = 'timeout';

      while (Date.now() - startTime < TURNSTILE_FOCUS_DURATION) {
        // Check if tab still exists
        try {
          await chrome.tabs.get(tabId);
        } catch (e) {
          solved = true;
          reason = 'tab_closed';
          break;
        }

        // Check Turnstile status
        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId },
            func: () => {
              // Check for Turnstile success response token
              const responseInput = document.querySelector('[name="cf-turnstile-response"]') ||
                                    document.querySelector('input[name="cf-turnstile-response"]');
              if (responseInput && responseInput.value) return 'solved';

              // Check if modal appeared (for Ahrefs - means Turnstile passed)
              const modal = document.querySelector('.ReactModalPortal [role="dialog"]') ||
                           document.querySelector('[class*="ReactModal__Content"]');
              if (modal && modal.textContent.indexOf('Organic traffic') !== -1) return 'modal_appeared';
              if (modal && modal.textContent.indexOf('Domain Rating') !== -1) return 'modal_appeared';
              if (modal && modal.textContent.indexOf('Backlinks') !== -1) return 'modal_appeared';

              // Check if Turnstile iframe is still present
              const iframes = document.querySelectorAll('iframe');
              for (const iframe of iframes) {
                if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) return 'pending';
              }
              return 'no_turnstile';
            }
          });
          const status = results?.[0]?.result;

          if (status === 'solved' || status === 'modal_appeared' || status === 'no_turnstile') {
            solved = true;
            reason = status;
            break;
          }
        } catch (e) {
          // Script execution error, tab might be navigating
          console.log(`[TurnstileFocusQueue] Tab ${tabId} script error:`, e.message);
        }

        await new Promise(r => setTimeout(r, TURNSTILE_FOCUS_CHECK_INTERVAL));
      }

      const elapsed = Date.now() - startTime;
      console.log(`[TurnstileFocusQueue] Tab ${tabId} done: solved=${solved}, reason=${reason}, elapsed=${elapsed}ms`);

      resolve({ solved, reason, elapsed });

    } catch (e) {
      console.error(`[TurnstileFocusQueue] Tab ${tabId} error:`, e.message);
      reject(e);
    }
  }

  turnstileFocusProcessing = false;
  console.log('[TurnstileFocusQueue] Queue empty, processing stopped');
}

function addToTurnstileFocusQueue(tabId) {
  return new Promise((resolve, reject) => {
    const queueItem = { tabId, resolve, reject, addedAt: Date.now() };
    turnstileFocusQueue.push(queueItem);
    console.log(`[TurnstileFocusQueue] Tab ${tabId} added to queue, position: ${turnstileFocusQueue.length}`);

    // Start processing if not already running
    processTurnstileFocusQueue();
  });
}

// === Geo Spoofing Functions ===
let geoSpoofEnabled = false;

function genUULE(lat, lng) {
  const latE7 = Math.floor(lat * 1e7);
  const lngE7 = Math.floor(lng * 1e7);
  const decoded = `role: CURRENT_LOCATION\nproducer: DEVICE_LOCATION\nradius: 65000\nlatlng <\n  latitude_e7: ${latE7}\n  longitude_e7: ${lngE7}\n>`;
  return 'a ' + btoa(decoded);
}

// Generate UULE for URL parameter (Google's standard format using location name)
function genUuleParam(locationName) {
  if (!locationName) return '';
  const encoded = new TextEncoder().encode(locationName);
  const lengthChar = String.fromCharCode(encoded.length);
  return 'w+CAIQICI' + btoa(lengthChar + locationName);
}

// Clear all Google location-related cookies so stale location doesn't persist
async function clearGoogleLocationCookies() {
  try {
    const domains = ['.google.com', 'www.google.com', 'google.com'];
    const locationCookieNames = ['UULE', 'NID', '1P_JAR', 'OGP', 'OGPC', 'AEC'];
    for (const domain of domains) {
      for (const name of locationCookieNames) {
        const cookies = await chrome.cookies.getAll({ domain, name });
        for (const cookie of cookies) {
          const url = 'https://' + cookie.domain.replace(/^\./, '') + cookie.path;
          await chrome.cookies.remove({ name: cookie.name, url });
        }
      }
    }
    console.log('[GeoSpoof] Cleared Google location cookies');
  } catch (e) {
    console.warn('[GeoSpoof] Cookie clear error:', e.message);
  }
}

async function enableGeoSpoof(latitude, longitude, hl = 'en', gl = 'US') {
  // Clear stale location cookies from previous sessions
  await clearGoogleLocationCookies();

  geoSpoofEnabled = true;
  // Single atomic call: remove old rule + add new rule
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [100],
    addRules: [{
      id: 100,
      priority: 1,
      action: {
        type: "modifyHeaders",
        requestHeaders: [
          { header: "x-geo", operation: "set", value: genUULE(latitude, longitude) },
          { header: "accept-language", operation: "set", value: `${hl}-${gl}` }
        ]
      },
      condition: {
        urlFilter: "google.com/",
        resourceTypes: ["main_frame", "sub_frame", "image", "xmlhttprequest", "ping"]
      }
    }]
  });

  // Verify the rule was actually applied
  const rules = await chrome.declarativeNetRequest.getSessionRules();
  const active = rules.find(r => r.id === 100);
  console.log('[GeoSpoof] Enabled:', { latitude, longitude, hl, gl, ruleActive: !!active });

  // Small delay to ensure rule propagates to network stack
  await new Promise(r => setTimeout(r, 100));
}

async function disableGeoSpoof() {
  geoSpoofEnabled = false;
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [100]
  });
  // Clear stale location cookies so real location is used
  await clearGoogleLocationCookies();
  console.log('[GeoSpoof] Disabled');
}

// === CSP Bypass Helpers ===

function isCSPError(...errorMessages) {
  const cspPatterns = [
    /content.?security.?policy/i,
    /unsafe-eval/i,
    /\bEvalError\b/,
    /call to eval\(\) blocked/i,
    /refused to evaluate/i
  ];
  return errorMessages.some(msg =>
    msg && cspPatterns.some(pattern => pattern.test(msg))
  );
}

async function cdpEvaluate(tabId, scriptCode) {
  try {
    await turnstileAttachDebugger(tabId);
    try {
      await chrome.debugger.sendCommand({ tabId }, 'Runtime.enable');
      const expression = '(async () => { ' + scriptCode + ' })()';
      const evalResult = await chrome.debugger.sendCommand(
        { tabId },
        'Runtime.evaluate',
        {
          expression: expression,
          returnByValue: true,
          awaitPromise: true,
          userGesture: true
        }
      );
      if (evalResult.exceptionDetails) {
        const errorMsg = evalResult.exceptionDetails.exception?.description
          || evalResult.exceptionDetails.text
          || 'CDP evaluate error';
        return { success: false, error: errorMsg };
      }
      const value = evalResult.result?.value;
      return {
        success: true,
        value: typeof value === 'string' ? value : JSON.stringify(value)
      };
    } finally {
      try { await chrome.debugger.detach({ tabId }); } catch (e) { /* ignore */ }
    }
  } catch (e) {
    return { success: false, error: 'CDP fallback failed: ' + e.message };
  }
}

// === Turnstile Auto-Click CDP Functions ===

async function turnstileAttachDebugger(tabId, maxRetries = 3, retryDelay = 500) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      await new Promise((resolve, reject) => {
        chrome.debugger.attach({ tabId }, '1.3', () => {
          if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
          else resolve();
        });
      });
      return;
    } catch (error) {
      if (error.message.includes('Already attached')) return;
      console.warn(`[Turnstile] Attach attempt ${attempt}/${maxRetries} failed:`, error.message);
      if (attempt < maxRetries) {
        await new Promise(r => setTimeout(r, retryDelay));
      } else {
        throw new Error(`Failed to attach debugger after ${maxRetries} attempts.`);
      }
    }
  }
}

async function findTurnstileIframeAndClick(tabId, payload) {
  const { xRatio, yRatio } = payload;
  const maxRetries = 3;
  const retryDelay = 1000;

  for (let i = 0; i < maxRetries; i++) {
    try {
      const getAttr = (attrs, name) => {
        if (!attrs) return undefined;
        for (let j = 0; j < attrs.length; j += 2) {
          if (attrs[j] === name) return attrs[j + 1];
        }
      };

      const { nodes } = await chrome.debugger.sendCommand({ tabId }, "DOM.getFlattenedDocument", {
        depth: -1, pierce: true
      });

      const iframeNode = nodes.find(n => {
        if (n.nodeName !== 'IFRAME') return false;
        const src = getAttr(n.attributes, 'src') || '';
        return src.includes('challenges.cloudflare.com');
      });

      if (!iframeNode) throw new Error('Turnstile iframe not found in flattened document');

      const { model: iframeBox } = await chrome.debugger.sendCommand({ tabId }, "DOM.getBoxModel", {
        nodeId: iframeNode.nodeId
      });

      const [x_start, y_start, , , x_end, y_end] = iframeBox.content;
      const clickX = x_start + ((x_end - x_start) * xRatio);
      const clickY = y_start + ((y_end - y_start) * yRatio);

      await turnstileClickAt(tabId, clickX, clickY);
      return { success: true };
    } catch (error) {
      console.warn(`[Turnstile] Attempt ${i + 1} error:`, error.message);
      if (i < maxRetries - 1) {
        await new Promise(r => setTimeout(r, retryDelay));
      }
    }
  }
  return { success: false, error: 'Failed to click Turnstile after all retries' };
}

async function turnstileClickAt(tabId, x, y) {
  const dispatch = (type, button) => {
    return chrome.debugger.sendCommand({ tabId }, "Input.dispatchMouseEvent", {
      type, x, y, button, buttons: button === "left" ? 1 : 0, clickCount: 1
    });
  };
  await dispatch("mousePressed", "left");
  await new Promise(r => setTimeout(r, Math.random() * 30 + 20));
  await dispatch("mouseReleased", "left");
}

// Connect to WebSocket server
function connectWebSocket() {
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    console.error('WebSocket create error:', e);
    scheduleReconnect();
    return;
  }
  
  ws.onopen = () => {
    console.log('✅ Connected to server');
    reconnectDelay = 1000;
  };
  
  ws.onmessage = async (event) => {
    try {
      const msg = JSON.parse(event.data);
      
      if (msg.action === 'startWorkflow' && msg.workflow) {
        // Run workflow without blocking - allows parallel execution
        handleStartWorkflow(msg.id, msg.workflow).then(result => {
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ id: msg.id, ...result }));
          }
        }).catch(e => {
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ id: msg.id, success: false, error: e.message }));
          }
        });
      }
      
      if (msg.action === 'ping') {
        ws.send(JSON.stringify({ id: msg.id, success: true, message: 'pong' }));
      }
      
      if (msg.action === 'getStatus') {
        ws.send(JSON.stringify({ 
          id: msg.id, 
          success: true, 
          running: runningWorkflows.size,
          workflows: [...runningWorkflows.keys()]
        }));
      }
      
      if (msg.action === 'cancelWorkflow' && msg.workflowId) {
        const ctx = runningWorkflows.get(msg.workflowId);
        if (ctx) {
          ctx.cancelled = true;
          if (ctx.tabId) {
            await chrome.tabs.remove(ctx.tabId).catch(() => {});
          }
          runningWorkflows.delete(msg.workflowId);
          ws.send(JSON.stringify({ id: msg.id, success: true, message: 'Cancelled' }));
        } else {
          ws.send(JSON.stringify({ id: msg.id, success: false, error: 'Workflow not found' }));
        }
      }
    } catch (e) {
      console.error('Message error:', e);
    }
  };
  
  ws.onclose = () => {
    console.log('❌ Disconnected, reconnecting...');
    scheduleReconnect();
  };
  
  ws.onerror = (e) => {
    console.error('WebSocket error:', e);
  };
}

// Reconnect with exponential backoff
function scheduleReconnect() {
  console.log(`Reconnecting in ${reconnectDelay / 1000}s...`);
  setTimeout(() => {
    connectWebSocket();
    reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY);
  }, reconnectDelay);
}

// Start workflow handler - each workflow gets its own context
async function handleStartWorkflow(workflowId, workflow) {
  // Create workflow context
  const ctx = {
    variables: { ...workflow.variables },
    logs: [],
    tabId: null,
    popupTabId: null,
    popupWindowId: null,
    cancelled: false
  };
  runningWorkflows.set(workflowId, ctx);
  
  const log = (type, message, data = null) => {
    const entry = { time: new Date().toISOString(), type, message, ...(data && { data }) };
    ctx.logs.push(entry);
    console.log(`[${workflowId.slice(0,8)}][${type}]`, message, data || '');
  };
  
  log('workflow', 'Starting workflow', { actionsCount: workflow.actions?.length });
  
  try {
    // Create new tab or use existing based on options
    const newTab = workflow.options?.new_tab !== false;
    // Check if first action is geo_spoof — if so, don't load the URL yet
    // because it would use stale geo rules from a previous workflow
    const firstActionIsGeoSpoof = workflow.actions?.[0]?.type === 'geo_spoof';

    if (newTab) {
      const initialUrl = firstActionIsGeoSpoof ? 'about:blank' : (workflow.url || 'about:blank');
      log('workflow', 'Creating new tab', { url: initialUrl, deferredUrl: firstActionIsGeoSpoof ? workflow.url : null });
      const tab = await chrome.tabs.create({ url: initialUrl, active: false });
      ctx.tabId = tab.id;
      log('workflow', 'Tab created', { tabId: ctx.tabId });
      if (!firstActionIsGeoSpoof && workflow.url) await waitForTabLoad(ctx.tabId);
      log('workflow', 'Tab ready');
    } else {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (workflow.url) {
        log('workflow', 'Updating existing tab', { url: workflow.url });
        await chrome.tabs.update(tab.id, { url: workflow.url });
        ctx.tabId = tab.id;
        await waitForTabLoad(ctx.tabId);
      } else {
        ctx.tabId = tab?.id;
      }
    }
    
    // Check if cancelled
    if (ctx.cancelled) throw new Error('Workflow cancelled');
    
    await executeWorkflow(workflow, ctx, log);
    
    // Close tab if requested
    if (workflow.options?.close_tab && ctx.tabId) {
      log('workflow', 'Closing tab');
      await chrome.tabs.remove(ctx.tabId).catch(() => {});
    }
    
    log('workflow', 'Workflow completed successfully');
    return { success: true, message: 'Workflow completed', variables: ctx.variables, logs: ctx.logs };
  } catch (e) {
    log('error', 'Workflow failed', { error: e.message });
    // Close tab on error if close_tab_on_error is true, OR if close_tab is true (always close)
    if ((workflow.options?.close_tab_on_error || workflow.options?.close_tab) && ctx.tabId) {
      log('workflow', 'Closing tab after error');
      await chrome.tabs.remove(ctx.tabId).catch(() => {});
    }
    return { success: false, error: e.message, variables: ctx.variables, logs: ctx.logs };
  } finally {
    runningWorkflows.delete(workflowId);
  }
}

// Execute workflow with context
async function executeWorkflow(workflow, ctx, log) {
  for (let i = 0; i < workflow.actions.length; i++) {
    if (ctx.cancelled) throw new Error('Workflow cancelled');
    
    const action = workflow.actions[i];
    log('workflow', `Executing action ${i + 1}/${workflow.actions.length}: ${action.type}`);
    try {
      await executeAction(action, ctx, log);
    } catch (e) {
      log('error', `Action ${action.type} failed`, { error: e.message });
      if (workflow.options?.execution?.stop_on_error !== false) throw e;
    }
  }
  
  if (workflow.webhook) {
    log('workflow', 'Sending webhook', { url: workflow.webhook });
    await sendWebhook(workflow.webhook, workflow.webhook_payload, ctx);
  }
}

// === reCAPTCHA helper functions ===

async function findRecaptchaFrames(tabId) {
  const allFrames = await chrome.webNavigation.getAllFrames({ tabId });
  let checkboxFrameId = null;
  let challengeFrameId = null;

  for (const frame of allFrames) {
    if (frame.url) {
      if (frame.url.includes('/recaptcha/api2/anchor') || frame.url.includes('/recaptcha/enterprise/anchor')) {
        checkboxFrameId = frame.frameId;
      }
      if (frame.url.includes('/recaptcha/api2/bframe') || frame.url.includes('/recaptcha/enterprise/bframe')) {
        challengeFrameId = frame.frameId;
      }
    }
  }
  return { checkboxFrameId, challengeFrameId };
}

async function executeInFrame(tabId, frameId, func, args = []) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId, frameIds: [frameId] },
      world: 'MAIN',
      func,
      args
    });
    return results?.[0]?.result;
  } catch (e) {
    console.error(`executeInFrame error (frameId=${frameId}):`, e.message);
    return null;
  }
}

async function waitForElementInFrame(tabId, frameId, selector, timeout = 10000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const found = await executeInFrame(tabId, frameId, (sel) => {
      return !!document.querySelector(sel);
    }, [selector]);
    if (found) return true;
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}

// CDP trusted click — uses chrome.debugger to dispatch isTrusted:true mouse events
async function cdpClick(tabId, frameId, selector, frameType = 'anchor') {
  console.log('[cdpClick] Starting', { tabId, frameId, selector, frameType });

  // Get element coordinates from inside the frame
  const coords = await executeInFrame(tabId, frameId, (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  }, [selector]);

  console.log('[cdpClick] Element coords inside frame:', coords);
  if (!coords) return false;

  // Walk up the frame tree to calculate absolute viewport coordinates
  // This handles nested iframes (e.g., reCAPTCHA inside a demo iframe inside the main page)
  const allFrames = await chrome.webNavigation.getAllFrames({ tabId });
  const frameMap = {};
  for (const f of allFrames) {
    frameMap[f.frameId] = f;
  }

  let totalOffsetX = 0;
  let totalOffsetY = 0;
  let currentFrameId = frameId;

  while (currentFrameId !== 0) {
    const frameInfo = frameMap[currentFrameId];
    if (!frameInfo || frameInfo.parentFrameId === -1) break;

    const parentFrameId = frameInfo.parentFrameId;
    const frameUrl = frameInfo.url || '';

    // In the parent frame, find the iframe element that contains our current frame
    const iframeOffset = await executeInFrame(tabId, parentFrameId, (childUrl) => {
      const iframes = document.querySelectorAll('iframe');
      // Try exact URL match first
      for (const iframe of iframes) {
        if (iframe.src === childUrl) {
          const rect = iframe.getBoundingClientRect();
          return { x: rect.left, y: rect.top };
        }
      }
      // Try partial URL match
      for (const iframe of iframes) {
        const src = iframe.src || '';
        if (src && childUrl && childUrl.includes(new URL(src).pathname)) {
          const rect = iframe.getBoundingClientRect();
          return { x: rect.left, y: rect.top };
        }
      }
      // Try matching by recaptcha URL patterns
      for (const iframe of iframes) {
        const src = iframe.src || '';
        if (childUrl.includes('/recaptcha/') && src.includes('/recaptcha/')) {
          const rect = iframe.getBoundingClientRect();
          return { x: rect.left, y: rect.top };
        }
      }
      return null;
    }, [frameUrl]);

    console.log('[cdpClick] Frame', currentFrameId, '-> parent', parentFrameId, 'offset:', iframeOffset);

    if (iframeOffset) {
      totalOffsetX += iframeOffset.x;
      totalOffsetY += iframeOffset.y;
    }

    currentFrameId = parentFrameId;
  }

  const absX = totalOffsetX + coords.x;
  const absY = totalOffsetY + coords.y;
  console.log('[cdpClick] Absolute click position:', { absX, absY, totalOffsetX, totalOffsetY });

  // Attach debugger, click, detach
  try {
    console.log('[cdpClick] Attaching debugger...');
    await chrome.debugger.attach({ tabId }, '1.3');
    console.log('[cdpClick] Debugger attached');
  } catch (e) {
    console.log('[cdpClick] Attach error:', e.message);
    // Already attached is OK
    if (!e.message.includes('Already attached')) throw e;
  }

  try {
    // Move mouse to element
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseMoved', x: absX, y: absY
    });
    console.log('[cdpClick] mouseMoved sent');
    await new Promise(r => setTimeout(r, 50 + Math.random() * 100));

    // Mouse down
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mousePressed', x: absX, y: absY, button: 'left', clickCount: 1
    });
    console.log('[cdpClick] mousePressed sent');
    await new Promise(r => setTimeout(r, 30 + Math.random() * 70));

    // Mouse up
    await chrome.debugger.sendCommand({ tabId }, 'Input.dispatchMouseEvent', {
      type: 'mouseReleased', x: absX, y: absY, button: 'left', clickCount: 1
    });
    console.log('[cdpClick] mouseReleased sent — click complete');

    return true;
  } catch (e) {
    console.error('[cdpClick] CDP command failed:', e.message);
    return false;
  } finally {
    try {
      await chrome.debugger.detach({ tabId });
      console.log('[cdpClick] Debugger detached');
    } catch (e) { /* ignore detach errors */ }
  }
}

async function solveRecaptchaAudio(targetTabId, ctx, log, action) {
  const maxAttempts = action.max_attempts || 3;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    log('action', `recaptcha_solve attempt ${attempt}/${maxAttempts}`);

    // Step 1: Find reCAPTCHA iframes
    let frames = await findRecaptchaFrames(targetTabId);

    if (!frames.checkboxFrameId && !frames.challengeFrameId) {
      log('action', 'recaptcha_solve: No reCAPTCHA iframes found, may already be solved');
      ctx.variables['captcha_solved'] = 'true';
      return;
    }

    // Step 2: Click checkbox if present and not already checked
    if (frames.checkboxFrameId) {
      const alreadyChecked = await executeInFrame(
        targetTabId, frames.checkboxFrameId,
        () => {
          const anchor = document.querySelector('span#recaptcha-anchor');
          return anchor?.getAttribute('aria-checked') === 'true';
        }
      );

      if (!alreadyChecked) {
        log('action', 'recaptcha_solve: Clicking checkbox with CDP trusted click');
        const clicked = await cdpClick(targetTabId, frames.checkboxFrameId, 'span#recaptcha-anchor');
        if (!clicked) {
          log('error', 'recaptcha_solve: CDP click failed, element not found');
        }
        await new Promise(r => setTimeout(r, 2000));
      }
    }

    // Step 3: Re-scan frames after checkbox click
    frames = await findRecaptchaFrames(targetTabId);

    // Check if solved by checkbox alone
    if (!frames.challengeFrameId) {
      if (frames.checkboxFrameId) {
        const isChecked = await executeInFrame(
          targetTabId, frames.checkboxFrameId,
          () => {
            const anchor = document.querySelector('span#recaptcha-anchor');
            return anchor?.getAttribute('aria-checked') === 'true';
          }
        );
        if (isChecked) {
          log('action', 'recaptcha_solve: Solved with checkbox click alone');
          ctx.variables['captcha_solved'] = 'true';
          return;
        }
      }
      // Wait for challenge frame to appear
      await new Promise(r => setTimeout(r, 2000));
      frames = await findRecaptchaFrames(targetTabId);
      if (!frames.challengeFrameId) {
        log('error', 'recaptcha_solve: Challenge frame never appeared');
        if (attempt < maxAttempts) continue;
        throw new Error('reCAPTCHA challenge frame not found');
      }
    }

    // Step 4: Click audio button
    log('action', 'recaptcha_solve: Switching to audio challenge');
    const audioButtonFound = await waitForElementInFrame(
      targetTabId, frames.challengeFrameId,
      'button#recaptcha-audio-button', 5000
    );

    if (audioButtonFound) {
      await executeInFrame(
        targetTabId, frames.challengeFrameId,
        () => {
          const btn = document.querySelector('button#recaptcha-audio-button');
          if (btn) btn.click();
        }
      );
      await new Promise(r => setTimeout(r, 2000));
    } else {
      // Check if already on audio challenge
      const hasAudioSource = await executeInFrame(
        targetTabId, frames.challengeFrameId,
        () => !!document.querySelector('audio#audio-source')
      );
      if (!hasAudioSource) {
        log('error', 'recaptcha_solve: No audio button and no audio source');
        if (attempt < maxAttempts) { await new Promise(r => setTimeout(r, 2000)); continue; }
        throw new Error('Audio button not found');
      }
    }

    // Step 4.5: Click play button to start audio playback
    log('action', 'recaptcha_solve: Looking for audio play button');
    await new Promise(r => setTimeout(r, 1500));

    // Try to find the play button using known selectors
    const playButtonSelector = await executeInFrame(
      targetTabId, frames.challengeFrameId,
      () => {
        const selectors = [
          '.rc-audiochallenge-play-button',
          '.rc-audiochallenge-control button',
          'button[aria-labelledby="audio-instructions"]',
          '.rc-audiochallenge-tdownload-link'
        ];
        for (const sel of selectors) {
          const el = document.querySelector(sel);
          if (el) return sel;
        }
        return null;
      }
    );

    if (playButtonSelector) {
      log('action', `recaptcha_solve: Found play button (${playButtonSelector}), clicking with CDP`);
      const playClicked = await cdpClick(targetTabId, frames.challengeFrameId, playButtonSelector, 'bframe');
      if (playClicked) {
        log('action', 'recaptcha_solve: Play button clicked successfully');
      } else {
        // Fallback: try regular click
        log('action', 'recaptcha_solve: CDP play click failed, trying regular click');
        await executeInFrame(
          targetTabId, frames.challengeFrameId,
          (sel) => {
            const btn = document.querySelector(sel);
            if (btn) btn.click();
          },
          [playButtonSelector]
        );
      }
      await new Promise(r => setTimeout(r, 2000));
    } else {
      log('action', 'recaptcha_solve: No play button found, audio may auto-load');
    }

    // Step 5: Get audio URL
    log('action', 'recaptcha_solve: Extracting audio URL');
    const audioSourceFound = await waitForElementInFrame(
      targetTabId, frames.challengeFrameId,
      'audio#audio-source', 10000
    );

    if (!audioSourceFound) {
      log('error', 'recaptcha_solve: Audio source not found (may be rate-limited)');
      if (attempt < maxAttempts) { await new Promise(r => setTimeout(r, 3000)); continue; }
      throw new Error('Audio source not found');
    }

    const audioUrl = await executeInFrame(
      targetTabId, frames.challengeFrameId,
      () => {
        const src = document.querySelector('audio#audio-source');
        return src?.getAttribute('src') || src?.src || null;
      }
    );

    if (!audioUrl) {
      log('error', 'recaptcha_solve: Audio URL is empty');
      if (attempt < maxAttempts) { await new Promise(r => setTimeout(r, 2000)); continue; }
      throw new Error('Audio URL not found');
    }

    log('action', 'recaptcha_solve: Got audio URL', { url: audioUrl.slice(0, 80) + '...' });

    // Step 6: Send to server for transcription
    log('action', 'recaptcha_solve: Sending to server for transcription');
    let transcription;
    try {
      const resp = await fetch('http://localhost:8766/transcribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio_url: audioUrl })
      });
      const result = await resp.json();
      if (!result.success || !result.text) {
        log('error', 'recaptcha_solve: Transcription failed', { error: result.error });
        if (attempt < maxAttempts) { await new Promise(r => setTimeout(r, 2000)); continue; }
        throw new Error('Transcription failed: ' + (result.error || 'empty result'));
      }
      transcription = result.text.trim();
      log('action', 'recaptcha_solve: Transcription received', { text: transcription });
    } catch (e) {
      if (e.message.startsWith('Transcription failed')) throw e;
      log('error', 'recaptcha_solve: Server request failed', { error: e.message });
      if (attempt < maxAttempts) { await new Promise(r => setTimeout(r, 2000)); continue; }
      throw e;
    }

    // Step 7: Type transcription into response field
    log('action', 'recaptcha_solve: Typing transcription');
    await executeInFrame(
      targetTabId, frames.challengeFrameId,
      (text) => {
        const input = document.querySelector('input#audio-response');
        if (!input) return false;
        input.focus();
        input.value = text;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      },
      [transcription]
    );

    await new Promise(r => setTimeout(r, 500));

    // Step 8: Click verify button
    log('action', 'recaptcha_solve: Clicking verify');
    await executeInFrame(
      targetTabId, frames.challengeFrameId,
      () => {
        const btn = document.querySelector('button#recaptcha-verify-button');
        if (btn) btn.click();
      }
    );

    await new Promise(r => setTimeout(r, 3000));

    // Step 9: Check if solved
    frames = await findRecaptchaFrames(targetTabId);

    let solved = false;
    if (frames.checkboxFrameId) {
      solved = await executeInFrame(
        targetTabId, frames.checkboxFrameId,
        () => {
          const anchor = document.querySelector('span#recaptcha-anchor');
          return anchor?.getAttribute('aria-checked') === 'true';
        }
      );
    }

    if (solved || !frames.challengeFrameId) {
      log('action', `recaptcha_solve: SOLVED on attempt ${attempt}`, { transcription });
      ctx.variables['captcha_solved'] = 'true';
      ctx.variables['captcha_transcription'] = transcription;
      return;
    }

    // Check for error messages in challenge frame
    if (frames.challengeFrameId) {
      const errorMsg = await executeInFrame(
        targetTabId, frames.challengeFrameId,
        () => {
          const err1 = document.querySelector('.rc-audiochallenge-error-message');
          if (err1 && err1.style.display !== 'none') return err1.textContent;
          const err2 = document.querySelector('.rc-audio-error-message');
          if (err2 && err2.style.display !== 'none') return err2.textContent;
          return null;
        }
      );
      if (errorMsg) {
        log('error', `recaptcha_solve: Verification error: ${errorMsg}`);
      }
    }

    log('error', `recaptcha_solve: Attempt ${attempt} failed`);
    if (attempt < maxAttempts) {
      await new Promise(r => setTimeout(r, 2000));
    }
  }

  ctx.variables['captcha_solved'] = 'false';
  throw new Error(`reCAPTCHA solve failed after ${maxAttempts} attempts`);
}

// Execute single action with context
async function executeAction(action, ctx, log) {
  const { tabId, variables } = ctx;

  // Aggressively check for OAuth popup before each evaluate action
  if (!ctx.popupTabId && action.type === 'evaluate') {
    log('workflow', 'Checking for OAuth popup before action...');

    // Try multiple times with polling
    for (let attempt = 0; attempt < 3; attempt++) {
      const allTabs = await chrome.tabs.query({});

      for (const tab of allTabs) {
        if (tab.id !== tabId && tab.url) {
          const isOAuthPage = tab.url.includes('accounts.google.com') ||
                              tab.url.includes('login.microsoftonline.com') ||
                              tab.url.includes('oauth') ||
                              tab.url.includes('signin') ||
                              tab.url.includes('authorize');

          if (isOAuthPage) {
            log('workflow', '🔐 OAuth popup found via polling!', {
              tabId: tab.id,
              url: tab.url,
              attempt: attempt + 1
            });

            ctx.popupTabId = tab.id;
            ctx.popupWindowId = tab.windowId;

            popupTracking.set(tab.id, {
              parentTabId: tabId,
              workflowId: Array.from(runningWorkflows.entries()).find(([_, c]) => c === ctx)?.[0],
              createdAt: Date.now(),
              windowId: tab.windowId
            });
            break;
          }
        }
      }

      if (ctx.popupTabId) break;
      if (attempt < 2) {
        log('workflow', `Popup not found, waiting... (attempt ${attempt + 1}/3)`);
        await new Promise(r => setTimeout(r, 1500));
      }
    }

    if (!ctx.popupTabId) {
      log('workflow', '⚠️ No OAuth popup detected after 3 attempts');
    }
  }

  // Determine which tab to execute action on
  let targetTabId = tabId;
  if (ctx.popupTabId) {
    try {
      const popupTab = await chrome.tabs.get(ctx.popupTabId);
      if (popupTab && popupTab.url && (
          popupTab.url.includes('accounts.google.com') ||
          popupTab.url.includes('login.microsoftonline.com') ||
          popupTab.url.includes('oauth'))) {
        targetTabId = ctx.popupTabId;
        log('workflow', '✅ Using OAuth popup tab', {
          popupTabId: ctx.popupTabId,
          url: popupTab.url
        });
      } else {
        // Popup no longer valid, clear it
        log('workflow', 'Popup no longer valid, using main tab');
        ctx.popupTabId = null;
        ctx.popupWindowId = null;
      }
    } catch (e) {
      // Popup tab closed or invalid
      log('workflow', 'Popup tab error, using main tab', { error: e.message });
      ctx.popupTabId = null;
      ctx.popupWindowId = null;
    }
  }

  if (action.type === 'delay') {
    const timeout = action.timeout || 1000;
    log('action', 'delay', { timeout });
    await new Promise(r => setTimeout(r, timeout));
    log('action', 'delay -> DONE');
  } else if (action.type === 'if_exists') {
    // Handle if_exists in background to support 'actions' array format
    log('action', 'if_exists', { selectors: action.selectors, timeout: action.timeout });
    const ifTimeout = action.timeout || 0;
    let exists = false;
    // JSON-stringify selectors so they're serializable for chrome.scripting.executeScript
    const selectorsJson = JSON.stringify(Array.from(action.selectors || []));

    if (ifTimeout > 0) {
      const start = Date.now();
      while (Date.now() - start < ifTimeout) {
        if (ctx.cancelled) throw new Error('Workflow cancelled');
        try {
          const checkResults = await chrome.scripting.executeScript({
            target: { tabId: targetTabId },
            func: (selsJson) => {
              const sels = JSON.parse(selsJson);
              for (const s of sels) {
                try { if (document.querySelector(s)) return true; } catch(e) {}
              }
              return false;
            },
            args: [selectorsJson]
          });
          if (checkResults?.[0]?.result) { exists = true; break; }
        } catch(e) {
          log('action', 'if_exists check error', { error: e.message });
        }
        await new Promise(r => setTimeout(r, 200));
      }
    } else {
      try {
        const checkResults = await chrome.scripting.executeScript({
          target: { tabId: targetTabId },
          func: (selsJson) => {
            const sels = JSON.parse(selsJson);
            for (const s of sels) {
              try { if (document.querySelector(s)) return true; } catch(e) {}
            }
            return false;
          },
          args: [selectorsJson]
        });
        exists = !!checkResults?.[0]?.result;
      } catch(e) {
        log('action', 'if_exists check error', { error: e.message });
      }
    }

    log('action', `if_exists -> ${exists ? 'EXISTS' : 'NOT EXISTS'}`);

    // Support both 'actions' array and 'then'/'else' single action
    const subActions = exists
      ? (action.actions || (action.then ? [action.then] : []))
      : (action.else_actions || (action.else ? [action.else] : []));

    for (const subAction of subActions) {
      if (ctx.cancelled) throw new Error('Workflow cancelled');
      await executeAction(subAction, ctx, log);
    }
  } else if (action.type === 'navigate') {
    log('action', 'navigate', { url: action.url });
    await chrome.tabs.update(targetTabId, { url: resolveVariables(action.url, variables) });
    await waitForTabLoad(targetTabId);
    log('action', 'navigate -> SUCCESS');
  } else if (action.type === 'evaluate') {
    // Handle evaluate in background using chrome.scripting API to bypass CSP
    const allFrames = action.allFrames || false;
    log('action', 'evaluate', { script: action.script?.slice(0, 50) + '...', targetTabId, allFrames });
    try {
      if (typeof action.script !== 'string') {
        throw new Error('Evaluate action missing required "script" field');
      }
      const script = resolveVariables(action.script, variables);
      const scriptTarget = allFrames
        ? { tabId: targetTabId, allFrames: true }
        : { tabId: targetTabId };
      const results = await chrome.scripting.executeScript({
        target: scriptTarget,
        world: 'MAIN',
        func: async (scriptCode) => {
          try {
            // Wrap in async IIFE to support await
            const asyncFunc = new Function('return (async () => { ' + scriptCode + ' })()');
            const result = await asyncFunc();
            return { success: true, value: typeof result === 'string' ? result : JSON.stringify(result) };
          } catch (e) {
            // Fallback to sync eval if async fails
            try {
              const result = eval(scriptCode);
              return { success: true, value: typeof result === 'string' ? result : JSON.stringify(result) };
            } catch (e2) {
              return { success: false, error: e2.message, originalError: e.message };
            }
          }
        },
        args: [script]
      });
      // When allFrames, find first successful result from any frame
      let result = null;
      if (allFrames) {
        for (const r of (results || [])) {
          if (r.result?.success && r.result.value && r.result.value !== 'null' && r.result.value !== 'undefined') {
            result = r.result;
            break;
          }
        }
        // If no meaningful success found, take first success or first result
        if (!result) {
          for (const r of (results || [])) {
            if (r.result?.success) { result = r.result; break; }
          }
        }
        if (!result) result = results?.[0]?.result;
      } else {
        result = results?.[0]?.result;
      }

      // If failure is CSP-related, fall back to CDP Runtime.evaluate
      if (!result?.success && isCSPError(result?.error, result?.originalError)) {
        log('action', 'evaluate -> CSP blocked, trying CDP Runtime.evaluate fallback');
        result = await cdpEvaluate(targetTabId, script);
      }

      if (result?.success) {
        if (action.set_variable) ctx.variables[action.set_variable] = result.value;
        log('action', 'evaluate -> SUCCESS', { result: result.value?.slice(0, 100) });
      } else {
        const errorMsg = result?.error || 'Unknown error';
        log('error', 'evaluate -> FAILED', { error: errorMsg });
        throw new Error(errorMsg);
      }
    } catch (e) {
      log('error', 'evaluate -> FAILED', { error: e.message });
      throw e;
    }
  } else if (action.type === 'recaptcha_solve') {
    log('action', 'recaptcha_solve', { timeout: action.timeout, max_attempts: action.max_attempts });
    try {
      await solveRecaptchaAudio(targetTabId, ctx, log, action);
      log('action', 'recaptcha_solve -> SUCCESS');
    } catch (e) {
      log('error', 'recaptcha_solve -> FAILED (will not abort workflow)', { error: e.message });
      ctx.variables['captcha_solved'] = 'false';
    }
  } else if (action.type === 'focus_tab') {
    log('action', 'focus_tab', { tabId: targetTabId });
    try {
      const tab = await chrome.tabs.get(targetTabId);
      await chrome.tabs.update(targetTabId, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      log('action', 'focus_tab -> SUCCESS');
    } catch (e) {
      log('error', 'focus_tab -> FAILED', { error: e.message });
      throw e;
    }
  } else if (action.type === 'screenshot') {
    log('action', 'screenshot', { set_variable: action.set_variable, full_page: action.full_page });
    try {
      let base64Data;
      // Always use CDP for screenshots (captureVisibleTab requires activeTab which
      // is not available for tabs created with active:false)
      try {
        await chrome.debugger.attach({ tabId: targetTabId }, '1.3');
      } catch (attachErr) {
        if (!attachErr.message?.includes('Already attached')) throw attachErr;
      }
      try {
        if (action.full_page) {
          // Full-page: resize viewport to content size before capture
          const metrics = await chrome.debugger.sendCommand({ tabId: targetTabId }, 'Page.getLayoutMetrics');
          const width = Math.ceil(metrics.cssContentSize?.width || metrics.contentSize?.width || 1280);
          const height = Math.ceil(metrics.cssContentSize?.height || metrics.contentSize?.height || 800);
          await chrome.debugger.sendCommand({ tabId: targetTabId }, 'Emulation.setDeviceMetricsOverride', {
            width: width,
            height: height,
            deviceScaleFactor: 1,
            mobile: false
          });
          const screenshot = await chrome.debugger.sendCommand({ tabId: targetTabId }, 'Page.captureScreenshot', {
            format: 'png',
            captureBeyondViewport: true
          });
          base64Data = screenshot.data;
          await chrome.debugger.sendCommand({ tabId: targetTabId }, 'Emulation.clearDeviceMetricsOverride');
        } else {
          // Viewport-only: capture current viewport via CDP
          const screenshot = await chrome.debugger.sendCommand({ tabId: targetTabId }, 'Page.captureScreenshot', {
            format: 'png'
          });
          base64Data = screenshot.data;
        }
      } finally {
        try { await chrome.debugger.detach({ tabId: targetTabId }); } catch (e) { /* ignore */ }
      }
      if (action.set_variable) {
        ctx.variables[action.set_variable] = base64Data;
      } else {
        ctx.variables['screenshot_base64'] = base64Data;
      }
      log('action', 'screenshot -> SUCCESS');
    } catch (e) {
      log('error', 'screenshot -> FAILED', { error: e.message });
      throw e;
    }
  } else if (action.type === 'download_and_upload') {
    // Click download button on page, wait for Chrome to finish downloading, return file path
    log('action', 'download_and_upload', { set_variable: action.set_variable });
    try {
      // Step 1: Set up download tracking BEFORE clicking
      let newDownloadId = null;
      const onCreated = (item) => { newDownloadId = item.id; };
      chrome.downloads.onCreated.addListener(onCreated);

      // Step 2: Click the download button on the page (let real download happen)
      const clickResults = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        world: 'MAIN',
        func: async () => {
          // Try ChatGPT's current DOM: find generated image by alt text prefix
          var imgs = document.querySelectorAll('img[alt^="Generated image"]');
          var lastImg = null;
          for (var i = imgs.length - 1; i >= 0; i--) {
            if (imgs[i].naturalWidth > 0) { lastImg = imgs[i]; break; }
          }
          // Fallback: try legacy selectors
          if (!lastImg) {
            var legacyImgs = document.querySelectorAll('generated-image img.image.loaded, single-image img.image.loaded, .generated-images img.image.loaded');
            for (var i = legacyImgs.length - 1; i >= 0; i--) {
              if (legacyImgs[i].naturalWidth > 0) { lastImg = legacyImgs[i]; break; }
            }
          }
          if (!lastImg) return 'no_image';
          // Try to find a download button by traversing parents
          var searchEl = lastImg;
          var dlBtn = null;
          for (var d = 0; d < 15 && searchEl && !dlBtn; d++) {
            searchEl = searchEl.parentElement;
            if (!searchEl) break;
            // Try various download button selectors
            dlBtn = searchEl.querySelector('button[aria-label*="ownload"]')
              || searchEl.querySelector('a[download]')
              || searchEl.querySelector('a[href*="download"]')
              || searchEl.querySelector('mat-icon[fonticon="download"]');
          }
          if (dlBtn) {
            var clickTarget = dlBtn.closest('button') || dlBtn.closest('a') || dlBtn.closest('[role="button"]') || dlBtn;
            clickTarget.click();
            return 'clicked';
          }
          // No download button found — convert image to blob data URL as fallback
          try {
            var canvas = document.createElement('canvas');
            canvas.width = lastImg.naturalWidth;
            canvas.height = lastImg.naturalHeight;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(lastImg, 0, 0);
            var dataUrl = canvas.toDataURL('image/png');
            return 'data:' + dataUrl;
          } catch (e) {
            // Canvas tainted (CORS) — fetch with page cookies instead
            try {
              var resp = await fetch(lastImg.src, { credentials: 'include' });
              var blob = await resp.blob();
              var reader = new FileReader();
              var b64 = await new Promise((resolve) => { reader.onload = () => resolve(reader.result); reader.readAsDataURL(blob); });
              return 'data:' + b64;
            } catch (e2) {
              return 'no_download_button';
            }
          }
        }
      });

      const clickResult = clickResults?.[0]?.result;
      if (clickResult !== 'clicked') {
        if (typeof clickResult === 'string' && clickResult.startsWith('data:')) {
          // Image was converted to data URL from page context — download it
          const dataUrl = clickResult.slice(5); // remove the extra 'data:' prefix
          log('action', 'download_and_upload: no download button, using canvas/fetch data URL');
          chrome.downloads.download({ url: dataUrl });
        } else {
          // Fallback: if generated_image_src is available, download directly via URL
          const imgSrc = ctx.variables.generated_image_src;
          if (imgSrc) {
            log('action', 'download_and_upload: click failed, falling back to direct URL download', { url: imgSrc });
            chrome.downloads.download({ url: imgSrc });
          } else {
            chrome.downloads.onCreated.removeListener(onCreated);
            throw new Error('Download click failed: ' + clickResult);
          }
        }
      } else {
        log('action', 'download_and_upload: download button clicked, waiting for download to start...');
      }

      // Step 3: Wait for download to be created (max 30s)
      const startTime = Date.now();
      while (!newDownloadId && Date.now() - startTime < 30000) {
        await new Promise(r => setTimeout(r, 500));
      }
      chrome.downloads.onCreated.removeListener(onCreated);

      if (!newDownloadId) {
        throw new Error('No download started within 30s');
      }
      log('action', 'download_and_upload: download started', { downloadId: newDownloadId });

      // Step 4: Wait for download to complete (max 60s)
      const filePath = await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => {
          chrome.downloads.onChanged.removeListener(onChange);
          reject(new Error('Download did not complete within 60s'));
        }, 60000);

        function onChange(delta) {
          if (delta.id !== newDownloadId) return;
          if (delta.state && delta.state.current === 'complete') {
            clearTimeout(timeout);
            chrome.downloads.onChanged.removeListener(onChange);
            chrome.downloads.search({ id: newDownloadId }).then(results => {
              if (results && results.length > 0) {
                resolve(results[0].filename);
              } else {
                reject(new Error('Download entry not found'));
              }
            });
          } else if (delta.error) {
            clearTimeout(timeout);
            chrome.downloads.onChanged.removeListener(onChange);
            reject(new Error('Download error: ' + (delta.error.current || 'unknown')));
          }
        }

        // Check if already complete before attaching listener
        chrome.downloads.search({ id: newDownloadId }).then(results => {
          if (results?.[0]?.state === 'complete') {
            clearTimeout(timeout);
            resolve(results[0].filename);
          } else {
            chrome.downloads.onChanged.addListener(onChange);
          }
        });
      });

      log('action', 'download_and_upload: download complete', { filePath });

      if (action.set_variable) {
        ctx.variables[action.set_variable] = filePath;
      }
      log('action', 'download_and_upload -> SUCCESS', { filePath });
    } catch (e) {
      log('error', 'download_and_upload -> FAILED', { error: e.message });
      throw e;
    }
  } else if (action.type === 'turnstile_solve') {
    // Turnstile auto-click is handled automatically by turnstile_injected.js + turnstile_bridge.js
    // This action waits for the Turnstile to be solved (response token populated)
    // If captcha is not solved within timeout, it will refresh the page and retry
    const timeout = action.timeout || 45000; // 45 sec default (user suggested 45-60s)
    const maxRetries = action.maxRetries || 3; // Max retry attempts
    const refreshDelay = action.refreshDelay || 3000; // Wait after refresh before checking

    log('action', 'turnstile_solve', { timeout, maxRetries, refreshDelay });

    let solved = false;
    let attempt = 0;

    while (attempt < maxRetries && !solved) {
      attempt++;
      log('action', `turnstile_solve attempt ${attempt}/${maxRetries}`);

      const start = Date.now();

      while (Date.now() - start < timeout) {
        if (ctx.cancelled) throw new Error('Workflow cancelled');

        // Check if tab is still open
        try {
          await chrome.tabs.get(targetTabId);
        } catch (e) {
          // Tab was closed - consider it solved (captcha passed, page redirected)
          log('action', 'turnstile_solve -> Tab closed (likely solved)');
          solved = true;
          break;
        }

        try {
          const results = await chrome.scripting.executeScript({
            target: { tabId: targetTabId },
            func: () => {
              // Check for Turnstile success response token
              const responseInput = document.querySelector('[name="cf-turnstile-response"]') ||
                                    document.querySelector('input[name="cf-turnstile-response"]');
              if (responseInput && responseInput.value) return 'solved';
              // Check if Turnstile iframe is still present
              const iframes = document.querySelectorAll('iframe');
              for (const iframe of iframes) {
                if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) return 'pending';
              }
              return 'no_turnstile';
            }
          });
          const status = results?.[0]?.result;
          if (status === 'solved') { solved = true; break; }
          if (status === 'no_turnstile') { solved = true; break; }
        } catch (e) {
          log('action', 'turnstile_solve check error', { error: e.message });
        }
        await new Promise(r => setTimeout(r, 1000));
      }

      // If not solved and we have retries left, refresh the page and try again
      if (!solved && attempt < maxRetries) {
        log('action', `turnstile_solve timeout on attempt ${attempt}, refreshing page...`);
        try {
          // Check if tab still exists before refreshing
          await chrome.tabs.get(targetTabId);
          await chrome.tabs.reload(targetTabId);

          // Wait for page to reload and Turnstile to appear
          await new Promise(r => setTimeout(r, refreshDelay));

          // Additional wait for Turnstile iframe to load
          let turnstileReady = false;
          const turnstileWaitStart = Date.now();
          const turnstileWaitTimeout = 10000; // 10 seconds to wait for Turnstile to appear

          while (Date.now() - turnstileWaitStart < turnstileWaitTimeout && !turnstileReady) {
            if (ctx.cancelled) throw new Error('Workflow cancelled');
            try {
              const results = await chrome.scripting.executeScript({
                target: { tabId: targetTabId },
                func: () => {
                  const iframes = document.querySelectorAll('iframe');
                  for (const iframe of iframes) {
                    if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) return true;
                  }
                  return false;
                }
              });
              turnstileReady = results?.[0]?.result === true;
              if (turnstileReady) {
                log('action', 'turnstile_solve -> Turnstile iframe detected after refresh');
              }
            } catch (e) {
              // Page might still be loading
            }
            if (!turnstileReady) await new Promise(r => setTimeout(r, 500));
          }

          log('action', `turnstile_solve -> Retrying (attempt ${attempt + 1}/${maxRetries})`);
        } catch (e) {
          // Tab was closed during refresh - consider it handled
          log('action', 'turnstile_solve -> Tab closed during refresh');
          solved = true;
        }
      }
    }

    ctx.variables['turnstile_solved'] = solved ? 'true' : 'false';
    ctx.variables['turnstile_attempts'] = String(attempt);

    if (solved) {
      log('action', `turnstile_solve -> SUCCESS (attempts: ${attempt})`);
    } else {
      throw new Error(`Turnstile solve failed after ${maxRetries} attempts`);
    }
  } else if (action.type === 'turnstile_focus_wait') {
    // Add this tab to the Turnstile focus queue and wait for its turn
    // Used when multiple tabs click submit simultaneously and need focus to solve Turnstile
    // The queue ensures each tab gets focused one at a time
    const skipIfNoTurnstile = action.skip_if_no_turnstile !== false; // Default true

    log('action', 'turnstile_focus_wait', { tabId: targetTabId, skipIfNoTurnstile });

    // First check if Turnstile is even present
    let hasTurnstile = false;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: targetTabId },
        func: () => {
          const iframes = document.querySelectorAll('iframe');
          for (const iframe of iframes) {
            if (iframe.src && iframe.src.includes('challenges.cloudflare.com')) return true;
          }
          return false;
        }
      });
      hasTurnstile = results?.[0]?.result === true;
    } catch (e) {
      log('action', 'turnstile_focus_wait -> Script error checking Turnstile', { error: e.message });
    }

    if (!hasTurnstile && skipIfNoTurnstile) {
      log('action', 'turnstile_focus_wait -> No Turnstile detected, skipping queue');
      ctx.variables['turnstile_focus_result'] = 'no_turnstile';
    } else {
      // Add to queue and wait for turn
      log('action', 'turnstile_focus_wait -> Adding to focus queue');
      try {
        const result = await addToTurnstileFocusQueue(targetTabId);
        ctx.variables['turnstile_focus_result'] = result.reason;
        ctx.variables['turnstile_focus_elapsed'] = String(result.elapsed || 0);
        log('action', `turnstile_focus_wait -> Done: ${result.reason}, elapsed: ${result.elapsed}ms`);
      } catch (e) {
        log('action', 'turnstile_focus_wait -> Queue error', { error: e.message });
        ctx.variables['turnstile_focus_result'] = 'error';
      }
    }
  } else if (action.type === 'focus_tab') {
    // Focus the current tab and its window
    // Useful when you need to ensure a tab is focused before an operation
    const duration = action.duration || 1000; // How long to keep focused (default 1 second)

    log('action', 'focus_tab', { tabId: targetTabId, duration });

    try {
      await chrome.tabs.update(targetTabId, { active: true });
      const tab = await chrome.tabs.get(targetTabId);
      if (tab.windowId) {
        await chrome.windows.update(tab.windowId, { focused: true });
      }

      // Wait for the specified duration
      if (duration > 0) {
        await new Promise(r => setTimeout(r, duration));
      }

      log('action', 'focus_tab -> SUCCESS');
      ctx.variables['tab_focused'] = 'true';
    } catch (e) {
      log('action', 'focus_tab -> FAILED', { error: e.message });
      ctx.variables['tab_focused'] = 'false';
    }
  } else if (action.type === 'geo_spoof') {
    // Spoof browser location for Google search via x-geo header injection
    // If latitude/longitude are empty or not provided, skip spoofing (run real location)
    const rawLat = resolveVariables(String(action.latitude || ''), variables).trim();
    const rawLng = resolveVariables(String(action.longitude || ''), variables).trim();
    log('action', 'geo_spoof', { rawLat, rawLng, hl: action.hl, gl: action.gl, enabled: action.enabled });

    if (action.enabled === false) {
      await disableGeoSpoof();
      log('action', 'geo_spoof -> DISABLED');
    } else if (!rawLat || !rawLng || rawLat === '0' || rawLng === '0') {
      // No coordinates provided — always remove session rule to clear any stale spoof
      await disableGeoSpoof();
      log('action', 'geo_spoof -> SKIPPED (no coordinates provided, using real location)');
    } else {
      const lat = parseFloat(rawLat);
      const lng = parseFloat(rawLng);
      if (isNaN(lat) || isNaN(lng)) {
        await disableGeoSpoof();
        log('action', 'geo_spoof -> SKIPPED (invalid coordinates)');
      } else {
        const hl = resolveVariables(action.hl || 'en', variables);
        const gl = resolveVariables(action.gl || 'US', variables);
        await enableGeoSpoof(lat, lng, hl, gl);
        ctx.variables['geo_latitude'] = String(lat);
        ctx.variables['geo_longitude'] = String(lng);
        // Generate UULE for URL parameter (Google's primary geo targeting method)
        const locationName = (variables.location || '').trim();
        if (locationName) {
          ctx.variables['geo_uule'] = genUuleParam(locationName);
        }
        log('action', 'geo_spoof -> ENABLED', { latitude: lat, longitude: lng, hl, gl, uule: !!locationName });
      }
    }
  } else if (action.type === 'wait_for') {
    // Handle wait_for in background using chrome.scripting to avoid content script dependency
    const selectors = action.selectors || [];
    const timeout = action.timeout || 15000;
    const allFrames = action.allFrames || false;
    log('action', 'wait_for', { selectors, timeout, allFrames });
    const selectorsJson = JSON.stringify(selectors);
    const start = Date.now();
    let found = false;

    while (Date.now() - start < timeout) {
      if (ctx.cancelled) throw new Error('Workflow cancelled');
      try {
        const scriptTarget = allFrames
          ? { tabId: targetTabId, allFrames: true }
          : { tabId: targetTabId };
        const results = await chrome.scripting.executeScript({
          target: scriptTarget,
          func: (selsJson) => {
            const sels = JSON.parse(selsJson);
            for (const s of sels) {
              try { if (document.querySelector(s)) return true; } catch(e) {}
            }
            return false;
          },
          args: [selectorsJson]
        });
        if (results?.some(r => r.result)) { found = true; break; }
      } catch (e) {
        // Page may not be ready yet, continue polling
      }
      await new Promise(r => setTimeout(r, 300));
    }

    if (found) {
      log('action', 'wait_for -> FOUND' + (allFrames ? ' (allFrames)' : ''));
    } else {
      throw new Error('wait_for timeout: none of [' + selectors.join(', ') + '] found within ' + timeout + 'ms');
    }
  } else if (action.type === 'click') {
    // Handle click in background using chrome.scripting to avoid content script dependency
    const selectors = action.selectors || (action.selector ? [action.selector] : []);
    log('action', 'click', { selectors });
    const selectorsJson = JSON.stringify(selectors);
    const results = await chrome.scripting.executeScript({
      target: { tabId: targetTabId },
      func: (selsJson) => {
        const sels = JSON.parse(selsJson);
        for (const s of sels) {
          try {
            if (s.startsWith('text:')) {
              const text = s.slice(5);
              const elements = document.querySelectorAll('button, a, [role="button"], [role="link"]');
              for (const el of elements) {
                if (el.textContent.trim().includes(text)) {
                  el.click();
                  return { clicked: true, selector: s };
                }
              }
            } else {
              const el = document.querySelector(s);
              if (el) {
                el.click();
                return { clicked: true, selector: s };
              }
            }
          } catch(e) {}
        }
        return { clicked: false };
      },
      args: [selectorsJson]
    });

    const clickResult = results?.[0]?.result;
    if (clickResult?.clicked) {
      log('action', 'click -> SUCCESS (' + clickResult.selector + ')');
    } else {
      log('action', 'click -> NO ELEMENT FOUND');
    }
  } else {
    const response = await chrome.tabs.sendMessage(targetTabId, {
      action: 'execute',
      payload: { ...action, variables: ctx.variables }
    });
    if (response?.variables) {
      ctx.variables = { ...ctx.variables, ...response.variables };
    }
    // Collect logs from content script
    if (response?.logs) {
      ctx.logs.push(...response.logs);
    }
    if (!response?.success) {
      log('error', `Action ${action.type} returned error`, { error: response?.error });
    }
  }
}

// Resolve ${var} placeholders with given variables
function resolveVariables(str, variables) {
  if (typeof str !== 'string') return str;
  return str.replace(/\$\{(\w+)\}/g, (_, key) => variables[key] ?? '');
}

// Wait for tab to load
function waitForTabLoad(tabId) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(); // Resolve anyway after timeout
    }, 30000);
    
    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        setTimeout(resolve, 500);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// Send webhook with context variables
async function sendWebhook(url, payload, ctx) {
  const resolved = JSON.parse(resolveVariables(JSON.stringify(payload), ctx.variables));
  await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(resolved)
  });
}

// Keep service worker alive with periodic alarm
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive') {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connectWebSocket();
    }
  }
});

// Connect on Chrome startup
chrome.runtime.onStartup.addListener(() => {
  console.log('Chrome started, connecting...');
  connectWebSocket();
});

// Connect on extension install/update
chrome.runtime.onInstalled.addListener(() => {
  console.log('Extension installed/updated, connecting...');
  connectWebSocket();
});

// OAuth popup detection - automatically detect and link OAuth tabs to workflows
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  // Detect OAuth pages as soon as URL is available (don't wait for complete)
  if (!tab.url && !changeInfo.url) return;

  const url = tab.url || changeInfo.url;

  // Check if this is an OAuth page
  const isOAuthPage = url.includes('accounts.google.com') ||
                      url.includes('login.microsoftonline.com') ||
                      url.includes('oauth') ||
                      url.includes('signin') ||
                      url.includes('authorize');

  if (!isOAuthPage) return;

  console.log('🔐 OAuth page detected:', {
    tabId,
    url,
    status: changeInfo.status,
    trigger: changeInfo.status === 'complete' ? 'complete' : 'loading'
  });

  // Find active workflow that doesn't have a popup yet
  for (const [workflowId, ctx] of runningWorkflows.entries()) {
    // Skip if this workflow already has a popup or if this is the main tab
    if (ctx.popupTabId || ctx.tabId === tabId) continue;

    // Link this OAuth tab to the workflow
    ctx.popupTabId = tabId;
    ctx.popupWindowId = tab.windowId;

    popupTracking.set(tabId, {
      parentTabId: ctx.tabId,
      workflowId: workflowId,
      createdAt: Date.now(),
      windowId: tab.windowId
    });

    // Log to workflow
    const log = (type, message, data = null) => {
      const entry = { time: new Date().toISOString(), type, message, ...(data && { data }) };
      ctx.logs.push(entry);
      console.log(`[${workflowId.slice(0,8)}][${type}]`, message, data || '');
    };

    log('workflow', '🔐 OAuth popup detected and linked by event listener', {
      tabId,
      url,
      windowId: tab.windowId,
      status: changeInfo.status
    });

    // Only link to first matching workflow
    break;
  }
});

// Cleanup popup tracking when tabs are closed
chrome.tabs.onRemoved.addListener((tabId) => {
  const tracking = popupTracking.get(tabId);
  if (tracking) {
    console.log('🗑️ OAuth popup closed:', { tabId, workflowId: tracking.workflowId });
    popupTracking.delete(tabId);

    // Clear popup reference from workflow context
    const ctx = runningWorkflows.get(tracking.workflowId);
    if (ctx && ctx.popupTabId === tabId) {
      ctx.popupTabId = null;
      ctx.popupWindowId = null;
    }
  }
});

// Handle recaptcha_solve requests from content.js fallback
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'solve_recaptcha' && sender.tab?.id) {
    const tabId = sender.tab.id;
    const action = msg.payload || {};
    const ctx = { variables: {}, logs: [] };
    const log = (type, message, data = null) => {
      console.log(`[recaptcha][${type}]`, message, data || '');
      ctx.logs.push({ time: new Date().toISOString(), type, message, ...(data && { data }) });
    };

    solveRecaptchaAudio(tabId, ctx, log, action)
      .then(() => {
        sendResponse({ success: true, variables: ctx.variables });
      })
      .catch(e => {
        sendResponse({ success: false, error: e.message });
      });

    return true; // Keep message channel open for async response
  }
});

// Turnstile auto-click message handler (receives from turnstile_bridge.js)
// Uses click queue to ensure tabs are processed one at a time with proper focus
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "detectAndClickTurnstile" && request.payload) {
    const tabId = sender.tab.id;
    console.log(`[Turnstile] Tab ${tabId} click request received, adding to queue...`);

    // Add to click queue instead of processing immediately
    addToTurnstileClickQueue(tabId, request.payload)
      .then(result => {
        sendResponse(result);
      })
      .catch(error => {
        console.error(`[Turnstile] Tab ${tabId} queue error:`, error);
        sendResponse({ success: false, error: error.message });
      });

    return true; // Keep channel open for async response
  }
});

// Turnstile focus queue request handler (receives from content script)
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === "requestTurnstileFocus" && sender.tab?.id) {
    const tabId = sender.tab.id;
    console.log(`[TurnstileFocusQueue] Focus request from tab ${tabId}`);

    (async () => {
      try {
        const result = await addToTurnstileFocusQueue(tabId);
        sendResponse({ success: true, ...result });
      } catch (error) {
        console.error('[TurnstileFocusQueue] Error:', error);
        sendResponse({ success: false, error: error.message });
      }
    })();

    return true; // Keep message channel open for async response
  }
});

// Initial connection
connectWebSocket();
