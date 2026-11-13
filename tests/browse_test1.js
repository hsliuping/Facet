async function testBrowser() {
  // Navigate to Volcengine coding plan page
  await tools.browser_navigate({ 
    url: 'https://console.volcengine.com/ark/region:cn-beijing/docs/coding-plan-personal-get-started' 
  });
  
  // Take compact snapshot
  const snap = await tools.browser_snapshot({ compact: true, interactive: true });
  
  // Extract title and key info
  const info = await tools.browser_evaluate({
    script: `() => {
      return {
        title: document.title,
        bodyText: document.body?.textContent?.substring(0, 500) ?? ''
      };
    }`
  });
  
  text(info?.content?.[0]?.text);
}

testBrowser();