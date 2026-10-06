<!--
Changelog conventions:
- Format follows Keep a Changelog (https://keepachangelog.com/en/1.1.0/) and Semantic Versioning (https://semver.org/).
- Only three sections are used, always in this order: Added, Fixed, Changed. Omit a section when it has no entries.
- Write every entry for users: what they notice or can now do, not how the code works.
-->

# Changelog

## [Unreleased]

### Fixed

- Chat apps set to a reasoning level the model does not offer (for example low or medium on Mistral Large 4, which only has none and high) now get an answer instead of an error. The gateway asks again with the closest level the model does offer, leaning toward less reasoning for minimal and low and toward more for everything else.
- Tool calls now work on models without reasoning, such as `mistral-large-latest`, Ministral and Codestral. Their answer to a tool result used to fail with an error.

## [1.0.1] - 2026-09-30

### Fixed

- Chat apps no longer break with a JSON error when a reply from Mistral ends part way through. The unfinished piece is now thrown away and the answer stops short, so whatever is reading it keeps running.

## [1.0.0] - 2026-09-23

### Added

- First release: a small local gateway that lets any OpenAI-compatible chat app (Open WebUI, editors, scripts) use the models in your Mistral Vibe subscription. It picks up the key from the official Vibe CLI login, streams answers live, shows reasoning models' thinking separately, handles tool calls, waits politely on rate limits, can fall back to a Mistral AI Studio key, records your daily token usage and only accepts requests from your own machine unless you set a gateway key.

[1.0.1]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.1
[1.0.0]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.0
