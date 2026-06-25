// Variables storage
let vars = {};
let logs = [];

// Log helper
const log = (type, message, data = null) => {
  const entry = { time: new Date().toISOString(), type, message, ...(data && { data }) };
  logs.push(entry);
  console.log(`[${type}]`, message, data || '');
};

// Resolve ${var} in strings
const resolve = (v) => typeof v === 'string' ? v.replace(/\$\{(\w+)\}/g, (_, k) => vars[k] ?? '') : v;

// Find element by XPath
const findByXPath = (xpath) => {
  return document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
};

// Find first matching element (supports text: and xpath: prefixes)
const find = (selectors) => {
  for (const s of [].concat(selectors)) {
    const selector = resolve(s);
    
    // Support "text:Button Text" syntax - uses XPath
    if (selector.startsWith('text:')) {
      const text = selector.slice(5);
      const xpath = `//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '${text.toLowerCase()}')]`;
      const el = findByXPath(xpath);
      log('find', `text:"${text}" -> ${el ? 'FOUND' : 'NOT FOUND'}`, { selector, xpath });
      if (el) return el;
      continue;
    }
    
    // Support "xpath:" prefix for direct XPath
    if (selector.startsWith('xpath:')) {
      const xpath = selector.slice(6);
      const el = findByXPath(xpath);
      log('find', `xpath -> ${el ? 'FOUND' : 'NOT FOUND'}`, { xpath });
      if (el) return el;
      continue;
    }
    
    // Regular CSS selector
    try {
      const el = document.querySelector(selector);
      log('find', `css:"${selector}" -> ${el ? 'FOUND' : 'NOT FOUND'}`);
      if (el) return el;
    } catch (e) {
      log('error', `Invalid selector: ${selector}`, { error: e.message });
    }
  }
  return null;
};

// Find all matching elements
const findAll = (selectors) => {
  for (const s of [].concat(selectors)) {
    const selector = resolve(s);
    
    // Support xpath: for findAll
    if (selector.startsWith('xpath:')) {
      const xpath = selector.slice(6);
      const result = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      const els = [];
      for (let i = 0; i < result.snapshotLength; i++) {
        els.push(result.snapshotItem(i));
      }
      log('findAll', `xpath -> found ${els.length} elements`, { xpath });
      if (els.length) return els;
      continue;
    }
    
    const els = document.querySelectorAll(selector);
    log('findAll', `css:"${selector}" -> found ${els.length} elements`);
    if (els.length) return [...els];
  }
  return [];
};

// Delay helper
const delay = (ms) => new Promise(r => setTimeout(r, ms));

// Random delay in range
const randomDelay = (range) => delay(Array.isArray(range) ? range[0] + Math.random() * (range[1] - range[0]) : range);

// Check if element is contenteditable
const isContentEditable = (el) => {
  return el.isContentEditable || el.contentEditable === 'true' || el.getAttribute('contenteditable') === 'true';
};

// Human-like typing for both input and contenteditable
async function humanType(el, text, delayRange = [50, 150]) {
  el.focus();
  const isEditable = isContentEditable(el);
  
  for (const char of text) {
    // Fire beforeinput event
    el.dispatchEvent(new InputEvent('beforeinput', { 
      bubbles: true, 
      cancelable: true,
      inputType: 'insertText', 
      data: char 
    }));
    
    if (isEditable) {
      // For contenteditable divs - insert text at cursor position
      const selection = window.getSelection();
      const range = selection.getRangeAt(0);
      const textNode = document.createTextNode(char);
      range.insertNode(textNode);
      range.setStartAfter(textNode);
      range.setEndAfter(textNode);
      selection.removeAllRanges();
      selection.addRange(range);
    } else {
      // For regular inputs
      el.value += char;
    }
    
    // Fire input event
    el.dispatchEvent(new InputEvent('input', { 
      bubbles: true, 
      inputType: 'insertText', 
      data: char 
    }));
    
    await randomDelay(delayRange);
  }
}

