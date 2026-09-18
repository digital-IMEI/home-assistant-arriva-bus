# Arriva Bus for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/digital-IMEI/home-assistant-arriva-bus)](https://github.com/digital-IMEI/home-assistant-arriva-bus/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

Track an Arriva bus line, direction and stop in the Netherlands, with optional
Live Activities on one or more iPhones. Runs directly on Home Assistant, including
Home Assistant Green; no separate server is needed.

This is an independent community project and is not affiliated with or endorsed by Arriva.

## Installation with HACS

Requires Home Assistant 2026.8.2 or newer.

1. Open HACS → **Integrations**.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/digital-IMEI/home-assistant-arriva-bus` as an **Integration**.
4. Search for **Arriva Bus** and install it.
5. Restart Home Assistant and refresh the frontend/Companion app.
6. Open **Settings → Devices & services → Add integration → Arriva Bus**.
7. Select line, direction, stop and optionally one or more iPhones.

## Manual installation

1. Download `arriva_bus.zip` from the [latest release](https://github.com/digital-IMEI/home-assistant-arriva-bus/releases/latest).
2. Create `/config/custom_components/arriva_bus/` and extract the contents of
   the archive into that directory. The manifest must be located at
   `/config/custom_components/arriva_bus/manifest.json`.
3. Restart Home Assistant and refresh the frontend/Companion app.
4. Open **Settings → Devices & services → Add integration → Arriva Bus**.
5. Select line, direction, stop and optionally your iPhones.

## Features and controls

- Configurable Arriva line, direction and target stop.
- Grouped destinations and stops in route order (branches inserted between shared stops).
- Sensors for current delay, stop position, journey status and scheduled passage.
- Integration switch and separate Live Activity switch for your own automations.
- Live Activity stays visible between journeys and displays cancellation/next service.
- Tapping a Live Activity opens the HA device containing that route's entities.
- No progress bar while waiting to depart; waiting and idle text stay compact.
- Multiple configurations and multiple iPhones with independent activity tags.
- Exact delay in seconds; explicitly scheduled waiting is not shown as early departure.

Colours are configurable for delay, early running and on-time/tolerance:

| State | Threshold | Default |
| --- | --- | --- |
| Early | 30 seconds early or more | Green |
| On time / tolerance | −29 to +60 seconds | White |
| Late | More than 60 seconds late | Red |
| Waiting, no bus, missing realtime | No current delay to display | Gray |
| Cancelled | Explicit cancellation | Red |

Waiting and cancellation colours are fixed. Existing custom colours are preserved;
choose **Default** or clear a colour field to reset it. Colour/device changes apply
without restarting the tracker. Removed phones receive an end command.

## Data and limitations

The compact catalogue is built from OVapi GTFS and NDOV CHB outside Home Assistant.
The current catalogue contains 436 lines and 4,897 stop places, around 160 kB compressed.
Live journey selection uses DRGL; vehicle updates use NDOV KV6. Missing/ambiguous
stop mappings and foreign stops may be omitted. Coverage depends on these sources.
For loops and route variants, one merged stop list cannot represent every trip;
live progress uses the selected trip's actual route.

Setup and Live Activity text are available in English and Dutch. Live Activity text
follows the Home Assistant language. The Companion notification registration does not
expose each phone's interface language, so separate automatic languages per phone are
not currently possible.
The fixed Live Activity title contains line/direction; the changing body contains time
and position. Waiting/cancelled states remain visible until the tracker is switched off.

iOS limits Live Activities to up to eight hours, can throttle silent updates and
limits new starts. The phone must be able to reach Home Assistant away from home
for token exchange. A successful HA notify action is not proof of phone delivery.
See [Companion documentation](https://companion.home-assistant.io/docs/notifications/live-activities/).

## Feedback

Report issues through [GitHub Issues](https://github.com/digital-IMEI/home-assistant-arriva-bus/issues)
with integration/HA versions, line, direction, stop, expected behaviour and the relevant
logs or diagnostics. Check diagnostics for private information before posting.
