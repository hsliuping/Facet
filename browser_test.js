// Browser verification using Trae browser tools
// This will be executed via the tool system

// Navigate to Volcengine coding plan page
await tools.browser_navigate({ 
  url: 'https://console.volcengine.com/ark/region:cn-beijing/docs/coding-plan-personal-get-started' 
});

// Take compact snapshot to extract page info
const snap = await tools.browser_snapshot({ compact: true, interactive: true });

// Extract key information
const info = await tools.browser_evaluate({
  script: `() => {
    return {
      title: document.title,
      h1: document.querySelector('h1')?.textContent?.trim() ?? '',
      has_free: /free|免费/.test(document.body.textContent || ''),
      quota_info: document.body.textContent?.match(/[0-9]+(?:\.[0-9]+)?\s*(?:tokens?|rpd|rpm|credits?|次)/g) || []
    };
  }`
});

console.log('Page title:', info?.content?.[0]?.text);
console.log('Has free mention:', info?.content?.[0]?.text?.includes('免费') || info?.content?.[0]?.text?.includes('free'));
console.log('Quota info found:', info?.content?.[0]?.text?.match(/[0-9]+/));