// Action handlers
const actions = {
  async wait_for({ selectors, timeout = 5000 }) {
    log('action', 'wait_for', { selectors, timeout });
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (find(selectors)) {
        log('action', 'wait_for -> SUCCESS');
        return;
      }
      await delay(100);
    }
    log('error', 'wait_for -> TIMEOUT');
    throw new Error('wait_for timeout');
  },

  async click({ selectors, wait_after }) {
    log('action', 'click', { selectors, wait_after });
    const el = find(selectors);
    if (el) {
      el.click();
      log('action', 'click -> SUCCESS', { tagName: el.tagName, text: el.textContent?.slice(0, 50) });
      if (wait_after) await delay(wait_after);
    } else {
      log('error', 'click -> ELEMENT NOT FOUND');
    }
  },

  async fill({ selectors, value, human_typing, delay: delayRange, clear = true }) {
    log('action', 'fill', { selectors, value: value?.slice(0, 20) + '...', human_typing });
    const el = find(selectors);
    if (!el) {
      log('error', 'fill -> ELEMENT NOT FOUND');
      return;
    }
    
    el.focus();
    const text = resolve(value);
    const isEditable = isContentEditable(el);
    
    // Clear existing content if requested
    if (clear) {
      if (isEditable) {
        el.innerHTML = '';
        // Also try setting textContent for some contenteditable implementations
        el.textContent = '';
      } else {
        el.value = '';
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true }));
      await delay(50);
    }
    
    if (human_typing) {
      await humanType(el, text, delayRange);
    } else {
      if (isEditable) {
        // For contenteditable, set innerHTML/textContent and dispatch events
        el.focus();
        document.execCommand('insertText', false, text);
        // Fallback if execCommand doesn't work
        if (!el.textContent) {
          el.textContent = text;
        }
      } else {
        el.value = text;
      }
      el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    }
    log('action', 'fill -> SUCCESS', { isContentEditable: isEditable });
  },

  async select({ selectors, value }) {
    log('action', 'select', { selectors, value });
    const el = find(selectors);
    if (el) {
      el.value = resolve(value);
      el.dispatchEvent(new Event('change', { bubbles: true }));
      log('action', 'select -> SUCCESS');
    } else {
      log('error', 'select -> ELEMENT NOT FOUND');
    }
  },

  async extract({ selectors, set_variable }) {
    log('action', 'extract', { selectors, set_variable });
    const el = find(selectors);
    if (el && set_variable) {
      vars[set_variable] = el.textContent?.trim() || el.value || '';
      log('action', 'extract -> SUCCESS', { value: vars[set_variable]?.slice(0, 100) });
    } else {
      log('error', 'extract -> ELEMENT NOT FOUND');
    }
  },

  async extract_all({ selectors, fields, set_variable, limit }) {
    log('action', 'extract_all', { selectors, fields, limit });
    const els = findAll(selectors).slice(0, limit || 100);
    const results = els.map(el => {
      const item = {};
      for (const [key, fieldSelectors] of Object.entries(fields)) {
        for (const s of fieldSelectors) {
          const [selector, attr] = s.split('@');
          const child = el.querySelector(selector);
          if (child) {
            item[key] = attr ? child.getAttribute(attr) : child.textContent?.trim();
            break;
          }
        }
      }
      return item;
    });
    if (set_variable) vars[set_variable] = results;
    log('action', 'extract_all -> SUCCESS', { count: results.length });
  },

  async set_variable({ name, selectors, value }) {
    log('action', 'set_variable', { name, selectors, value });
    if (selectors) {
      const el = find(selectors);
      if (el) {
        vars[name] = el.textContent?.trim() || el.value || '';
        log('action', 'set_variable -> SUCCESS (from element)', { value: vars[name]?.slice(0, 100) });
        return;
      }
    }
    vars[name] = resolve(value);
    log('action', 'set_variable -> SUCCESS', { value: vars[name] });
  },

  async if_exists({ selectors, actions: subActions, then: thenAction, else: elseAction, else_actions: elseActions, timeout = 0 }) {
    log('action', 'if_exists', { selectors, timeout });
    
    let exists = false;
    if (timeout > 0) {
      const start = Date.now();
      while (Date.now() - start < timeout) {
        if (find(selectors)) {
          exists = true;
          break;
        }
        await delay(100);
      }
      log('action', `if_exists -> waited ${Date.now() - start}ms`);
    } else {
      exists = !!find(selectors);
    }
    
    log('action', `if_exists -> ${exists ? 'EXISTS' : 'NOT EXISTS'}`);

    // Support both 'actions' array and 'then' single action
    if (exists) {
      const actionsToRun = subActions || (thenAction ? [thenAction] : []);
      for (const act of actionsToRun) {
        log('action', `if_exists -> executing sub-action: ${act.type}`);
        await executeAction(act);
      }
    } else {
      const actionsToRun = elseActions || (elseAction ? [elseAction] : []);
      for (const act of actionsToRun) {
        log('action', `if_exists -> executing else sub-action: ${act.type}`);
        await executeAction(act);
      }
    }
  },

  async if_visible({ selectors, then: thenAction, else: elseAction, timeout = 0 }) {
    log('action', 'if_visible', { selectors, timeout });
    
    let el = null;
    let visible = false;
    if (timeout > 0) {
      const start = Date.now();
      while (Date.now() - start < timeout) {
        el = find(selectors);
        if (el && el.offsetParent !== null) {
          visible = true;
          break;
        }
        await delay(100);
      }
    } else {
      el = find(selectors);
      visible = el && el.offsetParent !== null;
    }
    
    log('action', `if_visible -> ${visible ? 'VISIBLE' : 'NOT VISIBLE'}`);
    if (visible && thenAction) await executeAction(thenAction);
    else if (!visible && elseAction) await executeAction(elseAction);
  },

  async scroll({ direction, amount }) {
    log('action', 'scroll', { direction, amount });
    const y = direction === 'up' ? -amount : amount;
    window.scrollBy({ top: y, behavior: 'smooth' });
    await delay(300);
    log('action', 'scroll -> SUCCESS');
  },

  async hover({ selectors }) {
    log('action', 'hover', { selectors });
    const el = find(selectors);
    if (el) {
      el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
      el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
      log('action', 'hover -> SUCCESS');
    } else {
      log('error', 'hover -> ELEMENT NOT FOUND');
    }
  },

  async mouse_click({ selectors, wait_after }) {
    log('action', 'mouse_click', { selectors });
    const el = find(selectors);
    if (!el) {
      log('error', 'mouse_click -> ELEMENT NOT FOUND');
      return;
    }
    
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    
    log('action', 'mouse_click -> coordinates', { x, y, rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height } });
    
    // Move mouse to element
    el.dispatchEvent(new MouseEvent('mousemove', { clientX: x, clientY: y, bubbles: true }));
    el.dispatchEvent(new MouseEvent('mouseenter', { clientX: x, clientY: y, bubbles: true }));
    el.dispatchEvent(new MouseEvent('mouseover', { clientX: x, clientY: y, bubbles: true }));
    await delay(100);
    
    // Click at coordinates
    el.dispatchEvent(new MouseEvent('mousedown', { clientX: x, clientY: y, bubbles: true, button: 0 }));
    el.dispatchEvent(new MouseEvent('mouseup', { clientX: x, clientY: y, bubbles: true, button: 0 }));
    el.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true, button: 0 }));
    
    log('action', 'mouse_click -> SUCCESS', { x, y });
    if (wait_after) await delay(wait_after);
  },

  async delay({ timeout = 1000 }) {
    log('action', 'delay', { timeout });
    await delay(timeout);
    log('action', 'delay -> DONE');
  },

  async keyboard({ key, selectors }) {
    log('action', 'keyboard', { key, selectors });
    const el = selectors ? find(selectors) : document.activeElement;
    if (!el) {
      log('error', 'keyboard -> NO ELEMENT');
      return;
    }
    
    const keyMap = {
      'enter': { key: 'Enter', code: 'Enter', keyCode: 13 },
      'tab': { key: 'Tab', code: 'Tab', keyCode: 9 },
      'escape': { key: 'Escape', code: 'Escape', keyCode: 27 },
      'backspace': { key: 'Backspace', code: 'Backspace', keyCode: 8 },
      'space': { key: ' ', code: 'Space', keyCode: 32 },
      'arrowup': { key: 'ArrowUp', code: 'ArrowUp', keyCode: 38 },
      'arrowdown': { key: 'ArrowDown', code: 'ArrowDown', keyCode: 40 },
      'arrowleft': { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 },
      'arrowright': { key: 'ArrowRight', code: 'ArrowRight', keyCode: 39 }
    };
    
    const keyInfo = keyMap[key.toLowerCase()] || { key: key, code: `Key${key.toUpperCase()}`, keyCode: key.charCodeAt(0) };
    
    const eventInit = { 
      key: keyInfo.key, 
      code: keyInfo.code,
      keyCode: keyInfo.keyCode, 
      which: keyInfo.keyCode,
      bubbles: true,
      cancelable: true,
      composed: true
    };
    
    el.dispatchEvent(new KeyboardEvent('keydown', eventInit));
    el.dispatchEvent(new KeyboardEvent('keypress', eventInit));
    el.dispatchEvent(new KeyboardEvent('keyup', eventInit));
    
    log('action', 'keyboard -> SUCCESS', { key: keyInfo.key });
  },

  // Alias for keyboard
  async key_press({ key, selectors }) {
    return actions.keyboard({ key, selectors });
  },

  async clear({ selectors }) {
    log('action', 'clear', { selectors });
    const el = find(selectors);
    if (el) {
      el.value = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
      log('action', 'clear -> SUCCESS');
    } else {
      log('error', 'clear -> ELEMENT NOT FOUND');
    }
  },

  async focus({ selectors }) {
    log('action', 'focus', { selectors });
    const el = find(selectors);
    if (el) {
      el.focus();
      log('action', 'focus -> SUCCESS');
    } else {
      log('error', 'focus -> ELEMENT NOT FOUND');
    }
  },

  async double_click({ selectors, wait_after }) {
    log('action', 'double_click', { selectors });
    const el = find(selectors);
    if (!el) {
      log('error', 'double_click -> ELEMENT NOT FOUND');
      return;
    }
    el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    log('action', 'double_click -> SUCCESS');
    if (wait_after) await delay(wait_after);
  },

  async evaluate({ script, set_variable }) {
    // Evaluate is handled by background.js using chrome.scripting API
    // This is just a fallback that should not normally be called
    log('action', 'evaluate -> DELEGATED TO BACKGROUND');
  },

  async iframe({ selectors, actions }) {
    log('action', 'iframe', { selectors });
    const frame = find(selectors);
    if (!frame || frame.tagName !== 'IFRAME') {
      log('error', 'iframe -> NOT FOUND OR NOT IFRAME');
      return;
    }
    
    try {
      const frameDoc = frame.contentDocument || frame.contentWindow.document;
      // Execute actions in iframe context (simplified - may not work cross-origin)
      log('action', 'iframe -> ACCESS SUCCESS');
    } catch (e) {
      log('error', 'iframe -> CROSS-ORIGIN BLOCKED', { error: e.message });
    }
  },

  async screenshot({ selectors, set_variable }) {
    log('action', 'screenshot', { selectors, set_variable });
    const el = find(selectors);
    if (el && set_variable) {
      vars[set_variable] = `screenshot_${Date.now()}`;
      log('action', 'screenshot -> SUCCESS');
    }
  },

  async wait_for_navigation({ timeout = 10000 }) {
    log('action', 'wait_for_navigation', { timeout });
    await delay(timeout);
    log('action', 'wait_for_navigation -> DONE');
  },

  async recaptcha_solve(action) {
    log('action', 'recaptcha_solve', action);
    // Content scripts can't access chrome.webNavigation/chrome.scripting APIs.
    // Send message to background.js which has the real solver.
    const result = await chrome.runtime.sendMessage({
      action: 'solve_recaptcha',
      payload: action
    });
    if (result?.success) {
      if (result.variables) Object.assign(vars, result.variables);
      log('action', 'recaptcha_solve -> SUCCESS');
    } else {
      log('error', 'recaptcha_solve -> FAILED', { error: result?.error });
      throw new Error(result?.error || 'recaptcha_solve failed');
    }
  }
};

