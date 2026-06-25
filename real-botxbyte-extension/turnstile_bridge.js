// Turnstile Auto-Click: Bridge script (ISOLATED world)
// Forwards checkbox position from MAIN world (turnstile_injected.js) to background service worker
if (window.top !== window.self && window.location.href.includes('challenges.cloudflare.com')) {
    window.addEventListener('message', (event) => {
        if (event.source === window && event.data && event.data.type === 'CHECKBOX_POSITION_RATIO') {
            const { xRatio, yRatio } = event.data.payload;
            chrome.runtime.sendMessage({
                action: "detectAndClickTurnstile",
                payload: {
                    xRatio: xRatio,
                    yRatio: yRatio
                }
            }, (response) => {
                if (chrome.runtime.lastError) {
                    console.error('[Turnstile Bridge] Error sending message:', chrome.runtime.lastError.message);
                }
            });
        }
    }, false);
}
