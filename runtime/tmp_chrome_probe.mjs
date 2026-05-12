(async () => {
  const pluginRoot = 'file:///C:/Users/TB14Plus/.codex/plugins/cache/openai-bundled/chrome/0.1.7';
  const { setupAtlasRuntime } = await import(`${pluginRoot}/scripts/browser-client.mjs`);
  await setupAtlasRuntime({ globals: globalThis });
  const browser = await agent.browsers.get('extension');
  const tabs = await browser.user.openTabs();
  console.log(JSON.stringify(tabs.slice(0, 5), null, 2));
})().catch(err => { console.error(err); process.exit(1); });
