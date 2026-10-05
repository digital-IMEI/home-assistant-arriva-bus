# Arriva Bus 1.1.8

- Replace the early-running arrow with a bus icon using the configured early colour.
- Publish the next scheduled bus as soon as departure data arrives, before fetching route details.
- Start a new Live Activity with useful data instead of a loading card, avoiding a loading screen persisting while iOS batches silent updates.
- Preserve vehicle events received during route loading and guard against a stopped or replaced journey.
