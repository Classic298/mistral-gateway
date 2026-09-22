<!--
Changelog conventions:
- Format follows Keep a Changelog (https://keepachangelog.com/en/1.1.0/) and Semantic Versioning (https://semver.org/).
- Only three sections are used, always in this order: Added, Fixed, Changed. Omit a section when it has no entries.
- Write every entry for users: what they notice or can now do, not how the code works.
-->

# Changelog

## [1.0.0] - 2026-09-23

### Added

- First release: a small local gateway that lets any OpenAI-compatible chat app (Open WebUI, editors, scripts) use the models in your Mistral Vibe subscription. It picks up the key from the official Vibe CLI login, streams answers live, shows reasoning models' thinking separately, handles tool calls, waits politely on rate limits, can fall back to a Mistral AI Studio key, records your daily token usage and only accepts requests from your own machine unless you set a gateway key.

[1.0.0]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.0
