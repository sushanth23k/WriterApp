// Expo config plugin: force App Transport Security to allow plain http/ws to the
// DropNote backend's bare IP (the GCP VM).
//
// IMPORTANT: iOS IGNORES NSAllowsArbitraryLoads when NSAllowsLocalNetworking (or the
// other "specific" ATS subkeys) is also present. Expo's prebuild template adds
// NSAllowsLocalNetworking, so simply setting NSAllowsArbitraryLoads via app.json's
// infoPlist isn't enough — the two get deep-merged and arbitrary loads is ignored.
// This plugin OVERWRITES the whole NSAppTransportSecurity dict to exactly
// { NSAllowsArbitraryLoads: true }, which fully disables ATS (fine for personal use;
// use HTTPS/WSS for anything real).
const { withInfoPlist } = require('@expo/config-plugins');

module.exports = function withAtsArbitraryLoads(config) {
  return withInfoPlist(config, (config) => {
    config.modResults.NSAppTransportSecurity = {
      NSAllowsArbitraryLoads: true,
    };
    return config;
  });
};
