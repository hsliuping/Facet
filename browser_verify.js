// Browser verification script - will be executed via Exec
// Navigate to provider websites and extract free/paid mode info

async function main() {
  // We'll navigate to key provider websites
  const urls = [
    'https://console.volcengine.com/ark/region:cn-beijing/docs/coding-plan-personal-get-started', // Volcengine
    'https://help.aliyun.com/zh/model-studio/', // Alibaba Bailian
    'https://platform.xiaomimimo.com/docs', // Xiaomi MiMo
    'https://api.llm7.io/v1', // LLM7
    'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1', // OVHcloud
    'https://integrate.api.nvidia.com/v1', // NVIDIA
    'https://ai.google.dev/gemini-api/docs', // Google AI Studio
  ];
  
  for (const url of urls) {
    try {
      await tools.browser_navigate({ url });
      // Take a compact snapshot to get page info
      const snap = await tools.browser_snapshot({ compact: true, interactive: true });
      // Extract title and key info
      const info = await tools.browser_evaluate({
        script: `return {
          title: document.title,
          h1: document.querySelector('h1')?.textContent?.trim() ?? '',
          free_quota: document.querySelector('[class*="free"]')?.textContent?.trim() ?? '',
          pricing: document.querySelector('[class*="pricing"]')?.textContent?.trim() ?? ''
        }`
      });
      console.log(`URL: ${url}`);
      console.log(`Title: ${info?.content?.[0]?.text ?? info}`);
      console.log('---');
    } catch (e) {
      console.log(`Failed: ${url} - ${e.message}`);
    }
  }
}

main();