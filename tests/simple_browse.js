// Simple test to verify Exec + browser tools work
async function main() {
  try {
    // Navigate to a known page
    await tools.browser_navigate({ 
      url: 'https://www.aliyun.com' 
    });
    
    // Get page title
    const result = await tools.browser_evaluate({
      script: `() => document.title`
    });
    
    text('Page title: ' + (result?.content?.[0]?.text || 'unknown'));
  } catch (e) {
    text('Error: ' + e.message);
  }
}

main();