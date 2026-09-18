Arriva Bus 1.1.2 makes first-time setup more immediate and improves the public
documentation.

- A newly configured route now starts tracking explicitly during its first setup.
- Later restarts still respect a manually or automatically disabled runtime switch.
- The README now explains the no-code onboarding, all available entities and the
  built-in iPhone Live Activity in more detail.
- The first real Live Activity screenshot has been added.
- The corrected HACS archive structure introduced in 1.1.1 is retained.

It also contains all features introduced with the standalone community release:

- Select a line, direction and stop using the compact GTFS catalogue.
- Live delay, stop position, trip status and route progress.
- Optional persistent iPhone Live Activities on multiple devices.
- Correct English and Dutch labels: early colour starts at 30 seconds early.
- Default on-time colour is white (−29 through +60 seconds).
- Waiting/no bus/missing realtime information is gray. Cancellation is red.
- No progress bar is shown while waiting to depart.
- Waiting is displayed compactly on the same line as the scheduled time.
- Idle state is reduced to `Geen bus onderweg` / `No bus underway`.
- Tapping the activity opens the HA device page for that configured route.
- Live Activity text follows the Home Assistant language (Dutch or English).
- Existing custom colours are retained; clear the field or choose Default to reset.

Extract `arriva_bus.zip` into `/config/custom_components/arriva_bus/` and restart Home Assistant.
Existing installations keep the same `arriva_bus` integration domain and do not need to be recreated.
Refresh the HA frontend or reopen the Companion app after restarting to refresh translations.

Requires Home Assistant 2026.8.2 or newer. See the README for installation,
operation, data coverage and iOS limitations.
