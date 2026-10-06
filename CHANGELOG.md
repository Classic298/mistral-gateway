<!--
Changelog conventions:
- Format follows Keep a Changelog (https://keepachangelog.com/en/1.1.0/) and Semantic Versioning (https://semver.org/).
- Only three sections are used, always in this order: Added, Fixed, Changed. Omit a section when it has no entries.
- Write every entry for users: what they notice or can now do, not how the code works.
-->

# Changelog

## [1.0.2] - 2026-10-06

### Added

- Support for Mistral Large 4. It only offers the reasoning levels none and high, so the gateway turns minimal and low into none and everything above into high, and any level a chat app sends works.
- Support for the models without reasoning: `mistral-large-latest`, Ministral, Codestral and Voxtral. Tool calls work on them now, and a reasoning level sent by a chat app is left out instead of causing an error.
- The README lists the chat models you can use through the gateway, with their other names, context size, image support and the reasoning levels each one accepts. Every model in it was tested with tool calls and every reasoning level.

### Fixed

- Chat apps set to a reasoning level that Mistral Medium, Mistral Small or GLM 5.3 does not offer now get an answer instead of an error. The gateway asks again with the closest level the model offers, leaning toward less reasoning for minimal and low and toward more for everything else.
- Mistral Medium and Small no longer keep calling a tool instead of answering with its result. The extra reasoning the gateway asks for on those turns is now limited to GLM, which it was meant for.
- Requests that include `max_completion_tokens`, `seed` or `user`, as newer OpenAI clients send them, no longer fail with an error.

## [1.0.1] - 2026-09-30

### Fixed

- Chat apps no longer break with a JSON error when a reply from Mistral ends part way through. The unfinished piece is now thrown away and the answer stops short, so whatever is reading it keeps running.

## [1.0.0] - 2026-09-23

### Added

- First release: a small local gateway that lets any OpenAI-compatible chat app (Open WebUI, editors, scripts) use the models in your Mistral Vibe subscription. It picks up the key from the official Vibe CLI login, streams answers live, shows reasoning models' thinking separately, handles tool calls, waits politely on rate limits, can fall back to a Mistral AI Studio key, records your daily token usage and only accepts requests from your own machine unless you set a gateway key.

[1.0.2]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.2
[1.0.1]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.1
[1.0.0]: https://github.com/Classic298/mistral-gateway/releases/tag/v1.0.0