// Execute single action
async function executeAction(action) {
  log('execute', `Starting action: ${action.type}`, action);
  const handler = actions[action.type];
  if (handler) {
    await handler(action);
  } else {
    log('error', `Unknown action: ${action.type}`);
  }
}

// Listen for messages from background
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'execute') {
    logs = []; // Reset logs for each action
    vars = { ...vars, ...msg.payload.variables };
    log('init', 'Action received', { type: msg.payload.type, url: window.location.href });

    executeAction(msg.payload).then(() => {
      sendResponse({ success: true, variables: vars, logs });
    }).catch(e => {
      log('error', 'Action failed', { error: e.message });
      sendResponse({ success: false, error: e.message, variables: vars, logs });
    });
  }
  if (msg.action === 'getVars') {
    sendResponse({ variables: vars, logs });
  }
  return true;
});

// Listen for Turnstile focus requests from page scripts (evaluate scripts)
// Page scripts can't directly call chrome.runtime.sendMessage, so they use postMessage
window.addEventListener('message', async (event) => {
  if (event.source !== window) return;

  if (event.data && event.data.type === 'TURNSTILE_FOCUS_REQUEST') {
    console.log('[Content] Turnstile focus request received from page');

    try {
      const result = await chrome.runtime.sendMessage({
        action: 'requestTurnstileFocus'
      });

      // Send result back to page
      window.postMessage({
        type: 'TURNSTILE_FOCUS_RESPONSE',
        success: result?.success ?? false,
        reason: result?.reason ?? 'unknown',
        elapsed: result?.elapsed ?? 0
      }, '*');

      console.log('[Content] Turnstile focus result:', result);
    } catch (e) {
      console.error('[Content] Turnstile focus request error:', e);
      window.postMessage({
        type: 'TURNSTILE_FOCUS_RESPONSE',
        success: false,
        reason: 'error',
        error: e.message
      }, '*');
    }
  }
}, false);
